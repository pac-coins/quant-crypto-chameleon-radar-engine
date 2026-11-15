from __future__ import annotations

import numpy as np
import pandas as pd


def candle_wick_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Compute candle range, body, lower_wick_ratio, and upper_wick_ratio safely (H==L -> 0.0)."""
    o = df["open"].to_numpy(dtype="float64", copy=False)
    h = df["high"].to_numpy(dtype="float64", copy=False)
    low_p = df["low"].to_numpy(dtype="float64", copy=False)
    c = df["close"].to_numpy(dtype="float64", copy=False)

    rng = h - low_p
    body = np.abs(c - o)
    body_low = np.minimum(o, c)
    body_high = np.maximum(o, c)

    lower_wick = np.maximum(0.0, body_low - low_p)
    upper_wick = np.maximum(0.0, h - body_high)

    lower_ratio = np.zeros(len(df), dtype="float64")
    upper_ratio = np.zeros(len(df), dtype="float64")
    valid = np.isfinite(rng) & (rng > 0.0)
    lower_ratio[valid] = lower_wick[valid] / rng[valid]
    upper_ratio[valid] = upper_wick[valid] / rng[valid]

    return pd.DataFrame(
        {
            "range": pd.Series(rng, index=df.index, dtype="float64"),
            "body": pd.Series(body, index=df.index, dtype="float64"),
            "lower_wick_ratio": pd.Series(lower_ratio, index=df.index, dtype="float64"),
            "upper_wick_ratio": pd.Series(upper_ratio, index=df.index, dtype="float64"),
        },
        index=df.index,
    )


def bullish_pinbar(
    df: pd.DataFrame,
    atr: pd.Series,
    *,
    min_lower: float = 0.55,
    max_upper: float = 0.18,
    min_range_atr: float = 0.75,
) -> pd.Series:
    """Bullish Rejection Pinbar / Hammer pattern."""
    wicks = candle_wick_ratios(df)
    atr_f = atr.astype("float64")
    cond = (
        atr_f.notna()
        & (atr_f > 0.0)
        & (wicks["range"] >= float(min_range_atr) * atr_f)
        & (wicks["lower_wick_ratio"] >= float(min_lower))
        & (wicks["upper_wick_ratio"] <= float(max_upper))
    )
    return cond.fillna(False).astype("bool").rename("bullish_pinbar")


def bullish_engulfing(
    df: pd.DataFrame,
    atr: pd.Series,
    ema_fast: pd.Series,
    *,
    min_atr_mult: float = 1.10,
) -> pd.Series:
    """Institutional Bullish Momentum Engulfing pattern."""
    o = df["open"].astype("float64")
    h = df["high"].astype("float64")
    low_p = df["low"].astype("float64")
    c = df["close"].astype("float64")
    atr_f = atr.astype("float64")
    ema_f = ema_fast.astype("float64")

    prev_o = o.shift(1)
    prev_c = c.shift(1)
    prev_h = h.shift(1)

    candle_range = h - low_p
    cond = (
        atr_f.notna()
        & (atr_f > 0.0)
        & ema_f.notna()
        & (prev_c < prev_o)
        & (c > o)
        & (c > prev_o)
        & (o <= prev_c)
        & (c > prev_h)
        & (c > ema_f)
        & (candle_range >= float(min_atr_mult) * atr_f)
    )
    return cond.fillna(False).astype("bool").rename("bullish_engulfing")


def liquidity_sweep_bull(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """Bullish Liquidity Sweep (Stop Hunt / Spring): L_t < min(L_{t-lookback..t-1}) and C_t > min(L_{t-lookback..t-1})."""
    n = int(lookback)
    if n < 1:
        raise ValueError(f"lookback must be >= 1, got {n}")
    low_p = df["low"].astype("float64")
    close = df["close"].astype("float64")
    prior_min_low = low_p.shift(1).rolling(window=n, min_periods=n).min()

    cond = prior_min_low.notna() & (low_p < prior_min_low) & (close > prior_min_low)
    return cond.fillna(False).astype("bool").rename(f"liquidity_sweep_bull_{n}")
