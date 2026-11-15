"""Protocolo de execução, PaperExecutor (SHADOW_CHAMELEON) e OcoOrderManager (LIVE_CHAMELEON_PHASE_1) (Bloco 5)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Optional, Protocol

from src.core.database import SymbolFilters
from src.risk.position_sizer import SizingDecision


class TradeStatus(StrEnum):
    """Estados canônicos da tabela trades (Contrato de Dados SQLite v1)."""

    PENDING_ENTRY = "PENDING_ENTRY"
    ENTRY_FILLED = "ENTRY_FILLED"
    PROTECTED = "PROTECTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    HALT_REVIEW = "HALT_REVIEW"


class ExitReason(StrEnum):
    """Motivos de encerramento da tabela trades."""

    TARGET = "TARGET"
    STOP = "STOP"
    TIME = "TIME"
    FALLBACK_MARKET_2010 = "FALLBACK_MARKET_2010"
    STOP_WATCHDOG = "STOP_WATCHDOG"
    RECONCILER = "RECONCILER"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Pedido de entrada validado pelo PositionSizer e pronto para envio (Shadow ou Live)."""

    symbol: str
    strategy: str
    regime: str
    signal_close_time_ms: int
    atr_at_signal: float
    time_barrier_end_ms: int
    sizing: SizingDecision
    symbol_filters: SymbolFilters
    radar_signal_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Resultado auditável da tentativa de abertura/proteção de posição."""

    executed: bool
    mode: str
    trade_id: Optional[int]
    client_order_id: str
    status: str
    filled_qty: Decimal
    avg_entry_price: Decimal
    oco_order_list_id: Optional[int] = None
    exit_reason: Optional[str] = None
    error_msg: Optional[str] = None


class ExecutorProtocol(Protocol):
    """Interface unificada de execução para os 3 RunModes."""

    def execute_entry(self, req: ExecutionRequest) -> ExecutionReport: ...


class NoOpExecutor:
    """Executor inerte para o modo ANALYST_ONLY (zero ordens reais ou shadow)."""

    def execute_entry(self, req: ExecutionRequest) -> ExecutionReport:
        return ExecutionReport(
            executed=False,
            mode="ANALYST_ONLY",
            trade_id=None,
            client_order_id=f"NOOP_{req.symbol}_{req.signal_close_time_ms}",
            status="SKIPPED_ANALYST_ONLY",
            filled_qty=Decimal("0"),
            avg_entry_price=Decimal("0"),
        )
