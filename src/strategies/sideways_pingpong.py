"""Estratégia de reversão à média: sideways_pingpong (Clima SIDEWAYS, Fase 3)."""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from src.features.feature_store import FeatureRequest
from src.regimes.regime_detector import MarketRegime
from src.strategies.base_strategy import BaseStrategy, SignalCandidate
from src.strategies.bull_trend_pullback import _first_finite


class SidewaysPingPongStrategy(BaseStrategy):
    """
    Opera exclusivamente em MarketRegime.SIDEWAYS (desligada na Fase 1):
    - Entrada: Close <= Banda Inferior de Bollinger (20, 2.0) e RSI_14 < 32.0;
    - Alvo: SMA_20 no instante da entrada; Stop: Entry - 1.5 * ATR_14;
    - Trava Geométrica Obrigatória v6.0: bloqueia se (SMA_20 - Entry) / (1.5 * ATR_14) < 1.50.
    """

    def __init__(
        self,
        enabled: bool = False,
        rsi_oversold_entry: float = 32.0,
        stop_atr_mult: float = 1.5,
        min_initial_rr_ratio: float = 1.50,
        time_barrier_candles_1h: int = 12,
    ) -> None:
        self.enabled = enabled
        self.rsi_oversold_entry = rsi_oversold_entry
        self.stop_atr_mult = stop_atr_mult
        self.min_initial_rr_ratio = min_initial_rr_ratio
        self.time_barrier_candles_1h = time_barrier_candles_1h

    @property
    def name(self) -> str:
        return "sideways_pingpong"

    @property
    def required_regime(self) -> MarketRegime:
        return MarketRegime.SIDEWAYS

    def required_features(self) -> frozenset[FeatureRequest]:
        return frozenset(
            {
                FeatureRequest("bb", 20, "1h", (2.0,)),
                FeatureRequest("rsi", 14, "1h"),
                FeatureRequest("atr", 14, "1h"),
            }
        )

    def evaluate_row(
        self,
        symbol: str,
        row: pd.Series,
        regime: MarketRegime,
        require_order_flow_filter: bool = False,
    ) -> Optional[SignalCandidate]:
        if not self.enabled or regime != MarketRegime.SIDEWAYS:
            return None

        close_p = _first_finite(row, "close")
        sma_20 = _first_finite(row, "bb_20_2.0_mid", "bb_mid_20_1h")
        bb_lower = _first_finite(row, "bb_20_2.0_lower", "bb_lower_20_1h")
        rsi_14 = _first_finite(row, "rsi_14", "rsi_14_1h")
        atr_14 = _first_finite(row, "atr_14", "atr_14_1h")
        ct_val = _first_finite(row, "close_time_ms", "close_time")

        if not all(
            math.isfinite(x) for x in (close_p, sma_20, bb_lower, rsi_14, atr_14, ct_val)
        ):
            return None

        if close_p <= 0.0 or atr_14 <= 0.0:
            return None

        if not (close_p <= bb_lower and rsi_14 < self.rsi_oversold_entry):
            return None

        stop_dist = self.stop_atr_mult * atr_14
        target_dist = sma_20 - close_p
        if stop_dist <= 0.0 or target_dist <= 0.0:
            return None

        initial_rr = target_dist / stop_dist
        # TRAVA GEOMÉTRICA v6.0
        if initial_rr < self.min_initial_rr_ratio:
            return None

        of_delta = _first_finite(row, "of_delta_quote", "order_flow_delta", default=0.0)
        tbr = _first_finite(row, "tbr", "taker_buy_ratio", default=0.5)
        if require_order_flow_filter and not (of_delta > 0.0 and tbr > 0.50):
            return None

        rs_score = _first_finite(row, "rs_vol_adjusted_12h", default=0.0)

        return SignalCandidate(
            symbol=symbol,
            close_time_ms=int(ct_val),
            regime=regime,
            strategy_name=self.name,
            entry_price=close_p,
            stop_price=close_p - stop_dist,
            target_price=sma_20,
            atr_14=atr_14,
            stop_distance_pct=stop_dist / close_p,
            gross_rr_ratio=initial_rr,
            time_barrier_hours=self.time_barrier_candles_1h,
            rs_vol_adjusted_12h=rs_score,
            order_flow_delta=of_delta,
            taker_buy_ratio=tbr,
            has_liquidity_sweep=bool(
                row.get("is_liquidity_sweep", False) or row.get("liquidity_sweep_bull_20", False)
            ),
        )
