"""Reconciliador de Estado e Cura no Boot (PENDING_ENTRY, OCO Parcial, Fallback -2010 e HALT_REVIEW C1-D) (Bloco 5)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Optional, Sequence

from src.core.binance_client import (
    BinanceAPIError,
    Priority,
    SignedBinanceClient,
    floor_qty_to_step,
    to_api_str,
    to_decimal,
)
from src.core.config import FORBIDDEN_EXECUTION_SYMBOLS
from src.core.database import Database, SymbolFilters
from src.execution.executor_protocol import ExitReason, TradeStatus
from src.execution.oco_order_manager import (
    ActiveTradeContext,
    OcoOrderManager,
    extract_fill_summary,
)
from src.risk.shadow_manager import KillSwitchReason, ShadowManager


class ReconcileAction(StrEnum):
    """Ações determinísticas tomadas pelo StateReconciler."""

    CLEAN = "CLEAN"
    RECOVERED_PENDING_TO_PROTECTED = "RECOVERED_PENDING_TO_PROTECTED"
    RECOVERED_PENDING_TO_MARKET_FALLBACK = "RECOVERED_PENDING_TO_MARKET_FALLBACK"
    REJECTED_MISSING_PENDING = "REJECTED_MISSING_PENDING"
    PROTECTED_UNPROTECTED_FILL = "PROTECTED_UNPROTECTED_FILL"
    REPROTECTED_PARTIAL_OCO_RESIDUAL = "REPROTECTED_PARTIAL_OCO_RESIDUAL"
    CLOSED_COMPLETED_OCO = "CLOSED_COMPLETED_OCO"
    HALT_REVIEW_UNTRACKED_BALANCE = "HALT_REVIEW_UNTRACKED_BALANCE"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Relatório auditável da execução do StateReconciler."""

    action: ReconcileAction
    trade_id: Optional[int]
    symbol: Optional[str]
    protected_qty: Decimal = Decimal("0")
    untracked_qty: Decimal = Decimal("0")
    details: str = ""


