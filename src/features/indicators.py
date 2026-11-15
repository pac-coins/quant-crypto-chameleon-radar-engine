from __future__ import annotations

import math

import numpy as np
import pandas as pd


def ema(close: pd.Series, period: int) -> pd.Series:
    """Canonical Exponential Moving Average seeded with SMA(period) at index period - 1 (TA-Lib compatible)."""
    n = int(period)
    if n < 1:
        raise ValueError(f"period must be >= 1, got {n}")
    arr = close.to_numpy(dtype="float64", copy=False)
    length = len(arr)
    out = np.full(length, np.nan, dtype="float64")
    if length < n:
        return pd.Series(out, index=close.index, name=f"ema_{n}")

    # Find the first window of length n with no NaNs to seed the EMA
    alpha = 2.0 / (n + 1.0)
    one_minus_alpha = 1.0 - alpha

    seed_end = -1
    for i in range(n - 1, length):
        window = arr[i - n + 1 : i + 1]
        if not np.isnan(window).any():
            out[i] = float(np.mean(window))
            seed_end = i
            break

    if seed_end == -1:
        return pd.Series(out, index=close.index, name=f"ema_{n}")

    prev = out[seed_end]
    for i in range(seed_end + 1, length):
        v = arr[i]
        if np.isnan(v):
            out[i] = np.nan
            prev = np.nan
        elif np.isnan(prev):
            out[i] = np.nan
        else:
            prev = alpha * v + one_minus_alpha * prev
            out[i] = prev

    return pd.Series(out, index=close.index, name=f"ema_{n}")


def true_range(df: pd.DataFrame) -> pd.Series:
    """Canonical True Range (TA-Lib TRANGE: index 0 is NaN, index t >= 1 uses C_{t-1})."""
    high = df["high"].to_numpy(dtype="float64", copy=False)
    low = df["low"].to_numpy(dtype="float64", copy=False)
    close = df["close"].to_numpy(dtype="float64", copy=False)
    length = len(df)
    out = np.full(length, np.nan, dtype="float64")
    if length <= 1:
        return pd.Series(out, index=df.index, name="tr")

    prev_close = close[:-1]
    h = high[1:]
    low_s = low[1:]
    tr1 = h - low_s
    tr2 = np.abs(h - prev_close)
    tr3 = np.abs(low_s - prev_close)
    out[1:] = np.maximum(tr1, np.maximum(tr2, tr3))
    return pd.Series(out, index=df.index, name="tr")


def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's RMA Average True Range (TA-Lib ATR: seeded at index `period` with mean(TR[1..period]))."""
    n = int(period)
    if n < 1:
        raise ValueError(f"period must be >= 1, got {n}")
    tr = true_range(df).to_numpy(dtype="float64", copy=False)
    length = len(tr)
    out = np.full(length, np.nan, dtype="float64")
    if length <= n:
        return pd.Series(out, index=df.index, name=f"atr_{n}")

    first_window = tr[1 : n + 1]
    if np.isnan(first_window).any():
        return pd.Series(out, index=df.index, name=f"atr_{n}")

    prev_atr = float(np.mean(first_window))
    out[n] = prev_atr
    for i in range(n + 1, length):
        val = tr[i]
        if np.isnan(val) or np.isnan(prev_atr):
            prev_atr = np.nan
            out[i] = np.nan
        else:
            prev_atr = ((n - 1) * prev_atr + val) / n
            out[i] = prev_atr

    return pd.Series(out, index=df.index, name=f"atr_{n}")


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index (TA-Lib RSI: seeded at index `period`)."""
    n = int(period)
    if n < 1:
        raise ValueError(f"period must be >= 1, got {n}")
    arr = close.to_numpy(dtype="float64", copy=False)
    length = len(arr)
    out = np.full(length, np.nan, dtype="float64")
    if length <= n:
        return pd.Series(out, index=close.index, name=f"rsi_{n}")

    diffs = np.diff(arr)
    gains = np.where(diffs > 0.0, diffs, 0.0)
    losses = np.where(diffs < 0.0, -diffs, 0.0)

    first_gains = gains[:n]
    first_losses = losses[:n]
    if np.isnan(first_gains).any() or np.isnan(first_losses).any():
        return pd.Series(out, index=close.index, name=f"rsi_{n}")

    avg_gain = float(np.mean(first_gains))
    avg_loss = float(np.mean(first_losses))

    denom = avg_gain + avg_loss
    out[n] = 0.0 if denom == 0.0 else 100.0 * (avg_gain / denom)

    for i in range(n + 1, length):
        g = gains[i - 1]
        loss_val = losses[i - 1]
        if np.isnan(g) or np.isnan(loss_val) or np.isnan(avg_gain) or np.isnan(avg_loss):
            avg_gain = np.nan
            avg_loss = np.nan
            out[i] = np.nan
        else:
            avg_gain = ((n - 1) * avg_gain + g) / n
            avg_loss = ((n - 1) * avg_loss + loss_val) / n
            denom = avg_gain + avg_loss
            out[i] = 0.0 if denom == 0.0 else 100.0 * (avg_gain / denom)

    return pd.Series(out, index=close.index, name=f"rsi_{n}")


def adx_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average Directional Index matching TA-Lib ADX recurrence to < 1e-10."""
    n = int(period)
    if n < 1:
        raise ValueError(f"period must be >= 1, got {n}")
    high = df["high"].to_numpy(dtype="float64", copy=False)
    low = df["low"].to_numpy(dtype="float64", copy=False)
    close = df["close"].to_numpy(dtype="float64", copy=False)
    length = len(df)
    out = np.full(length, np.nan, dtype="float64")
    if length < 2 * n:
        return pd.Series(out, index=df.index, name=f"adx_{n}")

    prev_plus_dm = 0.0
    prev_minus_dm = 0.0
    prev_tr = 0.0

    # Step 1: accumulate initial (n - 1) directional movements and TR for indices 1 .. n - 1 (TA-Lib convention)
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0.0) else 0.0
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        prev_plus_dm += plus_dm
        prev_minus_dm += minus_dm
        prev_tr += tr

    # Step 2: compute first `n` DX values across indices n .. 2*n - 1
    dx_sum = 0.0
    for i in range(n, 2 * n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0.0) else 0.0
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        prev_plus_dm = prev_plus_dm - (prev_plus_dm / n) + plus_dm
        prev_minus_dm = prev_minus_dm - (prev_minus_dm / n) + minus_dm
        prev_tr = prev_tr - (prev_tr / n) + tr

        if prev_tr > 0.0:
            plus_di = 100.0 * (prev_plus_dm / prev_tr)
            minus_di = 100.0 * (prev_minus_dm / prev_tr)
            di_sum = plus_di + minus_di
            dx = 100.0 * (abs(plus_di - minus_di) / di_sum) if di_sum > 0.0 else 0.0
        else:
            dx = 0.0
        dx_sum += dx

    prev_adx = dx_sum / n
    out[2 * n - 1] = prev_adx

    # Step 3: Wilder smoothing of ADX for indices >= 2*n
    for i in range(2 * n, length):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0.0) else 0.0
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        if np.isnan(up_move) or np.isnan(down_move) or np.isnan(tr) or np.isnan(prev_adx):
            prev_adx = np.nan
            out[i] = np.nan
            continue

        prev_plus_dm = prev_plus_dm - (prev_plus_dm / n) + plus_dm
        prev_minus_dm = prev_minus_dm - (prev_minus_dm / n) + minus_dm
        prev_tr = prev_tr - (prev_tr / n) + tr

        if prev_tr > 0.0:
            plus_di = 100.0 * (prev_plus_dm / prev_tr)
            minus_di = 100.0 * (prev_minus_dm / prev_tr)
            di_sum = plus_di + minus_di
            dx = 100.0 * (abs(plus_di - minus_di) / di_sum) if di_sum > 0.0 else 0.0
        else:
            dx = 0.0

        prev_adx = ((prev_adx * (n - 1)) + dx) / n
        out[i] = prev_adx

    return pd.Series(out, index=df.index, name=f"adx_{n}")


