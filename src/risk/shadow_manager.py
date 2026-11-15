"""Controle de Drawdown (CPPI), Modo Fantasma CUSUM, Auto-Downgrade e Kill-Switches (Bloco 4)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Optional, Sequence

from src.core.binance_client import to_decimal
from src.core.config import DrawdownShadowProtectionCfg, PortfolioCfg
from src.core.database import Database

STATE_KEY_TACTICAL_HWM: str = "tactical_hwm_usdt"
STATE_KEY_LIVE_LOSS_STREAK: str = "live_loss_streak"
STATE_KEY_SHADOW_ACTIVE: str = "shadow_mode_active"
STATE_KEY_SHADOW_VIRTUAL_WINS: str = "shadow_virtual_wins"
STATE_KEY_SHADOW_VIRTUAL_R_SUM: str = "shadow_virtual_r_sum"
STATE_KEY_EMERGENCY_HALT: str = "emergency_halt"
STATE_KEY_KILL_SWITCH_REASON: str = "kill_switch_reason"
STATE_KEY_RECONCILE_FAIL_STREAK: str = "reconcile_fail_streak"
STATE_KEY_CONVICTION_DOWNGRADED: str = "conviction_downgraded"
STATE_KEY_SIDEWAYS_DISABLED: str = "sideways_disabled"

AUTH_KILL_CODES: frozenset[int] = frozenset({-1002, -1022, -2015})
HARD_STOP_DRAWDOWN_PCT: Decimal = Decimal("0.12")
HARD_MIN_BNB_RESERVE_USDT: Decimal = Decimal("2.00")


class KillSwitchReason(StrEnum):
    """Motivos de disparo dos 4 Disjuntores de Parada Total (EMERGENCY_HALT)."""

    NONE = "NONE"
    HARD_STOP_DRAWDOWN_12PCT = "HARD_STOP_DRAWDOWN_12PCT"
    BNB_FEE_RESERVE_DEPLETED = "BNB_FEE_RESERVE_DEPLETED"
    RECONCILIATION_OR_AUTH_FAILURE = "RECONCILIATION_OR_AUTH_FAILURE"
    DAILY_TRADES_CAP_EXCEEDED = "DAILY_TRADES_CAP_EXCEEDED"


@dataclass(frozen=True, slots=True)
class DefenseStatus:
    """Fotografia completa das travas defensivas no ciclo atual."""

    hwm_usdt: Decimal
    current_equity_usdt: Decimal
    drawdown_pct: Decimal
    risk_multiplier: Decimal
    cppi_active: bool
    shadow_mode_active: bool
    emergency_halt: bool
    kill_switch_reason: KillSwitchReason
    conviction_downgraded: bool
    sideways_disabled: bool


class ShadowManager:
    """
    Gerencia a defesa patrimonial e estatística do motor:
    1. CPPI_DRAWDOWN_CONTROL: corta o risco pela metade (0.50x) quando o drawdown
       atinge 4.0% abaixo do High-Water Mark e restaura 1.00x automaticamente ao
       recuperar o topo histórico.
    2. CUSUM_SHADOW_MODE (opcional): entra em modo virtual após N derrotas consecutivas
       e exige 4 vitórias virtuais com E[R] > 0 e soma acumulada de R > 0 (proibindo
       o atalho antigo de religar com 1 única vitória de +2.5R).
    3. Auto-Downgrade de Fase:
       - Fase 2 -> Fase 1 se média móvel de 30 trades reais < 0.0R ou ECE_30 > 0.08;
       - Desarme isolado de sideways_pingpong se acumular <= -3.0R nos últimos 20 trades.
    4. 4 Hard Kill-Switches (EMERGENCY_HALT):
       - Drawdown >= 12.0%;
       - Reserva livre de BNB < 2.00 USDT;
       - 3 falhas consecutivas de reconciliação ou erro de autenticação (-1002/-1022/-2015);
       - Número de trades em 24h >= max_daily_trades_cap (6).
    """

    def __init__(
        self,
        protection_cfg: DrawdownShadowProtectionCfg,
        portfolio_cfg: PortfolioCfg,
        db: Optional[Database] = None,
        *,
        hard_stop_drawdown_pct: Decimal = HARD_STOP_DRAWDOWN_PCT,
        hard_min_bnb_usdt: Decimal = HARD_MIN_BNB_RESERVE_USDT,
    ) -> None:
        self.protection_cfg = protection_cfg
        self.portfolio_cfg = portfolio_cfg
        self.db = db
        self.hard_stop_drawdown_pct = to_decimal(hard_stop_drawdown_pct)
        self.hard_min_bnb_usdt = to_decimal(hard_min_bnb_usdt)

        self._mem_state: dict[str, str] = {}

    def _get(self, key: str, default: str) -> str:
        if self.db is not None:
            val = self.db.get_state(key)
            if val is not None:
                return val
        return self._mem_state.get(key, default)

    def _set(self, key: str, value: str | Decimal) -> None:
        s = format(value, "f") if isinstance(value, Decimal) else str(value)
        self._mem_state[key] = s
        if self.db is not None:
            self.db.set_state(key, s)

    def record_reconciliation_outcome(
        self,
        *,
        success: bool,
        binance_error_code: Optional[int] = None,
    ) -> Optional[KillSwitchReason]:
        """Atualiza o contador de reconciliação ou dispara HALT imediato em erro de chave API."""
        if binance_error_code is not None and int(binance_error_code) in AUTH_KILL_CODES:
            self._trigger_halt(KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE)
            return KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE

        if success:
            self._set(STATE_KEY_RECONCILE_FAIL_STREAK, "0")
            return None

        streak = int(self._get(STATE_KEY_RECONCILE_FAIL_STREAK, "0")) + 1
        self._set(STATE_KEY_RECONCILE_FAIL_STREAK, str(streak))
        if streak >= 3:
            self._trigger_halt(KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE)
            return KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE
        return None

    def _trigger_halt(self, reason: KillSwitchReason) -> None:
        self._set(STATE_KEY_EMERGENCY_HALT, "1")
        self._set(STATE_KEY_KILL_SWITCH_REASON, reason.value)

    def reset_emergency_halt(self) -> None:
        """Reset manual após revisão humana."""
        self._set(STATE_KEY_EMERGENCY_HALT, "0")
        self._set(STATE_KEY_KILL_SWITCH_REASON, KillSwitchReason.NONE.value)
        self._set(STATE_KEY_RECONCILE_FAIL_STREAK, "0")

    def record_trade_outcome(
        self,
        *,
        net_r: float,
        is_virtual_shadow_trade: bool = False,
    ) -> None:
        """
        Registra o resultado de uma operação (real ou virtual) para a máquina de estados
        CUSUM_SHADOW_MODE:
        - No modo real: acumula derrotas consecutivas; ao atingir shadow_max_consecutive_live_losses,
          ativa shadow_mode_active = True (se shadow_mode_enabled ou protection_mode == CUSUM_SHADOW_MODE).
        - No modo virtual: exige shadow_virtual_wins_to_reactivate (4) vitórias virtuais com net_r > 0
          e soma acumulada de R virtual > 0 (nunca reativa com 1 única vitória grande).
        """
        use_shadow = (
            self.protection_cfg.shadow_mode_enabled
            or self.protection_cfg.protection_mode == "CUSUM_SHADOW_MODE"
        )
        if not use_shadow:
            return

        shadow_active = self._get(STATE_KEY_SHADOW_ACTIVE, "0") == "1"
        if not shadow_active and not is_virtual_shadow_trade:
            if float(net_r) < 0.0:
                losses = int(self._get(STATE_KEY_LIVE_LOSS_STREAK, "0")) + 1
                self._set(STATE_KEY_LIVE_LOSS_STREAK, str(losses))
                if losses >= int(self.protection_cfg.shadow_max_consecutive_live_losses):
                    self._set(STATE_KEY_SHADOW_ACTIVE, "1")
                    self._set(STATE_KEY_SHADOW_VIRTUAL_WINS, "0")
                    self._set(STATE_KEY_SHADOW_VIRTUAL_R_SUM, "0")
            else:
                self._set(STATE_KEY_LIVE_LOSS_STREAK, "0")
            return

        if shadow_active and is_virtual_shadow_trade:
            r_val = Decimal(str(round(float(net_r), 6)))
            cur_sum = to_decimal(self._get(STATE_KEY_SHADOW_VIRTUAL_R_SUM, "0")) + r_val
            self._set(STATE_KEY_SHADOW_VIRTUAL_R_SUM, cur_sum)
            if float(net_r) > 0.0:
                wins = int(self._get(STATE_KEY_SHADOW_VIRTUAL_WINS, "0")) + 1
                self._set(STATE_KEY_SHADOW_VIRTUAL_WINS, str(wins))
            else:
                wins = int(self._get(STATE_KEY_SHADOW_VIRTUAL_WINS, "0"))

            required_wins = int(self.protection_cfg.shadow_virtual_wins_to_reactivate)
            if wins >= required_wins and cur_sum > Decimal("0"):
                self._set(STATE_KEY_SHADOW_ACTIVE, "0")
                self._set(STATE_KEY_LIVE_LOSS_STREAK, "0")
                self._set(STATE_KEY_SHADOW_VIRTUAL_WINS, "0")
                self._set(STATE_KEY_SHADOW_VIRTUAL_R_SUM, "0")

    def evaluate_phase_downgrade(
        self,
        *,
        recent_live_r_30: Sequence[float] = (),
        recent_ece_30: Optional[float] = None,
        recent_sideways_r_20: Sequence[float] = (),
    ) -> tuple[bool, bool]:
        """
        Avalia as regras de regressão automática (Seção 5.1 do Roadmap):
        - Se nos últimos 30 trades reais a média < 0.0R ou ECE_30 > 0.08 -> conviction_downgraded = True
          (recupera quando 30 trades tiverem média >= +0.10R e ECE <= 0.08).
        - Se nos últimos 20 trades de sideways_pingpong a soma <= -3.0R -> sideways_disabled = True.
        Retorna (conviction_downgraded, sideways_disabled).
        """
        conv_down = self._get(STATE_KEY_CONVICTION_DOWNGRADED, "0") == "1"
        if len(recent_live_r_30) >= 30:
            last_30 = list(recent_live_r_30)[-30:]
            mean_30 = sum(float(x) for x in last_30) / 30.0
            ece_bad = recent_ece_30 is not None and float(recent_ece_30) > 0.08
            if mean_30 < 0.0 or ece_bad:
                conv_down = True
            elif conv_down and mean_30 >= 0.10 and not ece_bad:
                conv_down = False
        elif recent_ece_30 is not None and float(recent_ece_30) > 0.08:
            conv_down = True
        self._set(STATE_KEY_CONVICTION_DOWNGRADED, "1" if conv_down else "0")

        sw_dis = self._get(STATE_KEY_SIDEWAYS_DISABLED, "0") == "1"
        if len(recent_sideways_r_20) >= 20:
            last_20 = list(recent_sideways_r_20)[-20:]
            sum_20 = sum(float(x) for x in last_20)
            if sum_20 <= -3.0:
                sw_dis = True
        self._set(STATE_KEY_SIDEWAYS_DISABLED, "1" if sw_dis else "0")

        return conv_down, sw_dis

    def evaluate_defense(
        self,
        *,
        current_equity_usdt: Decimal | str,
        bnb_free_usdt_equiv: Decimal | str = Decimal("10.00"),
        trades_last_24h: int = 0,
    ) -> DefenseStatus:
        """
        Atualiza o High-Water Mark tático, verifica os 4 Hard Kill-Switches e retorna
        o multiplicador de risco efetivo (1.0, 0.5 ou 0.0).
        """
        equity = to_decimal(current_equity_usdt)
        bnb_usdt = to_decimal(bnb_free_usdt_equiv)

        stored_hwm_str = self._get(STATE_KEY_TACTICAL_HWM, "0")
        stored_hwm = to_decimal(stored_hwm_str)
        if equity > stored_hwm:
            hwm = equity
            self._set(STATE_KEY_TACTICAL_HWM, hwm)
        else:
            hwm = stored_hwm

        if hwm > Decimal("0"):
            dd_pct = max(Decimal("0"), (hwm - equity) / hwm)
        else:
            dd_pct = Decimal("0")

        # 1. Verificação dos 4 Hard Kill-Switches
        already_halted = self._get(STATE_KEY_EMERGENCY_HALT, "0") == "1"
        reason_str = self._get(STATE_KEY_KILL_SWITCH_REASON, KillSwitchReason.NONE.value)
        reason = (
            KillSwitchReason(reason_str)
            if reason_str in KillSwitchReason._value2member_map_
            else KillSwitchReason.NONE
        )

        if dd_pct >= self.hard_stop_drawdown_pct:
            already_halted = True
            reason = KillSwitchReason.HARD_STOP_DRAWDOWN_12PCT
            self._trigger_halt(reason)
        elif bnb_usdt < self.hard_min_bnb_usdt:
            already_halted = True
            reason = KillSwitchReason.BNB_FEE_RESERVE_DEPLETED
            self._trigger_halt(reason)
        elif int(trades_last_24h) >= int(self.portfolio_cfg.max_daily_trades_cap):
            already_halted = True
            reason = KillSwitchReason.DAILY_TRADES_CAP_EXCEEDED
            self._trigger_halt(reason)

        conv_down = self._get(STATE_KEY_CONVICTION_DOWNGRADED, "0") == "1"
        sw_dis = self._get(STATE_KEY_SIDEWAYS_DISABLED, "0") == "1"
        shadow_active = self._get(STATE_KEY_SHADOW_ACTIVE, "0") == "1"

        cppi_threshold = Decimal(str(self.protection_cfg.cppi_drawdown_threshold_pct))
        cppi_factor = Decimal(str(self.protection_cfg.cppi_risk_reduction_factor))
        cppi_enabled = self.protection_cfg.protection_mode == "CPPI_DRAWDOWN_CONTROL"
        cppi_active = bool(cppi_enabled and dd_pct >= cppi_threshold and not already_halted)

        if already_halted or shadow_active:
            risk_mult = Decimal("0")
        elif cppi_active:
            risk_mult = cppi_factor
        else:
            risk_mult = Decimal("1.0")

        return DefenseStatus(
            hwm_usdt=hwm,
            current_equity_usdt=equity,
            drawdown_pct=dd_pct,
            risk_multiplier=risk_mult,
            cppi_active=cppi_active,
            shadow_mode_active=shadow_active,
            emergency_halt=already_halted,
            kill_switch_reason=reason,
            conviction_downgraded=conv_down,
            sideways_disabled=sw_dis,
        )