class StateReconciler:
    """
    Executa a cura determinística no boot e a cada ciclo sobre execution_universe (7 moedas):
    1. Nunca toca em BNBUSDT nem em moedas fora de execution_universe (C1-C);
    2. Resolve PENDING_ENTRY consultando origClientOrderId (jamais duplicando compra em -1007);
    3. Protege posições em ENTRY_FILLED ou resíduos de OCO parcial (ex.: 40% executados -> protege 60%),
       disparando venda MARKET imediata se o preço já tiver violado o stopPrice (-2010);
    4. Se encontrar saldo excedente não rastreado acima do minNotional (ex.: compra manual na mesma conta),
       NUNCA vende a posição manual: entra em HALT_REVIEW e aciona o disjuntor (C1-D).
    """

    def __init__(
        self,
        *,
        db: Database,
        signed_client: SignedBinanceClient,
        oco_manager: OcoOrderManager,
        execution_universe: Sequence[str],
        filters_provider: Callable[[str], SymbolFilters],
        shadow_manager: Optional[ShadowManager] = None,
    ) -> None:
        forbidden = set(execution_universe) & set(FORBIDDEN_EXECUTION_SYMBOLS)
        if forbidden:
            raise ValueError(f"Forbidden symbols in StateReconciler universe (C1-C): {forbidden}")
        self.db = db
        self.signed_client = signed_client
        self.oco_manager = oco_manager
        self.execution_universe = tuple(dict.fromkeys(execution_universe))
        self.filters_provider = filters_provider
        self.shadow_manager = shadow_manager

    def _now_ms(self) -> int:
        return int(self.signed_client.clock.server_now_ms())

    def _fetch_account_balances(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Retorna mapa asset -> (free, locked) em Decimal."""
        acct = self.signed_client.get_account(omit_zero_balances=False)
        out: dict[str, tuple[Decimal, Decimal]] = {}
        for b in acct.get("balances", []):
            if isinstance(b, dict) and "asset" in b:
                asset = str(b["asset"]).upper()
                free_d = to_decimal(str(b.get("free", "0")))
                locked_d = to_decimal(str(b.get("locked", "0")))
                out[asset] = (free_d, locked_d)
        return out

    def reconcile(self) -> ReconcileResult:
        """Executa o protocolo completo de reconciliação de estado."""
        with self.db.read() as conn:
            active_row = conn.execute(
                """
                SELECT * FROM trades
                WHERE status IN ('PENDING_ENTRY', 'ENTRY_FILLED', 'PROTECTED', 'CLOSING', 'HALT_REVIEW')
                LIMIT 1
                """
            ).fetchone()

        # 1. Caso PENDING_ENTRY: resolve primeiro via origClientOrderId (pois o fill na corretora
        # já gerou saldo real no ativo base antes de o SQLite saber o entry_executed_qty!)
        if active_row is not None and str(active_row["status"]) == TradeStatus.PENDING_ENTRY.value:
            trade_id = int(active_row["trade_id"])
            symbol = str(active_row["symbol"])
            client_order_id = str(active_row["client_order_id"])
            flt = self.filters_provider(symbol)
            try:
                order_resp = self.signed_client.get_order(
                    symbol, orig_client_order_id=client_order_id
                )
            except BinanceAPIError as exc:
                if exc.code == -2013:
                    with self.db.write_tx() as tx:
                        tx.execute(
                            "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                            (TradeStatus.ENTRY_REJECTED.value, self._now_ms(), trade_id),
                        )
                    return ReconcileResult(
                        action=ReconcileAction.REJECTED_MISSING_PENDING,
                        trade_id=trade_id,
                        symbol=symbol,
                        details="Order does not exist (-2013)",
                    )
                raise

            exec_qty, avg_price, cum_quote, comm_base = extract_fill_summary(
                order_resp, flt.base_asset
            )
            if exec_qty <= Decimal("0"):
                with self.db.write_tx() as tx:
                    tx.execute(
                        "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                        (TradeStatus.ENTRY_REJECTED.value, self._now_ms(), trade_id),
                    )
                return ReconcileResult(
                    action=ReconcileAction.REJECTED_MISSING_PENDING,
                    trade_id=trade_id,
                    symbol=symbol,
                    details="Zero executedQty on recovered order",
                )

            entry_order_id = (
                int(order_resp["orderId"]) if order_resp.get("orderId") is not None else None
            )
            with self.db.write_tx() as tx:
                tx.execute(
                    """
                    UPDATE trades
                    SET status = ?,
                        entry_order_id = ?,
                        entry_executed_qty = ?,
                        entry_avg_price = ?,
                        entry_quote_spent = ?,
                        commission_base_asset = ?,
                        updated_at_ms = ?
                    WHERE trade_id = ?
                    """,
                    (
                        TradeStatus.ENTRY_FILLED.value,
                        entry_order_id,
                        to_api_str(exec_qty),
                        to_api_str(avg_price),
                        to_api_str(cum_quote),
                        to_api_str(comm_base),
                        self._now_ms(),
                        trade_id,
                    ),
                )

            ctx = ActiveTradeContext(
                trade_id=trade_id,
                client_order_id=client_order_id,
                symbol=symbol,
                base_asset=flt.base_asset,
                status=TradeStatus.ENTRY_FILLED.value,
                entry_order_id=entry_order_id,
                executed_qty=exec_qty,
                avg_entry_price=avg_price,
                quote_spent=cum_quote,
                commission_base_asset=comm_base,
                planned_stop_price=to_decimal(str(active_row["planned_stop_price"])),
                planned_target_price=to_decimal(str(active_row["planned_target_price"])),
                time_barrier_end_ms=int(active_row["time_barrier_end_ms"]),
            )
            rep = self.oco_manager.protect_or_fallback_market(ctx, flt)
            if rep.status == TradeStatus.CLOSED.value:
                return ReconcileResult(
                    action=ReconcileAction.RECOVERED_PENDING_TO_MARKET_FALLBACK,
                    trade_id=trade_id,
                    symbol=symbol,
                    protected_qty=rep.filled_qty,
                    details="Price breached stopPrice on boot -> emergency MARKET sell",
                )
            return ReconcileResult(
                action=ReconcileAction.RECOVERED_PENDING_TO_PROTECTED,
                trade_id=trade_id,
                symbol=symbol,
                protected_qty=rep.filled_qty,
            )

        balances = self._fetch_account_balances()

        # 2. Verifica saldo excedente não rastreado nos 7 ativos da cesta (Regra C1-D)
        tracked_symbol = str(active_row["symbol"]) if active_row is not None else None
        tracked_qty = Decimal("0")
        if active_row is not None:
            if active_row["oco_qty"] is not None:
                tracked_qty = to_decimal(str(active_row["oco_qty"]))
            elif active_row["entry_executed_qty"] is not None:
                tracked_qty = to_decimal(str(active_row["entry_executed_qty"]))

        for sym in self.execution_universe:
            flt = self.filters_provider(sym)
            free_b, locked_b = balances.get(flt.base_asset.upper(), (Decimal("0"), Decimal("0")))
            total_b = free_b + locked_b
            expected_b = tracked_qty if sym == tracked_symbol else Decimal("0")
            excess_b = total_b - expected_b
            if excess_b > flt.min_qty:
                ref_price = Decimal("0")
                if active_row is not None and sym == tracked_symbol and active_row["planned_entry_price"]:
                    ref_price = to_decimal(str(active_row["planned_entry_price"]))
                else:
                    try:
                        ticker = self.signed_client.get_book_ticker(sym, priority=Priority.CRITICAL)
                        ref_price = ticker.bid
                    except Exception:
                        ref_price = Decimal("0")

                if ref_price > Decimal("0") and (excess_b * ref_price) >= flt.min_notional:
                    # Regra C1-D: JAMAIS vender saldo não rastreado! Entrar em HALT_REVIEW.
                    if active_row is not None:
                        with self.db.write_tx() as tx:
                            tx.execute(
                                "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                                (
                                    TradeStatus.HALT_REVIEW.value,
                                    self._now_ms(),
                                    int(active_row["trade_id"]),
                                ),
                            )
                    if self.shadow_manager is not None:
                        self.shadow_manager._trigger_halt(
                            KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE
                        )
                    return ReconcileResult(
                        action=ReconcileAction.HALT_REVIEW_UNTRACKED_BALANCE,
                        trade_id=int(active_row["trade_id"]) if active_row is not None else None,
                        symbol=sym,
                        untracked_qty=excess_b,
                        details=f"Untracked balance {excess_b} {flt.base_asset} >= minNotional (C1-D)",
                    )

        if active_row is None:
            if self.shadow_manager is not None:
                self.shadow_manager.record_reconciliation_outcome(success=True)
            return ReconcileResult(action=ReconcileAction.CLEAN, trade_id=None, symbol=None)

        trade_id = int(active_row["trade_id"])
        symbol = str(active_row["symbol"])
        status = str(active_row["status"])
        client_order_id = str(active_row["client_order_id"])
        flt = self.filters_provider(symbol)

        # 3. Caso ENTRY_FILLED sem OCO ativa (ex.: falha após fill antes de criar OCO)
        if status == TradeStatus.ENTRY_FILLED.value:
            exec_qty = to_decimal(str(active_row["entry_executed_qty"] or "0"))
            avg_price = to_decimal(
                str(active_row["entry_avg_price"] or active_row["planned_entry_price"])
            )
            cum_quote = to_decimal(str(active_row["entry_quote_spent"] or "0"))
            comm_base = to_decimal(str(active_row["commission_base_asset"] or "0"))
            ctx = ActiveTradeContext(
                trade_id=trade_id,
                client_order_id=client_order_id,
                symbol=symbol,
                base_asset=flt.base_asset,
                status=TradeStatus.ENTRY_FILLED.value,
                entry_order_id=(
                    int(active_row["entry_order_id"])
                    if active_row["entry_order_id"] is not None
                    else None
                ),
                executed_qty=exec_qty,
                avg_entry_price=avg_price,
                quote_spent=cum_quote,
                commission_base_asset=comm_base,
                planned_stop_price=to_decimal(str(active_row["planned_stop_price"])),
                planned_target_price=to_decimal(str(active_row["planned_target_price"])),
                time_barrier_end_ms=int(active_row["time_barrier_end_ms"]),
            )
            rep = self.oco_manager.protect_or_fallback_market(ctx, flt)
            if rep.status == TradeStatus.CLOSED.value:
                return ReconcileResult(
                    action=ReconcileAction.RECOVERED_PENDING_TO_MARKET_FALLBACK,
                    trade_id=trade_id,
                    symbol=symbol,
                    protected_qty=rep.filled_qty,
                )
            return ReconcileResult(
                action=ReconcileAction.PROTECTED_UNPROTECTED_FILL,
                trade_id=trade_id,
                symbol=symbol,
                protected_qty=rep.filled_qty,
            )

        # 4. Caso PROTECTED: verifica integridade da OCO e cura execução parcial (ex.: 40% executados -> protege 60%)
        if status == TradeStatus.PROTECTED.value:
            oco_id = (
                int(active_row["oco_order_list_id"])
                if active_row["oco_order_list_id"] is not None
                else None
            )
            free_b, locked_b = balances.get(flt.base_asset.upper(), (Decimal("0"), Decimal("0")))
            order_list: dict[str, Any] = {}
            if oco_id is not None:
                try:
                    order_list = self.signed_client.get_order_list(oco_id)
                except BinanceAPIError:
                    order_list = {}

            list_status = str(order_list.get("listOrderStatus", ""))
            # Se a OCO executou parcialmente e tem saldo livre desprotegido acima de minNotional:
            unprotected_tracked = floor_qty_to_step(min(free_b, tracked_qty), flt.step_size)
            planned_entry = to_decimal(str(active_row["planned_entry_price"]))

            if unprotected_tracked >= flt.min_qty and (unprotected_tracked * planned_entry) >= flt.min_notional:
                # Cancela eventual resto travado da OCO antiga antes de recriar OCO para o saldo residual
                if oco_id is not None and list_status == "EXECUTING":
                    try:
                        self.signed_client.cancel_order_list(symbol, oco_id)
                    except BinanceAPIError:
                        pass

                ctx = ActiveTradeContext(
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    base_asset=flt.base_asset,
                    status=TradeStatus.PROTECTED.value,
                    entry_order_id=(
                        int(active_row["entry_order_id"])
                        if active_row["entry_order_id"] is not None
                        else None
                    ),
                    executed_qty=unprotected_tracked,
                    avg_entry_price=to_decimal(
                        str(active_row["entry_avg_price"] or active_row["planned_entry_price"])
                    ),
                    quote_spent=to_decimal(str(active_row["entry_quote_spent"] or "0")),
                    commission_base_asset=Decimal("0"),
                    planned_stop_price=to_decimal(str(active_row["planned_stop_price"])),
                    planned_target_price=to_decimal(str(active_row["planned_target_price"])),
                    time_barrier_end_ms=int(active_row["time_barrier_end_ms"]),
                )
                rep = self.oco_manager.protect_or_fallback_market(
                    ctx, flt, override_qty=unprotected_tracked
                )
                return ReconcileResult(
                    action=ReconcileAction.REPROTECTED_PARTIAL_OCO_RESIDUAL,
                    trade_id=trade_id,
                    symbol=symbol,
                    protected_qty=rep.filled_qty,
                )

            if list_status == "ALL_DONE" and locked_b <= flt.min_qty:
                now_ms = self._now_ms()
                with self.db.write_tx() as tx:
                    tx.execute(
                        """
                        UPDATE trades
                        SET status = ?, exit_reason = COALESCE(exit_reason, ?),
                            updated_at_ms = ?, closed_at_ms = COALESCE(closed_at_ms, ?)
                        WHERE trade_id = ?
                        """,
                        (
                            TradeStatus.CLOSED.value,
                            ExitReason.RECONCILER.value,
                            now_ms,
                            now_ms,
                            trade_id,
                        ),
                    )
                return ReconcileResult(
                    action=ReconcileAction.CLOSED_COMPLETED_OCO,
                    trade_id=trade_id,
                    symbol=symbol,
                )

        if self.shadow_manager is not None:
            self.shadow_manager.record_reconciliation_outcome(success=True)
        return ReconcileResult(action=ReconcileAction.CLEAN, trade_id=trade_id, symbol=symbol)
