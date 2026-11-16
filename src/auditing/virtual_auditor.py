from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from src.auditing.outcome_resolver import DualTargetResolution, resolve_dual_target_horizon
from src.core.database import CandleRow, Database
from src.radar.confluence_scorer import RadarSignal

ONE_HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class ResolvedVirtualSignal:
    signal_id: str
    symbol: str
    grade: str
    status: str
    outcome_r_gross: float | None
    outcome_r_net: float | None
    mfe_r: float | None
    mae_r: float | None
    same_candle_ambiguity: bool
    resolved_at_ms: int


class VirtualAuditor:
    """Persists Radar virtual signals and resolves them deterministically via outcome_resolver."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record_signal(self, sig: RadarSignal) -> bool:
        """Persist signal + compact order book snapshots/walls. Returns True if inserted."""
        ob = sig.orderbook
        ob_valid_int = 1 if (ob is not None and ob.ob_valid) else 0
        with self._db.write_tx() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO virtual_signals (
                        signal_id, symbol, signal_close_time_ms, emitted_at_ms, setup, regime_4h,
                        score_total, score_l1, score_l2, score_l3, score_l4, grade,
                        entry_ref_price, candle_close_price, stop_price, target1_price, target2_price,
                        atr14, horizon_end_ms, cost_roundtrip_pct, features_json,
                        ob_valid, ob_band_eff_pct, ob_obi_mean,
                        ob_bid_wall_price, ob_bid_wall_notional,
                        ob_ask_wall_price, ob_ask_wall_notional, ob_ask_wall_before_t2,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                    """,
                    (
                        sig.signal_id,
                        sig.symbol,
                        sig.signal_close_time_ms,
                        sig.emitted_at_ms,
                        sig.setup,
                        sig.regime_4h,
                        sig.score_total,
                        sig.score_l1,
                        sig.score_l2,
                        sig.score_l3,
                        sig.score_l4,
                        sig.grade,
                        sig.entry_ref_price,
                        sig.candle_close_price,
                        sig.stop_price,
                        sig.target1_price,
                        sig.target2_price,
                        sig.atr14,
                        sig.horizon_end_ms,
                        sig.cost_roundtrip_pct,
                        json.dumps(sig.features_snapshot, separators=(",", ":")),
                        ob_valid_int,
                        ob.band_eff_pct if ob is not None else None,
                        ob.obi_mean if ob is not None else None,
                        ob.bid_wall_price if ob is not None else None,
                        ob.bid_wall_notional if ob is not None else None,
                        ob.ask_wall_price if ob is not None else None,
                        ob.ask_wall_notional if ob is not None else None,
                        1 if sig.ob_ask_wall_before_t2 else 0,
                    ),
                )
            except sqlite3.IntegrityError:
                return False

            if ob is not None:
                for sm in ob.snapshots:
                    conn.execute(
                        """
                        INSERT INTO orderbook_snapshots (
                            signal_id, seq, symbol, requested_limit, taken_at_ms, last_update_id,
                            best_bid, best_ask, mid, spread_bps, bid_coverage_pct, ask_coverage_pct,
                            band_eff_pct, bid_notional_band, ask_notional_band, obi, impact_bps_ref, buckets_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sig.signal_id,
                            sm.seq,
                            sm.symbol,
                            sm.requested_limit,
                            sm.taken_at_ms,
                            sm.last_update_id,
                            sm.best_bid,
                            sm.best_ask,
                            sm.mid,
                            sm.spread_bps,
                            sm.bid_coverage_pct,
                            sm.ask_coverage_pct,
                            sm.band_eff_pct,
                            sm.bid_notional_band,
                            sm.ask_notional_band,
                            sm.obi,
                            sm.impact_bps_ref,
                            sm.buckets_json,
                        ),
                    )
                for wall in ob.walls:
                    conn.execute(
                        """
                        INSERT INTO orderbook_walls (
                            signal_id, side, bucket_low, bucket_high, notional_mean, median_multiple, persistence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sig.signal_id,
                            wall.side,
                            wall.bucket_low,
                            wall.bucket_high,
                            wall.notional_mean,
                            wall.median_multiple,
                            wall.persistence,
                        ),
                    )
        return True

    def set_telegram_message_id(self, signal_id: str, telegram_message_id: int) -> None:
        with self._db.write_tx() as conn:
            conn.execute(
                "UPDATE virtual_signals SET telegram_message_id = ? WHERE signal_id = ?",
                (int(telegram_message_id), signal_id),
            )

    def resolve_open_signals(self, now_ms: int) -> list[ResolvedVirtualSignal]:
        """Resolve all OPEN virtual_signals starting strictly at t+1 (open_time_ms >= signal_close_time_ms)."""
        with self._db.read() as conn:
            open_rows = conn.execute(
                """
                SELECT signal_id, symbol, grade, signal_close_time_ms, entry_ref_price,
                       stop_price, target1_price, target2_price, horizon_end_ms, cost_roundtrip_pct
                FROM virtual_signals
                WHERE status = 'OPEN'
                ORDER BY signal_close_time_ms ASC
                """
            ).fetchall()

        resolved: list[ResolvedVirtualSignal] = []
        for row in open_rows:
            sig_id = str(row["signal_id"])
            sym = str(row["symbol"])
            sig_close_ms = int(row["signal_close_time_ms"])
            horizon_end_ms = int(row["horizon_end_ms"])

            with self._db.read() as conn:
                candles = conn.execute(
                    """
                    SELECT symbol, interval, open_time_ms, close_time_ms, open, high, low, close,
                           volume, quote_volume, number_of_trades, taker_buy_base_volume,
                           taker_buy_quote_volume, ingested_at_ms
                    FROM candles
                    WHERE symbol = ? AND interval = '1h' AND open_time_ms >= ?
                    ORDER BY open_time_ms ASC
                    """,
                    (sym, sig_close_ms),
                ).fetchall()

            if not candles:
                continue

            candle_rows = [
                CandleRow(
                    symbol=str(c["symbol"]),
                    interval="1h",
                    open_time_ms=int(c["open_time_ms"]),
                    close_time_ms=int(c["close_time_ms"]),
                    open=float(c["open"]),
                    high=float(c["high"]),
                    low=float(c["low"]),
                    close=float(c["close"]),
                    volume=float(c["volume"]),
                    quote_volume=float(c["quote_volume"]),
                    number_of_trades=int(c["number_of_trades"]),
                    taker_buy_base_volume=float(c["taker_buy_base_volume"]),
                    taker_buy_quote_volume=float(c["taker_buy_quote_volume"]),
                    ingested_at_ms=int(c["ingested_at_ms"]),
                )
                for c in candles
            ]

            # Gap check before first t+1 candle (C2-7)
            if candle_rows[0].open_time_ms - sig_close_ms > 60_000:
                with self._db.write_tx() as conn:
                    conn.execute(
                        """
                        UPDATE virtual_signals
                        SET status = 'INVALID_GAP', resolved_at_ms = ?
                        WHERE signal_id = ?
                        """,
                        (int(now_ms), sig_id),
                    )
                resolved.append(
                    ResolvedVirtualSignal(
                        signal_id=sig_id,
                        symbol=sym,
                        grade=str(row["grade"]),
                        status="INVALID_GAP",
                        outcome_r_gross=None,
                        outcome_r_net=None,
                        mfe_r=None,
                        mae_r=None,
                        same_candle_ambiguity=False,
                        resolved_at_ms=int(now_ms),
                    )
                )
                continue

            outcome: DualTargetResolution = resolve_dual_target_horizon(
                signal_close_time_ms=sig_close_ms,
                entry_price=float(row["entry_ref_price"]),
                stop_price=float(row["stop_price"]),
                target1_price=float(row["target1_price"]),
                target2_price=float(row["target2_price"]),
                horizon_end_ms=horizon_end_ms,
                candles=candle_rows,
                roundtrip_cost_pct=float(row["cost_roundtrip_pct"]),
            )
            if outcome.status == "OPEN":
                continue

            resolved_ts = int(outcome.resolved_at_ms if outcome.resolved_at_ms is not None else now_ms)
            with self._db.write_tx() as conn:
                conn.execute(
                    """
                    UPDATE virtual_signals
                    SET status = ?,
                        same_candle_ambiguity = ?,
                        resolved_at_ms = ?,
                        outcome_r_gross = ?,
                        outcome_r_net = ?,
                        mfe_r = ?,
                        mae_r = ?
                    WHERE signal_id = ?
                    """,
                    (
                        outcome.status,
                        1 if outcome.same_candle_ambiguity else 0,
                        resolved_ts,
                        outcome.r_gross,
                        outcome.r_net,
                        outcome.mfe_r,
                        outcome.mae_r,
                        sig_id,
                    ),
                )

            resolved.append(
                ResolvedVirtualSignal(
                    signal_id=sig_id,
                    symbol=sym,
                    grade=str(row["grade"]),
                    status=outcome.status,
                    outcome_r_gross=outcome.r_gross,
                    outcome_r_net=outcome.r_net,
                    mfe_r=outcome.mfe_r,
                    mae_r=outcome.mae_r,
                    same_candle_ambiguity=outcome.same_candle_ambiguity,
                    resolved_at_ms=resolved_ts,
                )
            )
        return resolved

    def generate_weekly_scorecard_markdown(self, since_ms: int, until_ms: int) -> str:
        """Build a concise Markdown scorecard for virtual_signals in [since_ms, until_ms]."""
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT grade, status, outcome_r_net
                FROM virtual_signals
                WHERE emitted_at_ms >= ? AND emitted_at_ms <= ?
                """,
                (int(since_ms), int(until_ms)),
            ).fetchall()

        total = len(rows)
        invalid_gaps = sum(1 for r in rows if r["status"] == "INVALID_GAP")
        closed = [r for r in rows if r["status"] not in ("OPEN", "INVALID_GAP") and r["outcome_r_net"] is not None]
        wins = sum(1 for r in closed if float(r["outcome_r_net"]) > 0.0)
        mean_r = (sum(float(r["outcome_r_net"]) for r in closed) / len(closed)) if closed else 0.0
        win_rate = (wins / len(closed) * 100.0) if closed else 0.0

        lines = [
            "# Radar Weekly Scorecard",
            "",
            f"- **Total Signals:** {total}",
            f"- **Resolved:** {len(closed)} (Win Rate: {win_rate:.1f}%)",
            f"- **Mean Net R:** {mean_r:+.3f}R",
            f"- **Invalid Gaps:** {invalid_gaps}",
            "",
            "| Grade | Resolved | Mean Net R |",
            "| --- | --- | --- |",
        ]
        for g in ("A+", "A", "B"):
            sub = [float(r["outcome_r_net"]) for r in closed if r["grade"] == g]
            g_mean = (sum(sub) / len(sub)) if sub else 0.0
            lines.append(f"| {g} | {len(sub)} | {g_mean:+.3f}R |")
        return "\n".join(lines)
