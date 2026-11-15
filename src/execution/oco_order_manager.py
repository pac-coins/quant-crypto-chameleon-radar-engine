"""Gerenciador de Ordens OCO, Tratamento -1007/-2010, Watchdog de Stop Pendurado e Time-Stop de 4 Passos (Bloco 5)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional

from src.core.binance_client import (
    BinanceAPIError,
    Priority,
    RequestTimeoutUnknownState,
    SignedBinanceClient,
    floor_qty_to_step,
    round_price_to_tick,
    to_api_str,
    to_decimal,
)
from src.core.config import BinanceEnv, ExecutionResilienceCfg
from src.core.database import Database, DatabaseError, SymbolFilters
from src.execution.executor_protocol import (
    ExecutionReport,
    ExecutionRequest,
    ExitReason,
    TradeStatus,
)


@dataclass(slots=True)
class ActiveTradeContext:
    """Estado em memória pós-fill (fonte da verdade caso o SQLite sofra erro de I/O antes da OCO — C1-E)."""

    trade_id: int
    client_order_id: str
    symbol: str
    base_asset: str
    status: str
    entry_order_id: Optional[int]
    executed_qty: Decimal
    avg_entry_price: Decimal
    quote_spent: Decimal
    commission_base_asset: Decimal
    planned_stop_price: Decimal
    planned_target_price: Decimal
    time_barrier_end_ms: int
    oco_order_list_id: Optional[int] = None
    oco_list_client_id: Optional[str] = None
    oco_qty: Optional[Decimal] = None
    oco_stop_price: Optional[Decimal] = None
    oco_stop_limit_price: Optional[Decimal] = None
    oco_target_price: Optional[Decimal] = None
    post_fill_db_write_failed: bool = False


def extract_fill_summary(
    order_payload: dict[str, Any],
    base_asset: str,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """
    Extrai (executed_qty, avg_price, cummulative_quote_qty, commission_in_base_asset)
    da resposta FULL de uma ordem da Binance Spot API.
    """
    exec_qty = to_decimal(str(order_payload.get("executedQty", "0")))
    cum_quote = to_decimal(str(order_payload.get("cummulativeQuoteQty", "0")))
    if exec_qty > Decimal("0") and cum_quote > Decimal("0"):
        avg_price = cum_quote / exec_qty
    else:
        avg_price = to_decimal(str(order_payload.get("price", "0")))

    comm_base = Decimal("0")
    fills = order_payload.get("fills")
    if isinstance(fills, list):
        for f in fills:
            if isinstance(f, dict) and str(f.get("commissionAsset", "")).upper() == base_asset.upper():
                comm_base += to_decimal(str(f.get("commission", "0")))

    return exec_qty, avg_price, cum_quote, comm_base


def compute_net_oco_quantity(
    *,
    executed_qty: Decimal,
    commission_base: Decimal,
    free_base_balance: Optional[Decimal],
    step_size: Decimal,
) -> Decimal:
    """
    Calcula a quantidade líquida exata para a ordem OCO descontando comissão paga no ativo base
    e limitando ao saldo livre real na corretora:
      Q_oco = floor_qty_to_step(min(Q_executed - commission_base, FreeBalance(A)), stepSize_A)
    """
    net_from_fill = max(Decimal("0"), to_decimal(executed_qty) - to_decimal(commission_base))
    if free_base_balance is not None:
        raw_available = min(net_from_fill, max(Decimal("0"), to_decimal(free_base_balance)))
    else:
        raw_available = net_from_fill
    return floor_qty_to_step(raw_available, step_size)


def compute_oco_leg_prices(
    *,
    planned_target: Decimal,
    planned_stop: Decimal,
    tick_size: Decimal,
    below_type: str,
    stop_limit_buffer_pct: float,
) -> tuple[Decimal, Decimal, Optional[Decimal]]:
    """
    Arredonda direcionalmente os preços da OCO para múltiplos exatos de tickSize (Item Cat. 2):
    - abovePrice (Take Profit): arredonda DOWN (facilita execução maker);
    - belowStopPrice (Gatilho Stop): arredonda UP;
    - belowPrice (Limite do Stop): arredonda DOWN e garante pelo menos 1 tickSize abaixo de belowStopPrice
      para que stopPrice jamais cole em stopLimitPrice.
    """
    tp = round_price_to_tick(planned_target, tick_size, "DOWN")
    sp = round_price_to_tick(planned_stop, tick_size, "UP")
    if tp <= sp:
        tp = sp + tick_size

    if below_type == "STOP_LOSS":
        return tp, sp, None

    buf = Decimal(str(stop_limit_buffer_pct))
    raw_limit = sp * (Decimal("1") - buf)
    sl = round_price_to_tick(raw_limit, tick_size, "DOWN")
    if sl >= sp:
        sl = max(tick_size, sp - tick_size)
    return tp, sp, sl


class OcoOrderManager:
    """
    Orquestrador blindado de execução real/testnet na Binance Spot (LIVE_CHAMELEON_PHASE_1):
    - Grava PENDING_ENTRY antes do POST (falhou? não compra);
    - Mantém governor.critical_section() entre o envio da compra e a confirmação da OCO (C1-B);
    - Resolve Timeout (-1007) consultando origClientOrderId sem duplicar compra;
    - Executa escritas pós-fill em modo best-effort para que erro de SQLite nunca deixe posição sem OCO (C1-E);
    - Desconta comissão no ativo base ao dimensionar Q_oco;
    - Trata cirurgicamente o erro -2010 / preço <= stopPrice vendendo a MARKET imediatamente;
    - Possui Watchdog de Stop Pendurado e fechamento atômico de 4 passos no Time-Stop.
    """

    def __init__(
        self,
        *,
        db: Database,
        signed_client: SignedBinanceClient,
        resilience_cfg: ExecutionResilienceCfg,
        environment: BinanceEnv = BinanceEnv.TESTNET,
    ) -> None:
        self.db = db
        self.signed_client = signed_client
        self.resilience_cfg = resilience_cfg
        self.environment = environment
        self.last_in_memory_context: Optional[ActiveTradeContext] = None

    def _now_ms(self) -> int:
        return int(self.signed_client.clock.server_now_ms())

    def _best_effort_db_write(self, fn: Callable[[sqlite3.Connection], None], retries: int = 2) -> bool:
        """
        Executa escrita pós-fill em modo best-effort (C1-E): se o SQLite lançar OperationalError/DatabaseError,
        tenta novamente mas JAMAIS propaga a exceção para não interromper a criação da OCO.
        """
        for _ in range(max(1, retries)):
            try:
                with self.db.write_tx() as tx:
                    fn(tx)
                return True
            except (sqlite3.Error, DatabaseError):
                continue
        if self.last_in_memory_context is not None:
            self.last_in_memory_context.post_fill_db_write_failed = True
        return False

    def _log_order_event_best_effort(
        self,
        *,
        trade_id: Optional[int],
        client_order_id: str,
        method: str,
        endpoint: str,
        http_status: int,
        binance_code: Optional[int],
        request_obj: dict[str, Any],
        response_obj: Optional[dict[str, Any]],
    ) -> None:
        ts_ms = self._now_ms()

        def _insert(tx: sqlite3.Connection) -> None:
            tx.execute(
                """
                INSERT INTO order_events (
                    trade_id, client_order_id, method, endpoint,
                    http_status, binance_code, request_json, response_json, ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    client_order_id,
                    method,
                    endpoint,
                    http_status,
                    binance_code,
                    json.dumps(request_obj, default=str),
                    json.dumps(response_obj, default=str) if response_obj is not None else None,
                    ts_ms,
                ),
            )

        self._best_effort_db_write(_insert)

    def get_free_asset_balance(self, asset: str) -> Optional[Decimal]:
        """Consulta o saldo livre de um ativo em /api/v3/account."""
        try:
            acct = self.signed_client.get_account(omit_zero_balances=True)
        except Exception:
            return None
        balances = acct.get("balances", [])
        if not isinstance(balances, list):
            return None
        for b in balances:
            if isinstance(b, dict) and str(b.get("asset", "")).upper() == asset.upper():
                return to_decimal(str(b.get("free", "0")))
        return Decimal("0")

    def execute_entry(self, req: ExecutionRequest) -> ExecutionReport:
        """
        Executa a sequência completa de entrada:
        1. Grava PENDING_ENTRY no SQLite (obrigatório antes do POST);
        2. Entra em governor.critical_section() (C1-B);
        3. Envia MARKET BUY (e se der Timeout -1007, recupera via origClientOrderId sem duplicar);
        4. Atualiza ENTRY_FILLED (best-effort, C1-E) e chama protect_or_fallback_market().
        """
        if not req.sizing.approved or req.sizing.quote_order_qty <= Decimal("0"):
            return ExecutionReport(
                executed=False,
                mode="LIVE_CHAMELEON_PHASE_1",
                trade_id=None,
                client_order_id=f"REJ_{req.symbol}_{req.signal_close_time_ms}",
                status=TradeStatus.ENTRY_REJECTED.value,
                filled_qty=Decimal("0"),
                avg_entry_price=Decimal("0"),
                error_msg=req.sizing.reject_reason,
            )

        now_ms = self._now_ms()
        client_order_id = f"CHM_{req.symbol}_{now_ms}"
        risk_usdt = (req.sizing.effective_risk_pct * req.sizing.usable_cash_usdt).quantize(
            Decimal("0.01")
        )

        # Passo 1: Escrita obrigatória de PENDING_ENTRY antes do POST (se falhar, NÃO compra)
        with self.db.write_tx() as tx:
            cur = tx.execute(
                """
                INSERT INTO trades (
                    client_order_id, environment, symbol, strategy, regime,
                    signal_close_time_ms, status,
                    planned_entry_price, planned_stop_price, planned_target_price,
                    atr_at_signal, risk_pct, risk_usdt, quote_order_qty,
                    time_barrier_end_ms, ml_grade_silent, ml_p_cal_silent,
                    radar_signal_id, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    self.environment.value,
                    req.symbol,
                    req.strategy,
                    req.regime,
                    int(req.signal_close_time_ms),
                    TradeStatus.PENDING_ENTRY.value,
                    to_api_str(req.sizing.entry_price),
                    to_api_str(req.sizing.stop_price),
                    to_api_str(req.sizing.target_price),
                    float(req.atr_at_signal),
                    float(req.sizing.effective_risk_pct),
                    format(risk_usdt, "f"),
                    to_api_str(req.sizing.quote_order_qty),
                    int(req.time_barrier_end_ms),
                    req.sizing.ml_grade_silent,
                    req.sizing.ml_p_cal_silent,
                    req.radar_signal_id,
                    now_ms,
                    now_ms,
                ),
            )
            trade_id = int(cur.lastrowid)

        # Passo 2: Seção crítica de peso de IP entre o envio da compra e a confirmação da OCO (C1-B)
        with self.signed_client.governor.critical_section():
            order_resp: Optional[dict[str, Any]] = None
            try:
                order_resp = self.signed_client.new_market_buy_quote(
                    symbol=req.symbol,
                    quote_qty=req.sizing.quote_order_qty,
                    client_order_id=client_order_id,
                )
                self._log_order_event_best_effort(
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    method="POST",
                    endpoint="/api/v3/order",
                    http_status=200,
                    binance_code=None,
                    request_obj={
                        "symbol": req.symbol,
                        "side": "BUY",
                        "type": "MARKET",
                        "quoteOrderQty": to_api_str(req.sizing.quote_order_qty),
                        "newClientOrderId": client_order_id,
                    },
                    response_obj=order_resp,
                )
            except RequestTimeoutUnknownState:
                # Protocolo Idempotente contra Timeout (-1007): NUNCA reenviar POST; consultar origClientOrderId!
                try:
                    order_resp = self.signed_client.get_order(
                        req.symbol,
                        orig_client_order_id=client_order_id,
                    )
                    self._log_order_event_best_effort(
                        trade_id=trade_id,
                        client_order_id=client_order_id,
                        method="GET",
                        endpoint="/api/v3/order",
                        http_status=200,
                        binance_code=None,
                        request_obj={"symbol": req.symbol, "origClientOrderId": client_order_id},
                        response_obj=order_resp,
                    )
                except BinanceAPIError as get_exc:
                    if get_exc.code == -2013:
                        self._best_effort_db_write(
                            lambda tx: tx.execute(
                                "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                                (TradeStatus.ENTRY_REJECTED.value, self._now_ms(), trade_id),
                            )
                        )
                        return ExecutionReport(
                            executed=False,
                            mode="LIVE_CHAMELEON_PHASE_1",
                            trade_id=trade_id,
                            client_order_id=client_order_id,
                            status=TradeStatus.ENTRY_REJECTED.value,
                            filled_qty=Decimal("0"),
                            avg_entry_price=Decimal("0"),
                            error_msg="Order not found after -1007 timeout (-2013)",
                        )
                    raise
            except BinanceAPIError as api_exc:
                self._log_order_event_best_effort(
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    method="POST",
                    endpoint="/api/v3/order",
                    http_status=api_exc.http_status,
                    binance_code=api_exc.code,
                    request_obj={"symbol": req.symbol, "newClientOrderId": client_order_id},
                    response_obj={"msg": api_exc.msg},
                )
                self._best_effort_db_write(
                    lambda tx: tx.execute(
                        "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                        (TradeStatus.ENTRY_REJECTED.value, self._now_ms(), trade_id),
                    )
                )
                return ExecutionReport(
                    executed=False,
                    mode="LIVE_CHAMELEON_PHASE_1",
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    status=TradeStatus.ENTRY_REJECTED.value,
                    filled_qty=Decimal("0"),
                    avg_entry_price=Decimal("0"),
                    error_msg=api_exc.msg,
                )

            exec_qty, avg_price, cum_quote, comm_base = extract_fill_summary(
                order_resp, req.symbol_filters.base_asset
            )
            if exec_qty <= Decimal("0"):
                self._best_effort_db_write(
                    lambda tx: tx.execute(
                        "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                        (TradeStatus.ENTRY_REJECTED.value, self._now_ms(), trade_id),
                    )
                )
                return ExecutionReport(
                    executed=False,
                    mode="LIVE_CHAMELEON_PHASE_1",
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    status=TradeStatus.ENTRY_REJECTED.value,
                    filled_qty=Decimal("0"),
                    avg_entry_price=Decimal("0"),
                    error_msg="Zero executedQty on entry order",
                )

            entry_order_id = (
                int(order_resp["orderId"]) if order_resp.get("orderId") is not None else None
            )
            ctx = ActiveTradeContext(
                trade_id=trade_id,
                client_order_id=client_order_id,
                symbol=req.symbol,
                base_asset=req.symbol_filters.base_asset,
                status=TradeStatus.ENTRY_FILLED.value,
                entry_order_id=entry_order_id,
                executed_qty=exec_qty,
                avg_entry_price=avg_price,
                quote_spent=cum_quote,
                commission_base_asset=comm_base,
                planned_stop_price=req.sizing.stop_price,
                planned_target_price=req.sizing.target_price,
                time_barrier_end_ms=int(req.time_barrier_end_ms),
            )
            self.last_in_memory_context = ctx

            # Passo 3: Escrita pós-fill best-effort (C1-E: falha de SQLite NUNCA bloqueia a criação da OCO!)
            slip_bps = float(
                ((avg_price - req.sizing.entry_price) / req.sizing.entry_price) * Decimal("10000")
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
                        slippage_entry_bps = ?,
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
                        slip_bps,
                        self._now_ms(),
                        trade_id,
                    ),
                )
            )

            # Passo 4: Criação imediata da OCO líquida (ou fallback a mercado se preço <= stopPrice / -2010)
            return self.protect_or_fallback_market(ctx, req.symbol_filters)

    def protect_or_fallback_market(
        self,
        ctx: ActiveTradeContext,
        symbol_filters: SymbolFilters,
        *,
        override_qty: Optional[Decimal] = None,
    ) -> ExecutionReport:
        """
        Envia a ordem OCO de proteção descontando comissão no ativo base.
        Se o preço atual (bid) já estiver <= stopPrice * 1.0005 ou se a Binance recusar com -2010
        ("Stop price would trigger immediately"), vende imediatamente a MARKET (FALLBACK_MARKET_2010).
        """
        free_base = self.get_free_asset_balance(ctx.base_asset)
        if override_qty is not None:
            q_oco = compute_net_oco_quantity(
                executed_qty=override_qty,
                commission_base=Decimal("0"),
                free_base_balance=free_base,
                step_size=symbol_filters.step_size,
            )
        else:
            q_oco = compute_net_oco_quantity(
                executed_qty=ctx.executed_qty,
                commission_base=ctx.commission_base_asset,
                free_base_balance=free_base,
                step_size=symbol_filters.step_size,
            )

        tp_price, stop_price, stop_limit_price = compute_oco_leg_prices(
            planned_target=ctx.planned_target_price,
            planned_stop=ctx.planned_stop_price,
            tick_size=symbol_filters.tick_size,
            below_type=self.resilience_cfg.oco_below_type,
            stop_limit_buffer_pct=self.resilience_cfg.oco_stop_limit_buffer_pct,
        )

        # Pré-verificação de preço violado (bid <= stop_price * 1.0005)
        try:
            ticker = self.signed_client.get_book_ticker(ctx.symbol, priority=Priority.CRITICAL)
            if (
                self.resilience_cfg.fallback_to_market_on_stop_breach
                and ticker.bid <= stop_price * Decimal("1.0005")
            ):
                return self._execute_emergency_market_sell(
                    ctx,
                    q_oco,
                    reason=ExitReason.FALLBACK_MARKET_2010,
                )
        except Exception:
            pass

        oco_client_id = f"OCO_{ctx.symbol}_{ctx.trade_id}_{self._now_ms()}"
        try:
            oco_resp = self.signed_client.new_oco_sell(
                symbol=ctx.symbol,
                qty=q_oco,
                take_profit=tp_price,
                stop_price=stop_price,
                stop_limit_price=stop_limit_price,
                list_client_order_id=oco_client_id,
            )
            order_list_id = int(oco_resp["orderListId"])
            ctx.status = TradeStatus.PROTECTED.value
            ctx.oco_order_list_id = order_list_id
            ctx.oco_list_client_id = oco_client_id
            ctx.oco_qty = q_oco
            ctx.oco_stop_price = stop_price
            ctx.oco_stop_limit_price = stop_limit_price
            ctx.oco_target_price = tp_price

            self._log_order_event_best_effort(
                trade_id=ctx.trade_id,
                client_order_id=oco_client_id,
                method="POST",
                endpoint="/api/v3/orderList/oco",
                http_status=200,
                binance_code=None,
                request_obj={
                    "symbol": ctx.symbol,
                    "quantity": to_api_str(q_oco),
                    "abovePrice": to_api_str(tp_price),
                    "belowStopPrice": to_api_str(stop_price),
                    "belowPrice": to_api_str(stop_limit_price) if stop_limit_price else None,
                },
                response_obj=oco_resp,
            )

            self._best_effort_db_write(
                lambda tx: tx.execute(
                    """
                    UPDATE trades
                    SET status = ?,
                        oco_order_list_id = ?,
                        oco_list_client_id = ?,
                        oco_qty = ?,
                        oco_stop_price = ?,
                        oco_stop_limit_price = ?,
                        oco_target_price = ?,
                        updated_at_ms = ?
                    WHERE trade_id = ?
                    """,
                    (
                        TradeStatus.PROTECTED.value,
                        order_list_id,
                        oco_client_id,
                        to_api_str(q_oco),
                        to_api_str(stop_price),
                        to_api_str(stop_limit_price) if stop_limit_price is not None else None,
                        to_api_str(tp_price),
                        self._now_ms(),
                        ctx.trade_id,
                    ),
                )
            )
            return ExecutionReport(
                executed=True,
                mode="LIVE_CHAMELEON_PHASE_1",
                trade_id=ctx.trade_id,
                client_order_id=ctx.client_order_id,
                status=TradeStatus.PROTECTED.value,
                filled_qty=q_oco,
                avg_entry_price=ctx.avg_entry_price,
                oco_order_list_id=order_list_id,
            )
        except BinanceAPIError as oco_exc:
            msg_lower = oco_exc.msg.lower()
            is_immediate_trigger = (
                oco_exc.code in (-2010, -1013)
                and (
                    "immediately" in msg_lower
                    or "stop price" in msg_lower
                    or "trigger" in msg_lower
                )
            )
            if not is_immediate_trigger and oco_exc.code == -2010:
                try:
                    t_now = self.signed_client.get_book_ticker(
                        ctx.symbol, priority=Priority.CRITICAL
                    )
                    if t_now.bid <= stop_price * Decimal("1.0005"):
                        is_immediate_trigger = True
                except Exception:
                    is_immediate_trigger = True

            if self.resilience_cfg.fallback_to_market_on_stop_breach and is_immediate_trigger:
                return self._execute_emergency_market_sell(
                    ctx,
                    q_oco,
                    reason=ExitReason.FALLBACK_MARKET_2010,
                )
            raise

    def _execute_emergency_market_sell(
        self,
        ctx: ActiveTradeContext,
        qty: Decimal,
        *,
        reason: ExitReason,
    ) -> ExecutionReport:
        """Dispara venda total a MARKET imediatamente e marca a operação como CLOSED."""
        sell_client_id = f"EMERG_{ctx.symbol}_{ctx.trade_id}_{self._now_ms()}"
        sell_resp = self.signed_client.new_market_sell(
            symbol=ctx.symbol,
            qty=qty,
            client_order_id=sell_client_id,
        )
        sold_qty, sell_avg_price, quote_received, _ = extract_fill_summary(
            sell_resp, ctx.base_asset
        )
        ctx.status = TradeStatus.CLOSED.value
        now_ms = self._now_ms()
        pnl_usdt = quote_received - ctx.quote_spent
        risk_unit = max(Decimal("0.00000001"), ctx.avg_entry_price - ctx.planned_stop_price)
        realized_r = float((sell_avg_price - ctx.avg_entry_price) / risk_unit)

        self._best_effort_db_write(
            lambda tx: tx.execute(
                """
                UPDATE trades
                SET status = ?,
                    exit_reason = ?,
                    exit_avg_price = ?,
                    exit_quote_received = ?,
                    realized_pnl_usdt = ?,
                    realized_r = ?,
                    updated_at_ms = ?,
                    closed_at_ms = ?
                WHERE trade_id = ?
                """,
                (
                    TradeStatus.CLOSED.value,
                    reason.value,
                    to_api_str(sell_avg_price),
                    to_api_str(quote_received),
                    to_api_str(pnl_usdt),
                    realized_r,
                    now_ms,
                    now_ms,
                    ctx.trade_id,
                ),
            )
        )
        return ExecutionReport(
            executed=True,
            mode="LIVE_CHAMELEON_PHASE_1",
            trade_id=ctx.trade_id,
            client_order_id=ctx.client_order_id,
            status=TradeStatus.CLOSED.value,
            filled_qty=sold_qty,
            avg_entry_price=ctx.avg_entry_price,
            exit_reason=reason.value,
        )

    def check_hanging_stop_watchdog(
        self,
        *,
        trade_id: int,
        symbol: str,
        base_asset: str,
        oco_order_list_id: int,
        oco_stop_price: Decimal,
        oco_stop_limit_price: Optional[Decimal],
        symbol_filters: SymbolFilters,
    ) -> Optional[ExecutionReport]:
        """
        Watchdog de Stop Pendurado (Item Cat. 1 do TODO):
        Se a perna de stop da OCO foi acionada (ou o bid já caiu abaixo de oco_stop_limit_price)
        mas a ordem STOP_LOSS_LIMIT ficou pendurada no livro sem executar, cancela a OCO e
        vende imediatamente o saldo residual a MARKET (STOP_WATCHDOG).
        """
        if oco_stop_limit_price is None:
            return None

        ticker = self.signed_client.get_book_ticker(symbol, priority=Priority.CRITICAL)
        if ticker.bid >= oco_stop_limit_price:
            return None

        order_list = self.signed_client.get_order_list(oco_order_list_id)
        list_status = str(order_list.get("listOrderStatus", ""))
        if list_status == "ALL_DONE":
            return None

        # Preço pulou o stopLimitPrice com a OCO ainda aberta/pendurada -> Cancela e vende a MARKET!
        try:
            self.signed_client.cancel_order_list(symbol, oco_order_list_id)
        except BinanceAPIError:
            pass

        free_base = self.get_free_asset_balance(base_asset) or Decimal("0")
        q_sell = floor_qty_to_step(free_base, symbol_filters.step_size)
        if q_sell < symbol_filters.min_qty:
            return None

        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
        avg_entry = to_decimal(str(row["entry_avg_price"])) if row and row["entry_avg_price"] else oco_stop_price
        quote_spent = to_decimal(str(row["entry_quote_spent"])) if row and row["entry_quote_spent"] else Decimal("0")
        client_oid = str(row["client_order_id"]) if row else f"WD_{symbol}_{trade_id}"

        ctx = ActiveTradeContext(
            trade_id=trade_id,
            client_order_id=client_oid,
            symbol=symbol,
            base_asset=base_asset,
            status=TradeStatus.PROTECTED.value,
            entry_order_id=None,
            executed_qty=q_sell,
            avg_entry_price=avg_entry,
            quote_spent=quote_spent,
            commission_base_asset=Decimal("0"),
            planned_stop_price=oco_stop_price,
            planned_target_price=oco_stop_price,
            time_barrier_end_ms=0,
        )
        return self._execute_emergency_market_sell(
            ctx,
            q_sell,
            reason=ExitReason.STOP_WATCHDOG,
        )

    def close_on_time_barrier(
        self,
        *,
        trade_id: int,
        symbol: str,
        base_asset: str,
        oco_order_list_id: int,
        oco_qty: Decimal,
        symbol_filters: SymbolFilters,
    ) -> ExecutionReport:
        """
        Sequência serializada de 4 passos para encerramento por Barreira de Tempo (TIME):
        1. Cancela a orderList OCO (DELETE /api/v3/orderList);
        2. Lê as quantidades já executadas nas pernas da OCO cancelada;
        3. Consulta o saldo livre atualizado em /api/v3/account;
        4. Envia a venda MARKET exclusivamente para a quantidade residual não executada.
        """
        with self.signed_client.governor.critical_section():
            self._best_effort_db_write(
                lambda tx: tx.execute(
                    "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                    (TradeStatus.CLOSING.value, self._now_ms(), trade_id),
                )
            )

            # Passo 1: Cancela a orderList OCO
            cancel_resp: dict[str, Any] = {}
            try:
                cancel_resp = self.signed_client.cancel_order_list(symbol, oco_order_list_id)
            except BinanceAPIError:
                cancel_resp = self.signed_client.get_order_list(oco_order_list_id)

            # Passo 2: Soma quantidades já executadas nas pernas da OCO
            executed_on_oco = Decimal("0")
            order_reports = cancel_resp.get("orderReports", [])
            if isinstance(order_reports, list):
                for rep in order_reports:
                    if isinstance(rep, dict) and rep.get("executedQty") is not None:
                        executed_on_oco += to_decimal(str(rep["executedQty"]))

            # Passo 3: Consulta o saldo livre atualizado na corretora
            free_base = self.get_free_asset_balance(base_asset)
            remaining_from_oco = max(Decimal("0"), to_decimal(oco_qty) - executed_on_oco)
            if free_base is not None:
                raw_rem = min(remaining_from_oco, max(Decimal("0"), free_base))
            else:
                raw_rem = remaining_from_oco
            q_rem = floor_qty_to_step(raw_rem, symbol_filters.step_size)

            with self.db.read() as conn:
                row = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
            avg_entry = (
                to_decimal(str(row["entry_avg_price"]))
                if row and row["entry_avg_price"]
                else Decimal("1")
            )
            planned_stop = (
                to_decimal(str(row["planned_stop_price"]))
                if row and row["planned_stop_price"]
                else Decimal("0.95")
            )
            quote_spent = (
                to_decimal(str(row["entry_quote_spent"]))
                if row and row["entry_quote_spent"]
                else Decimal("0")
            )
            client_oid = str(row["client_order_id"]) if row else f"TIME_{symbol}_{trade_id}"

            # Passo 4: Vende a MARKET apenas a quantidade residual
            if q_rem >= symbol_filters.min_qty:
                ctx = ActiveTradeContext(
                    trade_id=trade_id,
                    client_order_id=client_oid,
                    symbol=symbol,
                    base_asset=base_asset,
                    status=TradeStatus.CLOSING.value,
                    entry_order_id=None,
                    executed_qty=q_rem,
                    avg_entry_price=avg_entry,
                    quote_spent=quote_spent,
                    commission_base_asset=Decimal("0"),
                    planned_stop_price=planned_stop,
                    planned_target_price=avg_entry,
                    time_barrier_end_ms=0,
                )
                return self._execute_emergency_market_sell(
                    ctx,
                    q_rem,
                    reason=ExitReason.TIME,
                )

            now_ms = self._now_ms()
            self._best_effort_db_write(
                lambda tx: tx.execute(
                    """
                    UPDATE trades
                    SET status = ?, exit_reason = ?, updated_at_ms = ?, closed_at_ms = ?
                    WHERE trade_id = ?
                    """,
                    (TradeStatus.CLOSED.value, ExitReason.TIME.value, now_ms, now_ms, trade_id),
                )
            )
            return ExecutionReport(
                executed=True,
                mode="LIVE_CHAMELEON_PHASE_1",
                trade_id=trade_id,
                client_order_id=client_oid,
                status=TradeStatus.CLOSED.value,
                filled_qty=Decimal("0"),
                avg_entry_price=avg_entry,
                exit_reason=ExitReason.TIME.value,
            )
