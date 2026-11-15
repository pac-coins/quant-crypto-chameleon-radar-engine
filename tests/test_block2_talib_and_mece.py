from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd
import pytest

from src.features.feature_store import FeatureRequest, FeatureStore
from src.features.indicators import (
    adx_wilder,
    atr_wilder,
    bollinger,
    chop_log10,
    ema,
    rsi_wilder,
    rvol_median,
    true_range,
)
from src.features.order_flow import (
    order_flow_delta_base,
    order_flow_delta_quote,
    taker_buy_ratio,
    wyckoff_absorption_bull,
)
from src.features.price_action import (
    bullish_engulfing,
    bullish_pinbar,
    candle_wick_ratios,
    liquidity_sweep_bull,
)
from src.regimes.regime_detector import (
    MarketRegime,
    RegimeDetector,
    classify_regime_values,
)


def _synthetic_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=0.15, scale=2.2, size=n)
    close = 200.0 + np.cumsum(steps)
    open_p = np.roll(close, 1)
    open_p[0] = close[0] - 0.5
    spread = rng.uniform(0.5, 4.5, size=n)
    high = np.maximum(open_p, close) + spread * rng.uniform(0.3, 1.0, size=n)
    low = np.minimum(open_p, close) - spread * rng.uniform(0.3, 1.0, size=n)
    volume = rng.uniform(500.0, 5000.0, size=n)
    taker_buy_base = volume * rng.uniform(0.35, 0.70, size=n)
    quote_volume = volume * close
    taker_buy_quote = taker_buy_base * close
    base_ms = 1_767_225_600_000
    h1 = 3_600_000
    open_times = [base_ms + i * h1 for i in range(n)]
    close_times = [t + h1 for t in open_times]
    return pd.DataFrame(
        {
            "symbol": ["SOLUSDT"] * n,
            "interval": ["1h"] * n,
            "open_time_ms": open_times,
            "close_time_ms": close_times,
            "open": open_p,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": quote_volume,
            "number_of_trades": [1000] * n,
            "taker_buy_base_volume": taker_buy_base,
            "taker_buy_quote_volume": taker_buy_quote,
            "ingested_at_ms": close_times,
        }
    )


def _reference_talib_ema(close: np.ndarray, period: int) -> np.ndarray:
    """Direct transcription of TA_INT_EMA from TA-Lib C source (`ta_EMA.c`)."""
    out = np.full(len(close), np.nan, dtype="float64")
    k = 2.0 / (period + 1.0)
    prev_ma = float(np.mean(close[:period]))
    out[period - 1] = prev_ma
    for i in range(period, len(close)):
        prev_ma = ((close[i] - prev_ma) * k) + prev_ma
        out[i] = prev_ma
    return out


def _reference_talib_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int
) -> np.ndarray:
    """Direct transcription of TA_ATR from TA-Lib C source (`ta_ATR.c`)."""
    out = np.full(len(close), np.nan, dtype="float64")
    tr = np.zeros(len(close), dtype="float64")
    for i in range(1, len(close)):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    prev_atr = float(np.mean(tr[1 : period + 1]))
    out[period] = prev_atr
    for i in range(period + 1, len(close)):
        prev_atr = ((prev_atr * (period - 1)) + tr[i]) / period
        out[i] = prev_atr
    return out


