from __future__ import annotations

import random
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.core.binance_client import (
    MonotonicClock,
    PublicBinanceClient,
    WeightGovernor,
    floor_qty_to_step,
    passes_filters,
    round_price_to_tick,
    to_api_str,
    to_decimal,
)
from src.core.database import Database, SymbolFilters
from src.data.candle_collector import (
    CandleCollector,
    INTERVAL_MS,
    KlineIntegrityError,
    align_4h_to_1h,
    align_btc_context,
    drop_unclosed,
    is_interval_boundary,
    last_closed_close_time,
    load_frame,
    normalize_close_time,
    parse_kline,
)


def _make_raw_kline(
    open_time_ms: int,
    interval_ms: int,
    *,
    open_p: float = 100.0,
    high_p: float = 105.0,
    low_p: float = 95.0,
    close_p: float = 102.0,
    volume: float = 1000.0,
    quote_volume: float = 101500.0,
    trades: int = 420,
    taker_buy_base: float = 580.0,
    taker_buy_quote: float = 58870.0,
) -> list[object]:
    raw_close_ms = open_time_ms + interval_ms - 1  # Binance kline format: XX:59:59.999
    return [
        open_time_ms,
        f"{open_p:.8f}",
        f"{high_p:.8f}",
        f"{low_p:.8f}",
        f"{close_p:.8f}",
        f"{volume:.8f}",
        raw_close_ms,
        f"{quote_volume:.8f}",
        trades,
        f"{taker_buy_base:.8f}",
        f"{taker_buy_quote:.8f}",
        "0",
    ]


# ==============================================================================
# Gate 1 (a): Causalidade temporal estrita (zero look-ahead bias às 02:00 e 04:00 UTC)
# ==============================================================================
def test_gate1a_strict_causal_alignment_truncation_at_0200_and_0400_utc(tmp_path: Path) -> None:
    db = Database(tmp_path / "causal.db")
    db.connect()
    db.migrate()

    # Base epoch: 2026-01-01 00:00:00 UTC (1767225600000, divisible by 4h = 14_400_000)
    base_ms = 1_767_225_600_000
    assert base_ms % INTERVAL_MS["4h"] == 0

    h1 = INTERVAL_MS["1h"]
    h4 = INTERVAL_MS["4h"]

    # Build 24 hours of 1h candles and 6 candles of 4h
    rows_1h = [
        parse_kline(
            _make_raw_kline(
                base_ms + i * h1,
                h1,
                close_p=100.0 + i,
                quote_volume=50000.0 + i * 100,
                trades=100 + i,
                taker_buy_base=500.0 + i,
                taker_buy_quote=28000.0 + i * 50,
            ),
            "SOLUSDT",
            "1h",
            ingested_at_ms=base_ms + (i + 1) * h1,
        )
        for i in range(24)
    ]
    rows_4h = [
        parse_kline(
            _make_raw_kline(
                base_ms + j * h4,
                h4,
                close_p=1000.0 + j * 10.0,
            ),
            "SOLUSDT",
            "4h",
            ingested_at_ms=base_ms + (j + 1) * h4,
        )
        for j in range(6)
    ]
    db.upsert_candles(rows_1h)
    db.upsert_candles(rows_4h)

    full_1h = load_frame(db, "SOLUSDT", "1h", limit=1000)
    full_4h = load_frame(db, "SOLUSDT", "4h", limit=1000)
    aligned_full = align_4h_to_1h(full_1h, full_4h)

    # Truncate at 06:00 UTC (which is 02:00 into the second 4h candle [04:00..08:00 UTC])
    t_0600_utc = base_ms + 6 * h1  # 02:00 after the 04:00 UTC 4h close
    trunc_1h = load_frame(db, "SOLUSDT", "1h", limit=1000, until_close_time_ms=t_0600_utc)
    trunc_4h = load_frame(db, "SOLUSDT", "4h", limit=1000, until_close_time_ms=t_0600_utc)
    aligned_trunc = align_4h_to_1h(trunc_1h, trunc_4h)

    # Compare the prefix of full alignment up to t_0600_utc with the truncated alignment
    aligned_full_prefix = aligned_full[aligned_full["close_time_ms"] <= t_0600_utc].reset_index(
        drop=True
    )
    pd.testing.assert_frame_equal(aligned_trunc, aligned_full_prefix)

    # Verify exact causal visibility at 04:00, 05:00, 06:00, 07:00, and 08:00 UTC:
    # - At 04:00 UTC (j=0 close_time = base_ms + 4h), the 4h candle that closed at 04:00 UTC is visible.
    # - At 05:00, 06:00, 07:00 UTC, ONLY the 04:00 UTC 4h candle is visible (NOT the 08:00 UTC candle).
    # - At 08:00 UTC (j=1 close_time = base_ms + 8h), the new 4h candle is immediately visible (+1ms fix).
    row_at_0400 = aligned_full[aligned_full["close_time_ms"] == base_ms + 4 * h1].iloc[0]
    row_at_0600 = aligned_full[aligned_full["close_time_ms"] == base_ms + 6 * h1].iloc[0]
    row_at_0800 = aligned_full[aligned_full["close_time_ms"] == base_ms + 8 * h1].iloc[0]

    assert row_at_0400["close_time_ms_4h"] == base_ms + 4 * h1
    assert row_at_0400["close_4h"] == 1000.0
    assert row_at_0600["close_time_ms_4h"] == base_ms + 4 * h1
    assert row_at_0600["close_4h"] == 1000.0
    assert row_at_0800["close_time_ms_4h"] == base_ms + 8 * h1
    assert row_at_0800["close_4h"] == 1010.0

    # Also verify cross-asset BTC context alignment is strictly backward causal
    btc_1h = [
        parse_kline(
            _make_raw_kline(base_ms + i * h1, h1, close_p=50000.0 + i * 100.0),
            "BTCUSDT",
            "1h",
            ingested_at_ms=base_ms + (i + 1) * h1,
        )
        for i in range(24)
    ]
    db.upsert_candles(btc_1h)
    df_btc = load_frame(db, "BTCUSDT", "1h", limit=1000)
    with_btc = align_btc_context(full_1h, df_btc)
    assert (with_btc["close_time_ms_btc"] <= with_btc["close_time_ms"]).all()

    db.close()


