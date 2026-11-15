"""Módulos de execução do QuantCrypto Chameleon Engine (Bloco 5)."""

from src.execution.executor_protocol import (
    ExecutionReport,
    ExecutionRequest,
    ExecutorProtocol,
    ExitReason,
    NoOpExecutor,
    TradeStatus,
)
from src.execution.oco_order_manager import (
    ActiveTradeContext,
    OcoOrderManager,
    compute_net_oco_quantity,
    compute_oco_leg_prices,
    extract_fill_summary,
)
from src.execution.paper_executor import PaperExecutor

__all__ = [
    "ActiveTradeContext",
    "ExecutionReport",
    "ExecutionRequest",
    "ExecutorProtocol",
    "ExitReason",
    "NoOpExecutor",
    "OcoOrderManager",
    "PaperExecutor",
    "TradeStatus",
    "compute_net_oco_quantity",
    "compute_oco_leg_prices",
    "extract_fill_summary",
]
