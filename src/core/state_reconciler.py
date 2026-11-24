"""Reconciliador de Estado e Cura no Boot (PENDING_ENTRY, OCO Parcial, Fallback -2010 e HALT_REVIEW C1-D) (Bloco 5)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Optional, Sequence

from src.core.binance_client import (
    BinanceAPIError,
    IPBannedError,
    Priority,
    SignedBinanceClient,
    floor_qty_to_step,
    passes_filters,
    to_api_str,
    to_decimal,
)
from src.core.config import FORBIDDEN_EXECUTION_SYMBOLS
from src.core.database import Database, DatabaseError, SymbolFilters
from src.execution.executor_protocol import ExitReason, TradeStatus
from src.execution.oco_order_manager import (
    ActiveTradeContext,
    OcoOrderManager,
    compute_oco_leg_prices,
    extract_fill_summary,
)
from src.risk.shadow_manager import AUTH_KILL_CODES, KillSwitchReason, ShadowManager

logger = logging.getLogger("state_reconciler")

EXTENDED_AUTH_KILL_CODES: frozenset[int] = AUTH_KILL_CODES | frozenset({-2014})


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
    HEALED_EXISTING_OCO = "HEALED_EXISTING_OCO"
    RECOVERED_CLOSING_TO_CLOSED = "RECOVERED_CLOSING_TO_CLOSED"
    HALT_REVIEW_PERSISTENT = "HALT_REVIEW_PERSISTENT"


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
    2. Resolve PENDING_ENTRY consultando origClientOrderId (jamais duplicando compra em -1007)
       e anexa OCO já ativa na corretora após falha C1-E / timeout (A1-03a);
    3. Cura estados CLOSING interrompidos (A1-01) e mantém HALT_REVIEW sob bloqueio até revisão humana;
    4. Protege posições em ENTRY_FILLED ou resíduos de OCO parcial (ex.: 40% executados -> protege 60%),
       reconsultando saldos após cancelar OCO EXECUTING (A1-06) e disparando venda MARKET imediata se o
       preço já tiver violado stopPrice ou takeProfit (-2010);
    5. Se encontrar saldo excedente não rastreado acima do minNotional — ou não puder comprovar que
       é poeira < minNotional (fail-closed, A1-10) — NUNCA vende a posição manual: entra em
       HALT_REVIEW e aciona o disjuntor (C1-D);
    6. Propaga falhas de API/autenticação para `ShadowManager.record_reconciliation_outcome(success=False)`
       (A2-01) e extrai PnL/exit_reason reais de OCOs concluídas (A2-02).
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

    def _best_effort_db_write(
        self,
        fn: Callable[[sqlite3.Connection], None],
        *,
        context: str,
        retries: int = 2,
    ) -> bool:
        """Escrita pós-fill best-effort (C1-E / FI-04): erro de SQLite nunca impede proteção de posição viva."""
        last_exc: Optional[Exception] = None
        for _ in range(max(1, retries)):
            try:
                with self.db.write_tx() as tx:
                    fn(tx)
                return True
            except (sqlite3.Error, DatabaseError) as exc:
                last_exc = exc
                continue
        logger.warning(
            "Best-effort SQLite write failed during reconciliation (%s); proceeding with exchange protection (C1-E): %s",
            context,
            last_exc,
        )
        return False

    def _record_success(self) -> None:
        if self.shadow_manager is not None:
            try:
                self.shadow_manager.record_reconciliation_outcome(success=True)
            except (sqlite3.Error, DatabaseError) as exc:
                logger.warning("Failed to persist reconciliation success in SQLite: %s", exc)

    def _record_failure(self, exc: Exception) -> None:
        code = exc.code if isinstance(exc, BinanceAPIError) else None
        is_immediate_halt = isinstance(exc, IPBannedError) or (
            code is not None and int(code) in EXTENDED_AUTH_KILL_CODES
        )
        try:
            if self.shadow_manager is not None:
                self.shadow_manager.record_reconciliation_outcome(
                    success=False,
                    binance_error_code=code,
                )
                if is_immediate_halt:
                    self.shadow_manager._trigger_halt(KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE)
            elif is_immediate_halt:
                self.db.set_state("emergency_halt", "1")
                self.db.set_state(
                    "kill_switch_reason",
                    KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE.value,
                )
        except (sqlite3.Error, DatabaseError) as db_exc:
            logger.warning("Failed to persist reconciliation failure state in SQLite: %s", db_exc)

    def _record_closed_trade_outcome_if_available(self, trade_id: int) -> None:
        if self.shadow_manager is None:
            return
        try:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT realized_r FROM trades WHERE trade_id = ?",
                    (trade_id,),
                ).fetchone()
            if row is not None and row["realized_r"] is not None:
                self.shadow_manager.record_trade_outcome(
                    net_r=float(row["realized_r"]),
                    is_virtual_shadow_trade=False,
                )
        except Exception as exc:
            logger.warning("Failed to record live trade outcome for trade #%s: %s", trade_id, exc)

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

    def _latest_candle_close(self, symbol: str) -> Optional[Decimal]:
        try:
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT close FROM candles WHERE symbol = ? ORDER BY close_time_ms DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
            if row is not None and row["close"] is not None:
                val = to_decimal(str(row["close"]))
                if val > Decimal("0"):
                    return val
        except Exception:
            return None
        return None

    def _resolve_live_ref_price(
        self,
        symbol: str,
        *,
        fallback_price: Optional[Decimal] = None,
    ) -> Optional[Decimal]:
        try:
            ticker = self.signed_client.get_book_ticker(symbol, priority=Priority.CRITICAL)
            if ticker.bid > Decimal("0"):
                return ticker.bid
        except BinanceAPIError as exc:
            if exc.code is not None and int(exc.code) in EXTENDED_AUTH_KILL_CODES:
                raise
        except Exception:
            pass
        candle_close = self._latest_candle_close(symbol)
        if candle_close is not None and candle_close > Decimal("0"):
            return candle_close
        if fallback_price is not None and fallback_price > Decimal("0"):
            return fallback_price
        return None

    def _find_open_oco_for_symbol(
        self,
        symbol: str,
        *,
        list_client_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Descobre OCO aberta na corretora via origClientOrderId e/ou GET /api/v3/openOrderList (A1-03a)."""
        if list_client_id:
            get_ol = getattr(self.signed_client, "get_order_list", None)
            if callable(get_ol):
                try:
                    resp = get_ol(orig_client_order_id=str(list_client_id))
                    if (
                        isinstance(resp, dict)
                        and resp.get("orderListId") is not None
                        and str(resp.get("listOrderStatus", "")).upper() != "ALL_DONE"
                    ):
                        return resp
                except BinanceAPIError as exc:
                    if exc.code is not None and int(exc.code) in EXTENDED_AUTH_KILL_CODES:
                        raise
                except TypeError:
                    pass
                except Exception:
                    pass

        get_open = getattr(self.signed_client, "get_open_order_lists", None)
        if callable(get_open):
            try:
                open_lists = get_open()
                if isinstance(open_lists, list):
                    matched_symbol: Optional[dict[str, Any]] = None
                    for ol in open_lists:
                        if not isinstance(ol, dict) or ol.get("orderListId") is None:
                            continue
                        if list_client_id and str(ol.get("listClientOrderId", "")) == str(list_client_id):
                            return ol
                        if str(ol.get("symbol", "")).upper() == symbol.upper():
                            matched_symbol = ol
                    if matched_symbol is not None:
                        return matched_symbol
            except BinanceAPIError as exc:
                if exc.code is not None and int(exc.code) in EXTENDED_AUTH_KILL_CODES:
                    raise
            except Exception as exc:
                logger.warning("get_open_order_lists failed while reconciling %s: %s", symbol, exc)
        return None

    def _heal_existing_oco_record(
        self,
        *,
        trade_id: int,
        symbol: str,
        open_oco: dict[str, Any],
        locked_qty: Decimal,
        exec_qty: Decimal,
        flt: SymbolFilters,
    ) -> ReconcileResult:
        """Anexa ao SQLite uma OCO já ativa na corretora (cura pós-C1-E / timeout — A1-03a)."""
        oco_id = int(open_oco["orderListId"])
        oco_coid = str(open_oco.get("listClientOrderId", "")) or None
        raw_qty = min(locked_qty, exec_qty) if exec_qty > Decimal("0") else locked_qty
        oco_qty = floor_qty_to_step(raw_qty, flt.step_size)
        now_ms = self._now_ms()
        with self.db.write_tx() as tx:
            tx.execute(
                """
                UPDATE trades
                SET status = ?,
                    oco_order_list_id = ?,
                    oco_list_client_id = COALESCE(?, oco_list_client_id),
                    oco_qty = ?,
                    updated_at_ms = ?
                WHERE trade_id = ?
                """,
                (
                    TradeStatus.PROTECTED.value,
                    oco_id,
                    oco_coid,
                    to_api_str(oco_qty),
                    now_ms,
                    trade_id,
                ),
            )
        self._record_success()
        return ReconcileResult(
            action=ReconcileAction.HEALED_EXISTING_OCO,
            trade_id=trade_id,
            symbol=symbol,
            protected_qty=oco_qty,
            details=f"Attached existing live OCO #{oco_id} after C1-E/timeout",
        )

    def _inspect_completed_oco_outcome(
        self,
        symbol: str,
        order_list: dict[str, Any],
    ) -> tuple[ExitReason, Decimal, Decimal]:
        """
        Extrai (exit_reason, executed_qty, quote_received) das pernas preenchidas de uma OCO concluída (A2-02).
        Suporta tanto `orderReports[]` quanto o schema oficial `orders[] -> GET /api/v3/order`.
        """
        legs: list[dict[str, Any]] = []
        order_reports = order_list.get("orderReports")
        if isinstance(order_reports, list) and len(order_reports) > 0:
            legs = [r for r in order_reports if isinstance(r, dict)]
        else:
            orders = order_list.get("orders")
            if isinstance(orders, list) and len(orders) > 0:
                get_order_fn = getattr(self.signed_client, "get_order", None)
                if callable(get_order_fn):
                    for leg in orders:
                        if not isinstance(leg, dict):
                            continue
                        try:
                            coid = leg.get("clientOrderId")
                            oid = leg.get("orderId")
                            if coid:
                                legs.append(get_order_fn(symbol, orig_client_order_id=str(coid)))
                            elif oid is not None:
                                legs.append(get_order_fn(symbol, order_id=int(oid)))
                        except BinanceAPIError as exc:
                            if exc.code is not None and int(exc.code) in EXTENDED_AUTH_KILL_CODES:
                                raise
                        except Exception as exc:
                            logger.warning(
                                "Failed to inspect completed OCO leg on %s (%s): %s",
                                symbol,
                                leg,
                                exc,
                            )

        exit_reason = ExitReason.RECONCILER
        total_exec = Decimal("0")
        total_quote = Decimal("0")
        for leg in legs:
            exec_q = to_decimal(str(leg.get("executedQty", "0")))
            cum_q = to_decimal(str(leg.get("cummulativeQuoteQty", "0")))
            if exec_q > Decimal("0"):
                total_exec += exec_q
                if cum_q > Decimal("0"):
                    total_quote += cum_q
                else:
                    leg_price = to_decimal(str(leg.get("price", "0")))
                    if leg_price > Decimal("0"):
                        total_quote += exec_q * leg_price
                leg_type = str(leg.get("type", "")).upper()
                if leg_type in ("LIMIT_MAKER", "LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"):
                    exit_reason = ExitReason.TARGET
                elif leg_type in ("STOP_LOSS_LIMIT", "STOP_LOSS"):
                    exit_reason = ExitReason.STOP
        return exit_reason, total_exec, total_quote

    def reconcile(self) -> ReconcileResult:
        """Executa o protocolo completo de reconciliação de estado com registro de falhas no ShadowManager (A2-01)."""
        try:
            return self._reconcile_impl()
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _reconcile_impl(self) -> ReconcileResult:
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
                    self._record_success()
                    return ReconcileResult(
                        action=ReconcileAction.REJECTED_MISSING_PENDING,
                        trade_id=trade_id,
                        symbol=symbol,
                        details="Order does not exist (-2013)",
                    )
                raise

            exec_qty, avg_price, cum_quote, _ = extract_fill_summary(
                order_resp, flt.base_asset
            )
            comm_base, comm_exact = self.oco_manager.resolve_base_commission(
                symbol, order_resp, flt.base_asset
            )
            if exec_qty <= Decimal("0"):
                with self.db.write_tx() as tx:
                    tx.execute(
                        "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                        (TradeStatus.ENTRY_REJECTED.value, self._now_ms(), trade_id),
                    )
                self._record_success()
                return ReconcileResult(
                    action=ReconcileAction.REJECTED_MISSING_PENDING,
                    trade_id=trade_id,
                    symbol=symbol,
                    details="Zero executedQty on recovered order",
                )

            entry_order_id = (
                int(order_resp["orderId"]) if order_resp.get("orderId") is not None else None
            )
            self._best_effort_db_write(
                lambda tx: tx.execute(
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
                ),
                context=f"PENDING_ENTRY->ENTRY_FILLED trade #{trade_id} ({symbol})",
            )

            # A1-03a: verifica se a OCO já foi criada na Binance (ex.: pós-C1-E ou timeout em POST OCO)
            balances = self._fetch_account_balances()
            free_b, locked_b = balances.get(flt.base_asset.upper(), (Decimal("0"), Decimal("0")))
            open_oco = self._find_open_oco_for_symbol(
                symbol,
                list_client_id=active_row["oco_list_client_id"],
            )
            if open_oco is not None and locked_b >= flt.min_qty:
                return self._heal_existing_oco_record(
                    trade_id=trade_id,
                    symbol=symbol,
                    open_oco=open_oco,
                    locked_qty=locked_b,
                    exec_qty=exec_qty,
                    flt=flt,
                )

            if free_b < flt.min_qty and locked_b < flt.min_qty:
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
                self._record_success()
                return ReconcileResult(
                    action=ReconcileAction.CLOSED_COMPLETED_OCO,
                    trade_id=trade_id,
                    symbol=symbol,
                    details="Recovered PENDING_ENTRY has zero remaining balance on exchange",
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
                commission_exact=comm_exact,
            )
            rep = self.oco_manager.protect_or_fallback_market(ctx, flt)
            if rep.status == TradeStatus.CLOSED.value:
                self._record_closed_trade_outcome_if_available(trade_id)
                self._record_success()
                return ReconcileResult(
                    action=ReconcileAction.RECOVERED_PENDING_TO_MARKET_FALLBACK,
                    trade_id=trade_id,
                    symbol=symbol,
                    protected_qty=rep.filled_qty,
                    details="Price breached stopPrice/target on boot -> emergency MARKET sell",
                )
            self._record_success()
            return ReconcileResult(
                action=ReconcileAction.RECOVERED_PENDING_TO_PROTECTED,
                trade_id=trade_id,
                symbol=symbol,
                protected_qty=rep.filled_qty,
            )

        balances = self._fetch_account_balances()

        # 2. Verifica saldo excedente não rastreado nos 7 ativos da cesta (Regra C1-D / A1-10 fail-closed)
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
                fallback_px = (
                    to_decimal(str(active_row["planned_entry_price"]))
                    if active_row is not None
                    and sym == tracked_symbol
                    and active_row["planned_entry_price"]
                    else None
                )
                ref_price = self._resolve_live_ref_price(sym, fallback_price=fallback_px)

                # A1-10 (fail-closed): se não conseguimos provar que excess_b é poeira < min_notional,
                # OU se excess_b * ref_price >= min_notional, acionar HALT_REVIEW C1-D!
                if ref_price is None or ref_price <= Decimal("0") or (excess_b * ref_price) >= flt.min_notional:
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
                    else:
                        self.db.set_state("emergency_halt", "1")
                        self.db.set_state(
                            "kill_switch_reason",
                            KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE.value,
                        )
                    reason_detail = (
                        f"Untracked balance {excess_b} {flt.base_asset} (ticker/price unavailable — fail-closed C1-D)"
                        if ref_price is None or ref_price <= Decimal("0")
                        else f"Untracked balance {excess_b} {flt.base_asset} >= minNotional (C1-D)"
                    )
                    return ReconcileResult(
                        action=ReconcileAction.HALT_REVIEW_UNTRACKED_BALANCE,
                        trade_id=int(active_row["trade_id"]) if active_row is not None else None,
                        symbol=sym,
                        untracked_qty=excess_b,
                        details=reason_detail,
                    )

        if active_row is None:
            self._record_success()
            return ReconcileResult(action=ReconcileAction.CLEAN, trade_id=None, symbol=None)

        trade_id = int(active_row["trade_id"])
        symbol = str(active_row["symbol"])
        status = str(active_row["status"])
        client_order_id = str(active_row["client_order_id"])
        flt = self.filters_provider(symbol)

        # 3. Caso HALT_REVIEW (A1-01): nunca retornar CLEAN nem resetar halt até revisão humana
        if status == TradeStatus.HALT_REVIEW.value:
            if self.shadow_manager is not None:
                self.shadow_manager._trigger_halt(KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE)
            else:
                self.db.set_state("emergency_halt", "1")
                self.db.set_state(
                    "kill_switch_reason",
                    KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE.value,
                )
            return ReconcileResult(
                action=ReconcileAction.HALT_REVIEW_PERSISTENT,
                trade_id=trade_id,
                symbol=symbol,
                details="Trade in HALT_REVIEW requires manual human inspection",
            )

        # 4. Caso CLOSING (A1-01): Time-Stop/Watchdog interrompido após cancelar OCO antes da venda a mercado
        if status == TradeStatus.CLOSING.value:
            oco_id = (
                int(active_row["oco_order_list_id"])
                if active_row["oco_order_list_id"] is not None
                else None
            )
            free_b, locked_b = balances.get(flt.base_asset.upper(), (Decimal("0"), Decimal("0")))
            with self.signed_client.governor.critical_section():
                if oco_id is not None:
                    order_list = self.signed_client.get_order_list(oco_id)
                    if str(order_list.get("listOrderStatus", "")).upper() == "EXECUTING":
                        try:
                            self.signed_client.cancel_order_list(symbol, oco_id)
                        except BinanceAPIError as cancel_exc:
                            if cancel_exc.code not in (-2011, -2013):
                                raise
                        balances = self._fetch_account_balances()
                        free_b, locked_b = balances.get(
                            flt.base_asset.upper(), (Decimal("0"), Decimal("0"))
                        )

                q_rem = floor_qty_to_step(
                    min(free_b, tracked_qty) if tracked_qty > Decimal("0") else Decimal("0"),
                    flt.step_size,
                )
                planned_entry = to_decimal(str(active_row["planned_entry_price"]))
                ref_price = self._resolve_live_ref_price(symbol, fallback_price=planned_entry)
                if q_rem >= flt.min_qty and (
                    ref_price is None or (q_rem * ref_price) >= flt.min_notional
                ):
                    orig_qty = to_decimal(str(active_row["entry_executed_qty"] or tracked_qty or q_rem))
                    quote_spent = to_decimal(str(active_row["entry_quote_spent"] or "0"))
                    prorated_quote_spent = (
                        quote_spent * (q_rem / orig_qty)
                        if orig_qty > Decimal("0") and q_rem < orig_qty
                        else quote_spent
                    )
                    ctx = ActiveTradeContext(
                        trade_id=trade_id,
                        client_order_id=client_order_id,
                        symbol=symbol,
                        base_asset=flt.base_asset,
                        status=TradeStatus.CLOSING.value,
                        entry_order_id=(
                            int(active_row["entry_order_id"])
                            if active_row["entry_order_id"] is not None
                            else None
                        ),
                        executed_qty=q_rem,
                        avg_entry_price=to_decimal(
                            str(active_row["entry_avg_price"] or active_row["planned_entry_price"])
                        ),
                        quote_spent=prorated_quote_spent,
                        commission_base_asset=Decimal("0"),
                        planned_stop_price=to_decimal(str(active_row["planned_stop_price"])),
                        planned_target_price=to_decimal(str(active_row["planned_target_price"])),
                        time_barrier_end_ms=int(active_row["time_barrier_end_ms"]),
                    )
                    self.oco_manager._execute_emergency_market_sell(
                        ctx, q_rem, reason=ExitReason.RECONCILER
                    )
                    self._record_closed_trade_outcome_if_available(trade_id)
                    self._record_success()
                    return ReconcileResult(
                        action=ReconcileAction.RECOVERED_CLOSING_TO_CLOSED,
                        trade_id=trade_id,
                        symbol=symbol,
                        protected_qty=q_rem,
                        details="Liquidated unprotected CLOSING balance at market",
                    )

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
                self._record_success()
                return ReconcileResult(
                    action=ReconcileAction.RECOVERED_CLOSING_TO_CLOSED,
                    trade_id=trade_id,
                    symbol=symbol,
                    details="Finalized CLOSING trade with no sellable remainder",
                )

        # 5. Caso ENTRY_FILLED sem OCO ativa (ex.: falha após fill antes de criar OCO ou pós-C1-E)
        if status == TradeStatus.ENTRY_FILLED.value:
            exec_qty = to_decimal(str(active_row["entry_executed_qty"] or "0"))
            avg_price = to_decimal(
                str(active_row["entry_avg_price"] or active_row["planned_entry_price"])
            )
            cum_quote = to_decimal(str(active_row["entry_quote_spent"] or "0"))
            comm_base = to_decimal(str(active_row["commission_base_asset"] or "0"))
            free_b, locked_b = balances.get(flt.base_asset.upper(), (Decimal("0"), Decimal("0")))

            # A1-03a: verifica primeiro se a OCO já existe na corretora (C1-E / timeout de POST OCO)
            open_oco = self._find_open_oco_for_symbol(
                symbol,
                list_client_id=active_row["oco_list_client_id"],
            )
            if open_oco is not None and locked_b >= flt.min_qty:
                return self._heal_existing_oco_record(
                    trade_id=trade_id,
                    symbol=symbol,
                    open_oco=open_oco,
                    locked_qty=locked_b,
                    exec_qty=exec_qty,
                    flt=flt,
                )

            if free_b < flt.min_qty and locked_b < flt.min_qty:
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
                self._record_success()
                return ReconcileResult(
                    action=ReconcileAction.CLOSED_COMPLETED_OCO,
                    trade_id=trade_id,
                    symbol=symbol,
                    details="ENTRY_FILLED trade has zero remaining balance on exchange",
                )

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
                self._record_closed_trade_outcome_if_available(trade_id)
                self._record_success()
                return ReconcileResult(
                    action=ReconcileAction.RECOVERED_PENDING_TO_MARKET_FALLBACK,
                    trade_id=trade_id,
                    symbol=symbol,
                    protected_qty=rep.filled_qty,
                )
            self._record_success()
            return ReconcileResult(
                action=ReconcileAction.PROTECTED_UNPROTECTED_FILL,
                trade_id=trade_id,
                symbol=symbol,
                protected_qty=rep.filled_qty,
            )

        # 6. Caso PROTECTED: verifica integridade da OCO, cura execução parcial (A1-06) e calcula PnL real (A2-02)
        if status == TradeStatus.PROTECTED.value:
            oco_id = (
                int(active_row["oco_order_list_id"])
                if active_row["oco_order_list_id"] is not None
                else None
            )
            free_b, locked_b = balances.get(flt.base_asset.upper(), (Decimal("0"), Decimal("0")))
            order_list: dict[str, Any] = {}
            if oco_id is not None:
                # A2-01: NÃO engolir BinanceAPIError aqui (inclusive -2015 / -1022 / -2014 / 5XX)!
                order_list = self.signed_client.get_order_list(oco_id)
            else:
                open_oco = self._find_open_oco_for_symbol(
                    symbol,
                    list_client_id=active_row["oco_list_client_id"],
                )
                if open_oco is not None and locked_b >= flt.min_qty:
                    return self._heal_existing_oco_record(
                        trade_id=trade_id,
                        symbol=symbol,
                        open_oco=open_oco,
                        locked_qty=locked_b,
                        exec_qty=tracked_qty,
                        flt=flt,
                    )

            list_status = str(order_list.get("listOrderStatus", "")).upper()
            # A2-02b: avalia o notional do resíduo livre pelo preço atual (bid), alinhado ao Step 2 C1-D
            unprotected_tracked = floor_qty_to_step(min(free_b, tracked_qty), flt.step_size)
            planned_entry = to_decimal(str(active_row["planned_entry_price"]))
            ref_price = (
                self._resolve_live_ref_price(symbol, fallback_price=planned_entry) or planned_entry
            )

            if unprotected_tracked >= flt.min_qty and (unprotected_tracked * ref_price) >= flt.min_notional:
                qty_to_protect = unprotected_tracked
                # A1-06: ao cancelar OCO EXECUTING, reconsulta saldos para proteger TAMBÉM a fração destravada!
                if oco_id is not None and list_status == "EXECUTING":
                    with self.signed_client.governor.critical_section():
                        try:
                            self.signed_client.cancel_order_list(symbol, oco_id)
                        except BinanceAPIError as cancel_exc:
                            if cancel_exc.code not in (-2011, -2013):
                                raise
                        balances = self._fetch_account_balances()
                        free_b, locked_b = balances.get(
                            flt.base_asset.upper(), (Decimal("0"), Decimal("0"))
                        )
                        qty_to_protect = floor_qty_to_step(
                            min(free_b, tracked_qty), flt.step_size
                        )

                if qty_to_protect >= flt.min_qty and (qty_to_protect * ref_price) >= flt.min_notional:
                    orig_qty = to_decimal(
                        str(active_row["entry_executed_qty"] or tracked_qty or qty_to_protect)
                    )
                    entry_quote_spent = to_decimal(str(active_row["entry_quote_spent"] or "0"))
                    prorated_quote_spent = (
                        entry_quote_spent * (qty_to_protect / orig_qty)
                        if orig_qty > Decimal("0") and qty_to_protect < orig_qty
                        else entry_quote_spent
                    )
                    planned_stop = to_decimal(str(active_row["planned_stop_price"]))
                    planned_target = to_decimal(str(active_row["planned_target_price"]))
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
                        executed_qty=qty_to_protect,
                        avg_entry_price=to_decimal(
                            str(active_row["entry_avg_price"] or active_row["planned_entry_price"])
                        ),
                        quote_spent=prorated_quote_spent,
                        commission_base_asset=Decimal("0"),
                        planned_stop_price=planned_stop,
                        planned_target_price=planned_target,
                        time_barrier_end_ms=int(active_row["time_barrier_end_ms"]),
                    )

                    # Se resíduo valorizado tem notional >= min_notional no bid mas falharia NOTIONAL
                    # na perna STOP_LOSS_LIMIT (e não está acima de TP), liquida o resíduo limpo a mercado.
                    tp_px, stop_px, stop_lim_px = compute_oco_leg_prices(
                        planned_target=planned_target,
                        planned_stop=planned_stop,
                        tick_size=flt.tick_size,
                        below_type=self.oco_manager.resilience_cfg.oco_below_type,
                        stop_limit_buffer_pct=self.oco_manager.resilience_cfg.oco_stop_limit_buffer_pct,
                    )
                    stop_leg_px = stop_lim_px if stop_lim_px is not None else stop_px
                    stop_leg_ok, _ = passes_filters(stop_leg_px, qty_to_protect, flt)
                    if not stop_leg_ok and ref_price < tp_px:
                        rep = self.oco_manager._execute_emergency_market_sell(
                            ctx, qty_to_protect, reason=ExitReason.RECONCILER
                        )
                    else:
                        rep = self.oco_manager.protect_or_fallback_market(
                            ctx, flt, override_qty=qty_to_protect
                        )

                    if rep.status == TradeStatus.CLOSED.value:
                        self._record_closed_trade_outcome_if_available(trade_id)
                        self._record_success()
                        return ReconcileResult(
                            action=(
                                ReconcileAction.CLOSED_COMPLETED_OCO
                                if list_status == "ALL_DONE"
                                else ReconcileAction.RECOVERED_PENDING_TO_MARKET_FALLBACK
                            ),
                            trade_id=trade_id,
                            symbol=symbol,
                            protected_qty=rep.filled_qty,
                        )
                    self._record_success()
                    return ReconcileResult(
                        action=ReconcileAction.REPROTECTED_PARTIAL_OCO_RESIDUAL,
                        trade_id=trade_id,
                        symbol=symbol,
                        protected_qty=rep.filled_qty,
                    )

            if list_status == "ALL_DONE" and locked_b <= flt.min_qty:
                now_ms = self._now_ms()
                exit_reason, oco_exec_qty, oco_quote_rec = self._inspect_completed_oco_outcome(
                    symbol, order_list
                )
                realized_r: Optional[float] = None
                if oco_exec_qty > Decimal("0") and oco_quote_rec > Decimal("0"):
                    exit_avg = oco_quote_rec / oco_exec_qty
                    entry_avg = to_decimal(
                        str(active_row["entry_avg_price"] or active_row["planned_entry_price"])
                    )
                    planned_stop = to_decimal(str(active_row["planned_stop_price"]))
                    entry_quote_spent = to_decimal(str(active_row["entry_quote_spent"] or "0"))
                    orig_qty = to_decimal(str(active_row["entry_executed_qty"] or oco_exec_qty))
                    prorated_cost = (
                        entry_quote_spent * (oco_exec_qty / orig_qty)
                        if orig_qty > Decimal("0") and oco_exec_qty < orig_qty
                        else entry_quote_spent
                    )
                    pnl_usdt = oco_quote_rec - prorated_cost
                    risk_unit = max(Decimal("0.00000001"), entry_avg - planned_stop)
                    entry_fee_usdt = self.oco_manager._load_existing_trade_fee_usdt(
                        trade_id,
                        symbol=symbol,
                        base_asset=flt.base_asset,
                        entry_order_id=(
                            int(active_row["entry_order_id"])
                            if active_row["entry_order_id"] is not None
                            else None
                        ),
                        entry_avg_price=entry_avg,
                        allow_network=True,
                    )
                    exit_fee_usdt = Decimal("0")
                    legs = order_list.get("orderReports") or order_list.get("orders") or []
                    if isinstance(legs, list):
                        for leg in legs:
                            if isinstance(leg, dict):
                                exit_fee_usdt += self.oco_manager.resolve_order_fee_usdt(
                                    symbol,
                                    leg,
                                    base_asset=flt.base_asset,
                                    base_price_usdt=exit_avg,
                                    exclude_base_asset=False,
                                    allow_network=True,
                                )
                    total_fees_usdt = (entry_fee_usdt + exit_fee_usdt).quantize(Decimal("0.00000001"))
                    if total_fees_usdt > Decimal("0"):
                        risk_total_usdt = max(Decimal("0.00000001"), risk_unit * oco_exec_qty)
                        realized_r = float((pnl_usdt - total_fees_usdt) / risk_total_usdt)
                    else:
                        realized_r = float((exit_avg - entry_avg) / risk_unit)
                    with self.db.write_tx() as tx:
                        tx.execute(
                            """
                            UPDATE trades
                            SET status = ?,
                                exit_reason = ?,
                                exit_avg_price = ?,
                                exit_quote_received = ?,
                                fees_usdt_equiv = ?,
                                realized_pnl_usdt = ?,
                                realized_r = ?,
                                updated_at_ms = ?,
                                closed_at_ms = COALESCE(closed_at_ms, ?)
                            WHERE trade_id = ?
                            """,
                            (
                                TradeStatus.CLOSED.value,
                                exit_reason.value,
                                to_api_str(exit_avg),
                                to_api_str(oco_quote_rec),
                                to_api_str(total_fees_usdt),
                                to_api_str(pnl_usdt),
                                realized_r,
                                now_ms,
                                now_ms,
                                trade_id,
                            ),
                        )
                    if self.shadow_manager is not None:
                        self.shadow_manager.record_trade_outcome(
                            net_r=realized_r,
                            is_virtual_shadow_trade=False,
                        )
                else:
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
                                exit_reason.value,
                                now_ms,
                                now_ms,
                                trade_id,
                            ),
                        )
                self._record_success()
                return ReconcileResult(
                    action=ReconcileAction.CLOSED_COMPLETED_OCO,
                    trade_id=trade_id,
                    symbol=symbol,
                )

        self._record_success()
        return ReconcileResult(action=ReconcileAction.CLEAN, trade_id=trade_id, symbol=symbol)
