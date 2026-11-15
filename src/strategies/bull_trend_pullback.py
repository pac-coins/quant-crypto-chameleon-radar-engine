"""Estratégia canônica da Fase 1: bull_trend_pullback (Clima BULL)."""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from src.features.feature_store import FeatureRequest
from src.regimes.regime_detector import MarketRegime
from src.strategies.base_strategy import BaseStrategy, SignalCandidate


def _first_finite(row: pd.Series, *candidates: str, default: float = float("nan")) -> float:
    for col in candidates:
        if col in row:
            val = row[col]
            if val is not None and isinstance(val, (int, float)) and math.isfinite(float(val)):
                return float(val)
    return default


class BullTrendPullbackStrategy(BaseStrategy):
    """
    Opera exclusivamente em MarketRegime.BULL:
    - Estrutura 1h: EMA_20 > EMA_100 e Close > EMA_100;
    - Pullback na EMA_20: Low <= EMA_20 * (1 + pullback_tolerance_pct) e Close >= EMA_20;
    - Confirmação de rejeição compradora (Close > Open ou Pinbar/Engolfo/Liquidity Sweep);
    - Filtro opcional de ablação (Degrau C): order_flow_delta > 0 e taker_buy_ratio > 0.50;
    - Stop em 2.0 * ATR_14 (-1.0R) e Alvo em 5.0 * ATR_14 (+2.5R), barreira de 24h.
    """

    def __init__(
        self,
        enabled: bool = True,
        ema_fast_1h: int = 20,
        ema_slow_1h: int = 100,
        stop_atr_mult: float = 2.0,
        target_atr_mult: float = 5.0,
        time_barrier_candles_1h: int = 24,
        pullback_tolerance_pct: float = 0.003,
    ) -> None:
        self.enabled = enabled
        self.ema_fast_1h = ema_fast_1h
        self.ema_slow_1h = ema_slow_1h
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.time_barrier_candles_1h = time_barrier_candles_1h
        self.pullback_tolerance_pct = pullback_tolerance_pct

    @property
    def name(self) -> str:
        return "bull_trend_pullback"

    @property
    def required_regime(self) -> MarketRegime:
        return MarketRegime.BULL

    def required_features(self) -> frozenset[FeatureRequest]:
        return frozenset(
            {
                FeatureRequest("ema", self.ema_fast_1h, "1h"),
                FeatureRequest("ema", self.ema_slow_1h, "1h"),
                FeatureRequest("atr", 14, "1h"),
                FeatureRequest("tbr", 1, "1h"),
                FeatureRequest("of_delta", 1, "1h"),
                FeatureRequest("sweep", 20, "1h"),
            }
        )

    def evaluate_row(
        self,
        symbol: str,
        row: pd.Series,
        regime: MarketRegime,
        require_order_flow_filter: bool = False,
    ) -> Optional[SignalCandidate]:
        if not self.enabled or regime != MarketRegime.BULL:
            return None

        open_p = _first_finite(row, "open")
        low_p = _first_finite(row, "low")
        close_p = _first_finite(row, "close")
        ema_fast = _first_finite(row, f"ema_{self.ema_fast_1h}", f"ema_{self.ema_fast_1h}_1h")
        ema_slow = _first_finite(row, f"ema_{self.ema_slow_1h}", f"ema_{self.ema_slow_1h}_1h")
        atr_14 = _first_finite(row, "atr_14", "atr_14_1h")
        ct_val = _first_finite(row, "close_time_ms", "close_time")

        if not all(
            math.isfinite(x) for x in (open_p, low_p, close_p, ema_fast, ema_slow, atr_14, ct_val)
        ):
            return None

        if close_p <= 0.0 or atr_14 <= 0.0:
            return None

        # 1. Alinhamento de tendência 1h
        if not (ema_fast > ema_slow and close_p > ema_slow):
            return None

        # 2. Pullback na EMA rápida com recuperação acima dela
        touched_ema_fast = low_p <= ema_fast * (1.0 + self.pullback_tolerance_pct)
        recovered_above_ema = close_p >= ema_fast
        if not (touched_ema_fast and recovered_above_ema):
            return None

        # 3. Confirmação de rejeição compradora
        has_sweep = bool(
            row.get("is_liquidity_sweep", False) or row.get("liquidity_sweep_bull_20", False)
        )
        has_pa_trigger = bool(
            row.get("is_bullish_pinbar", False)
            or row.get("is_bullish_engulfing", False)
            or has_sweep
        )
        if not (close_p > open_p or has_pa_trigger):
            return None

        of_delta = _first_finite(row, "of_delta_quote", "order_flow_delta", default=0.0)
        tbr = _first_finite(row, "tbr", "taker_buy_ratio", default=0.5)

        # 4. Ablação do filtro de Order Flow no Degrau C
        if require_order_flow_filter and not (of_delta > 0.0 and tbr > 0.50):
            return None

        stop_dist = self.stop_atr_mult * atr_14
        target_dist = self.target_atr_mult * atr_14
        stop_price = close_p - stop_dist
        target_price = close_p + target_dist
        if stop_price <= 0.0:
            return None

        rs_score = _first_finite(row, "rs_vol_adjusted_12h", default=0.0)

        return SignalCandidate(
            symbol=symbol,
            close_time_ms=int(ct_val),
            regime=regime,
            strategy_name=self.name,
            entry_price=close_p,
            stop_price=stop_price,
            target_price=target_price,
            atr_14=atr_14,
            stop_distance_pct=stop_dist / close_p,
            gross_rr_ratio=target_dist / stop_dist,
            time_barrier_hours=self.time_barrier_candles_1h,
            rs_vol_adjusted_12h=rs_score,
            order_flow_delta=of_delta,
            taker_buy_ratio=tbr,
            has_liquidity_sweep=has_sweep,
        )
