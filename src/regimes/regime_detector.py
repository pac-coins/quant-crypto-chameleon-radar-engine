from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

import pandas as pd

from src.core.config import RegimeThresholds4hCfg
from src.features.feature_store import FeatureRequest
from src.features.indicators import adx_wilder, chop_log10, ema

MIN_WARMUP_CANDLES_4H: Final[int] = 120

DEFAULT_THRESHOLDS_4H: Final[RegimeThresholds4hCfg] = RegimeThresholds4hCfg(
    ema_macro_period=100,
    chop_period=14,
    chop_bull_max=50.0,
    chop_sideways_min=55.0,
    adx_period=14,
    adx_sideways_max=22.0,
)


class MarketRegime(StrEnum):
    BEAR = "BEAR"
    BULL = "BULL"
    SIDEWAYS = "SIDEWAYS"
    TRANSITION = "TRANSITION"


FallbackReason = Literal["WARMUP_LT_120", "NAN"] | None


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    regime: MarketRegime
    close_4h: float | None
    ema100_4h: float | None
    ema100_slope_4h: float | None
    chop14_4h: float | None
    adx14_4h: float | None
    fallback_reason: FallbackReason = None


def _is_valid_finite(x: float | None) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))


def classify_regime_values(
    *,
    close_4h: float | None,
    ema100_4h: float | None,
    ema100_slope_4h: float | None,
    chop14_4h: float | None,
    adx14_4h: float | None,
    thresholds: RegimeThresholds4hCfg | None = None,
    has_min_history: bool = True,
) -> RegimeSnapshot:
    """Strictly MECE 4-state market regime classification (BEAR -> BULL -> SIDEWAYS -> TRANSITION)."""
    th = thresholds if thresholds is not None else DEFAULT_THRESHOLDS_4H

    if not has_min_history:
        return RegimeSnapshot(
            regime=MarketRegime.TRANSITION,
            close_4h=close_4h if _is_valid_finite(close_4h) else None,
            ema100_4h=ema100_4h if _is_valid_finite(ema100_4h) else None,
            ema100_slope_4h=ema100_slope_4h if _is_valid_finite(ema100_slope_4h) else None,
            chop14_4h=chop14_4h if _is_valid_finite(chop14_4h) else None,
            adx14_4h=adx14_4h if _is_valid_finite(adx14_4h) else None,
            fallback_reason="WARMUP_LT_120",
        )

    inputs = (close_4h, ema100_4h, ema100_slope_4h, chop14_4h, adx14_4h)
    if not all(_is_valid_finite(v) for v in inputs):
        return RegimeSnapshot(
            regime=MarketRegime.TRANSITION,
            close_4h=float(close_4h) if _is_valid_finite(close_4h) else None,
            ema100_4h=float(ema100_4h) if _is_valid_finite(ema100_4h) else None,
            ema100_slope_4h=float(ema100_slope_4h) if _is_valid_finite(ema100_slope_4h) else None,
            chop14_4h=float(chop14_4h) if _is_valid_finite(chop14_4h) else None,
            adx14_4h=float(adx14_4h) if _is_valid_finite(adx14_4h) else None,
            fallback_reason="NAN",
        )

    c = float(close_4h)        # type: ignore[arg-type]
    e = float(ema100_4h)       # type: ignore[arg-type]
    slope = float(ema100_slope_4h)  # type: ignore[arg-type]
    chop = float(chop14_4h)    # type: ignore[arg-type]
    adx = float(adx14_4h)      # type: ignore[arg-type]

    # 1st priority: BEAR (Capital defense)
    if c < e and slope < 0.0:
        regime = MarketRegime.BEAR
    # 2nd priority: BULL (Clean trend)
    elif c > e and slope > 0.0 and chop < float(th.chop_bull_max):
        regime = MarketRegime.BULL
    # 3rd priority: SIDEWAYS (Not BEAR, high chop, low ADX)
    elif chop >= float(th.chop_sideways_min) and adx < float(th.adx_sideways_max):
        regime = MarketRegime.SIDEWAYS
    # 4th priority: TRANSITION (All remaining intermediate states)
    else:
        regime = MarketRegime.TRANSITION

    return RegimeSnapshot(
        regime=regime,
        close_4h=c,
        ema100_4h=e,
        ema100_slope_4h=slope,
        chop14_4h=chop,
        adx14_4h=adx,
        fallback_reason=None,
    )