# ==============================================================================
# Gate 1 (b): 10.000 simulações de preços e lotes via Decimal (tickSize/stepSize e sem E-)
# ==============================================================================
def test_gate1b_10000_random_decimal_prices_and_quantities() -> None:
    rng = random.Random(20261115)
    tick_choices = [
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.0001"),
        Decimal("0.00001"),
        Decimal("0.00000001"),
    ]
    step_choices = [
        Decimal("1"),
        Decimal("0.1"),
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.00001"),
        Decimal("0.000001"),
    ]

    for i in range(10_000):
        tick = tick_choices[i % len(tick_choices)]
        step = step_choices[i % len(step_choices)]

        raw_price_str = f"{rng.uniform(0.00001, 120000.0):.12f}"
        raw_qty_str = f"{rng.uniform(0.00001, 50000.0):.12f}"
        raw_price = to_decimal(raw_price_str)
        raw_qty = to_decimal(raw_qty_str)

        p_down = round_price_to_tick(raw_price, tick, "DOWN")
        p_up = round_price_to_tick(raw_price, tick, "UP")
        q_floor = floor_qty_to_step(raw_qty, step)

        assert p_down <= raw_price <= p_up
        assert (p_down % tick) == Decimal("0")
        assert (p_up % tick) == Decimal("0")
        assert q_floor <= raw_qty
        assert (q_floor % step) == Decimal("0")

        # Directional OCO stop vs stop_limit separation check
        stop_raw = raw_price * Decimal("0.98")
        stop_limit_raw = stop_raw * Decimal("0.975")
        stop_p = round_price_to_tick(stop_raw, tick, "UP")
        stop_lim_p = round_price_to_tick(stop_limit_raw, tick, "DOWN")
        if stop_raw > tick:
            assert stop_p > stop_lim_p

        s_price = to_api_str(p_down)
        s_qty = to_api_str(q_floor)
        assert "e" not in s_price.lower()
        assert "e" not in s_qty.lower()

    # Explicitly reject binary float
    with pytest.raises(TypeError):
        to_decimal(1.2345)  # type: ignore[arg-type]

    # Check scientific notation edge case (1E-8)
    tiny = Decimal("1E-8")
    assert to_api_str(tiny) == "0.00000001"

    filters = SymbolFilters(
        symbol="SOLUSDT",
        status="TRADING",
        base_asset="SOL",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.010"),
        min_notional=Decimal("5.00"),
        oco_allowed=True,
        fetched_at_ms=1_700_000_000_000,
    )
    ok, reason = passes_filters(Decimal("180.25"), Decimal("0.100"), filters)
    assert ok and reason is None
    ok_bad_notional, reason_notional = passes_filters(Decimal("10.00"), Decimal("0.100"), filters)
    assert not ok_bad_notional and reason_notional == "MIN_NOTIONAL"