def _reference_talib_rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Direct transcription of TA_RSI from TA-Lib C source (`ta_RSI.c`)."""
    out = np.full(len(close), np.nan, dtype="float64")
    prev_gain = 0.0
    prev_loss = 0.0
    for i in range(1, period + 1):
        diff = close[i] - close[i - 1]
        if diff < 0.0:
            prev_loss -= diff
        else:
            prev_gain += diff
    prev_loss /= period
    prev_gain /= period
    denom = prev_gain + prev_loss
    out[period] = 100.0 * (prev_gain / denom) if denom != 0.0 else 0.0
    for i in range(period + 1, len(close)):
        diff = close[i] - close[i - 1]
        prev_gain *= period - 1
        prev_loss *= period - 1
        if diff < 0.0:
            prev_loss -= diff
        else:
            prev_gain += diff
        prev_gain /= period
        prev_loss /= period
        denom = prev_gain + prev_loss
        out[i] = 100.0 * (prev_gain / denom) if denom != 0.0 else 0.0
    return out


def _reference_talib_adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int
) -> np.ndarray:
    """Direct transcription of TA_ADX from TA-Lib C source (`ta_ADX.c`)."""
    out = np.full(len(close), np.nan, dtype="float64")
    prev_plus_dm = 0.0
    prev_minus_dm = 0.0
    prev_tr = 0.0
    for i in range(1, period):
        diff_p = high[i] - high[i - 1]
        diff_m = low[i - 1] - low[i]
        if (diff_p > 0.0) and (diff_p > diff_m):
            prev_plus_dm += diff_p
        elif (diff_m > 0.0) and (diff_m > diff_p):
            prev_minus_dm += diff_m
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        prev_tr += tr

    sum_dx = 0.0
    for i in range(period, 2 * period):
        diff_p = high[i] - high[i - 1]
        diff_m = low[i - 1] - low[i]
        prev_plus_dm -= prev_plus_dm / period
        prev_minus_dm -= prev_minus_dm / period
        if (diff_p > 0.0) and (diff_p > diff_m):
            prev_plus_dm += diff_p
        elif (diff_m > 0.0) and (diff_m > diff_p):
            prev_minus_dm += diff_m
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        prev_tr = prev_tr - (prev_tr / period) + tr
        if prev_tr != 0.0:
            plus_di = 100.0 * (prev_plus_dm / prev_tr)
            minus_di = 100.0 * (prev_minus_dm / prev_tr)
            temp = plus_di + minus_di
            dx = 100.0 * (abs(plus_di - minus_di) / temp) if temp != 0.0 else 0.0
        else:
            dx = 0.0
        sum_dx += dx

    prev_adx = sum_dx / period
    out[2 * period - 1] = prev_adx
    for i in range(2 * period, len(close)):
        diff_p = high[i] - high[i - 1]
        diff_m = low[i - 1] - low[i]
        prev_plus_dm -= prev_plus_dm / period
        prev_minus_dm -= prev_minus_dm / period
        if (diff_p > 0.0) and (diff_p > diff_m):
            prev_plus_dm += diff_p
        elif (diff_m > 0.0) and (diff_m > diff_p):
            prev_minus_dm += diff_m
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        prev_tr = prev_tr - (prev_tr / period) + tr
        if prev_tr != 0.0:
            plus_di = 100.0 * (prev_plus_dm / prev_tr)
            minus_di = 100.0 * (prev_minus_dm / prev_tr)
            temp = plus_di + minus_di
            dx = 100.0 * (abs(plus_di - minus_di) / temp) if temp != 0.0 else 0.0
        else:
            dx = 0.0
        prev_adx = ((prev_adx * (period - 1)) + dx) / period
        out[i] = prev_adx
    return out


# ==============================================================================
# Gate 2 (a): Equivalência exata com TA-Lib / Wilder (< 1e-10 e correlação > 0.9999)
# ==============================================================================
def test_gate2a_talib_wilder_equivalence_under_1e10() -> None:
    df = _synthetic_ohlcv(n=400, seed=2026)
    h = df["high"].to_numpy(dtype="float64")
    low_arr = df["low"].to_numpy(dtype="float64")
    c = df["close"].to_numpy(dtype="float64")

    for p in (9, 20, 50, 100, 200):
        calc_ema = ema(df["close"], p).to_numpy(dtype="float64")
        ref_ema = _reference_talib_ema(c, p)
        valid = ~np.isnan(ref_ema)
        assert np.max(np.abs(calc_ema[valid] - ref_ema[valid])) < 1e-10
        assert np.corrcoef(calc_ema[valid], ref_ema[valid])[0, 1] > 0.999999

    calc_atr = atr_wilder(df, 14).to_numpy(dtype="float64")
    ref_atr = _reference_talib_atr(h, low_arr, c, 14)
    valid_atr = ~np.isnan(ref_atr)
    assert np.max(np.abs(calc_atr[valid_atr] - ref_atr[valid_atr])) < 1e-10
    assert np.corrcoef(calc_atr[valid_atr], ref_atr[valid_atr])[0, 1] > 0.999999

    calc_rsi = rsi_wilder(df["close"], 14).to_numpy(dtype="float64")
    ref_rsi = _reference_talib_rsi(c, 14)
    valid_rsi = ~np.isnan(ref_rsi)
    assert np.max(np.abs(calc_rsi[valid_rsi] - ref_rsi[valid_rsi])) < 1e-10
    assert np.corrcoef(calc_rsi[valid_rsi], ref_rsi[valid_rsi])[0, 1] > 0.999999

    calc_adx = adx_wilder(df, 14).to_numpy(dtype="float64")
    ref_adx = _reference_talib_adx(h, low_arr, c, 14)
    valid_adx = ~np.isnan(ref_adx)
    assert np.max(np.abs(calc_adx[valid_adx] - ref_adx[valid_adx])) < 1e-10
    assert np.corrcoef(calc_adx[valid_adx], ref_adx[valid_adx])[0, 1] > 0.999999

    # Verify CHOP base-10 bounds on trending vs choppy synthetic series
    chop = chop_log10(df, 14).dropna()
    assert ((chop >= 0.0) & (chop <= 100.0)).all()
    bb = bollinger(df["close"], 20, 2.0).dropna()
    assert (bb["upper"] >= bb["mid"]).all() and (bb["mid"] >= bb["lower"]).all()
    assert true_range(df).isna().iloc[0]


# ==============================================================================
# Gate 2 (b) & (c): Série constante (divisão por zero -> NaN / TRANSITION) e warmup < 120
# ==============================================================================
def test_gate2b_and_2c_flat_prices_zero_volume_and_warmup_guard() -> None:
    n = 150
    flat_df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [100.0] * n,
            "low": [100.0] * n,
            "close": [100.0] * n,
            "volume": [0.0] * n,
            "quote_volume": [0.0] * n,
            "taker_buy_base_volume": [0.0] * n,
            "taker_buy_quote_volume": [0.0] * n,
        }
    )

    # CHOP on flat prices (High == Low) MUST return NaN without ZeroDivisionError or inf (TODO Cat.1)
    chop_flat = chop_log10(flat_df, 14)
    assert chop_flat.isna().all()
    assert not np.isinf(chop_flat.to_numpy(dtype="float64")).any()

    # RVOL and TBR on zero volume MUST return NaN, never inf (C2-6)
    rvol_zero = rvol_median(flat_df["volume"], 20)
    tbr_zero = taker_buy_ratio(flat_df["volume"], flat_df["taker_buy_base_volume"])
    delta_q_zero = order_flow_delta_quote(
        flat_df["quote_volume"], flat_df["taker_buy_quote_volume"]
    )
    assert rvol_zero.isna().all()
    assert tbr_zero.isna().all()
    assert delta_q_zero.isna().all()

    # RegimeDetector on flat prices returns TRANSITION with fallback_reason == "NAN"
    detector = RegimeDetector()
    snap_flat = detector.detect_on_4h_frame(flat_df)
    assert snap_flat.regime == MarketRegime.TRANSITION
    assert snap_flat.fallback_reason == "NAN"

    # Gate 2 (c): history < 120 4h candles MUST return TRANSITION with fallback_reason == "WARMUP_LT_120"
    short_df = _synthetic_ohlcv(n=119, seed=7)
    snap_short = detector.detect_on_4h_frame(short_df)
    assert snap_short.regime == MarketRegime.TRANSITION
    assert snap_short.fallback_reason == "WARMUP_LT_120"


# ==============================================================================
# Gate 2 (d): Cobertura MECE exaustiva e disjunta em grade de estados e fronteiras
# ==============================================================================
def test_gate2d_mece_exhaustive_and_disjoint_boundary_grid() -> None:
    ema_val = 100.0
    closes = [95.0, 100.0, 105.0, float("nan"), float("inf")]
    slopes = [-1.0, 0.0, 1.0, float("nan")]
    chops = [35.0, 49.9999, 50.0, 52.0, 54.9999, 55.0, 65.0, float("nan")]
    adxs = [15.0, 21.9999, 22.0, 30.0, float("nan")]

    valid_regimes = {
        MarketRegime.BEAR,
        MarketRegime.BULL,
        MarketRegime.SIDEWAYS,
        MarketRegime.TRANSITION,
    }
    counts = {r: 0 for r in valid_regimes}

    for c, s, ch, ax in itertools.product(closes, slopes, chops, adxs):
        snap = classify_regime_values(
            close_4h=c,
            ema100_4h=ema_val,
            ema100_slope_4h=s,
            chop14_4h=ch,
            adx14_4h=ax,
            has_min_history=True,
        )
        assert snap.regime in valid_regimes

        # Verify independent predicate definitions to prove mutual exclusivity
        any_nan = not all(math.isfinite(x) for x in (c, ema_val, s, ch, ax))
        is_bear = (not any_nan) and (c < ema_val and s < 0.0)
        is_bull = (not any_nan) and (not is_bear) and (c > ema_val and s > 0.0 and ch < 50.0)
        is_sideways = (
            (not any_nan)
            and (not is_bear)
            and (not is_bull)
            and (ch >= 55.0 and ax < 22.0)
        )
        is_transition = any_nan or (not (is_bear or is_bull or is_sideways))

        # Exactly one bucket is True (MECE)
        assert sum([is_bear, is_bull, is_sideways, is_transition]) == 1
        if is_bear:
            assert snap.regime == MarketRegime.BEAR
        elif is_bull:
            assert snap.regime == MarketRegime.BULL
        elif is_sideways:
            assert snap.regime == MarketRegime.SIDEWAYS
        else:
            assert snap.regime == MarketRegime.TRANSITION

        counts[snap.regime] += 1

    # Every regime was exercised in the grid
    assert all(v > 0 for v in counts.values())

    # Explicit boundary checks from specification:
    # 1. CHOP == 52.0 (between 50 and 55) in uptrend -> TRANSITION (no hole!)
    assert (
        classify_regime_values(
            close_4h=105.0,
            ema100_4h=100.0,
            ema100_slope_4h=1.0,
            chop14_4h=52.0,
            adx14_4h=25.0,
        ).regime
        == MarketRegime.TRANSITION
    )
    # 2. BEAR has strict precedence over SIDEWAYS even when CHOP >= 55 and ADX < 22
    assert (
        classify_regime_values(
            close_4h=90.0,
            ema100_4h=100.0,
            ema100_slope_4h=-0.5,
            chop14_4h=62.0,
            adx14_4h=14.0,
        ).regime
        == MarketRegime.BEAR
    )


# ==============================================================================
# Gate 2 (e): FeatureStore deduplicado (EMA 20/100 + EMA 9/50/200) + PriceAction + OrderFlow
# ==============================================================================
class _DummyChameleonConsumer:
    def required_features(self) -> frozenset[FeatureRequest]:
        return frozenset(
            {
                FeatureRequest("ema", 20, "1h"),
                FeatureRequest("ema", 100, "1h"),
                FeatureRequest("atr", 14, "1h"),
            }
        )


class _DummyRadarConsumer:
    def required_features(self) -> frozenset[FeatureRequest]:
        return frozenset(
            {
                FeatureRequest("ema", 9, "1h"),
                FeatureRequest("ema", 50, "1h"),
                FeatureRequest("ema", 200, "1h"),
                FeatureRequest("atr", 14, "1h"),  # duplicate with Chameleon -> computed once
                FeatureRequest("rvol", 20, "1h"),
                FeatureRequest("tbr", 1, "1h"),
                FeatureRequest("of_delta", 1, "1h"),
                FeatureRequest("sweep", 20, "1h"),
                FeatureRequest("bb", 20, "1h", (2.0,)),
            }
        )


def test_gate2e_feature_store_deduplication_and_price_action_order_flow() -> None:
    detector = RegimeDetector()
    store = FeatureStore(
        consumers=[_DummyChameleonConsumer(), _DummyRadarConsumer(), detector]
    )

    # atr_14 was requested by both Chameleon and Radar, but exists once in store.requests
    atr_reqs = [r for r in store.requests if r.kind == "atr" and r.timeframe == "1h"]
    assert len(atr_reqs) == 1

    df_1h = _synthetic_ohlcv(n=240, seed=101)
    # Build 60 4h candles covering the same 240 hours
    h4 = 14_400_000
    base_ms = int(df_1h["open_time_ms"].iloc[0])
    df_4h = _synthetic_ohlcv(n=60, seed=202)
    df_4h["interval"] = "4h"
    df_4h["open_time_ms"] = [base_ms + j * h4 for j in range(60)]
    df_4h["close_time_ms"] = [base_ms + (j + 1) * h4 for j in range(60)]

    merged = store.build(df_1h, df_4h)
    expected_cols = {
        "ema_9",
        "ema_20",
        "ema_50",
        "ema_100",
        "ema_200",
        "atr_14",
        "rvol_20",
        "tbr",
        "of_delta_quote",
        "liquidity_sweep_bull_20",
        "bb_20_2.0_mid",
        "bb_20_2.0_upper",
        "bb_20_2.0_lower",
        "ema_100_4h",
        "chop_14_4h",
        "adx_14_4h",
    }
    assert expected_cols.issubset(set(merged.columns))

    # Synthetic Price Action verification: Pinbar, Engulfing, and Liquidity Sweep
    pa_df = _synthetic_ohlcv(n=30, seed=99).copy()
    # Force candle at index 25 to be a textbook Bullish Rejection Pinbar + 20h Liquidity Sweep
    min_prior_20 = float(pa_df["low"].iloc[5:25].min())
    pa_df.loc[25, "low"] = min_prior_20 - 5.0
    pa_df.loc[25, "open"] = min_prior_20 + 3.5
    pa_df.loc[25, "close"] = min_prior_20 + 4.5
    pa_df.loc[25, "high"] = min_prior_20 + 5.0
    atr_s = pd.Series([2.0] * len(pa_df), index=pa_df.index)

    pinbars = bullish_pinbar(pa_df, atr_s)
    sweeps = liquidity_sweep_bull(pa_df, lookback=20)
    assert bool(pinbars.iloc[25]) is True
    assert bool(sweeps.iloc[25]) is True

    # Force candle at index 27 to be a Bullish Momentum Engulfing above EMA9
    pa_df.loc[26, "open"] = 210.0
    pa_df.loc[26, "close"] = 208.0
    pa_df.loc[26, "high"] = 210.5
    pa_df.loc[26, "low"] = 207.5

    pa_df.loc[27, "open"] = 207.8
    pa_df.loc[27, "close"] = 212.0
    pa_df.loc[27, "high"] = 212.2
    pa_df.loc[27, "low"] = 207.5
    ema9_s = pd.Series([209.0] * len(pa_df), index=pa_df.index)

    engulf = bullish_engulfing(pa_df, atr_s, ema9_s)
    assert bool(engulf.iloc[27]) is True

    # Order Flow Delta (base & quote) + Wyckoff absorption
    vol_s = pd.Series([100.0], index=[0])
    tb_base_s = pd.Series([60.0], index=[0])
    qvol_s = pd.Series([10_000.0], index=[0])
    tb_quote_s = pd.Series([6_000.0], index=[0])
    assert taker_buy_ratio(vol_s, tb_base_s).iloc[0] == pytest.approx(0.60)
    assert order_flow_delta_base(vol_s, tb_base_s).iloc[0] == pytest.approx(20.0)
    assert order_flow_delta_quote(qvol_s, tb_quote_s).iloc[0] == pytest.approx(2_000.0)

    abs_series = wyckoff_absorption_bull(
        pa_df,
        ema50=pd.Series([float(pa_df.loc[25, "low"])] * len(pa_df), index=pa_df.index),
        ema200=pd.Series([150.0] * len(pa_df), index=pa_df.index),
        atr14=atr_s,
        rvol20=pd.Series([2.5] * len(pa_df), index=pa_df.index),
        tbr=pd.Series([0.58] * len(pa_df), index=pa_df.index),
    )
    assert bool(abs_series.iloc[25]) is True
    assert len(candle_wick_ratios(pa_df)) == len(pa_df)
