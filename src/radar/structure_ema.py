from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.core.config import AnalystConfig
from src.features.price_action import bullish_engulfing, bullish_pinbar
from src.regimes.regime_detector import MarketRegime


@dataclass(frozen=True, slots=True)
class Layer123Score:
    symbol: str
    signal_close_time_ms: int
    regime_4h: MarketRegime
    score_l1: int
    score_l2: int
    score_l3: int
    subtotal_l1_l3: int
    close_price: float
    low_price: float
    atr14: float
    setup: str
    features_snapshot: dict[str, Any]


def _finite(val: Any) -> float | None:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def score_layers_1_to_3(
    symbol: str,
    df_1h: pd.DataFrame,
    regime_4h: MarketRegime,
    cfg: AnalystConfig,
) -> Layer123Score | None:
    """Evaluate Radar Layers 1 (Structure/EMA), 2 (Price Action), and 3 (Order Flow).

    Consumes shared FeatureStore columns and RegimeDetector output (ADR-008).
    """
    if df_1h is None or len(df_1h) < 3:
        return None

    row = df_1h.iloc[-1]
    close_p = _finite(row.get("close"))
    open_p = _finite(row.get("open"))
    high_p = _finite(row.get("high"))
    low_p = _finite(row.get("low"))
    atr14 = _finite(row.get("atr_14"))
    close_time_ms = int(row.get("close_time_ms", row.get("close_time", 0)))

    if (
        close_p is None
        or open_p is None
        or high_p is None
        or low_p is None
        or atr14 is None
        or atr14 <= 0.0
        or close_time_ms <= 0
    ):
        return None

    l1_cfg = cfg.layer_1_structure_ema
    l2_cfg = cfg.layer_2_price_action
    l3_cfg = cfg.layer_3_volume_and_order_flow

    ema_fast = _finite(row.get(f"ema_{l1_cfg.ema_fast}"))
    ema_mid = _finite(row.get(f"ema_{l1_cfg.ema_mid}"))
    ema_slow = _finite(row.get(f"ema_{l1_cfg.ema_slow}"))
    chop_14 = _finite(row.get(f"chop_{l1_cfg.chop_period}"))

    # ---- Layer 1: Structure & EMA (0..25) ----
    score_l1 = 0
    if regime_4h == MarketRegime.BULL:
        score_l1 += 6
    elif regime_4h == MarketRegime.SIDEWAYS:
        score_l1 += 2

    if ema_fast is not None and ema_mid is not None and ema_slow is not None:
        if ema_fast > ema_mid > ema_slow and close_p > ema_mid:
            score_l1 += 8
        elif close_p > ema_slow and ema_mid > ema_slow:
            score_l1 += 4

        tol = l1_cfg.pullback_touch_tolerance_atr * atr14
        near_fast = abs(low_p - ema_fast) <= tol and close_p >= ema_fast
        near_mid = abs(low_p - ema_mid) <= tol and close_p >= ema_mid
        if near_fast or near_mid:
            score_l1 += 6

    if chop_14 is not None and chop_14 <= l1_cfg.max_chop_for_trend:
        score_l1 += 5
    score_l1 = min(25, score_l1)

    # ---- Layer 2: Price Action (0..25) ----
    score_l2 = 0
    candle_range = high_p - low_p
    if candle_range >= l2_cfg.min_candle_range_atr * atr14 and close_p > open_p:
        score_l2 += 7

    tail = df_1h.tail(3)
    atr_series = tail["atr_14"]
    ema_fast_col = f"ema_{l1_cfg.ema_fast}"
    ema_fast_series = tail[ema_fast_col] if ema_fast_col in tail.columns else pd.Series(float("nan"), index=tail.index)
    pin_series = bullish_pinbar(
        tail,
        atr_series,
        min_lower=l2_cfg.pinbar_min_lower_wick_ratio,
        max_upper=l2_cfg.pinbar_max_upper_wick_ratio,
        min_range_atr=l2_cfg.min_candle_range_atr,
    )
    eng_series = bullish_engulfing(
        tail,
        atr_series,
        ema_fast_series,
        min_atr_mult=l2_cfg.engulfing_min_atr_mult,
    )
    is_pin = bool(pin_series.iloc[-1]) if len(pin_series) > 0 else False
    is_eng = bool(eng_series.iloc[-1]) if len(eng_series) > 0 else False
    sweep_col = f"liquidity_sweep_bull_{l2_cfg.liquidity_sweep_lookback_candles}"
    is_sweep = bool(row.get(sweep_col, False))

    if is_pin:
        score_l2 += 8
    if is_eng:
        score_l2 += 9
    if is_sweep:
        score_l2 += 8
    score_l2 = min(25, score_l2)

    # ---- Layer 3: Volume & Order Flow (0..25) ----
    score_l3 = 0
    rvol = _finite(row.get(f"rvol_{l3_cfg.rvol_lookback}"))
    tbr = _finite(row.get("taker_buy_ratio"))

    if rvol is not None:
        if rvol >= l3_cfg.high_conviction_rvol:
            score_l3 += 13
        elif rvol >= l3_cfg.min_rvol_for_signal:
            score_l3 += 8

    if tbr is not None:
        if tbr >= l3_cfg.strong_taker_buy_ratio:
            score_l3 += 12
        elif tbr >= l3_cfg.min_taker_buy_ratio:
            score_l3 += 8
    score_l3 = min(25, score_l3)

    if is_sweep:
        setup = "RADAR_SWEEP_REVERSAL"
    elif is_eng:
        setup = "RADAR_BULL_ENGULFING"
    elif is_pin:
        setup = "RADAR_BULL_PINBAR"
    else:
        setup = "RADAR_EMA_TREND"

    features_snapshot = {
        "regime_4h": str(regime_4h.value),
        "close": close_p,
        "atr_14": atr14,
        "chop_14": chop_14,
        "rvol": rvol,
        "tbr": tbr,
        "pinbar": is_pin,
        "engulfing": is_eng,
        "sweep": is_sweep,
    }

    return Layer123Score(
        symbol=symbol,
        signal_close_time_ms=close_time_ms,
        regime_4h=regime_4h,
        score_l1=score_l1,
        score_l2=score_l2,
        score_l3=score_l3,
        subtotal_l1_l3=score_l1 + score_l2 + score_l3,
        close_price=close_p,
        low_price=low_p,
        atr14=atr14,
        setup=setup,
        features_snapshot=features_snapshot,
    )
