"""Dimensionamento Fracionário Fixo com Teto Duplo, Trava de Expectância e Precisão Decimal (Bloco 4)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Optional

from src.core.binance_client import (
    floor_qty_to_step,
    passes_filters,
    round_price_to_tick,
    to_decimal,
)
from src.core.config import PortfolioCfg, PositionSizingCfg
from src.core.database import SymbolFilters


class SignalGrade(StrEnum):
    """Notas de convicção do sinal (Fase 1 opera sempre como B; Fase 2 usa C / B / A+)."""

    A_PLUS = "A+"
    B = "B"
    C = "C"


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """Decisão auditável de dimensionamento de lote em Decimal."""

    approved: bool
    symbol: str
    grade: SignalGrade
    effective_risk_pct: Decimal
    indifference_prob: float
    net_expected_r: Optional[float]
    usable_cash_usdt: Decimal
    quote_order_qty: Decimal
    base_qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    cap_binding: str  # "RISK_CAP" | "CASH_CAP" | "ORDER_CAP" | "REJECTED"
    reject_reason: Optional[str] = None
    ml_grade_silent: Optional[str] = None
    ml_p_cal_silent: Optional[float] = None


def _dec_from_float_cfg(val: float) -> Decimal:
    return Decimal(str(val))


class PositionSizer:
    """
    Implementa o Dimensionamento Fracionário Fixo com Teto Duplo (v6.0):
    1. Desconta pending_hodl_bucket_usdt do caixa livre operável;
    2. Calcula o limiar de indiferença p_underline = (1 + C_R) / (R_eff + 1);
    3. Aplica o Teto Duplo (Risco máximo no Stop + Fricção vs. Alocação máxima de caixa)
       combinado ao disjuntor proporcional min(500 USDT, 55% da banca);
    4. Arredonda preços e quantidades via Decimal (tickSize / stepSize) e aborta
       estritamente se o lote ficar abaixo de 1.05 * minNotional ou minQty.
    """

    def __init__(
        self,
        sizing_cfg: PositionSizingCfg,
        portfolio_cfg: PortfolioCfg,
        use_ml_filter: bool = False,
    ) -> None:
        self.sizing_cfg = sizing_cfg
        self.portfolio_cfg = portfolio_cfg
        self.use_ml_filter = bool(use_ml_filter)

    def compute_indifference_threshold(
        self,
        stop_distance_pct: float,
        gross_rr_ratio: float,
    ) -> tuple[float, float]:
        """
        Retorna (custo_em_r, p_indiferenca) onde:
          C_R = (f_roundtrip + s_stress) / d_stop
          p_indiferenca = (1 + C_R) / (R_eff + 1)
        """
        d_stop = max(float(stop_distance_pct), 1e-9)
        r_eff = max(float(gross_rr_ratio), 1e-9)
        total_friction_pct = (
            self.sizing_cfg.roundtrip_fee_assumption_pct
            + self.sizing_cfg.stress_slippage_assumption_pct
        )
        cost_in_r = total_friction_pct / d_stop
        p_indiff = (1.0 + cost_in_r) / (r_eff + 1.0)
        return cost_in_r, p_indiff

    def compute_net_expected_r(
        self,
        calibrated_prob: float,
        stop_distance_pct: float,
        gross_rr_ratio: float,
    ) -> float:
        """
        E[R_i] = P_cal * R_eff - (1 - P_cal) - C_R
        """
        cost_in_r, _ = self.compute_indifference_threshold(stop_distance_pct, gross_rr_ratio)
        p = float(calibrated_prob)
        return p * float(gross_rr_ratio) - (1.0 - p) - cost_in_r

    def evaluate_and_size(
        self,
        *,
        symbol: str,
        entry_price: Decimal | str,
        stop_price: Decimal | str,
        target_price: Decimal | str,
        free_usdt: Decimal | str,
        tactical_equity_usdt: Decimal | str | None = None,
        pending_hodl_bucket_usdt: Decimal | str = Decimal("0"),
        symbol_filters: SymbolFilters,
        drawdown_risk_multiplier: Decimal | str = Decimal("1.0"),
        ml_calibrated_prob: Optional[float] = None,
        ml_proposed_grade: Optional[SignalGrade] = None,
        portfolio_cvar99_loss_pct: Optional[float] = None,
        phase2b_graduation_stage: int = 2,
    ) -> SizingDecision:
        p_entry_raw = to_decimal(entry_price)
        p_stop_raw = to_decimal(stop_price)
        p_target_raw = to_decimal(target_price)
        c_free = to_decimal(free_usdt)
        b_hodl = max(Decimal("0"), to_decimal(pending_hodl_bucket_usdt))
        dd_mult = max(Decimal("0"), to_decimal(drawdown_risk_multiplier))

        # 1. Arredondamento direcional de preços para múltiplos exatos de tickSize
        p_entry = round_price_to_tick(p_entry_raw, symbol_filters.tick_size, "UP")
        p_stop = round_price_to_tick(p_stop_raw, symbol_filters.tick_size, "UP")
        p_target = round_price_to_tick(p_target_raw, symbol_filters.tick_size, "DOWN")

        usable_cash = max(Decimal("0"), c_free - b_hodl)
        if tactical_equity_usdt is not None:
            total_equity = max(Decimal("0"), to_decimal(tactical_equity_usdt) - b_hodl)
        else:
            total_equity = usable_cash

        if p_entry <= Decimal("0") or p_stop <= Decimal("0") or p_stop >= p_entry or p_target <= p_entry:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                grade=SignalGrade.C,
                effective_risk_pct=Decimal("0"),
                indifference_prob=1.0,
                net_expected_r=None,
                usable_cash_usdt=usable_cash,
                quote_order_qty=Decimal("0"),
                base_qty=Decimal("0"),
                entry_price=p_entry,
                stop_price=p_stop,
                target_price=p_target,
                cap_binding="REJECTED",
                reject_reason="INVALID_BARRIER_GEOMETRY",
            )

        d_stop_dec = (p_entry - p_stop) / p_entry
        r_eff_dec = (p_target - p_entry) / (p_entry - p_stop)
        d_stop_flt = float(d_stop_dec)
        r_eff_flt = float(r_eff_dec)

        _, p_indiff = self.compute_indifference_threshold(d_stop_flt, r_eff_flt)
        margin = float(self.sizing_cfg.min_expectancy_margin_over_breakeven)

        # 2. Avaliação do Juiz de IA (Observador Silencioso na Fase 1 vs Filtro Ativo na Fase 2)
        net_exp_r: Optional[float] = None
        silent_grade_str: Optional[str] = None
        silent_p_cal: Optional[float] = None

        if ml_calibrated_prob is not None:
            silent_p_cal = float(ml_calibrated_prob)
            net_exp_r = self.compute_net_expected_r(silent_p_cal, d_stop_flt, r_eff_flt)
            if silent_p_cal <= p_indiff + margin or net_exp_r <= 0.0:
                inferred_grade = SignalGrade.C
            elif ml_proposed_grade == SignalGrade.A_PLUS:
                inferred_grade = SignalGrade.A_PLUS
            else:
                inferred_grade = ml_proposed_grade or SignalGrade.B
            silent_grade_str = inferred_grade.value
        elif ml_proposed_grade is not None:
            inferred_grade = ml_proposed_grade
            silent_grade_str = inferred_grade.value
        else:
            inferred_grade = SignalGrade.B

        if self.use_ml_filter:
            if ml_calibrated_prob is not None and ml_calibrated_prob <= p_indiff + margin:
                return SizingDecision(
                    approved=False,
                    symbol=symbol,
                    grade=SignalGrade.C,
                    effective_risk_pct=Decimal("0"),
                    indifference_prob=p_indiff,
                    net_expected_r=net_exp_r,
                    usable_cash_usdt=usable_cash,
                    quote_order_qty=Decimal("0"),
                    base_qty=Decimal("0"),
                    entry_price=p_entry,
                    stop_price=p_stop,
                    target_price=p_target,
                    cap_binding="REJECTED",
                    reject_reason="BELOW_INDIFFERENCE_THRESHOLD",
                    ml_grade_silent=silent_grade_str,
                    ml_p_cal_silent=silent_p_cal,
                )
            if inferred_grade == SignalGrade.C:
                return SizingDecision(
                    approved=False,
                    symbol=symbol,
                    grade=SignalGrade.C,
                    effective_risk_pct=Decimal("0"),
                    indifference_prob=p_indiff,
                    net_expected_r=net_exp_r,
                    usable_cash_usdt=usable_cash,
                    quote_order_qty=Decimal("0"),
                    base_qty=Decimal("0"),
                    entry_price=p_entry,
                    stop_price=p_stop,
                    target_price=p_target,
                    cap_binding="REJECTED",
                    reject_reason="GRADE_C_REJECTED",
                    ml_grade_silent=silent_grade_str,
                    ml_p_cal_silent=silent_p_cal,
                )
            cvar_breach = (
                portfolio_cvar99_loss_pct is not None and abs(float(portfolio_cvar99_loss_pct)) > 0.05
            )
            active_grade = (
                SignalGrade.A_PLUS
                if (
                    inferred_grade == SignalGrade.A_PLUS
                    and self.sizing_cfg.conviction_sizing_enabled
                    and not cvar_breach
                )
                else SignalGrade.B
            )
        else:
            # Fase 1: opera sempre como Nota B (risco base 1.0%, teto de caixa 50%),
            # preservando ml_grade_silent e ml_p_cal_silent apenas para auditoria no SQLite.
            active_grade = SignalGrade.B

        if dd_mult <= Decimal("0") or usable_cash <= Decimal("0") or total_equity <= Decimal("0"):
            return SizingDecision(
                approved=False,
                symbol=symbol,
                grade=active_grade,
                effective_risk_pct=Decimal("0"),
                indifference_prob=p_indiff,
                net_expected_r=net_exp_r,
                usable_cash_usdt=usable_cash,
                quote_order_qty=Decimal("0"),
                base_qty=Decimal("0"),
                entry_price=p_entry,
                stop_price=p_stop,
                target_price=p_target,
                cap_binding="REJECTED",
                reject_reason="ZERO_RISK_MULTIPLIER_OR_CASH",
                ml_grade_silent=silent_grade_str,
                ml_p_cal_silent=silent_p_cal,
            )

        # 3. Parâmetros da Nota Ativa (Nota B vs Nota A+ com escada Fase 2B: 1.5% -> 2.0%)
        if active_grade == SignalGrade.A_PLUS and self.sizing_cfg.conviction_sizing_enabled:
            full_a_plus_risk = _dec_from_float_cfg(self.sizing_cfg.grade_a_plus_risk_pct)
            if int(phase2b_graduation_stage) == 1:
                base_risk_pct = min(full_a_plus_risk, Decimal("0.015"))
            else:
                base_risk_pct = full_a_plus_risk
            max_cash_pct = _dec_from_float_cfg(self.sizing_cfg.grade_a_plus_max_cash_pct)
        else:
            base_risk_pct = _dec_from_float_cfg(self.sizing_cfg.default_grade_b_risk_pct)
            max_cash_pct = _dec_from_float_cfg(self.sizing_cfg.default_grade_b_max_cash_pct)

        eff_risk_pct = base_risk_pct * dd_mult

        # 4. Equação de Teto Duplo + Disjuntor Proporcional min(500 USDT, 55% da banca)
        fee_dec = _dec_from_float_cfg(self.sizing_cfg.roundtrip_fee_assumption_pct)
        slip_dec = _dec_from_float_cfg(self.sizing_cfg.stress_slippage_assumption_pct)
        effective_loss_pct_at_stop = d_stop_dec + fee_dec + slip_dec

        cap_risk_usdt = (eff_risk_pct * total_equity) / effective_loss_pct_at_stop
        cap_cash_usdt = max_cash_pct * usable_cash

        abs_order_cap = _dec_from_float_cfg(self.portfolio_cfg.max_single_order_usdt_cap)
        prop_order_cap = _dec_from_float_cfg(self.portfolio_cfg.max_single_order_equity_pct) * total_equity
        cap_order_usdt = min(abs_order_cap, prop_order_cap)

        v_pos = min(cap_risk_usdt, cap_cash_usdt, cap_order_usdt, usable_cash)
        if v_pos == cap_risk_usdt:
            binding = "RISK_CAP"
        elif v_pos == cap_cash_usdt:
            binding = "CASH_CAP"
        else:
            binding = "ORDER_CAP"

        # 5. Arredondamento para stepSize e verificação estrita de 1.05 * minNotional
        raw_qty = v_pos / p_entry
        step_qty = floor_qty_to_step(raw_qty, symbol_filters.step_size)
        notional_usdt = (step_qty * p_entry).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

        safety_buffer = _dec_from_float_cfg(self.portfolio_cfg.min_notional_safety_buffer)
        required_min_notional = symbol_filters.min_notional * safety_buffer

        if step_qty < symbol_filters.min_qty:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                grade=active_grade,
                effective_risk_pct=eff_risk_pct,
                indifference_prob=p_indiff,
                net_expected_r=net_exp_r,
                usable_cash_usdt=usable_cash,
                quote_order_qty=Decimal("0"),
                base_qty=Decimal("0"),
                entry_price=p_entry,
                stop_price=p_stop,
                target_price=p_target,
                cap_binding="REJECTED",
                reject_reason="BELOW_MIN_QTY",
                ml_grade_silent=silent_grade_str,
                ml_p_cal_silent=silent_p_cal,
            )

        if notional_usdt < required_min_notional:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                grade=active_grade,
                effective_risk_pct=eff_risk_pct,
                indifference_prob=p_indiff,
                net_expected_r=net_exp_r,
                usable_cash_usdt=usable_cash,
                quote_order_qty=Decimal("0"),
                base_qty=Decimal("0"),
                entry_price=p_entry,
                stop_price=p_stop,
                target_price=p_target,
                cap_binding="REJECTED",
                reject_reason="BELOW_MIN_NOTIONAL_BUFFER",
                ml_grade_silent=silent_grade_str,
                ml_p_cal_silent=silent_p_cal,
            )

        ok_flt, flt_err = passes_filters(p_entry, step_qty, symbol_filters)
        if not ok_flt:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                grade=active_grade,
                effective_risk_pct=eff_risk_pct,
                indifference_prob=p_indiff,
                net_expected_r=net_exp_r,
                usable_cash_usdt=usable_cash,
                quote_order_qty=Decimal("0"),
                base_qty=Decimal("0"),
                entry_price=p_entry,
                stop_price=p_stop,
                target_price=p_target,
                cap_binding="REJECTED",
                reject_reason=flt_err or "FILTER_REJECTED",
                ml_grade_silent=silent_grade_str,
                ml_p_cal_silent=silent_p_cal,
            )

        return SizingDecision(
            approved=True,
            symbol=symbol,
            grade=active_grade,
            effective_risk_pct=eff_risk_pct,
            indifference_prob=p_indiff,
            net_expected_r=net_exp_r,
            usable_cash_usdt=usable_cash,
            quote_order_qty=notional_usdt,
            base_qty=step_qty,
            entry_price=p_entry,
            stop_price=p_stop,
            target_price=p_target,
            cap_binding=binding,
            reject_reason=None,
            ml_grade_silent=silent_grade_str,
            ml_p_cal_silent=silent_p_cal,
        )
