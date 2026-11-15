"""Simulador causal Single-Slot com 3 cenários de custo e resolução 1h/1m (Bloco 3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence

import pandas as pd

from src.regimes.regime_detector import MarketRegime, RegimeDetector
from src.strategies.base_strategy import BaseStrategy, SignalCandidate


class CostScenario(str, Enum):
    """Cenários de custo friccional e slippage de cauda (Bloco 3)."""

    OPTIMISTIC = "OPTIMISTIC"
    BASE = "BASE"
    PESSIMISTIC = "PESSIMISTIC"


@dataclass(frozen=True)
class SimulatedTrade:
    """Resultado auditável de uma operação no simulador Single-Slot."""

    symbol: str
    strategy_name: str
    regime: str
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    exit_reason: str  # "TARGET", "STOP", "BREAKEVEN", "TIME_BARRIER"
    gross_r: float
    net_r: float
    pnl_pct_equity: float
    bars_held: int
    rs_score: float


def _time_col(df: pd.DataFrame) -> str:
    return "close_time_ms" if "close_time_ms" in df.columns else "close_time"


class CausalSingleSlotSimulator:
    """
    Simulador determinístico de fila única (MAX_OPEN_POSITIONS = 1) com:
    - Desempate coerente por rs_vol_adjusted_12h;
    - Taxas assimétricas e slippage conforme CostScenario;
    - Cálculo empírico de E[R] incluindo saídas por tempo (TIME_BARRIER 24h);
    - Resolução intra-vela de 1 minuto (1m) para avaliação do Breakeven (Degrau E);
    - Controle de drawdown CPPI (corte de 50% do risco quando drawdown >= 4%).
    """

    def __init__(
        self,
        cost_scenario: CostScenario = CostScenario.BASE,
        risk_per_trade_pct: float = 0.010,
        max_cash_pct: float = 0.50,
        breakeven_enabled: bool = False,
        breakeven_trigger_r: float = 1.2,
        cppi_enabled: bool = True,
        cppi_drawdown_threshold_pct: float = 0.04,
        cppi_reduction_factor: float = 0.50,
    ) -> None:
        self.cost_scenario = cost_scenario
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_cash_pct = max_cash_pct
        self.breakeven_enabled = breakeven_enabled
        self.breakeven_trigger_r = breakeven_trigger_r
        self.cppi_enabled = cppi_enabled
        self.cppi_drawdown_threshold_pct = cppi_drawdown_threshold_pct
        self.cppi_reduction_factor = cppi_reduction_factor

    def select_single_slot_winner(
        self,
        candidates: Sequence[SignalCandidate],
    ) -> Optional[SignalCandidate]:
        """Seleciona o ativo com maior rs_vol_adjusted_12h (desempate alfabético por symbol)."""
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda c: (-float(c.rs_vol_adjusted_12h), c.symbol),
        )[0]

    def _compute_friction_in_r(
        self,
        stop_distance_pct: float,
        exit_reason: str,
    ) -> float:
        """
        Retorna o custo total (taxas ida+volta + slippage + choque de gap) em unidades de R.
        """
        d_stop = max(stop_distance_pct, 1e-6)
        if self.cost_scenario == CostScenario.OPTIMISTIC:
            roundtrip_fee = 0.0015
            slippage = 0.00015 if exit_reason == "TARGET" else 0.0003
            gap_shock = 0.0
        elif self.cost_scenario == CostScenario.BASE:
            roundtrip_fee = 0.0015
            slippage = 0.0003 if exit_reason == "TARGET" else 0.0006
            gap_shock = 0.0
        else:
            roundtrip_fee = 0.0020
            slippage = 0.0006 if exit_reason == "TARGET" else 0.0012
            gap_shock = 0.0015 if exit_reason in ("STOP", "BREAKEVEN") else 0.0

        return (roundtrip_fee + slippage + gap_shock) / d_stop

    def resolve_trade_1h_or_1m(
        self,
        candidate: SignalCandidate,
        future_bars_1h: pd.DataFrame,
        intracandle_bars_1m: Optional[pd.DataFrame] = None,
        effective_risk_pct: Optional[float] = None,
    ) -> SimulatedTrade:
        """
        Resolve o desfecho da operação usando velas de 1m (quando fornecidas) ou 1h.
        """
        entry_p = candidate.entry_price
        orig_stop_p = candidate.stop_price
        current_stop_p = orig_stop_p
        target_p = candidate.target_price
        risk_per_unit = max(entry_p - orig_stop_p, 1e-12)
        be_trigger_price = entry_p + self.breakeven_trigger_r * risk_per_unit
        be_active = False

        tcol_1h = _time_col(future_bars_1h)
        horizon_1h = (
            future_bars_1h[future_bars_1h[tcol_1h] > candidate.close_time_ms]
            .sort_values(tcol_1h, kind="mergesort")
            .head(candidate.time_barrier_hours)
        )

        if horizon_1h.empty:
            friction_r = self._compute_friction_in_r(candidate.stop_distance_pct, "TIME_BARRIER")
            net_r = -friction_r
            r_pct = effective_risk_pct if effective_risk_pct is not None else self.risk_per_trade_pct
            return SimulatedTrade(
                symbol=candidate.symbol,
                strategy_name=candidate.strategy_name,
                regime=candidate.regime.value,
                entry_time_ms=candidate.close_time_ms,
                exit_time_ms=candidate.close_time_ms,
                entry_price=entry_p,
                exit_price=entry_p,
                stop_price=orig_stop_p,
                target_price=target_p,
                exit_reason="TIME_BARRIER",
                gross_r=0.0,
                net_r=net_r,
                pnl_pct_equity=net_r * r_pct,
                bars_held=0,
                rs_score=candidate.rs_vol_adjusted_12h,
            )

        last_1h_close_time = int(horizon_1h[tcol_1h].iloc[-1])
        exit_price = float(horizon_1h["close"].iloc[-1])
        exit_time_ms = last_1h_close_time
        exit_reason = "TIME_BARRIER"
        bars_held = len(horizon_1h)

        if intracandle_bars_1m is not None and not intracandle_bars_1m.empty:
            tcol_1m = _time_col(intracandle_bars_1m)
            bars_1m = intracandle_bars_1m[
                (intracandle_bars_1m[tcol_1m] > candidate.close_time_ms)
                & (intracandle_bars_1m[tcol_1m] <= last_1h_close_time)
            ].sort_values(tcol_1m, kind="mergesort")

            for _, m_row in bars_1m.iterrows():
                m_high = float(m_row["high"])
                m_low = float(m_row["low"])
                m_time = int(m_row[tcol_1m])

                if m_low <= current_stop_p:
                    exit_price = current_stop_p
                    exit_time_ms = m_time
                    exit_reason = "BREAKEVEN" if be_active else "STOP"
                    bars_held = max(1, int((m_time - candidate.close_time_ms) // 3_600_000))
                    break

                if m_high >= target_p:
                    exit_price = target_p
                    exit_time_ms = m_time
                    exit_reason = "TARGET"
                    bars_held = max(1, int((m_time - candidate.close_time_ms) // 3_600_000))
                    break

                if self.breakeven_enabled and not be_active and m_high >= be_trigger_price:
                    be_active = True
                    current_stop_p = entry_p
        else:
            for idx_bar, (_, h_row) in enumerate(horizon_1h.iterrows(), start=1):
                h_high = float(h_row["high"])
                h_low = float(h_row["low"])
                h_time = int(h_row[tcol_1h])

                if h_low <= current_stop_p:
                    exit_price = current_stop_p
                    exit_time_ms = h_time
                    exit_reason = "BREAKEVEN" if be_active else "STOP"
                    bars_held = idx_bar
                    break

                if h_high >= target_p:
                    exit_price = target_p
                    exit_time_ms = h_time
                    exit_reason = "TARGET"
                    bars_held = idx_bar
                    break

                if self.breakeven_enabled and not be_active and h_high >= be_trigger_price:
                    be_active = True
                    current_stop_p = entry_p

        gross_r = (exit_price - entry_p) / risk_per_unit
        friction_r = self._compute_friction_in_r(candidate.stop_distance_pct, exit_reason)
        net_r = gross_r - friction_r

        r_pct = effective_risk_pct if effective_risk_pct is not None else self.risk_per_trade_pct
        max_r_loss_from_cash_cap = self.max_cash_pct * candidate.stop_distance_pct
        effective_r_unit_equity = min(r_pct, max_r_loss_from_cash_cap)
        pnl_pct_equity = net_r * effective_r_unit_equity

        return SimulatedTrade(
            symbol=candidate.symbol,
            strategy_name=candidate.strategy_name,
            regime=candidate.regime.value,
            entry_time_ms=candidate.close_time_ms,
            exit_time_ms=exit_time_ms,
            entry_price=entry_p,
            exit_price=exit_price,
            stop_price=orig_stop_p,
            target_price=target_p,
            exit_reason=exit_reason,
            gross_r=gross_r,
            net_r=net_r,
            pnl_pct_equity=pnl_pct_equity,
            bars_held=bars_held,
            rs_score=candidate.rs_vol_adjusted_12h,
        )

    def run_multi_asset_simulation(
        self,
        aligned_frames: Dict[str, pd.DataFrame],
        strategies: Sequence[BaseStrategy],
        regime_detector: RegimeDetector,
        ignore_regime_filter: bool = False,
        require_order_flow_filter: bool = False,
        intracandle_1m_map: Optional[Dict[str, pd.DataFrame]] = None,
        start_time_ms: Optional[int] = None,
        end_time_ms: Optional[int] = None,
    ) -> List[SimulatedTrade]:
        """
        Executa a simulação cronológica Single-Slot sobre todos os pares do universo.
        """
        regime_lookup: Dict[str, Dict[int, MarketRegime]] = {}
        if not ignore_regime_filter:
            for sym, full_df in aligned_frames.items():
                regime_lookup[sym] = regime_detector.detect_series_from_aligned_frame(full_df)
        elif "BTCUSDT" in aligned_frames:
            regime_lookup["BTCUSDT"] = regime_detector.detect_series_from_aligned_frame(
                aligned_frames["BTCUSDT"]
            )

        btc_rs_lookup: Dict[int, float] = {}
        btc_regime_str_lookup: Dict[int, str] = {
            t: reg.value for t, reg in regime_lookup.get("BTCUSDT", {}).items()
        }
        if "BTCUSDT" in aligned_frames:
            btc_df = aligned_frames["BTCUSDT"]
            tcol_btc = _time_col(btc_df)
            btc_sorted = btc_df.sort_values(tcol_btc, kind="mergesort").reset_index(drop=True)
            if "rs_vol_adjusted_12h" not in btc_sorted.columns and "atr_14" in btc_sorted.columns:
                btc_rs_series = (btc_sorted["close"] - btc_sorted["close"].shift(12)) / btc_sorted[
                    "atr_14"
                ].replace(0.0, float("nan"))
            elif "rs_vol_adjusted_12h" in btc_sorted.columns:
                btc_rs_series = btc_sorted["rs_vol_adjusted_12h"]
            else:
                btc_rs_series = pd.Series(0.0, index=btc_sorted.index)
            btc_rs_lookup = {
                int(t): float(v)
                for t, v in zip(btc_sorted[tcol_btc].tolist(), btc_rs_series.tolist())
            }

        all_times: set[int] = set()
        indexed_frames: Dict[str, pd.DataFrame] = {}
        for sym, df in aligned_frames.items():
            tcol = _time_col(df)
            sub = df.sort_values(tcol, kind="mergesort").reset_index(drop=True)
            if "rs_vol_adjusted_12h" not in sub.columns and "atr_14" in sub.columns:
                sub["rs_vol_adjusted_12h"] = (sub["close"] - sub["close"].shift(12)) / sub[
                    "atr_14"
                ].replace(0.0, float("nan"))
            if "prev_high_12h" not in sub.columns and "high" in sub.columns:
                sub["prev_high_12h"] = sub["high"].shift(1).rolling(12, min_periods=12).max()
            if "btc_rs_12h" not in sub.columns and btc_rs_lookup:
                sub["btc_rs_12h"] = sub[tcol].map(btc_rs_lookup)
            if "btc_regime_4h" not in sub.columns and btc_regime_str_lookup:
                sub["btc_regime_4h"] = sub[tcol].map(btc_regime_str_lookup)
            if start_time_ms is not None:
                sub = sub[sub[tcol] >= start_time_ms]
            if end_time_ms is not None:
                sub = sub[sub[tcol] <= end_time_ms]
            indexed_frames[sym] = sub
            all_times.update(int(t) for t in sub[tcol].tolist())

        sorted_times = sorted(all_times)
        row_lookup: Dict[str, Dict[int, pd.Series]] = {
            sym: {int(r[_time_col(df)]): r for _, r in df.iterrows()}
            for sym, df in indexed_frames.items()
        }

        trades: List[SimulatedTrade] = []
        slot_busy_until_ms: int = -1
        equity: float = 1.0
        hwm_equity: float = 1.0

        for t_ms in sorted_times:
            if t_ms <= slot_busy_until_ms:
                continue

            candidates: List[SignalCandidate] = []
            for sym in sorted(indexed_frames.keys()):
                row = row_lookup[sym].get(t_ms)
                if row is None:
                    continue

                if ignore_regime_filter:
                    regime = MarketRegime.BULL
                else:
                    regime = regime_lookup[sym].get(t_ms, MarketRegime.TRANSITION)

                for strat in strategies:
                    cand = strat.evaluate_row(
                        symbol=sym,
                        row=row,
                        regime=regime,
                        require_order_flow_filter=require_order_flow_filter,
                    )
                    if cand is not None:
                        candidates.append(cand)

            winner = self.select_single_slot_winner(candidates)
            if winner is None:
                continue

            drawdown_pct = (hwm_equity - equity) / hwm_equity if hwm_equity > 0 else 0.0
            eff_risk = self.risk_per_trade_pct
            if self.cppi_enabled and drawdown_pct >= self.cppi_drawdown_threshold_pct:
                eff_risk = self.risk_per_trade_pct * self.cppi_reduction_factor

            future_1h = aligned_frames[winner.symbol]
            bars_1m = (
                intracandle_1m_map.get(winner.symbol)
                if intracandle_1m_map is not None
                else None
            )
            sim_trade = self.resolve_trade_1h_or_1m(
                candidate=winner,
                future_bars_1h=future_1h,
                intracandle_bars_1m=bars_1m,
                effective_risk_pct=eff_risk,
            )
            trades.append(sim_trade)
            slot_busy_until_ms = sim_trade.exit_time_ms
            equity *= 1.0 + sim_trade.pnl_pct_equity
            if equity > hwm_equity:
                hwm_equity = equity

        return trades
