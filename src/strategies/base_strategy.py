"""Contrato base de estratégias do Chameleon Engine (Bloco 3)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.regimes.regime_detector import MarketRegime


@dataclass(frozen=True)
class SignalCandidate:
    """Candidato a entrada gerado no fechamento confirmado de 1h."""

    symbol: str
    close_time_ms: int
    regime: MarketRegime
    strategy_name: str
    entry_price: float
    stop_price: float
    target_price: float
    atr_14: float
    stop_distance_pct: float
    gross_rr_ratio: float
    time_barrier_hours: int
    rs_vol_adjusted_12h: float
    order_flow_delta: float
    taker_buy_ratio: float
    has_liquidity_sweep: bool


class BaseStrategy(ABC):
    """Interface padrão (Entrada, Stop, Alvo, R:R Efetivo) segregada por regime."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nome identificador da estratégia."""

    @property
    @abstractmethod
    def required_regime(self) -> MarketRegime:
        """Regime MECE exigido para avaliar esta estratégia."""

    @abstractmethod
    def evaluate_row(
        self,
        symbol: str,
        row: pd.Series,
        regime: MarketRegime,
        require_order_flow_filter: bool = False,
    ) -> Optional[SignalCandidate]:
        """Avalia causalmente a última linha fechada (1h alinhada com 4h e BTC)."""
