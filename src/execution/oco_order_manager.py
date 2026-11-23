"""Gerenciador de Ordens OCO, Tratamento -1007/-2010, Watchdog de Stop Pendurado e Time-Stop de 4 Passos (Bloco 5)."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional

from src.core.binance_client import (
    BinanceAPIError,
    IPBannedError,
    Priority,
    RateLimitedError,
    RequestTimeoutUnknownState,
    SignedBinanceClient,
    floor_qty_to_step,
    passes_filters,
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

logger = logging.getLogger("oco_order_manager")


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
    commission_exact: bool = True
    commission_estimated: bool = False


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
        alert_callback: Optional[Callable[[str, str], None]] = None,
        shadow_manager: Optional[Any] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = db
        self.signed_client = signed_client
        self.resilience_cfg = resilience_cfg
        self.environment = environment
        self.alert_callback = alert_callback
        self.shadow_manager = shadow_manager
        self._sleep = sleep_fn
        self.last_in_memory_context: Optional[ActiveTradeContext] = None

    def _now_ms(self) -> int:
        return int(self.signed_client.clock.server_now_ms())

    def _emit_alert(self, kind: str, text: str) -> None:
        if self.alert_callback is None:
            return
        try:
            self.alert_callback(kind, text)
        except Exception as exc:
            logger.warning("Alert callback failed (%s): %s", kind, exc)

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

    def _resync_clock_best_effort(self) -> bool:
        """Ressincroniza o relógio monotônico via GET /api/v3/time em prioridade CRITICAL após erro -1021 (FI-02)."""
        get_time = getattr(self.signed_client, "get_server_time_ms", None)
        clock = getattr(self.signed_client, "clock", None)
        if not callable(get_time) or clock is None or not callable(getattr(clock, "sync", None)):
            return False

        def _fetch() -> int:
            try:
                return int(get_time(priority=Priority.CRITICAL))
            except TypeError:
                return int(get_time())

        try:
            clock.sync(_fetch, samples=1)
            return True
        except Exception as exc:
            logger.warning("Best-effort clock resync failed after -1021: %s", exc)
            return False

    def get_free_asset_balance(self, asset: str, *, retries: int = 2) -> Optional[Decimal]:
        """Consulta o saldo livre de um ativo em /api/v3/account com retry curto."""
        acct: Optional[dict[str, Any]] = None
        for attempt in range(max(1, int(retries))):
            try:
                acct = self.signed_client.get_account(omit_zero_balances=True)
                break
            except BinanceAPIError as exc:
                if exc.code == -1021 and attempt + 1 < max(1, int(retries)):
                    self._resync_clock_best_effort()
                    continue
                if attempt + 1 < max(1, int(retries)):
                    self._sleep(0.05)
                continue
            except Exception:
                if attempt + 1 < max(1, int(retries)):
                    self._sleep(0.05)
                continue
        if acct is None:
            return None
        balances = acct.get("balances", [])
        if not isinstance(balances, list):
            return None
        for b in balances:
            if isinstance(b, dict) and str(b.get("asset", "")).upper() == asset.upper():
                return to_decimal(str(b.get("free", "0")))
        return Decimal("0")

    def resolve_base_commission(
        self,
        symbol: str,
        order_payload: dict[str, Any],
        base_asset: str,
    ) -> tuple[Decimal, bool]:
        """
        Determina (commission_base_asset, is_exact) esgotando:
        1. fills[] da resposta FULL de POST /api/v3/order;
        2. GET /api/v3/myTrades?symbol=...&orderId=... quando GET /api/v3/order não traz fills (A2-04).
        Retorna (0, False) somente quando nenhuma fonte exata trouxe a comissão.
        """
        fills = order_payload.get("fills")
        if isinstance(fills, list) and len(fills) > 0:
            comm_base = Decimal("0")
            for f in fills:
                if isinstance(f, dict) and str(f.get("commissionAsset", "")).upper() == base_asset.upper():
                    comm_base += to_decimal(str(f.get("commission", "0")))
            return comm_base, True

        order_id = order_payload.get("orderId")
        my_trades_fn = getattr(self.signed_client, "get_my_trades", None)
        if order_id is not None and callable(my_trades_fn):
            for attempt in range(2):
                try:
                    trades = my_trades_fn(symbol, order_id=int(order_id))
                    if isinstance(trades, list) and len(trades) > 0:
                        comm_base = Decimal("0")
                        for t in trades:
                            if (
                                isinstance(t, dict)
                                and str(t.get("commissionAsset", "")).upper() == base_asset.upper()
                            ):
                                comm_base += to_decimal(str(t.get("commission", "0")))
                        return comm_base, True
                    break
                except Exception:
                    if attempt == 0:
                        self._sleep(0.05)
                    continue

        return Decimal("0"), False

    def execute_entry(self, req: ExecutionRequest) -> ExecutionReport:
        """
        Executa a sequência completa de entrada:
        1. Pré-valida filtros da OCO (status TRADING, oco_allowed, NOTIONAL/LOT_SIZE da perna STOP_LOSS_LIMIT);
        2. Grava PENDING_ENTRY no SQLite (obrigatório antes do POST);
        3. Entra em governor.critical_section() (C1-B);
        4. Envia MARKET BUY (e se der Timeout -1007 / HTTP 5XX, recupera via origClientOrderId sem duplicar);
        5. Atualiza ENTRY_FILLED (best-effort, C1-E) e chama protect_or_fallback_market().
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

        # Pré-validação de filtros da OCO antes de comprar (A1-11c): jamais abrir posição cuja OCO será rejeitada!
        if str(req.symbol_filters.status).upper() != "TRADING":
            return ExecutionReport(
                executed=False,
                mode="LIVE_CHAMELEON_PHASE_1",
                trade_id=None,
                client_order_id=f"REJ_{req.symbol}_{req.signal_close_time_ms}",
                status=TradeStatus.ENTRY_REJECTED.value,
                filled_qty=Decimal("0"),
                avg_entry_price=Decimal("0"),
                error_msg=f"SYMBOL_NOT_TRADING:{req.symbol_filters.status}",
            )
        if not req.symbol_filters.oco_allowed:
            return ExecutionReport(
                executed=False,
                mode="LIVE_CHAMELEON_PHASE_1",
                trade_id=None,
                client_order_id=f"REJ_{req.symbol}_{req.signal_close_time_ms}",
                status=TradeStatus.ENTRY_REJECTED.value,
                filled_qty=Decimal("0"),
                avg_entry_price=Decimal("0"),
                error_msg="OCO_NOT_ALLOWED_ON_SYMBOL",
            )
        preflight_tp, preflight_sp, preflight_sl = compute_oco_leg_prices(
            planned_target=req.sizing.target_price,
            planned_stop=req.sizing.stop_price,
            tick_size=req.symbol_filters.tick_size,
            below_type=self.resilience_cfg.oco_below_type,
            stop_limit_buffer_pct=self.resilience_cfg.oco_stop_limit_buffer_pct,
        )
        stop_check_price = preflight_sl if preflight_sl is not None else preflight_sp
        ok_stop_leg, stop_leg_err = passes_filters(stop_check_price, req.sizing.base_qty, req.symbol_filters)
        if not ok_stop_leg:
            return ExecutionReport(
                executed=False,
                mode="LIVE_CHAMELEON_PHASE_1",
                trade_id=None,
                client_order_id=f"REJ_{req.symbol}_{req.signal_close_time_ms}",
                status=TradeStatus.ENTRY_REJECTED.value,
                filled_qty=Decimal("0"),
                avg_entry_price=Decimal("0"),
                error_msg=f"PREFLIGHT_OCO_STOP_LEG_FAILED:{stop_leg_err}",
            )
        ok_tp_leg, tp_leg_err = passes_filters(preflight_tp, req.sizing.base_qty, req.symbol_filters)
        if not ok_tp_leg:
            return ExecutionReport(
                executed=False,
                mode="LIVE_CHAMELEON_PHASE_1",
                trade_id=None,
                client_order_id=f"REJ_{req.symbol}_{req.signal_close_time_ms}",
                status=TradeStatus.ENTRY_REJECTED.value,
                filled_qty=Decimal("0"),
                avg_entry_price=Decimal("0"),
                error_msg=f"PREFLIGHT_OCO_TP_LEG_FAILED:{tp_leg_err}",
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

            exec_qty, avg_price, cum_quote, _ = extract_fill_summary(
                order_resp, req.symbol_filters.base_asset
            )
            comm_base, comm_exact = self.resolve_base_commission(
                req.symbol, order_resp, req.symbol_filters.base_asset
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
                commission_exact=comm_exact,
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

            # Passo 4: Criação imediata da OCO líquida (ou fallback a mercado se preço <= stopPrice / >= tpPrice / -2010)
            return self.protect_or_fallback_market(ctx, req.symbol_filters)

    def _recover_oco_after_timeout(
        self,
        symbol: str,
        oco_client_id: str,
    ) -> Optional[dict[str, Any]]:
        """Consulta a OCO após timeout (-1007 / 5XX) sem duplicar cegamente (A1-03b)."""
        get_ol_fn = getattr(self.signed_client, "get_order_list", None)
        if callable(get_ol_fn):
            try:
                found = get_ol_fn(orig_client_order_id=oco_client_id)
                if isinstance(found, dict) and found.get("orderListId") is not None:
                    return found
            except TypeError:
                pass
            except BinanceAPIError as exc:
                if exc.code not in (-2013, -2011):
                    logger.warning("get_order_list(%s) failed during timeout recovery: %s", oco_client_id, exc)
            except Exception as exc:
                logger.warning("get_order_list(%s) unexpected error during recovery: %s", oco_client_id, exc)

        open_lists_fn = getattr(self.signed_client, "get_open_order_lists", None)
        if callable(open_lists_fn):
            try:
                open_lists = open_lists_fn()
                for ol in open_lists:
                    if not isinstance(ol, dict):
                        continue
                    if str(ol.get("listClientOrderId", "")) == oco_client_id:
                        return ol
                    if str(ol.get("symbol", "")).upper() == symbol.upper() and ol.get("orderListId") is not None:
                        return ol
            except Exception as exc:
                logger.warning("get_open_order_lists() failed during timeout recovery: %s", exc)
        return None

    def _finalize_protected_oco(
        self,
        *,
        ctx: ActiveTradeContext,
        oco_resp: dict[str, Any],
        oco_client_id: str,
        q_oco: Decimal,
        tp_price: Decimal,
        stop_price: Decimal,
        stop_limit_price: Optional[Decimal],
    ) -> ExecutionReport:
        order_list_id = int(oco_resp["orderListId"])
        resolved_client_id = str(oco_resp.get("listClientOrderId") or oco_client_id)
        ctx.status = TradeStatus.PROTECTED.value
        ctx.oco_order_list_id = order_list_id
        ctx.oco_list_client_id = resolved_client_id
        ctx.oco_qty = q_oco
        ctx.oco_stop_price = stop_price
        ctx.oco_stop_limit_price = stop_limit_price
        ctx.oco_target_price = tp_price

        self._log_order_event_best_effort(
            trade_id=ctx.trade_id,
            client_order_id=resolved_client_id,
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
                    resolved_client_id,
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

    def protect_or_fallback_market(
        self,
        ctx: ActiveTradeContext,
        symbol_filters: SymbolFilters,
        *,
        override_qty: Optional[Decimal] = None,
    ) -> ExecutionReport:
        """
        Envia a ordem OCO de proteção descontando comissão no ativo base.
        Se o preço atual (bid) já estiver <= stopPrice * 1.0005 (ou >= tpPrice) ou se a Binance recusar
        com -2010 de gatilho imediato/relação de preços, vende imediatamente a MARKET.
        Recupera timeout (-1007 / 5XX) em POST /api/v3/orderList/oco de forma idempotente (A1-03b).
        """
        free_base = self.get_free_asset_balance(ctx.base_asset, retries=2)
        if override_qty is not None:
            q_oco = compute_net_oco_quantity(
                executed_qty=override_qty,
                commission_base=Decimal("0"),
                free_base_balance=free_base,
                step_size=symbol_filters.step_size,
            )
        elif free_base is None and not ctx.commission_exact:
            # A2-04: somente após esgotar get_order (sem fills), myTrades e get_account com retry,
            # aplica margem conservadora de -0.1%, registra em log + SQLite e envia alerta Telegram.
            est_comm = ctx.executed_qty * Decimal("0.001")
            q_oco = compute_net_oco_quantity(
                executed_qty=ctx.executed_qty,
                commission_base=est_comm,
                free_base_balance=None,
                step_size=symbol_filters.step_size,
            )
            ctx.commission_estimated = True
            logger.warning(
                "Estimated OCO quantity (-0.1%% conservative fee fallback) for %s trade_id=%d: "
                "executed_qty=%s -> q_oco=%s (get_order had no fills, myTrades and get_account exhausted).",
                ctx.symbol,
                ctx.trade_id,
                to_api_str(ctx.executed_qty),
                to_api_str(q_oco),
            )
            self._log_order_event_best_effort(
                trade_id=ctx.trade_id,
                client_order_id=ctx.client_order_id,
                method="FALLBACK_ESTIMATE",
                endpoint="local://oco_qty_estimated_fee",
                http_status=0,
                binance_code=None,
                request_obj={
                    "symbol": ctx.symbol,
                    "executed_qty": to_api_str(ctx.executed_qty),
                    "estimated_fee_pct": "0.001",
                    "estimated_oco_qty": to_api_str(q_oco),
                    "reason": "exhausted_get_order_myTrades_and_get_account",
                },
                response_obj=None,
            )
            self._best_effort_db_write(
                lambda tx: tx.execute(
                    """
                    INSERT INTO system_state(key, value, updated_at_ms)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        f"trade_{ctx.trade_id}_oco_qty_estimated",
                        to_api_str(q_oco),
                        self._now_ms(),
                    ),
                )
            )
            self._emit_alert(
                "OCO_QTY_ESTIMATED",
                (
                    f"⚠️ OCO com quantidade ESTIMADA (-0.1%) em {ctx.symbol} (trade_id={ctx.trade_id}): "
                    f"get_order sem fills, myTrades e get_account falharam após retry. "
                    f"Q_oco={to_api_str(q_oco)} (executado={to_api_str(ctx.executed_qty)})."
                ),
            )
        else:
            q_oco = compute_net_oco_quantity(
                executed_qty=ctx.executed_qty,
                commission_base=ctx.commission_base_asset,
                free_base_balance=free_base,
                step_size=symbol_filters.step_size,
            )

        if q_oco < symbol_filters.min_qty:
            raise ValueError(
                f"Computed Q_oco ({q_oco}) < min_qty ({symbol_filters.min_qty}) "
                f"for {ctx.symbol} (trade_id={ctx.trade_id}, free_base={free_base})."
            )

        tp_price, stop_price, stop_limit_price = compute_oco_leg_prices(
            planned_target=ctx.planned_target_price,
            planned_stop=ctx.planned_stop_price,
            tick_size=symbol_filters.tick_size,
            below_type=self.resilience_cfg.oco_below_type,
            stop_limit_buffer_pct=self.resilience_cfg.oco_stop_limit_buffer_pct,
        )

        # Pré-verificação de preço violado (stop abaixo OU take-profit já alcançado — A1-11a)
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
            if ticker.bid >= tp_price:
                return self._execute_emergency_market_sell(
                    ctx,
                    q_oco,
                    reason=ExitReason.TARGET,
                )
        except Exception:
            pass

        oco_client_id = f"OCO_{ctx.symbol}_{ctx.trade_id}_{self._now_ms()}"
        self._best_effort_db_write(
            lambda tx: tx.execute(
                "UPDATE trades SET oco_list_client_id = ?, updated_at_ms = ? WHERE trade_id = ?",
                (oco_client_id, self._now_ms(), ctx.trade_id),
            )
        )

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            cur_client_id = oco_client_id if attempt == 0 else f"OCO_{ctx.symbol}_{ctx.trade_id}_{self._now_ms()}"
            try:
                oco_resp = self.signed_client.new_oco_sell(
                    symbol=ctx.symbol,
                    qty=q_oco,
                    take_profit=tp_price,
                    stop_price=stop_price,
                    stop_limit_price=stop_limit_price,
                    list_client_order_id=cur_client_id,
                )
                return self._finalize_protected_oco(
                    ctx=ctx,
                    oco_resp=oco_resp,
                    oco_client_id=cur_client_id,
                    q_oco=q_oco,
                    tp_price=tp_price,
                    stop_price=stop_price,
                    stop_limit_price=stop_limit_price,
                )
            except RequestTimeoutUnknownState as to_exc:
                # A1-03b: jamais duplicar cegamente após timeout/-1007/5XX; verificar se a OCO entrou!
                recovered = self._recover_oco_after_timeout(ctx.symbol, cur_client_id)
                if recovered is not None:
                    return self._finalize_protected_oco(
                        ctx=ctx,
                        oco_resp=recovered,
                        oco_client_id=cur_client_id,
                        q_oco=q_oco,
                        tp_price=tp_price,
                        stop_price=stop_price,
                        stop_limit_price=stop_limit_price,
                    )
                last_exc = to_exc
                if attempt == 0:
                    # Antes de tentar 1 reenvio, checar se preço já violou stop ou alvo
                    try:
                        t_retry = self.signed_client.get_book_ticker(ctx.symbol, priority=Priority.CRITICAL)
                        if (
                            self.resilience_cfg.fallback_to_market_on_stop_breach
                            and t_retry.bid <= stop_price * Decimal("1.0005")
                        ):
                            return self._execute_emergency_market_sell(
                                ctx, q_oco, reason=ExitReason.FALLBACK_MARKET_2010
                            )
                        if t_retry.bid >= tp_price:
                            return self._execute_emergency_market_sell(
                                ctx, q_oco, reason=ExitReason.TARGET
                            )
                    except Exception:
                        pass
                    continue
                raise
            except IPBannedError:
                raise
            except RateLimitedError as rl_exc:
                last_exc = rl_exc
                if attempt == 0:
                    wait_s = min(float(rl_exc.retry_after_s or 1.0), 5.0)
                    self._sleep(wait_s)
                    continue
                raise
            except BinanceAPIError as oco_exc:
                if oco_exc.code == -1021 and attempt == 0:
                    last_exc = oco_exc
                    resynced = self._resync_clock_best_effort()
                    logger.warning(
                        "Binance -1021 timestamp error placing OCO for %s trade #%s; clock resynced=%s",
                        ctx.symbol,
                        ctx.trade_id,
                        resynced,
                    )
                    if resynced:
                        try:
                            t_retry = self.signed_client.get_book_ticker(
                                ctx.symbol, priority=Priority.CRITICAL
                            )
                            if (
                                self.resilience_cfg.fallback_to_market_on_stop_breach
                                and t_retry.bid <= stop_price * Decimal("1.0005")
                            ):
                                return self._execute_emergency_market_sell(
                                    ctx, q_oco, reason=ExitReason.FALLBACK_MARKET_2010
                                )
                            if t_retry.bid >= tp_price:
                                return self._execute_emergency_market_sell(
                                    ctx, q_oco, reason=ExitReason.TARGET
                                )
                        except Exception:
                            pass
                        continue
                    raise

                msg_lower = oco_exc.msg.lower()
                is_stop_trigger = oco_exc.code in (-2010, -1013) and (
                    "would trigger immediately" in msg_lower
                    or "stop price" in msg_lower
                    or "trigger" in msg_lower
                )
                is_price_relation_or_maker = oco_exc.code in (-2010, -1013) and (
                    "relationship of the prices" in msg_lower
                    or "immediately match and take" in msg_lower
                )

                if is_price_relation_or_maker:
                    try:
                        t_now = self.signed_client.get_book_ticker(
                            ctx.symbol, priority=Priority.CRITICAL
                        )
                        if t_now.bid >= tp_price:
                            return self._execute_emergency_market_sell(
                                ctx, q_oco, reason=ExitReason.TARGET
                            )
                        if (
                            self.resilience_cfg.fallback_to_market_on_stop_breach
                            and t_now.bid <= stop_price * Decimal("1.0005")
                        ):
                            return self._execute_emergency_market_sell(
                                ctx, q_oco, reason=ExitReason.FALLBACK_MARKET_2010
                            )
                    except Exception:
                        pass
                    if "immediately match and take" in msg_lower:
                        return self._execute_emergency_market_sell(
                            ctx, q_oco, reason=ExitReason.TARGET
                        )
                    raise

                if not is_stop_trigger and oco_exc.code == -2010:
                    # A1-11b: erro -2010 sem confirmação de gatilho de stop -> só vende se bookTicker
                    # CONFIRMAR violação; falha de ticker JAMAIS liquida posição saudável!
                    try:
                        t_now = self.signed_client.get_book_ticker(
                            ctx.symbol, priority=Priority.CRITICAL
                        )
                        if t_now.bid <= stop_price * Decimal("1.0005"):
                            is_stop_trigger = True
                        elif t_now.bid >= tp_price:
                            return self._execute_emergency_market_sell(
                                ctx, q_oco, reason=ExitReason.TARGET
                            )
                    except Exception as tick_exc:
                        logger.warning(
                            "bookTicker failed while diagnosing -2010 on %s (%s); refusing blind market sell: %s",
                            ctx.symbol,
                            oco_exc.msg,
                            tick_exc,
                        )
                        raise oco_exc from None

                if self.resilience_cfg.fallback_to_market_on_stop_breach and is_stop_trigger:
                    return self._execute_emergency_market_sell(
                        ctx,
                        q_oco,
                        reason=ExitReason.FALLBACK_MARKET_2010,
                    )
                raise

        assert last_exc is not None
        raise last_exc

    def _execute_emergency_market_sell(
        self,
        ctx: ActiveTradeContext,
        qty: Decimal,
        *,
        reason: ExitReason,
    ) -> ExecutionReport:
        """Dispara venda total a MARKET imediatamente e marca a operação como CLOSED."""
        sell_resp: Optional[dict[str, Any]] = None
        for attempt in range(2):
            sell_client_id = f"EMERG_{ctx.symbol}_{ctx.trade_id}_{self._now_ms()}"
            try:
                sell_resp = self.signed_client.new_market_sell(
                    symbol=ctx.symbol,
                    qty=qty,
                    client_order_id=sell_client_id,
                )
                self._log_order_event_best_effort(
                    trade_id=ctx.trade_id,
                    client_order_id=sell_client_id,
                    method="POST",
                    endpoint="/api/v3/order",
                    http_status=200,
                    binance_code=None,
                    request_obj={
                        "symbol": ctx.symbol,
                        "side": "SELL",
                        "type": "MARKET",
                        "quantity": to_api_str(qty),
                        "newClientOrderId": sell_client_id,
                        "reason": reason.value,
                    },
                    response_obj=sell_resp,
                )
                break
            except RequestTimeoutUnknownState:
                sell_resp = self.signed_client.get_order(
                    ctx.symbol, orig_client_order_id=sell_client_id
                )
                self._log_order_event_best_effort(
                    trade_id=ctx.trade_id,
                    client_order_id=sell_client_id,
                    method="GET",
                    endpoint="/api/v3/order",
                    http_status=200,
                    binance_code=None,
                    request_obj={"symbol": ctx.symbol, "origClientOrderId": sell_client_id},
                    response_obj=sell_resp,
                )
                break
            except BinanceAPIError as sell_exc:
                if sell_exc.code == -1021 and attempt == 0 and self._resync_clock_best_effort():
                    logger.warning(
                        "Binance -1021 timestamp error on emergency market sell for %s trade #%s; clock resynced, retrying once",
                        ctx.symbol,
                        ctx.trade_id,
                    )
                    continue
                raise

        assert sell_resp is not None

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

    def _extract_oco_executed_qty(
        self,
        symbol: str,
        order_list_resp: dict[str, Any],
    ) -> tuple[Decimal, Decimal, bool]:
        """
        Extrai (executed_qty, cummulative_quote_qty, confirmed) das pernas de uma orderList OCO (A1-07).
        - DELETE /api/v3/orderList retorna `orderReports[]` com `executedQty`;
        - GET /api/v3/orderList (schema oficial Binance) retorna apenas `orders[]` sem `orderReports`,
          exigindo consulta por perna via GET /api/v3/order.
        """
        order_reports = order_list_resp.get("orderReports")
        if isinstance(order_reports, list) and len(order_reports) > 0:
            executed = Decimal("0")
            quote_rec = Decimal("0")
            for rep in order_reports:
                if isinstance(rep, dict):
                    if rep.get("executedQty") is not None:
                        executed += to_decimal(str(rep["executedQty"]))
                    if rep.get("cummulativeQuoteQty") is not None:
                        quote_rec += to_decimal(str(rep["cummulativeQuoteQty"]))
            return executed, quote_rec, True

        orders = order_list_resp.get("orders")
        if isinstance(orders, list) and len(orders) > 0:
            executed = Decimal("0")
            quote_rec = Decimal("0")
            for leg in orders:
                if not isinstance(leg, dict):
                    continue
                try:
                    coid = leg.get("clientOrderId")
                    oid = leg.get("orderId")
                    if coid:
                        leg_resp = self.signed_client.get_order(
                            symbol, orig_client_order_id=str(coid)
                        )
                    elif oid is not None:
                        leg_resp = self.signed_client.get_order(
                            symbol, order_id=int(oid)
                        )
                    else:
                        return Decimal("0"), Decimal("0"), False
                except Exception as exc:
                    logger.warning(
                        "Failed to inspect OCO leg for %s (%s): %s",
                        symbol,
                        leg,
                        exc,
                    )
                    return Decimal("0"), Decimal("0"), False
                if leg_resp.get("executedQty") is not None:
                    executed += to_decimal(str(leg_resp["executedQty"]))
                if leg_resp.get("cummulativeQuoteQty") is not None:
                    quote_rec += to_decimal(str(leg_resp["cummulativeQuoteQty"]))
            return executed, quote_rec, True

        return Decimal("0"), Decimal("0"), False

    @staticmethod
    def _is_insufficient_balance_error(exc: BinanceAPIError) -> bool:
        return exc.code == -2010 and "insufficient" in exc.msg.lower()

    def _enter_halt_review(
        self,
        *,
        trade_id: int,
        symbol: str,
        reason_msg: str,
    ) -> None:
        """Coloca o trade em HALT_REVIEW, aciona emergency_halt e dispara alerta Telegram (nunca silencioso)."""
        now_ms = self._now_ms()
        logger.error("HALT_REVIEW triggered on %s trade #%s: %s", symbol, trade_id, reason_msg)
        self._best_effort_db_write(
            lambda tx: tx.execute(
                "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                (TradeStatus.HALT_REVIEW.value, now_ms, trade_id),
            )
        )
        if self.shadow_manager is not None:
            try:
                from src.risk.shadow_manager import KillSwitchReason

                self.shadow_manager._trigger_halt(KillSwitchReason.RECONCILIATION_OR_AUTH_FAILURE)
            except Exception as exc:
                logger.warning("Failed to trigger halt via ShadowManager: %s", exc)
        try:
            self.db.set_state("emergency_halt", "1")
            self.db.set_state("kill_switch_reason", "RECONCILIATION_OR_AUTH_FAILURE")
            self.db.set_state("halt_review_reason", reason_msg)
        except Exception as exc:
            logger.warning("Failed to persist halt_review state in SQLite: %s", exc)
        self._log_order_event_best_effort(
            trade_id=trade_id,
            client_order_id=f"HALT_REVIEW_{symbol}_{trade_id}",
            method="HALT_REVIEW",
            endpoint="local://watchdog_halt_review",
            http_status=0,
            binance_code=None,
            request_obj={"trade_id": trade_id, "symbol": symbol},
            response_obj={"reason": reason_msg},
        )
        self._emit_alert(
            "KILL_SWITCH",
            f"HALT_REVIEW em {symbol} (trade #{trade_id}): {reason_msg}",
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
        oco_qty: Optional[Decimal] = None,
    ) -> Optional[ExecutionReport]:
        """
        Watchdog de Stop Pendurado (Item Cat. 1 do TODO / A1-05):
        Se a perna de stop da OCO foi acionada (ou o bid já caiu abaixo de oco_stop_limit_price)
        mas a ordem STOP_LOSS_LIMIT ficou pendurada no livro sem executar, cancela a OCO dentro
        de `critical_section()` e vende imediatamente o saldo residual rastreado a MARKET (STOP_WATCHDOG),
        respeitando o teto C1-D (`oco_qty`), com fallback para `remaining_from_oco` se `get_account`
        falhar e retry único seguido de `HALT_REVIEW` + alerta em caso de saldo insuficiente.
        """
        if oco_stop_limit_price is None:
            return None

        ticker = self.signed_client.get_book_ticker(symbol, priority=Priority.CRITICAL)
        if ticker.bid >= oco_stop_limit_price:
            return None

        order_list = self.signed_client.get_order_list(oco_order_list_id)
        list_status = str(order_list.get("listOrderStatus", "")).upper()
        if list_status == "ALL_DONE":
            return None

        # Preço pulou o stopLimitPrice com a OCO ainda aberta/pendurada -> Cancela e vende a MARKET em seção crítica!
        with self.signed_client.governor.critical_section():
            with self.db.read() as conn:
                row = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()

            if oco_qty is not None:
                tracked_qty = to_decimal(oco_qty)
            elif row and row["oco_qty"]:
                tracked_qty = to_decimal(str(row["oco_qty"]))
            elif row and row["entry_executed_qty"]:
                tracked_qty = to_decimal(str(row["entry_executed_qty"]))
            else:
                tracked_qty = Decimal("0")

            avg_entry = (
                to_decimal(str(row["entry_avg_price"]))
                if row and row["entry_avg_price"]
                else oco_stop_price
            )
            quote_spent = (
                to_decimal(str(row["entry_quote_spent"]))
                if row and row["entry_quote_spent"]
                else Decimal("0")
            )
            orig_entry_qty = (
                to_decimal(str(row["entry_executed_qty"]))
                if row and row["entry_executed_qty"]
                else tracked_qty
            )
            client_oid = str(row["client_order_id"]) if row else f"WD_{symbol}_{trade_id}"

            self._best_effort_db_write(
                lambda tx: tx.execute(
                    "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                    (TradeStatus.CLOSING.value, self._now_ms(), trade_id),
                )
            )

            cancel_resp: dict[str, Any] = {}
            try:
                cancel_resp = self.signed_client.cancel_order_list(symbol, oco_order_list_id)
            except (BinanceAPIError, RequestTimeoutUnknownState) as cancel_exc:
                try:
                    check_resp = self.signed_client.get_order_list(oco_order_list_id)
                except Exception:
                    raise cancel_exc
                check_status = str(check_resp.get("listOrderStatus", "")).upper()
                if check_status != "ALL_DONE":
                    self._best_effort_db_write(
                        lambda tx: tx.execute(
                            "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                            (TradeStatus.PROTECTED.value, self._now_ms(), trade_id),
                        )
                    )
                    raise cancel_exc
                cancel_resp = check_resp

            executed_on_oco, quote_from_oco, executed_confirmed = self._extract_oco_executed_qty(
                symbol, cancel_resp
            )
            remaining_from_oco = max(Decimal("0"), tracked_qty - executed_on_oco)

            # Se a OCO já preencheu 100% no instante do cancelamento, não vende nada e encerra o trade.
            if executed_confirmed and tracked_qty > Decimal("0") and executed_on_oco >= tracked_qty:
                now_ms = self._now_ms()
                self._best_effort_db_write(
                    lambda tx: tx.execute(
                        """
                        UPDATE trades
                        SET status = ?, exit_reason = ?, updated_at_ms = ?, closed_at_ms = ?
                        WHERE trade_id = ?
                        """,
                        (
                            TradeStatus.CLOSED.value,
                            ExitReason.STOP_WATCHDOG.value,
                            now_ms,
                            now_ms,
                            trade_id,
                        ),
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
                    exit_reason=ExitReason.STOP_WATCHDOG.value,
                )

            free_base = self.get_free_asset_balance(base_asset, retries=2)
            if free_base is not None:
                # A1-05b (C1-D): jamais vender saldo manual além da quantidade rastreada.
                raw_sell = (
                    min(tracked_qty, max(Decimal("0"), free_base))
                    if tracked_qty > Decimal("0")
                    else Decimal("0")
                )
                if (
                    executed_confirmed
                    and executed_on_oco > Decimal("0")
                    and free_base > remaining_from_oco
                ):
                    logger.warning(
                        "Watchdog %s trade #%s observed free_base=%s > remaining_from_oco=%s (tracked_qty=%s, executed_on_oco=%s)",
                        symbol,
                        trade_id,
                        free_base,
                        remaining_from_oco,
                        tracked_qty,
                        executed_on_oco,
                    )
            else:
                # A1-05a: get_account falhou após cancelar a OCO; usa remaining_from_oco (limitado ao rastreado)
                # em vez de retornar None silenciosamente deixando a posição sem stop em flash-crash.
                raw_sell = remaining_from_oco
                logger.warning(
                    "Watchdog get_account failed for %s trade #%s after OCO cancel; "
                    "using tracked remaining_from_oco=%s",
                    symbol,
                    trade_id,
                    remaining_from_oco,
                )

            q_sell = floor_qty_to_step(raw_sell, symbol_filters.step_size)
            if q_sell < symbol_filters.min_qty or (q_sell * ticker.bid) < symbol_filters.min_notional:
                now_ms = self._now_ms()
                self._best_effort_db_write(
                    lambda tx: tx.execute(
                        """
                        UPDATE trades
                        SET status = ?, exit_reason = ?, updated_at_ms = ?, closed_at_ms = ?
                        WHERE trade_id = ?
                        """,
                        (
                            TradeStatus.CLOSED.value,
                            ExitReason.STOP_WATCHDOG.value,
                            now_ms,
                            now_ms,
                            trade_id,
                        ),
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
                    exit_reason=ExitReason.STOP_WATCHDOG.value,
                )

            def _build_watchdog_ctx(sell_qty: Decimal) -> ActiveTradeContext:
                prorated_quote_spent = (
                    quote_spent * (sell_qty / orig_entry_qty)
                    if orig_entry_qty > Decimal("0") and sell_qty < orig_entry_qty
                    else quote_spent
                )
                return ActiveTradeContext(
                    trade_id=trade_id,
                    client_order_id=client_oid,
                    symbol=symbol,
                    base_asset=base_asset,
                    status=TradeStatus.CLOSING.value,
                    entry_order_id=None,
                    executed_qty=sell_qty,
                    avg_entry_price=avg_entry,
                    quote_spent=prorated_quote_spent,
                    commission_base_asset=Decimal("0"),
                    planned_stop_price=oco_stop_price,
                    planned_target_price=oco_stop_price,
                    time_barrier_end_ms=0,
                )

            try:
                return self._execute_emergency_market_sell(
                    _build_watchdog_ctx(q_sell),
                    q_sell,
                    reason=ExitReason.STOP_WATCHDOG,
                )
            except BinanceAPIError as sell_exc:
                if self._is_insufficient_balance_error(sell_exc):
                    # A1-05a: releia o saldo e tente UMA vez; se falhar de novo, vá para HALT_REVIEW com alerta.
                    logger.warning(
                        "Watchdog market sell failed with insufficient balance on %s trade #%s (qty=%s); "
                        "re-reading balance for single retry",
                        symbol,
                        trade_id,
                        q_sell,
                    )
                    free_retry = self.get_free_asset_balance(base_asset, retries=2)
                    if free_retry is not None:
                        q_retry = floor_qty_to_step(
                            min(remaining_from_oco, max(Decimal("0"), free_retry)),
                            symbol_filters.step_size,
                        )
                        if (
                            q_retry >= symbol_filters.min_qty
                            and (q_retry * ticker.bid) >= symbol_filters.min_notional
                        ):
                            try:
                                return self._execute_emergency_market_sell(
                                    _build_watchdog_ctx(q_retry),
                                    q_retry,
                                    reason=ExitReason.STOP_WATCHDOG,
                                )
                            except BinanceAPIError as retry_exc:
                                self._enter_halt_review(
                                    trade_id=trade_id,
                                    symbol=symbol,
                                    reason_msg=(
                                        f"Watchdog retry market sell failed after OCO cancel "
                                        f"(qty={to_api_str(q_retry)}, err={retry_exc})"
                                    ),
                                )
                                raise
                    self._enter_halt_review(
                        trade_id=trade_id,
                        symbol=symbol,
                        reason_msg=(
                            f"Watchdog insufficient balance after OCO cancel and balance re-read "
                            f"(free_retry={free_retry}, remaining_from_oco={to_api_str(remaining_from_oco)}, "
                            f"err={sell_exc})"
                        ),
                    )
                    raise
                self._emit_alert(
                    "WATCHDOG_ERROR",
                    f"Watchdog falhou ao vender {symbol} (trade #{trade_id}) após cancelar OCO: {sell_exc}",
                )
                raise

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
        1. Cancela a orderList OCO (DELETE /api/v3/orderList) e valida `listOrderStatus == ALL_DONE` (A1-02);
        2. Lê as quantidades já executadas nas pernas da OCO (incluindo `orders[] -> get_order`, A1-07);
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

            # Passo 1: Cancela a orderList OCO e confirma encerramento real (A1-02)
            cancel_resp: dict[str, Any] = {}
            try:
                cancel_resp = self.signed_client.cancel_order_list(symbol, oco_order_list_id)
            except (BinanceAPIError, RequestTimeoutUnknownState) as cancel_exc:
                try:
                    cancel_resp = self.signed_client.get_order_list(oco_order_list_id)
                except Exception:
                    raise cancel_exc
                list_status = str(cancel_resp.get("listOrderStatus", "")).upper()
                if list_status != "ALL_DONE":
                    self._best_effort_db_write(
                        lambda tx: tx.execute(
                            "UPDATE trades SET status = ?, updated_at_ms = ? WHERE trade_id = ?",
                            (TradeStatus.PROTECTED.value, self._now_ms(), trade_id),
                        )
                    )
                    logger.warning(
                        "Time-stop cancel failed for %s trade #%s and OCO %s remains %s; keeping PROTECTED",
                        symbol,
                        trade_id,
                        oco_order_list_id,
                        list_status or "UNKNOWN",
                    )
                    raise cancel_exc

            # Passo 2: Soma quantidades já executadas nas pernas da OCO (orderReports ou orders[] -> get_order)
            executed_on_oco, quote_from_oco, executed_confirmed = self._extract_oco_executed_qty(
                symbol, cancel_resp
            )
            tracked_oco_qty = to_decimal(oco_qty)
            remaining_from_oco = max(Decimal("0"), tracked_oco_qty - executed_on_oco)

            # Passo 3: Consulta o saldo livre atualizado na corretora (somente se ainda restar quantidade)
            if remaining_from_oco <= Decimal("0"):
                raw_rem = Decimal("0")
            else:
                free_base = self.get_free_asset_balance(base_asset, retries=2)
                if free_base is not None:
                    raw_rem = min(remaining_from_oco, max(Decimal("0"), free_base))
                elif executed_confirmed:
                    raw_rem = remaining_from_oco
                else:
                    raise BinanceAPIError(
                        503,
                        -1001,
                        "Unable to confirm OCO executedQty or free balance during time-stop; deferring to reconciler",
                        "GET /api/v3/account",
                    )
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
            orig_entry_qty = (
                to_decimal(str(row["entry_executed_qty"]))
                if row and row["entry_executed_qty"]
                else tracked_oco_qty
            )
            client_oid = str(row["client_order_id"]) if row else f"TIME_{symbol}_{trade_id}"

            # Verifica min_notional antes do Passo 4 para não crashar com -1013 NOTIONAL em resíduo de poeira
            if q_rem >= symbol_filters.min_qty:
                try:
                    t_now = self.signed_client.get_book_ticker(symbol, priority=Priority.CRITICAL)
                    if (q_rem * t_now.bid) < symbol_filters.min_notional:
                        q_rem = Decimal("0")
                except Exception:
                    pass

            # Passo 4: Vende a MARKET apenas a quantidade residual
            if q_rem >= symbol_filters.min_qty:
                prorated_quote_spent = (
                    quote_spent * (q_rem / orig_entry_qty)
                    if orig_entry_qty > Decimal("0") and q_rem < orig_entry_qty
                    else quote_spent
                )
                ctx = ActiveTradeContext(
                    trade_id=trade_id,
                    client_order_id=client_oid,
                    symbol=symbol,
                    base_asset=base_asset,
                    status=TradeStatus.CLOSING.value,
                    entry_order_id=None,
                    executed_qty=q_rem,
                    avg_entry_price=avg_entry,
                    quote_spent=prorated_quote_spent,
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
            if executed_on_oco > Decimal("0") and quote_from_oco > Decimal("0"):
                exit_avg = quote_from_oco / executed_on_oco
                pnl_usdt = quote_from_oco - quote_spent
                risk_unit = max(Decimal("0.00000001"), avg_entry - planned_stop)
                realized_r = float((exit_avg - avg_entry) / risk_unit)
                self._best_effort_db_write(
                    lambda tx: tx.execute(
                        """
                        UPDATE trades
                        SET status = ?, exit_reason = ?,
                            exit_avg_price = ?, exit_quote_received = ?,
                            realized_pnl_usdt = ?, realized_r = ?,
                            updated_at_ms = ?, closed_at_ms = ?
                        WHERE trade_id = ?
                        """,
                        (
                            TradeStatus.CLOSED.value,
                            ExitReason.TIME.value,
                            to_api_str(exit_avg),
                            to_api_str(quote_from_oco),
                            to_api_str(pnl_usdt),
                            realized_r,
                            now_ms,
                            now_ms,
                            trade_id,
                        ),
                    )
                )
            else:
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
