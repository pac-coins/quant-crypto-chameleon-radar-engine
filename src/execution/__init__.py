"""Módulos de execução do QuantCrypto Chameleon Engine (Bloco 5)."""

from __future__ import annotations

from typing import Any

from src.execution.executor_protocol import (
    ExecutionReport,
    ExecutionRequest,
    ExecutorProtocol,
    ExitReason,
    NoOpExecutor,
    TradeStatus,
)
from src.execution.paper_executor import PaperExecutor

_LAZY_OCO_EXPORTS: frozenset[str] = frozenset(
    {
        "ActiveTradeContext",
        "OcoOrderManager",
        "compute_net_oco_quantity",
        "compute_oco_leg_prices",
        "extract_fill_summary",
    }
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_OCO_EXPORTS:
        from src.execution import oco_order_manager

        return getattr(oco_order_manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
