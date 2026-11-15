"""Resolvedor determinístico de desfecho intra-vela compartilhado entre PaperExecutor e VirtualAuditor (C2-3, C2-7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from src.core.database import CandleRow

SingleExitStatus = Literal["OPEN", "TARGET", "STOP", "SAME_CANDLE_STOP", "TIME", "INVALID_GAP"]
DualExitStatus = Literal[
    "OPEN",
    "T1_THEN_T2",
    "T1_THEN_STOP",
    "T1_THEN_TIME",
    "T2",
    "STOP",
    "TIME",
    "INVALID_GAP",
]


@dataclass(frozen=True, slots=True)
class SingleTargetResolution:
    """Resultado da resolução de horizonte para uma posição de alvo único (PaperExecutor / Shadow)."""

    status: SingleExitStatus
    resolved_at_ms: Optional[int]
    exit_price: Optional[float]
    r_gross: Optional[float]
    r_net: Optional[float]
    mfe_r: float
    mae_r: float
    same_candle_ambiguity: bool = False


@dataclass(frozen=True, slots=True)
class DualTargetResolution:
    """Resultado da resolução de horizonte para sinais do Radar com 2 alvos (T1 parcial 50% + T2 50%)."""

    status: DualExitStatus
    resolved_at_ms: Optional[int]
    r_gross: Optional[float]
    r_net: Optional[float]
    mfe_r: float
    mae_r: float
    same_candle_ambiguity: bool = False


def _has_inter_candle_gap(candles: Sequence[CandleRow], expected_delta_ms: int = 3_600_000) -> bool:
    """Detecta lacunas entre velas consecutivas (ex.: manutenção programada da Binance — C2-7)."""
    if len(candles) < 2:
        return False
    for prev, cur in zip(candles[:-1], candles[1:]):
        if int(cur.open_time_ms) - int(prev.close_time_ms) > expected_delta_ms // 2:
            return True
    return False


def resolve_single_target_horizon(
    *,
    signal_close_time_ms: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    time_barrier_end_ms: int,
    candles: Sequence[CandleRow],
    roundtrip_cost_pct: float = 0.0021,
    expected_interval_ms: int = 3_600_000,
    invalidate_on_gap: bool = False,
) -> SingleTargetResolution:
    """
    Resolve o desfecho de uma operação Long a partir das velas estritamente posteriores ao sinal
    (open_time_ms >= signal_close_time_ms):
    - Pior caso intra-vela (C2-3): se low <= stop_price e high >= target_price na mesma vela,
      prevalece o stop ('SAME_CANDLE_STOP', same_candle_ambiguity=True).
    - Barreira de tempo por timestamp (C2-7): quando close_time_ms >= time_barrier_end_ms sem ter
      tocado stop nem target antes, encerra no close dessa vela ('TIME').
    """
    future_bars = sorted(
        [c for c in candles if int(c.open_time_ms) >= int(signal_close_time_ms)],
        key=lambda x: int(x.close_time_ms),
    )
    risk_unit = max(float(entry_price) - float(stop_price), 1e-9)
    cost_in_r = (float(roundtrip_cost_pct) * float(entry_price)) / risk_unit

    if not future_bars:
        return SingleTargetResolution(
            status="OPEN",
            resolved_at_ms=None,
            exit_price=None,
            r_gross=None,
            r_net=None,
            mfe_r=0.0,
            mae_r=0.0,
            same_candle_ambiguity=False,
        )

    if invalidate_on_gap and _has_inter_candle_gap(future_bars, expected_interval_ms):
        return SingleTargetResolution(
            status="INVALID_GAP",
            resolved_at_ms=int(future_bars[-1].close_time_ms),
            exit_price=None,
            r_gross=None,
            r_net=None,
            mfe_r=0.0,
            mae_r=0.0,
            same_candle_ambiguity=False,
        )

    mfe_r = 0.0
    mae_r = 0.0

    for bar in future_bars:
        bar_mfe = (float(bar.high) - float(entry_price)) / risk_unit
        bar_mae = (float(bar.low) - float(entry_price)) / risk_unit
        mfe_r = max(mfe_r, bar_mfe)
        mae_r = min(mae_r, bar_mae)

        hit_stop = float(bar.low) <= float(stop_price)
        hit_target = float(bar.high) >= float(target_price)

        if hit_stop and hit_target:
            r_gross = -1.0
            return SingleTargetResolution(
                status="SAME_CANDLE_STOP",
                resolved_at_ms=int(bar.close_time_ms),
                exit_price=float(stop_price),
                r_gross=r_gross,
                r_net=r_gross - cost_in_r,
                mfe_r=mfe_r,
                mae_r=mae_r,
                same_candle_ambiguity=True,
            )

        if hit_stop:
            r_gross = -1.0
            return SingleTargetResolution(
                status="STOP",
                resolved_at_ms=int(bar.close_time_ms),
                exit_price=float(stop_price),
                r_gross=r_gross,
                r_net=r_gross - cost_in_r,
                mfe_r=mfe_r,
                mae_r=mae_r,
                same_candle_ambiguity=False,
            )

        if hit_target:
            r_gross = (float(target_price) - float(entry_price)) / risk_unit
            return SingleTargetResolution(
                status="TARGET",
                resolved_at_ms=int(bar.close_time_ms),
                exit_price=float(target_price),
                r_gross=r_gross,
                r_net=r_gross - cost_in_r,
                mfe_r=mfe_r,
                mae_r=mae_r,
                same_candle_ambiguity=False,
            )

        if int(bar.close_time_ms) >= int(time_barrier_end_ms):
            exit_p = float(bar.close)
            r_gross = (exit_p - float(entry_price)) / risk_unit
            return SingleTargetResolution(
                status="TIME",
                resolved_at_ms=int(bar.close_time_ms),
                exit_price=exit_p,
                r_gross=r_gross,
                r_net=r_gross - cost_in_r,
                mfe_r=mfe_r,
                mae_r=mae_r,
                same_candle_ambiguity=False,
            )

    return SingleTargetResolution(
        status="OPEN",
        resolved_at_ms=None,
        exit_price=None,
        r_gross=None,
        r_net=None,
        mfe_r=mfe_r,
        mae_r=mae_r,
        same_candle_ambiguity=False,
    )


def resolve_dual_target_horizon(
    *,
    signal_close_time_ms: int,
    entry_price: float,
    stop_price: float,
    target1_price: float,
    target2_price: float,
    horizon_end_ms: int,
    candles: Sequence[CandleRow],
    roundtrip_cost_pct: float = 0.0021,
    expected_interval_ms: int = 3_600_000,
) -> DualTargetResolution:
    """
    Resolve sinais virtuais de 2 alvos (T1 realiza 50%, T2 realiza 50%) para o VirtualAuditor:
    - Pior caso intra-vela: se low <= stop_price na mesma vela de T1/T2 antes de T1 ser atingido,
      conta como STOP.
    - Se houver gap de velas antes do desfecho, retorna 'INVALID_GAP' (C2-7).
    """
    future_bars = sorted(
        [c for c in candles if int(c.open_time_ms) >= int(signal_close_time_ms)],
        key=lambda x: int(x.close_time_ms),
    )
    risk_unit = max(float(entry_price) - float(stop_price), 1e-9)
    cost_in_r = (float(roundtrip_cost_pct) * float(entry_price)) / risk_unit

    if not future_bars:
        return DualTargetResolution(
            status="OPEN",
            resolved_at_ms=None,
            r_gross=None,
            r_net=None,
            mfe_r=0.0,
            mae_r=0.0,
            same_candle_ambiguity=False,
        )

    if _has_inter_candle_gap(future_bars, expected_interval_ms):
        return DualTargetResolution(
            status="INVALID_GAP",
            resolved_at_ms=int(future_bars[-1].close_time_ms),
            r_gross=None,
            r_net=None,
            mfe_r=0.0,
            mae_r=0.0,
            same_candle_ambiguity=False,
        )

    t1_hit = False
    ambiguity = False
    mfe_r = 0.0
    mae_r = 0.0
    r_t1 = (float(target1_price) - float(entry_price)) / risk_unit
    r_t2 = (float(target2_price) - float(entry_price)) / risk_unit

    for bar in future_bars:
        bar_mfe = (float(bar.high) - float(entry_price)) / risk_unit
        bar_mae = (float(bar.low) - float(entry_price)) / risk_unit
        mfe_r = max(mfe_r, bar_mfe)
        mae_r = min(mae_r, bar_mae)

        hit_stop = float(bar.low) <= float(stop_price)
        hit_t1 = float(bar.high) >= float(target1_price)
        hit_t2 = float(bar.high) >= float(target2_price)

        if not t1_hit:
            if hit_stop and (hit_t1 or hit_t2):
                return DualTargetResolution(
                    status="STOP",
                    resolved_at_ms=int(bar.close_time_ms),
                    r_gross=-1.0,
                    r_net=-1.0 - cost_in_r,
                    mfe_r=mfe_r,
                    mae_r=mae_r,
                    same_candle_ambiguity=True,
                )
            if hit_stop:
                return DualTargetResolution(
                    status="STOP",
                    resolved_at_ms=int(bar.close_time_ms),
                    r_gross=-1.0,
                    r_net=-1.0 - cost_in_r,
                    mfe_r=mfe_r,
                    mae_r=mae_r,
                    same_candle_ambiguity=False,
                )
            if hit_t2:
                gross = 0.5 * r_t1 + 0.5 * r_t2
                return DualTargetResolution(
                    status="T2",
                    resolved_at_ms=int(bar.close_time_ms),
                    r_gross=gross,
                    r_net=gross - cost_in_r,
                    mfe_r=mfe_r,
                    mae_r=mae_r,
                    same_candle_ambiguity=False,
                )
            if hit_t1:
                t1_hit = True
        else:
            if hit_stop and hit_t2:
                ambiguity = True
                gross = 0.5 * r_t1 + 0.5 * (-1.0)
                return DualTargetResolution(
                    status="T1_THEN_STOP",
                    resolved_at_ms=int(bar.close_time_ms),
                    r_gross=gross,
                    r_net=gross - cost_in_r,
                    mfe_r=mfe_r,
                    mae_r=mae_r,
                    same_candle_ambiguity=ambiguity,
                )
            if hit_stop:
                gross = 0.5 * r_t1 + 0.5 * (-1.0)
                return DualTargetResolution(
                    status="T1_THEN_STOP",
                    resolved_at_ms=int(bar.close_time_ms),
                    r_gross=gross,
                    r_net=gross - cost_in_r,
                    mfe_r=mfe_r,
                    mae_r=mae_r,
                    same_candle_ambiguity=False,
                )
            if hit_t2:
                gross = 0.5 * r_t1 + 0.5 * r_t2
                return DualTargetResolution(
                    status="T1_THEN_T2",
                    resolved_at_ms=int(bar.close_time_ms),
                    r_gross=gross,
                    r_net=gross - cost_in_r,
                    mfe_r=mfe_r,
                    mae_r=mae_r,
                    same_candle_ambiguity=False,
                )

        if int(bar.close_time_ms) >= int(horizon_end_ms):
            r_close = (float(bar.close) - float(entry_price)) / risk_unit
            if t1_hit:
                gross = 0.5 * r_t1 + 0.5 * r_close
                st: DualExitStatus = "T1_THEN_TIME"
            else:
                gross = r_close
                st = "TIME"
            return DualTargetResolution(
                status=st,
                resolved_at_ms=int(bar.close_time_ms),
                r_gross=gross,
                r_net=gross - cost_in_r,
                mfe_r=mfe_r,
                mae_r=mae_r,
                same_candle_ambiguity=ambiguity,
            )

    return DualTargetResolution(
        status="OPEN",
        resolved_at_ms=None,
        r_gross=None,
        r_net=None,
        mfe_r=mfe_r,
        mae_r=mae_r,
        same_candle_ambiguity=ambiguity,
    )