class RegimeDetector:
    def __init__(
        self,
        thresholds: RegimeThresholds4hCfg | None = None,
        *,
        min_warmup_candles: int = MIN_WARMUP_CANDLES_4H,
    ) -> None:
        self.thresholds = thresholds if thresholds is not None else DEFAULT_THRESHOLDS_4H
        self.min_warmup_candles = int(min_warmup_candles)

    def required_features(self) -> frozenset[FeatureRequest]:
        return frozenset(
            {
                FeatureRequest("ema", self.thresholds.ema_macro_period, "4h"),
                FeatureRequest("chop", self.thresholds.chop_period, "4h"),
                FeatureRequest("adx", self.thresholds.adx_period, "4h"),
            }
        )

    def _ensure_4h_indicators(self, df_4h: pd.DataFrame) -> pd.DataFrame:
        out = df_4h.copy()
        ema_col = f"ema_{self.thresholds.ema_macro_period}"
        chop_col = f"chop_{self.thresholds.chop_period}"
        adx_col = f"adx_{self.thresholds.adx_period}"

        if ema_col not in out.columns:
            out[ema_col] = ema(out["close"], self.thresholds.ema_macro_period)
        if chop_col not in out.columns:
            out[chop_col] = chop_log10(out, self.thresholds.chop_period)
        if adx_col not in out.columns:
            out[adx_col] = adx_wilder(out, self.thresholds.adx_period)
        out[f"{ema_col}_slope"] = out[ema_col].diff()
        return out

    def detect_on_4h_frame(self, df_4h: pd.DataFrame) -> RegimeSnapshot:
        if len(df_4h) < self.min_warmup_candles:
            if df_4h.empty:
                return classify_regime_values(
                    close_4h=None,
                    ema100_4h=None,
                    ema100_slope_4h=None,
                    chop14_4h=None,
                    adx14_4h=None,
                    thresholds=self.thresholds,
                    has_min_history=False,
                )
            enriched = self._ensure_4h_indicators(df_4h)
            last = enriched.iloc[-1]
            ema_col = f"ema_{self.thresholds.ema_macro_period}"
            chop_col = f"chop_{self.thresholds.chop_period}"
            adx_col = f"adx_{self.thresholds.adx_period}"
            return classify_regime_values(
                close_4h=float(last["close"]),
                ema100_4h=float(last[ema_col]),
                ema100_slope_4h=float(last[f"{ema_col}_slope"]),
                chop14_4h=float(last[chop_col]),
                adx14_4h=float(last[adx_col]),
                thresholds=self.thresholds,
                has_min_history=False,
            )

        enriched = self._ensure_4h_indicators(df_4h)
        last = enriched.iloc[-1]
        ema_col = f"ema_{self.thresholds.ema_macro_period}"
        chop_col = f"chop_{self.thresholds.chop_period}"
        adx_col = f"adx_{self.thresholds.adx_period}"
        return classify_regime_values(
            close_4h=float(last["close"]),
            ema100_4h=float(last[ema_col]),
            ema100_slope_4h=float(last[f"{ema_col}_slope"]),
            chop14_4h=float(last[chop_col]),
            adx14_4h=float(last[adx_col]),
            thresholds=self.thresholds,
            has_min_history=True,
        )

    def detect_series(self, df_4h: pd.DataFrame) -> pd.Series:
        if df_4h.empty:
            return pd.Series(dtype="object", name="regime")
        enriched = self._ensure_4h_indicators(df_4h)
        ema_col = f"ema_{self.thresholds.ema_macro_period}"
        chop_col = f"chop_{self.thresholds.chop_period}"
        adx_col = f"adx_{self.thresholds.adx_period}"
        slope_col = f"{ema_col}_slope"

        regimes: list[str] = []
        for idx_pos, (_, row) in enumerate(enriched.iterrows()):
            has_hist = (idx_pos + 1) >= self.min_warmup_candles
            snap = classify_regime_values(
                close_4h=float(row["close"]),
                ema100_4h=float(row[ema_col]),
                ema100_slope_4h=float(row[slope_col]),
                chop14_4h=float(row[chop_col]),
                adx14_4h=float(row[adx_col]),
                thresholds=self.thresholds,
                has_min_history=has_hist,
            )
            regimes.append(snap.regime.value)
        return pd.Series(regimes, index=df_4h.index, name="regime")
