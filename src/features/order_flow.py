from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.price_action import candle_wick_ratios


def taker_buy_ratio(volume: pd.Series, taker_buy_base: pd.Series) -> pd.Series:
    """TBR_t = V_buy,t / V_t in [0, 1]. Returns NaN when V_t <= 0 (C2-6)."""
    v = volume.to_numpy(dtype="float64", copy=False)
    vb = taker_buy_base.to_numpy(dtype="float64", copy=False)
    out = np.full(len(v), np.nan, dtype="float64")
    valid = np.isfinite(v) & np.isfinite(vb) & (v > 0.0) & (vb >= 0.0)
    out[valid] = np.clip(vb[valid] / v[valid], 0.0, 1.0)
    return pd.Series(out, index=volume.index, name="tbr")


def order_flow_delta_base(volume: pd.Series, taker_buy_base: pd.Series) -> pd.Series:
    """Base asset Order Flow Delta: 2 * V_buy,t - V_t. Returns NaN when V_t <= 0."""
    v = volume.to_numpy(dtype="float64", copy=False)
    vb = taker_buy_base.to_numpy(dtype="float64", copy=False)
    out = np.full(len(v), np.nan, dtype="float64")
    valid = np.isfinite(v) & np.isfinite(vb) & (v > 0.0) & (vb >= 0.0)
    out[valid] = 2.0 * vb[valid] - v[valid]
    return pd.Series(out, index=volume.index, name="of_delta_base")


def order_flow_delta_quote(
    quote_volume: pd.Series,
    taker_buy_quote: pd.Series,
) -> pd.Series:
    """USDT quote Order Flow Delta (C2-5): 2 * Q_buy,t - Q_t. Returns NaN when Q_t <= 0."""
    q = quote_volume.to_numpy(dtype="float64", copy=False)
    qb = taker_buy_quote.to_numpy(dtype="float64", copy=False)
    out = np.full(len(q), np.nan, dtype="float64")
    valid = np.isfinite(q) & np.isfinite(qb) & (q > 0.0) & (qb >= 0.0)
    out[valid] = 2.0 * qb[valid] - q[valid]
    return pd.Series(out, index=quote_volume.index, name="of_delta_quote")


def wyckoff_absorption_bull(
    df: pd.DataFrame,
    ema50: pd.Series,
    ema200: pd.Series,
    atr14: pd.Series,
    rvol20: pd.Series,
    tbr: pd.Series,
    *,
    touch_tol_atr: float = 0.45,
    min_rvol: float = 2.0,
    min_lower_wick_ratio: float = 0.50,
    min_tbr: float = 0.52,
) -> pd.Series:
    """Institutional Wyckoff Absorption (Effort vs Result) at EMA50 or EMA200."""
    wicks = candle_wick_ratios(df)
    low = df["low"].astype("float64")
    close = df["close"].astype("float64")

    dist_50 = (low - ema50).abs()
    dist_200 = (low - ema200).abs()
    tol = float(touch_tol_atr) * atr14

    touched_50 = (dist_50 <= tol) & (close >= ema50)
    touched_200 = (dist_200 <= tol) & (close >= ema200)
    touched_zone = touched_50 | touched_200

    cond = (
        touched_zone
        & (rvol20 >= float(min_rvol))
        & (wicks["lower_wick_ratio"] >= float(min_lower_wick_ratio))
        & (tbr >= float(min_tbr))
    )
    return cond.fillna(False).astype("bool").rename("wyckoff_absorption_bull")
