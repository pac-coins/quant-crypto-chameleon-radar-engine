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


def compute_depth_vwap_ask(
    asks: Sequence[tuple[float, float]],
    qty: Decimal,
) -> Optional[Decimal]:
    """
    Compute volume-weighted average ask price to buy `qty` base units across L2 ask levels (B2).
    Returns None if the book is empty or lacks sufficient liquidity.
    """
    target_qty = to_decimal(qty)
    if target_qty <= Decimal("0") or not asks:
        return None
    remaining = target_qty
    total_cost = Decimal("0")
    ordered = sorted(
        ((to_decimal(str(p)), to_decimal(str(q))) for p, q in asks),
        key=lambda lvl: lvl[0],
    )
    for price, level_qty in ordered:
        if price <= Decimal("0") or level_qty <= Decimal("0"):
            continue
        take = min(remaining, level_qty)
        total_cost += take * price
        remaining -= take
        if remaining <= Decimal("0"):
            break
    if remaining > Decimal("0"):
        return None
    return total_cost / target_qty


class PaperExecutor:
    """
    Executa operações em modo SHADOW_CHAMELEON (Gate 6B):
    - Lê o melhor ask real da corretora via /api/v3/ticker/bookTicker e, quando disponível,
      estima o preço médio ponderado por profundidade (VWAP L2) para `base_qty` (B2);
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

        depth_vwap_ask: Optional[Decimal] = None
        book_impact_bps: Optional[float] = None
        get_depth = getattr(self.public_client, "get_depth", None)
        if callable(get_depth):
            try:
                snap = get_depth(req.symbol, limit=20)
                if snap is not None and getattr(snap, "asks", None):
                    vwap = compute_depth_vwap_ask(snap.asks, req.sizing.base_qty)
                    if vwap is not None and vwap > Decimal("0"):
                        depth_vwap_ask = vwap
                        if ask_dec > Decimal("0"):
                            book_impact_bps = float(((vwap - ask_dec) / ask_dec) * Decimal("10000"))
            except Exception:
                depth_vwap_ask = None
                book_impact_bps = None

        effective_ask = (
            depth_vwap_ask
            if (depth_vwap_ask is not None and depth_vwap_ask >= ask_dec)
            else ask_dec
        )
        slip_half = Decimal(str(self.sizing_cfg.stress_slippage_assumption_pct)) / Decimal("2")
        sim_entry_dec = max(effective_ask, ask_dec * (Decimal("1") + slip_half))

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
                    decision_ask_price, depth_vwap_ask, book_impact_bps,
                    sim_entry_price, stop_price, target_price,
                    qty, risk_pct, risk_usdt, virtual_equity_before,
                    time_barrier_end_ms, ml_grade_silent, radar_signal_id, created_at_ms
                ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.symbol,
                    req.strategy,
                    req.regime,
                    int(req.signal_close_time_ms),
                    float(effective_ask),
                    float(depth_vwap_ask) if depth_vwap_ask is not None else None,
                    book_impact_bps,
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
            now_ms = int(res.resolved_at_ms if res.resolved_at_ms is not None else time.time() * 1000)
            eq_before = to_decimal(str(row["virtual_equity_before"]))
            risk_usdt = to_decimal(str(row["risk_usdt"]))
            if res.r_net is not None:
                pnl_usdt = (risk_usdt * Decimal(str(round(float(res.r_net), 6)))).quantize(
                    Decimal("0.01")
                )
                new_eq = max(Decimal("0.01"), eq_before + pnl_usdt)
            else:
                new_eq = eq_before
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
                tx.execute(
                    """
                    INSERT OR IGNORE INTO system_state(key, value, updated_at_ms)
                    VALUES ('initial_shadow_equity_usdt', ?, ?)
                    """,
                    (format(eq_before, "f"), now_ms),
                )
                tx.execute(
                    """
                    INSERT INTO system_state(key, value, updated_at_ms)
                    VALUES ('shadow_virtual_equity_usdt', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (format(new_eq, "f"), now_ms),
                )
            if self.shadow_manager is not None and res.r_net is not None:
                self.shadow_manager.record_trade_outcome(
                    net_r=float(res.r_net),
                    is_virtual_shadow_trade=True,
                )
        return res
