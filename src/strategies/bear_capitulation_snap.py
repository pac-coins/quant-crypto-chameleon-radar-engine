"""Estratégia em quarentena: bear_capitulation_snap (Clima BEAR)."""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from src.regimes.regime_detector import MarketRegime
from src.strategies.base_strategy import BaseStrategy, SignalCandidate


class BearCapitulationSnapStrategy(BaseStrategy):
    """
    Permanentemente desligada por padrão (defesa 100% em USDT no regime BEAR).
    Usada apenas em estudos de estresse fora da amostra:
    - Exige RSI_2 < 5.0, RVOL_20 >= 3.0 e lower_wick_ratio >= 0.40.
    """

    def __init__(
        self,
        enabled: bool = False,
        rsi_panic_threshold: float = 5.0,
        relative_volume_min: float = 3.0,
        min_lower_wick_ratio: float = 0.40,
        stop_atr_mult: float = 1.8,
        target_atr_mult: float = 3.6,
        time_barrier_candles_1h: int = 8,
    ) -> None:
        self.enabled = enabled
        self.rsi_panic_threshold = rsi_panic_threshold
        self.relative_volume_min = relative_volume_min
        self.min_lower_wick_ratio = min_lower_wick_ratio
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.time_barrier_candles_1h = time_barrier_candles_1h

    @property
    def name(self) -> str:
        return "bear_capitulation_snap"

    @property
    def required_regime(self) -> MarketRegime:
        return MarketRegime.BEAR

    def evaluate_row(
        self,
        symbol: str,
        row: pd.Series,
        regime: MarketRegime,
        require_order_flow_filter: bool = False,
    ) -> Optional[SignalCandidate]:
        if not self.enabled or regime != MarketRegime.BEAR:
            return None

        required_cols = ("close", "rsi_2_1h", "rvol_20_1h", "lower_wick_ratio", "atr_14_1h")
        for col in required_cols:
            val = row.get(col, float("nan"))
            if val is None or not math.isfinite(float(val)):
                return None

        close_p = float(row["close"])
        rsi_2 = float(row["rsi_2_1h"])
        rvol_20 = float(row["rvol_20_1h"])
        lower_wick = float(row["lower_wick_ratio"])
        atr_14 = float(row["atr_14_1h"])

        if close_p <= 0.0 or atr_14 <= 0.0:
            return None

        if not (
            rsi_2 < self.rsi_panic_threshold
            and rvol_20 >= self.relative_volume_min
            and lower_wick >= self.min_lower_wick_ratio
        ):
            return None

        stop_dist = self.stop_atr_mult * atr_14
        target_dist = self.target_atr_mult * atr_14

        return SignalCandidate(
            symbol=symbol,
            close_time_ms=int(row["close_time"]),
            regime=regime,
            strategy_name=self.name,
            entry_price=close_p,
            stop_price=close_p - stop_dist,
            target_price=close_p + target_dist,
            atr_14=atr_14,
            stop_distance_pct=stop_dist / close_p,
            gross_rr_ratio=target_dist / stop_dist,
            time_barrier_hours=self.time_barrier_candles_1h,
            rs_vol_adjusted_12h=float(row.get("rs_vol_adjusted_12h", 0.0)),
            order_flow_delta=float(row.get("order_flow_delta", 0.0)),
            taker_buy_ratio=float(row.get("taker_buy_ratio", 0.5)),
            has_liquidity_sweep=bool(row.get("is_liquidity_sweep", False)),
        )
