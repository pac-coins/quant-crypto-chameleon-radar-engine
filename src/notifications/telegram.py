from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import requests

from src.core.database import Database
from src.radar.confluence_scorer import RadarSignal


class TelegramTransport(Protocol):
    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int: ...


class HttpTelegramTransport:
    """Real Telegram Bot API transport (https://api.telegram.org)."""

    def __init__(self, bot_token: str, timeout_s: float = 8.0) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._timeout_s = float(timeout_s)
        self._session = requests.Session()

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        resp = self._session.post(f"{self._base}/sendMessage", json=payload, timeout=self._timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return int(data["result"]["message_id"])


@dataclass(frozen=True, slots=True)
class FlushStats:
    sent: int
    failed: int
    pending_remaining: int


class TelegramOutbox:
    """Persistent SQLite outbox for Telegram alerts (never loses critical alerts)."""

    def __init__(
        self,
        db: Database,
        chat_id: str,
        transport: TelegramTransport | None = None,
        *,
        max_attempts: int = 5,
    ) -> None:
        self._db = db
        self._chat_id = chat_id
        self._transport = transport
        self._max_attempts = int(max_attempts)

    def enqueue(
        self,
        kind: str,
        text: str,
        *,
        reply_to_signal_id: str | None = None,
        now_ms: int | None = None,
    ) -> int:
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._db.write_tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO telegram_outbox (kind, payload, reply_to_signal_id, status, attempts, created_at_ms)
                VALUES (?, ?, ?, 'PENDING', 0, ?)
                """,
                (kind, text, reply_to_signal_id, ts),
            )
            return int(cur.lastrowid)

    def flush(self, *, max_batch: int = 10, now_ms: int | None = None) -> FlushStats:
        if self._transport is None:
            with self._db.read() as conn:
                rem = conn.execute(
                    "SELECT COUNT(*) FROM telegram_outbox WHERE status = 'PENDING'"
                ).fetchone()[0]
            return FlushStats(sent=0, failed=0, pending_remaining=int(rem))

        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT o.msg_id, o.kind, o.payload, o.reply_to_signal_id, o.attempts,
                       vs.telegram_message_id AS parent_tg_msg_id
                FROM telegram_outbox o
                LEFT JOIN virtual_signals vs ON vs.signal_id = o.reply_to_signal_id
                WHERE o.status = 'PENDING'
                ORDER BY o.msg_id ASC
                LIMIT ?
                """,
                (int(max_batch),),
            ).fetchall()

        sent_count = 0
        failed_count = 0
        for row in rows:
            msg_id = int(row["msg_id"])
            attempts = int(row["attempts"]) + 1
            parent_tg = row["parent_tg_msg_id"]
            try:
                tg_id = self._transport.send_message(
                    chat_id=self._chat_id,
                    text=str(row["payload"]),
                    reply_to_message_id=int(parent_tg) if parent_tg is not None else None,
                )
                with self._db.write_tx() as conn:
                    conn.execute(
                        """
                        UPDATE telegram_outbox
                        SET status = 'SENT', attempts = ?, telegram_message_id = ?, sent_at_ms = ?
                        WHERE msg_id = ?
                        """,
                        (attempts, tg_id, ts, msg_id),
                    )
                    if row["kind"] == "RADAR_SIGNAL" and row["reply_to_signal_id"]:
                        conn.execute(
                            "UPDATE virtual_signals SET telegram_message_id = ? WHERE signal_id = ?",
                            (tg_id, str(row["reply_to_signal_id"])),
                        )
                sent_count += 1
            except Exception:
                new_status = "FAILED" if attempts >= self._max_attempts else "PENDING"
                with self._db.write_tx() as conn:
                    conn.execute(
                        "UPDATE telegram_outbox SET status = ?, attempts = ? WHERE msg_id = ?",
                        (new_status, attempts, msg_id),
                    )
                if new_status == "FAILED":
                    failed_count += 1

        with self._db.read() as conn:
            rem = conn.execute(
                "SELECT COUNT(*) FROM telegram_outbox WHERE status = 'PENDING'"
            ).fetchone()[0]
        return FlushStats(sent=sent_count, failed=failed_count, pending_remaining=int(rem))


def format_radar_signal_alert(sig: RadarSignal) -> str:
    obi_txt = f"{sig.orderbook.obi_mean:+.2f}" if (sig.orderbook and sig.orderbook.obi_mean is not None) else "N/A"
    return (
        f"📡 RADAR [{sig.grade}] {sig.symbol} | Score {sig.score_total}/100\n"
        f"Clima 4h: {sig.regime_4h} | Setup: {sig.setup}\n"
        f"L1={sig.score_l1} L2={sig.score_l2} L3={sig.score_l3} L4={sig.score_l4} (OBI={obi_txt})\n"
        f"Ref Ask: {sig.entry_ref_price:.4f} | Stop: {sig.stop_price:.4f} | T1: {sig.target1_price:.4f} | T2: {sig.target2_price:.4f}"
    )


def format_kill_switch_alert(mode: str, reason: str) -> str:
    return f"🚨 EMERGENCY_HALT [{mode}]\nMotivo: {reason}\nNovas compras bloqueadas; posições abertas seguem protegidas."


def format_trade_entry_alert(
    *,
    mode: str,
    symbol: str,
    regime: str,
    quote_usdt: str,
    entry_price: str,
    stop_price: str,
    target_price: str,
    ml_grade: str,
) -> str:
    return (
        f"🦎 CHAMELEON ENTRADA [{mode}] {symbol}\n"
        f"Clima: {regime} | Nota IA (silenciosa): {ml_grade}\n"
        f"Lote: {quote_usdt} USDT @ {entry_price}\n"
        f"Stop: {stop_price} | Alvo: {target_price}"
    )
