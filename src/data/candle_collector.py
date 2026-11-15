from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Sequence

import pandas as pd

from src.core.binance_client import MonotonicClock, PublicBinanceClient
from src.core.config import Interval
from src.core.database import CandleRow, Database

INTERVAL_MS: Final[dict[str, int]] = {
    "1m": 60_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


class KlineIntegrityError(ValueError):
    """Raised when a Binance kline payload fails structural or causal integrity checks."""


def normalize_close_time(
    raw_close_time_ms: int,
    open_time_ms: int,
    interval: Interval,
) -> int:
    if interval not in INTERVAL_MS:
        raise KlineIntegrityError(f"Unsupported interval: {interval!r}")
    delta_ms = INTERVAL_MS[interval]
    open_ms = int(open_time_ms)
    if open_ms % delta_ms != 0:
        raise KlineIntegrityError(
            f"open_time_ms {open_ms} is not aligned to {interval} boundary ({delta_ms} ms)."
        )
    normalized = int(raw_close_time_ms) + 1
    expected = open_ms + delta_ms
    if normalized != expected:
        raise KlineIntegrityError(
            f"Normalized close_time_ms {normalized} != open_time_ms + Δ ({expected}) "
            f"for interval={interval} (raw_close_time_ms={raw_close_time_ms})."
        )
    return normalized


def parse_kline(
    raw: Sequence[Any],
    symbol: str,
    interval: Interval,
    ingested_at_ms: int,
) -> CandleRow:
    if len(raw) < 11:
        raise KlineIntegrityError(
            f"Kline payload for {symbol} ({interval}) has {len(raw)} elements; expected >= 11."
        )
    open_time_ms = int(raw[0])
    close_time_ms = normalize_close_time(int(raw[6]), open_time_ms, interval)

    o = float(raw[1])
    h = float(raw[2])
    low_p = float(raw[3])
    c = float(raw[4])
    vol = float(raw[5])
    quote_vol = float(raw[7])
    num_trades = int(raw[8])
    taker_buy_base = float(raw[9])
    taker_buy_quote = float(raw[10])

    if h < low_p:
        raise KlineIntegrityError(f"Invalid kline high < low ({h} < {low_p}) for {symbol}.")
    if vol < 0.0 or quote_vol < 0.0 or taker_buy_base < 0.0 or taker_buy_quote < 0.0:
        raise KlineIntegrityError(f"Negative volume field in kline for {symbol}.")
    if taker_buy_base > vol + 1e-9:
        raise KlineIntegrityError(
            f"taker_buy_base_volume ({taker_buy_base}) > volume ({vol}) for {symbol}."
        )

    return CandleRow(
        symbol=str(symbol),
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=close_time_ms,
        open=o,
        high=h,
        low=low_p,
        close=c,
        volume=vol,
        quote_volume=quote_vol,
        number_of_trades=num_trades,
        taker_buy_base_volume=taker_buy_base,
        taker_buy_quote_volume=taker_buy_quote,
        ingested_at_ms=int(ingested_at_ms),
    )


def last_closed_close_time(server_now_ms: int, interval: Interval) -> int:
    if interval not in INTERVAL_MS:
        raise KlineIntegrityError(f"Unsupported interval: {interval!r}")
    delta_ms = INTERVAL_MS[interval]
    return (int(server_now_ms) // delta_ms) * delta_ms


def is_interval_boundary(close_time_ms: int, interval: Interval) -> bool:
    if interval not in INTERVAL_MS:
        raise KlineIntegrityError(f"Unsupported interval: {interval!r}")
    return int(close_time_ms) % INTERVAL_MS[interval] == 0


def drop_unclosed(rows: Sequence[CandleRow], server_now_ms: int) -> list[CandleRow]:
    now_ms = int(server_now_ms)
    return [r for r in rows if r.close_time_ms <= now_ms]


@dataclass(slots=True)
class CollectionReport:
    interval: Interval
    expected_close_time_ms: int
    ok: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    gaps: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    retries_used: int = 0


class CandleCollector:
    def __init__(
        self,
        client: PublicBinanceClient,
        db: Database,
        clock: MonotonicClock,
        *,
        fetch_delay_s: float = 3.0,
        max_retries: int = 5,
        retry_sleep_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.db = db
        self.clock = clock
        self.fetch_delay_s = float(fetch_delay_s)
        self.max_retries = int(max_retries)
        self.retry_sleep_s = float(retry_sleep_s)
        self._sleep = sleep

    def wait_for_cycle(self) -> int:
        now_ms = self.clock.server_now_ms()
        hour_ms = INTERVAL_MS["1h"]
        delay_ms = int(round(self.fetch_delay_s * 1000.0))
        current_boundary_ms = (now_ms // hour_ms) * hour_ms
        if now_ms < current_boundary_ms + delay_ms:
            target_boundary_ms = current_boundary_ms
        else:
            target_boundary_ms = current_boundary_ms + hour_ms
        wake_ms = target_boundary_ms + delay_ms
        sleep_s = max(0.0, (wake_ms - now_ms) / 1000.0)
        if sleep_s > 0.0:
            self._sleep(sleep_s)
        return target_boundary_ms

    def backfill(
        self,
        symbols: Sequence[str],
        interval: Interval,
        *,
        lookback_candles: int = 1000,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        limit = max(1, min(1000, int(lookback_candles)))
        for sym in symbols:
            now_ms = self.clock.server_now_ms()
            raw_list = self.client.get_klines(sym, interval, limit=limit)
            parsed: list[CandleRow] = []
            for r in raw_list:
                try:
                    parsed.append(parse_kline(r, sym, interval, now_ms))
                except KlineIntegrityError:
                    continue
            closed = drop_unclosed(parsed, now_ms)
            counts[sym] = self.db.upsert_candles(closed)
        return counts

    def backfill_historical_range(
        self,
        symbol: str,
        interval: Interval,
        *,
        start_time_ms: int,
        end_time_ms: int | None = None,
    ) -> int:
        """
        Paginates 1000-candle chunks from start_time_ms to end_time_ms for Colab historical backfills.
        Automatically skips truncated maintenance klines (e.g., Binance 2023-03-24 matching engine halt
        at raw_close_time_ms=1679661581646) so indicator series only receive complete aligned bars.
        """
        delta_ms = INTERVAL_MS[interval]
        cursor_ms = (int(start_time_ms) // delta_ms) * delta_ms
        target_end_ms = int(end_time_ms) if end_time_ms is not None else self.clock.server_now_ms()
        total_upserted = 0

        while cursor_ms < target_end_ms:
            now_ms = self.clock.server_now_ms()
            raw_list = self.client.get_klines(
                symbol,
                interval,
                start_time_ms=cursor_ms,
                end_time_ms=target_end_ms,
                limit=1000,
            )
            if not raw_list:
                break
            parsed: list[CandleRow] = []
            for r in raw_list:
                try:
                    parsed.append(parse_kline(r, symbol, interval, now_ms))
                except KlineIntegrityError:
                    # Historical Binance maintenance outage candle (truncated close_time or unaligned open_time)
                    continue
            closed = drop_unclosed(parsed, now_ms)
            if closed:
                total_upserted += self.db.upsert_candles(closed)
            last_raw_open = int(raw_list[-1][0])
            last_raw_close_plus_1 = int(raw_list[-1][6]) + 1
            next_cursor_ms = max(
                ((last_raw_open // delta_ms) + 1) * delta_ms,
                ((last_raw_close_plus_1 + delta_ms - 1) // delta_ms) * delta_ms,
            )
            if next_cursor_ms <= cursor_ms:
                break
            cursor_ms = next_cursor_ms
            if len(raw_list) < 1000:
                break

        return total_upserted

    def collect_closed(
        self,
        symbols: Sequence[str],
        interval: Interval,
        *,
        expected_close_time_ms: int,
    ) -> CollectionReport:
        report = CollectionReport(
            interval=interval,
            expected_close_time_ms=int(expected_close_time_ms),
        )
        pending = list(dict.fromkeys(symbols))
        attempt = 0

        while pending:
            still_pending: list[str] = []
            for sym in pending:
                now_ms = max(self.clock.server_now_ms(), int(expected_close_time_ms))
                raw_list = self.client.get_klines(sym, interval, limit=3)
                parsed = [parse_kline(r, sym, interval, now_ms) for r in raw_list]
                closed = drop_unclosed(parsed, now_ms)
                if closed:
                    self.db.upsert_candles(closed)
                has_expected = any(
                    r.close_time_ms == int(expected_close_time_ms) for r in closed
                )
                if has_expected:
                    report.ok.append(sym)
                else:
                    still_pending.append(sym)

            if not still_pending:
                break
            if attempt >= self.max_retries:
                report.stale.extend(still_pending)
                break

            attempt += 1
            report.retries_used = attempt
            self._sleep(self.retry_sleep_s)
            pending = still_pending

        delta_ms = INTERVAL_MS[interval]
        since_ms = int(expected_close_time_ms) - 50 * delta_ms
        for sym in report.ok:
            sym_gaps = self.detect_gaps(sym, interval, since_ms=since_ms)
            if sym_gaps:
                report.gaps[sym] = sym_gaps

        return report

    def detect_gaps(
        self,
        symbol: str,
        interval: Interval,
        since_ms: int,
    ) -> list[tuple[int, int]]:
        delta_ms = INTERVAL_MS[interval]
        times = self.db.close_times(symbol, interval, since_ms=int(since_ms))
        gaps: list[tuple[int, int]] = []
        for prev_close, next_close in zip(times, times[1:]):
            if next_close - prev_close > delta_ms:
                missing_first_close = prev_close + delta_ms
                missing_last_close = next_close - delta_ms
                gaps.append((missing_first_close, missing_last_close))
        return gaps

    def repair_gaps(
        self,
        symbol: str,
        interval: Interval,
        gaps: Sequence[tuple[int, int]],
    ) -> int:
        delta_ms = INTERVAL_MS[interval]
        repaired_total = 0
        for first_close_ms, last_close_ms in gaps:
            start_open_ms = int(first_close_ms) - delta_ms
            end_close_ms = int(last_close_ms)
            now_ms = self.clock.server_now_ms()
            raw_list = self.client.get_klines(
                symbol,
                interval,
                start_time_ms=start_open_ms,
                end_time_ms=end_close_ms,
                limit=1000,
            )
            parsed = [parse_kline(r, symbol, interval, now_ms) for r in raw_list]
            closed = drop_unclosed(parsed, now_ms)
            repaired_total += self.db.upsert_candles(closed)
        return repaired_total

    def run_cycle(self, symbols: Sequence[str]) -> list[CollectionReport]:
        now_ms = self.clock.server_now_ms()
        expected_1h_close = last_closed_close_time(now_ms, "1h")
        reports: list[CollectionReport] = [
            self.collect_closed(symbols, "1h", expected_close_time_ms=expected_1h_close)
        ]
        if is_interval_boundary(expected_1h_close, "4h"):
            reports.append(
                self.collect_closed(
                    symbols,
                    "4h",
                    expected_close_time_ms=expected_1h_close,
                )
            )
        return reports


def load_frame(
    db: Database,
    symbol: str,
    interval: Interval,
    *,
    limit: int,
    until_close_time_ms: int | None = None,
) -> pd.DataFrame:
    return db.load_candles(
        symbol,
        interval,
        limit=limit,
        until_close_time_ms=until_close_time_ms,
    )


def align_4h_to_1h(
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    *,
    suffix: str = "_4h",
) -> pd.DataFrame:
    left = df_1h.sort_values("close_time_ms").reset_index(drop=True)
    right = df_4h.sort_values("close_time_ms").reset_index(drop=True)

    rename_map = {
        c: f"{c}{suffix}"
        for c in right.columns
        if c != "close_time_ms"
    }
    right_renamed = right.rename(columns=rename_map)
    right_renamed[f"close_time_ms{suffix}"] = right["close_time_ms"].astype("int64")

    merged = pd.merge_asof(
        left,
        right_renamed,
        on="close_time_ms",
        direction="backward",
        allow_exact_matches=True,
    )
    return merged


def align_btc_context(
    df_asset: pd.DataFrame,
    df_btc: pd.DataFrame,
) -> pd.DataFrame:
    left = df_asset.sort_values("close_time_ms").reset_index(drop=True)
    right = df_btc.sort_values("close_time_ms").reset_index(drop=True)

    rename_map = {
        c: f"{c}_btc"
        for c in right.columns
        if c != "close_time_ms"
    }
    right_renamed = right.rename(columns=rename_map)
    right_renamed["close_time_ms_btc"] = right["close_time_ms"].astype("int64")

    merged = pd.merge_asof(
        left,
        right_renamed,
        on="close_time_ms",
        direction="backward",
        allow_exact_matches=True,
    )
    return merged
