"""PaperExecutor para o modo SHADOW_CHAMELEON usando bookTicker real e outcome_resolver compartilhado (Bloco 5)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional, Sequence

from src.auditing.outcome_resolver import SingleTargetResolution, resolve_single_target_horizon
from src.core.binance_client import Priority, PublicBinanceClient, to_api_str, to_decimal
from src.core.config import PositionSizingCfg
from src.core.database import CandleRow, Database
from src.execution.executor_protocol import ExecutionReport, ExecutionRequest
from src.risk.shadow_manager import ShadowManager


class PaperExecutor:
    """
    Executa operações em modo SHADOW_CHAMELEON (Gate 6B):
    - Lê o melhor ask real da corretora via /api/v3/ticker/bookTicker no instante da decisão;
    - Aplica o modelo de slippage parametrizado em strategies.yaml;
    - Grava na tabela shadow_trades (protegida pelo índice único parcial ux_shadow_single_slot);
    - Resolve o desfecho nas velas subsequentes usando resolve_single_target_horizon (C2-3).
    """

    def __init__(
        self,
        *,
        db: Database,
        public_client: PublicBinanceClient,
        sizing_cfg: PositionSizingCfg,
        shadow_manager: Optional[ShadowManager] = None,
    ) -> None:
        self.db = db
        self.public_client = public_client
        self.sizing_cfg = sizing_cfg
        self.shadow_manager = shadow_manager

    def has_open_shadow_trade(self) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT shadow_id FROM shadow_trades WHERE status = 'OPEN' LIMIT 1"
            ).fetchone()
            return row is not None

    def execute_entry(
        self,
        req: ExecutionRequest,
        *,
        virtual_equity_before: Decimal = Decimal("300.00"),
        now_ms: Optional[int] = None,
    ) -> ExecutionReport:
        if not req.sizing.approved or req.sizing.base_qty <= Decimal("0"):
            return ExecutionReport(
                executed=False,
                mode="SHADOW_CHAMELEON",
                trade_id=None,
                client_order_id=f"SHD_{req.symbol}_{req.signal_close_time_ms}",
                status="REJECTED_BY_SIZER",
                filled_qty=Decimal("0"),
                avg_entry_price=Decimal("0"),
                error_msg=req.sizing.reject_reason,
            )

        if self.has_open_shadow_trade():
            return ExecutionReport(
                executed=False,
                mode="SHADOW_CHAMELEON",
                trade_id=None,
                client_order_id=f"SHD_{req.symbol}_{req.signal_close_time_ms}",
                status="SLOT_OCCUPIED",
                filled_qty=Decimal("0"),
                avg_entry_price=Decimal("0"),
                error_msg="ux_shadow_single_slot active",
            )

        ticker = self.public_client.get_book_ticker(req.symbol, priority=Priority.HIGH)
        ask_dec = ticker.ask
        slip_half = Decimal(str(self.sizing_cfg.stress_slippage_assumption_pct)) / Decimal("2")
        sim_entry_dec = ask_dec * (Decimal("1") + slip_half)

        entry_flt = float(sim_entry_dec)
        stop_flt = float(req.sizing.stop_price)
        target_flt = float(req.sizing.target_price)
        if stop_flt >= entry_flt:
            return ExecutionReport(
                executed=False,
                mode="SHADOW_CHAMELEON",
                trade_id=None,
                client_order_id=f"SHD_{req.symbol}_{req.signal_close_time_ms}",
                status="STOP_ALREADY_BREACHED_AT_ASK",
                filled_qty=Decimal("0"),
                avg_entry_price=sim_entry_dec,
            )

        created_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        risk_usdt = (
            req.sizing.effective_risk_pct * to_decimal(virtual_equity_before)
        ).quantize(Decimal("0.01"))

        with self.db.write_tx() as tx:
            cur = tx.execute(
                """
                INSERT INTO shadow_trades (
                    symbol, strategy, regime, signal_close_time_ms, status,
                    decision_ask_price, sim_entry_price, stop_price, target_price,
                    qty, risk_pct, risk_usdt, virtual_equity_before,
                    time_barrier_end_ms, ml_grade_silent, radar_signal_id, created_at_ms
                ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.symbol,
                    req.strategy,
                    req.regime,
                    int(req.signal_close_time_ms),
                    float(ask_dec),
                    entry_flt,
                    stop_flt,
                    target_flt,
                    to_api_str(req.sizing.base_qty),
                    float(req.sizing.effective_risk_pct),
                    format(risk_usdt, "f"),
                    format(to_decimal(virtual_equity_before), "f"),
                    int(req.time_barrier_end_ms),
                    req.sizing.ml_grade_silent,
                    req.radar_signal_id,
                    created_ms,
                ),
            )
            shadow_id = int(cur.lastrowid)

        return ExecutionReport(
            executed=True,
            mode="SHADOW_CHAMELEON",
            trade_id=shadow_id,
            client_order_id=f"SHD_{req.symbol}_{req.signal_close_time_ms}",
            status="OPEN",
            filled_qty=req.sizing.base_qty,
            avg_entry_price=sim_entry_dec,
        )

    def evaluate_open_shadow_trade(
        self,
        candles_by_symbol: dict[str, Sequence[CandleRow]],
    ) -> Optional[SingleTargetResolution]:
        """
        Verifica se a operação aberta em shadow_trades atingiu alvo, stop ou barreira de tempo
        usando resolve_single_target_horizon (C2-3).
        """
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE status = 'OPEN' LIMIT 1"
            ).fetchone()
        if row is None:
            return None

        symbol = str(row["symbol"])
        bars = candles_by_symbol.get(symbol, ())
        roundtrip_cost = (
            float(self.sizing_cfg.roundtrip_fee_assumption_pct)
            + float(self.sizing_cfg.stress_slippage_assumption_pct)
        )
        res = resolve_single_target_horizon(
            signal_close_time_ms=int(row["signal_close_time_ms"]),
            entry_price=float(row["sim_entry_price"]),
            stop_price=float(row["stop_price"]),
            target_price=float(row["target_price"]),
            time_barrier_end_ms=int(row["time_barrier_end_ms"]),
            candles=bars,
            roundtrip_cost_pct=roundtrip_cost,
        )
        if res.status in ("TARGET", "STOP", "SAME_CANDLE_STOP", "TIME"):
            with self.db.write_tx() as tx:
                tx.execute(
                    """
                    UPDATE shadow_trades
                    SET status = 'CLOSED',
                        exit_reason = ?,
                        exit_price = ?,
                        r_gross = ?,
                        r_net = ?,
                        closed_at_ms = ?
                    WHERE shadow_id = ?
                    """,
                    (
                        res.status,
                        res.exit_price,
                        res.r_gross,
                        res.r_net,
                        res.resolved_at_ms,
                        int(row["shadow_id"]),
                    ),
                )
            if self.shadow_manager is not None and res.r_net is not None:
                self.shadow_manager.record_trade_outcome(
                    net_r=float(res.r_net),
                    is_virtual_shadow_trade=True,
                )
        return res