# ==============================================================================
# Gate 1 (g): parse_kline persiste cols 7..10, normaliza close_time e descarta vela aberta
# ==============================================================================
def test_gate1g_kline_columns_7_to_10_close_time_normalization_and_drop_unclosed(
    tmp_path: Path,
) -> None:
    open_ms = 1_767_225_600_000
    h1 = INTERVAL_MS["1h"]
    raw_closed = _make_raw_kline(
        open_ms,
        h1,
        open_p=180.0,
        high_p=185.0,
        low_p=179.0,
        close_p=183.5,
        volume=1234.5,
        quote_volume=225432.10,
        trades=8765,
        taker_buy_base=700.25,
        taker_buy_quote=128145.75,
    )
    row_closed = parse_kline(raw_closed, "SOLUSDT", "1h", ingested_at_ms=open_ms + h1 + 3000)

    # Normalized close_time_ms == open_time_ms + 3_600_000 (raw[6] + 1)
    assert row_closed.close_time_ms == open_ms + h1
    assert row_closed.quote_volume == pytest.approx(225432.10)
    assert row_closed.number_of_trades == 8765
    assert row_closed.taker_buy_base_volume == pytest.approx(700.25)
    assert row_closed.taker_buy_quote_volume == pytest.approx(128145.75)

    # Unclosed candle (opens at open_ms + h1, closes at open_ms + 2*h1)
    raw_open = _make_raw_kline(open_ms + h1, h1)
    row_open = parse_kline(raw_open, "SOLUSDT", "1h", ingested_at_ms=open_ms + h1 + 3000)

    now_at_cycle = open_ms + h1 + 3000  # XX:00:03 UTC
    filtered = drop_unclosed([row_closed, row_open], server_now_ms=now_at_cycle)
    assert len(filtered) == 1
    assert filtered[0].close_time_ms == open_ms + h1

    # Integrity error if raw close_time does not match open_time + Δ - 1
    with pytest.raises(KlineIntegrityError):
        normalize_close_time(open_ms + 1234, open_ms, "1h")

    # End-to-end persistence and gap detection in SQLite
    db = Database(tmp_path / "kline.db")
    db.connect()
    db.migrate()

    # Insert candle 0 and candle 2 (leaving candle 1 as a gap)
    row_after_gap = parse_kline(
        _make_raw_kline(open_ms + 2 * h1, h1),
        "SOLUSDT",
        "1h",
        ingested_at_ms=open_ms + 3 * h1,
    )
    db.upsert_candles([row_closed, row_after_gap])

    df = load_frame(db, "SOLUSDT", "1h", limit=10)
    assert len(df) == 2
    assert df.iloc[0]["quote_volume"] == pytest.approx(225432.10)
    assert int(df.iloc[0]["number_of_trades"]) == 8765
    assert df.iloc[0]["taker_buy_base_volume"] == pytest.approx(700.25)
    assert df.iloc[0]["taker_buy_quote_volume"] == pytest.approx(128145.75)

    clock = MonotonicClock()
    collector = CandleCollector(
        PublicBinanceClient("https://data-api.binance.vision", WeightGovernor()),
        db,
        clock,
    )
    gaps = collector.detect_gaps("SOLUSDT", "1h", since_ms=open_ms)
    assert gaps == [(open_ms + 2 * h1, open_ms + 2 * h1)]
    assert is_interval_boundary(open_ms + 4 * h1, "4h")
    assert last_closed_close_time(open_ms + h1 + 3000, "1h") == open_ms + h1

    db.close()
