"""Módulo de auditoria virtual e resolução determinística de desfechos intra-vela (Blocos 5 e 6)."""

from src.auditing.outcome_resolver import (
    DualTargetResolution,
    SingleTargetResolution,
    resolve_dual_target_horizon,
    resolve_single_target_horizon,
)

__all__ = [
    "DualTargetResolution",
    "SingleTargetResolution",
    "resolve_dual_target_horizon",
    "resolve_single_target_horizon",
]