def chop_log10(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Dreiss (1993) Choppiness Index in base-10 logarithm with zero-range NaN guard (TODO Cat.1)."""
    n = int(period)
    if n < 2:
        raise ValueError(f"period must be >= 2, got {n}")
    tr_series = true_range(df)
    tr_sum = tr_series.rolling(window=n, min_periods=n).sum()
    high_max = df["high"].astype("float64").rolling(window=n, min_periods=n).max()
    low_min = df["low"].astype("float64").rolling(window=n, min_periods=n).min()

    hl_range = high_max - low_min
    denom_log = math.log10(float(n))

    tr_arr = tr_sum.to_numpy(dtype="float64", copy=False)
    rng_arr = hl_range.to_numpy(dtype="float64", copy=False)
    out = np.full(len(df), np.nan, dtype="float64")

    valid = (
        np.isfinite(tr_arr)
        & np.isfinite(rng_arr)
        & (rng_arr > 0.0)
        & (tr_arr > 0.0)
    )
    ratio = np.ones_like(tr_arr)
    ratio[valid] = tr_arr[valid] / rng_arr[valid]
    valid &= ratio > 0.0
    out[valid] = 100.0 * (np.log10(ratio[valid]) / denom_log)
    return pd.Series(out, index=df.index, name=f"chop_{n}")


def bollinger(
    close: pd.Series,
    period: int = 20,
    n_std: float = 2.0,
) -> pd.DataFrame:
    """Bollinger Bands using population standard deviation (ddof=0, TA-Lib standard)."""
    n = int(period)
    if n < 1:
        raise ValueError(f"period must be >= 1, got {n}")
    c = close.astype("float64")
    mid = c.rolling(window=n, min_periods=n).mean()
    std = c.rolling(window=n, min_periods=n).std(ddof=0)
    upper = mid + float(n_std) * std
    lower = mid - float(n_std) * std
    with np.errstate(divide="ignore", invalid="ignore"):
        bandwidth = np.where(mid > 0.0, (upper - lower) / mid, np.nan)
    return pd.DataFrame(
        {
            "mid": mid,
            "upper": upper,
            "lower": lower,
            "bandwidth": pd.Series(bandwidth, index=close.index, dtype="float64"),
        },
        index=close.index,
    )


def rvol_median(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Relative Volume vs rolling median with zero-median NaN guard (C2-6: never returns inf)."""
    n = int(lookback)
    if n < 1:
        raise ValueError(f"lookback must be >= 1, got {n}")
    v = volume.astype("float64")
    med = v.rolling(window=n, min_periods=n).median()
    v_arr = v.to_numpy(dtype="float64", copy=False)
    m_arr = med.to_numpy(dtype="float64", copy=False)
    out = np.full(len(v_arr), np.nan, dtype="float64")
    valid = np.isfinite(v_arr) & np.isfinite(m_arr) & (m_arr > 0.0) & (v_arr >= 0.0)
    out[valid] = v_arr[valid] / m_arr[valid]
    return pd.Series(out, index=volume.index, name=f"rvol_{n}")
