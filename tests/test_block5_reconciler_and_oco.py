"""Testes de Tortura do Gate 5 (Bloco 5): Timeout -1007, OCO Líquida sem BNB, Fallback -2010 no Boot, OCO Parcial, C1-D, C1-E e C2-3."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.auditing.outcome_resolver import (
    resolve_dual_target_horizon,
    resolve_single_target_horizon,
)
from src.core.binance_client import (
    BinanceAPIError,
    BookTicker,
    MonotonicClock,
    RequestTimeoutUnknownState,
    WeightGovernor,
    to_decimal,
)
from src.core.config import BinanceEnv, load_config
from src.core.database import CandleRow, Database, SymbolFilters
from src.core.state_reconciler import ReconcileAction, StateReconciler
from src.execution.executor_protocol import (
    ExecutionRequest,
    ExitReason,
    NoOpExecutor,
    TradeStatus,
)
from src.execution.oco_order_manager import OcoOrderManager
from src.execution.paper_executor import PaperExecutor
from src.risk.position_sizer import PositionSizer
from src.risk.shadow_manager import ShadowManager


def _sol_filters(symbol: str = "SOLUSDT") -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        status="TRADING",
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("5.00"),
        oco_allowed=True,
        fetched_at_ms=1_700_000_000_000,
    )


class _FakeBinanceExchange:
    """Dublê determinístico da Binance Spot API para simular os 4 Testes de Tortura."""

    def __init__(self) -> None:
        self.governor = WeightGovernor()
        self.clock = MonotonicClock()
        self.buy_calls: list[dict[str, Any]] = []
        self.oco_calls: list[dict[str, Any]] = []
        self.market_sell_calls: list[dict[str, Any]] = []
        self.cancel_oco_calls: list[int] = []

        self.raise_timeout_on_buy: bool = False
        self.order_exists_after_timeout: bool = True
        self.commission_in_base: str = "0.00"
        self.free_sol_balance: str = "10.00"
        self.locked_sol_balance: str = "0.00"
        self.current_bid: str = "100.00"
        self.current_ask: str = "100.05"
        self.raise_2010_on_oco: bool = False
        self.oco_list_status: str = "EXECUTING"
        self.oco_executed_qty_on_cancel: str = "0.00"

    def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
        return BookTicker(
            symbol=symbol,
            bid=to_decimal(self.current_bid),
            bid_qty=Decimal("100"),
            ask=to_decimal(self.current_ask),
            ask_qty=Decimal("100"),
        )

    def get_account(self, **_: Any) -> dict[str, Any]:
        return {
            "balances": [
                {"asset": "USDT", "free": "500.00", "locked": "0.00"},
                {"asset": "SOL", "free": self.free_sol_balance, "locked": self.locked_sol_balance},
                {"asset": "BNB", "free": "1.00", "locked": "0.00"},
            ]
        }

    def new_market_buy_quote(
        self, symbol: str, quote_qty: Decimal, client_order_id: str
    ) -> dict[str, Any]:
        self.buy_calls.append(
            {"symbol": symbol, "quote_qty": quote_qty, "client_order_id": client_order_id}
        )
        if self.raise_timeout_on_buy:
            raise RequestTimeoutUnknownState("Simulated -1007 timeout on POST /api/v3/order")
        return {
            "symbol": symbol,
            "orderId": 1001,
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": "10.00",
            "cummulativeQuoteQty": "1000.00",
            "fills": [
                {
                    "price": "100.00",
                    "qty": "10.00",
                    "commission": self.commission_in_base,
                    "commissionAsset": "SOL" if Decimal(self.commission_in_base) > 0 else "BNB",
                }
            ],
        }

    def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
        if not self.order_exists_after_timeout:
            raise BinanceAPIError(400, -2013, "Order does not exist.", "GET /api/v3/order")
        return {
            "symbol": symbol,
            "orderId": 1001,
            "clientOrderId": orig_client_order_id,
            "status": "FILLED",
            "executedQty": "10.00",
            "cummulativeQuoteQty": "1000.00",
            "fills": [
                {
                    "price": "100.00",
                    "qty": "10.00",
                    "commission": self.commission_in_base,
                    "commissionAsset": "SOL" if Decimal(self.commission_in_base) > 0 else "BNB",
                }
            ],
        }

    def new_oco_sell(
        self,
        symbol: str,
        qty: Decimal,
        take_profit: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal | None,
        list_client_order_id: str,
    ) -> dict[str, Any]:
        if self.raise_2010_on_oco:
            raise BinanceAPIError(
                400,
                -2010,
                "Order would trigger immediately.",
                "POST /api/v3/orderList/oco",
            )
        self.oco_calls.append(
            {
                "symbol": symbol,
                "qty": qty,
                "take_profit": take_profit,
                "stop_price": stop_price,
                "stop_limit_price": stop_limit_price,
                "list_client_order_id": list_client_order_id,
            }
        )
        return {
            "orderListId": 5000 + len(self.oco_calls),
            "listClientOrderId": list_client_order_id,
            "listOrderStatus": "EXECUTING",
        }

    def new_market_sell(
        self, symbol: str, qty: Decimal, client_order_id: str
    ) -> dict[str, Any]:
        self.market_sell_calls.append(
            {"symbol": symbol, "qty": qty, "client_order_id": client_order_id}
        )
        bid_d = Decimal(self.current_bid)
        quote_rec = (qty * bid_d).quantize(Decimal("0.01"))
        return {
            "symbol": symbol,
            "orderId": 9000 + len(self.market_sell_calls),
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": str(qty),
            "cummulativeQuoteQty": str(quote_rec),
            "fills": [{"price": self.current_bid, "qty": str(qty), "commission": "0", "commissionAsset": "BNB"}],
        }

    def get_order_list(self, order_list_id: int) -> dict[str, Any]:
        return {
            "orderListId": order_list_id,
            "listOrderStatus": self.oco_list_status,
            "orderReports": [
                {"executedQty": self.oco_executed_qty_on_cancel},
                {"executedQty": "0.00"},
            ],
        }

    def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
        self.cancel_oco_calls.append(order_list_id)
        return {
            "orderListId": order_list_id,
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"executedQty": self.oco_executed_qty_on_cancel},
                {"executedQty": "0.00"},
            ],
        }


def _make_exec_request(monkeypatch: pytest.MonkeyPatch) -> ExecutionRequest:
    monkeypatch.setenv("MODE", "SHADOW_CHAMELEON")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = load_config()
    sizer = PositionSizer(cfg.strategies.position_sizing, cfg.strategies.portfolio)
    flt = _sol_filters()
    sizing = sizer.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="94.00",
        target_price="112.00",
        free_usdt="6210.00",
        symbol_filters=flt,
    )
    return ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_000_000_000,
        atr_at_signal=2.0,
        time_barrier_end_ms=1_700_000_000_000 + 72 * 3_600_000,
        sizing=sizing,
        symbol_filters=flt,
    )


def test_gate5_torture1_timeout_1007_idempotent_recovery_via_orig_client_order_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Teste de Tortura 1:
    - Timeout simulado (-1007) no envio da compra a mercado recupera a ordem via origClientOrderId
      sem jamais disparar uma segunda compra duplicada, e cria a OCO normalmente.
    - Se a consulta origClientOrderId retornar -2013 (ordem não existiu), marca ENTRY_REJECTED.
    """
    req = _make_exec_request(monkeypatch)
    cfg = load_config()
    db = Database(tmp_path / "t1.sqlite")
    db.migrate()

    fake = _FakeBinanceExchange()
    fake.raise_timeout_on_buy = True
    fake.order_exists_after_timeout = True

    mgr = OcoOrderManager(
        db=db,
        signed_client=fake,  # type: ignore[arg-type]
        resilience_cfg=cfg.strategies.execution_and_resilience,
        environment=BinanceEnv.TESTNET,
    )
    rep = mgr.execute_entry(req)
    assert rep.executed is True
    assert rep.status == TradeStatus.PROTECTED.value
    assert len(fake.buy_calls) == 1  # PROIBIDO comprar duplicado!
    assert len(fake.oco_calls) == 1
    assert rep.filled_qty == Decimal("10.00")

    # Agora testa o caso em que o timeout ocorreu antes da ordem chegar à Binance (-2013)
    db2 = Database(tmp_path / "t1_missing.sqlite")
    db2.migrate()
    fake_missing = _FakeBinanceExchange()
    fake_missing.raise_timeout_on_buy = True
    fake_missing.order_exists_after_timeout = False
    mgr_missing = OcoOrderManager(
        db=db2,
        signed_client=fake_missing,  # type: ignore[arg-type]
        resilience_cfg=cfg.strategies.execution_and_resilience,
    )
    rep_missing = mgr_missing.execute_entry(req)
    assert rep_missing.executed is False
    assert rep_missing.status == TradeStatus.ENTRY_REJECTED.value
    assert len(fake_missing.buy_calls) == 1
    assert len(fake_missing.oco_calls) == 0
    db.close()
    db2.close()


def test_gate5_torture2_base_asset_commission_deduction_and_directional_tick_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Teste de Tortura 2:
    - Compra sem BNB desconta a taxa (0.01 SOL) do ativo base comprado (10.00 SOL -> 9.99 SOL)
      e cria a OCO com Q_oco = 9.99 sem erro -2010;
    - Verifica arredondamento direcional de tickSize (stop_limit_price < stop_price < take_profit).
    """
    req = _make_exec_request(monkeypatch)
    cfg = load_config()
    db = Database(tmp_path / "t2.sqlite")
    db.migrate()

    fake = _FakeBinanceExchange()
    fake.commission_in_base = "0.01"
    fake.free_sol_balance = "9.99"

    mgr = OcoOrderManager(
        db=db,
        signed_client=fake,  # type: ignore[arg-type]
        resilience_cfg=cfg.strategies.execution_and_resilience,
    )
    rep = mgr.execute_entry(req)
    assert rep.executed is True
    assert rep.status == TradeStatus.PROTECTED.value
    assert rep.filled_qty == Decimal("9.99")
    assert len(fake.oco_calls) == 1
    oco_sent = fake.oco_calls[0]
    assert oco_sent["qty"] == Decimal("9.99")
    assert oco_sent["stop_limit_price"] < oco_sent["stop_price"] < oco_sent["take_profit"]
    db.close()


def test_gate5_torture3_power_outage_price_below_stop_and_error_2010_market_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Teste de Tortura 3:
    - Simula queda de energia logo após o envio da compra (registro ficou em PENDING_ENTRY no SQLite),
      e quando o robô religa no boot o preço já caiu abaixo do stopPrice (93.50 < 94.00) ou a Binance
      devolve -2010 ("Order would trigger immediately").
    - Prova que no 1º segundo do boot o StateReconciler + OcoOrderManager disparam venda a MARKET
      imediata (FALLBACK_MARKET_2010), zerando a posição desprotegida!
    """
    monkeypatch.setenv("MODE", "SHADOW_CHAMELEON")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = load_config()

    db = Database(tmp_path / "t3.sqlite")
    db.migrate()

    # Insere estado deixado por queda de energia durante o POST da compra
    with db.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt, quote_order_qty,
                time_barrier_end_ms, created_at_ms, updated_at_ms
            ) VALUES (
                'CHM_SOLUSDT_BOOT1', 'TESTNET', 'SOLUSDT', 'bull_trend_pullback', 'BULL',
                1700000000000, 'PENDING_ENTRY',
                '100.00', '94.00', '112.00',
                2.0, 0.01, '10.00', '1000.00',
                1700259200000, 1700000000000, 1700000000000
            )
            """
        )

    fake = _FakeBinanceExchange()
    fake.current_bid = "93.20"  # Abaixo do stopPrice de 94.00!
    fake.raise_2010_on_oco = True
    fake.free_sol_balance = "10.00"

    mgr = OcoOrderManager(
        db=db,
        signed_client=fake,  # type: ignore[arg-type]
        resilience_cfg=cfg.strategies.execution_and_resilience,
    )
    reconciler = StateReconciler(
        db=db,
        signed_client=fake,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=cfg.execution_universe,
        filters_provider=_sol_filters,
    )

    res = reconciler.reconcile()
    assert res.action == ReconcileAction.RECOVERED_PENDING_TO_MARKET_FALLBACK
    assert len(fake.market_sell_calls) == 1
    assert fake.market_sell_calls[0]["qty"] == Decimal("10.00")

    with db.read() as conn:
        row = conn.execute("SELECT status, exit_reason FROM trades WHERE trade_id = 1").fetchone()
    assert row is not None
    assert str(row["status"]) == TradeStatus.CLOSED.value
    assert str(row["exit_reason"]) == ExitReason.FALLBACK_MARKET_2010.value
    db.close()


def test_gate5_torture4_partial_oco_40pct_reconcile_hanging_stop_watchdog_and_4step_time_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Teste de Tortura 4:
    - Execução parcial de 40% da OCO (4.00 SOL executados, 6.00 SOL livres desprotegidos) faz o
      StateReconciler proteger exatamente os 60% restantes (6.00 SOL);
    - Watchdog de Stop Pendurado: se o preço pular o stopLimitPrice com a OCO ainda aberta, cancela
      a OCO e vende a MARKET (STOP_WATCHDOG);
    - Time-Stop de 4 passos: cancela OCO, lê 4.00 SOL já executados na OCO, consulta saldo livre e
      vende a MARKET apenas os 6.00 SOL residuais (TIME).
    """
    req = _make_exec_request(monkeypatch)
    cfg = load_config()
    db = Database(tmp_path / "t4.sqlite")
    db.migrate()

    fake = _FakeBinanceExchange()
    mgr = OcoOrderManager(
        db=db,
        signed_client=fake,  # type: ignore[arg-type]
        resilience_cfg=cfg.strategies.execution_and_resilience,
    )
    rep = mgr.execute_entry(req)
    assert rep.status == TradeStatus.PROTECTED.value
    assert rep.trade_id is not None

    # 1. Simula execução parcial de 40% da OCO seguida de cancelamento/expiração deixando 6.00 SOL livres
    fake.free_sol_balance = "6.00"
    fake.locked_sol_balance = "0.00"
    fake.oco_list_status = "ALL_DONE"

    reconciler = StateReconciler(
        db=db,
        signed_client=fake,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=cfg.execution_universe,
        filters_provider=_sol_filters,
    )
    rec_res = reconciler.reconcile()
    assert rec_res.action == ReconcileAction.REPROTECTED_PARTIAL_OCO_RESIDUAL
    assert rec_res.protected_qty == Decimal("6.00")
    assert fake.oco_calls[-1]["qty"] == Decimal("6.00")

    # 2. Testa o Fechamento Atômico de 4 Passos no Time-Stop (com 2.50 SOL já executados na perna da OCO e 3.50 SOL restantes)
    fake.oco_list_status = "EXECUTING"
    fake.oco_executed_qty_on_cancel = "2.50"
    fake.free_sol_balance = "3.50"
    time_rep = mgr.close_on_time_barrier(
        trade_id=rep.trade_id,
        symbol="SOLUSDT",
        base_asset="SOL",
        oco_order_list_id=5002,
        oco_qty=Decimal("6.00"),
        symbol_filters=_sol_filters(),
    )
    assert time_rep.status == TradeStatus.CLOSED.value
    assert time_rep.exit_reason == ExitReason.TIME.value
    assert fake.market_sell_calls[-1]["qty"] == Decimal("3.50")

    # 3. Testa o Watchdog de Stop Pendurado (preço caiu para 90.00 < stopLimitPrice de ~91.65)
    fake.current_bid = "90.00"
    fake.oco_list_status = "EXECUTING"
    fake.free_sol_balance = "6.00"
    wd_rep = mgr.check_hanging_stop_watchdog(
        trade_id=rep.trade_id,
        symbol="SOLUSDT",
        base_asset="SOL",
        oco_order_list_id=5002,
        oco_stop_price=Decimal("94.00"),
        oco_stop_limit_price=Decimal("91.65"),
        symbol_filters=_sol_filters(),
    )
    assert wd_rep is not None
    assert wd_rep.exit_reason == ExitReason.STOP_WATCHDOG.value
    assert fake.market_sell_calls[-1]["qty"] == Decimal("6.00")
    db.close()


def test_gate5_c1d_halt_review_c1e_best_effort_post_fill_and_c23_paper_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Testes de Integração C1-D, C1-E e C2-3:
    - C1-D: Saldo excedente não rastreado (ex.: compra manual de 5.00 SOL na mesma conta) entra em HALT_REVIEW
      e JAMAIS vende a posição manual;
    - C1-E: Falha de escrita no SQLite imediatamente após o fill da compra NUNCA interrompe a criação da OCO;
    - C2-3: PaperExecutor + outcome_resolver resolvem ambiguidade intra-vela pelo pior caso (SAME_CANDLE_STOP)
      e respeitam o índice ux_shadow_single_slot.
    """
    req = _make_exec_request(monkeypatch)
    cfg = load_config()

    # --- 1. Teste C1-D (Posição manual não rastreada -> HALT_REVIEW, zero vendas) ---
    db_c1d = Database(tmp_path / "c1d.sqlite")
    db_c1d.migrate()
    fake_c1d = _FakeBinanceExchange()
    fake_c1d.free_sol_balance = "5.00"  # 5.00 SOL ($500) comprados manualmente sem trade no banco!
    sm = ShadowManager(
        cfg.strategies.drawdown_and_shadow_protection,
        cfg.strategies.portfolio,
        db=db_c1d,
    )
    mgr_c1d = OcoOrderManager(
        db=db_c1d,
        signed_client=fake_c1d,  # type: ignore[arg-type]
        resilience_cfg=cfg.strategies.execution_and_resilience,
    )
    reconciler_c1d = StateReconciler(
        db=db_c1d,
        signed_client=fake_c1d,  # type: ignore[arg-type]
        oco_manager=mgr_c1d,
        execution_universe=cfg.execution_universe,
        filters_provider=_sol_filters,
        shadow_manager=sm,
    )
    res_c1d = reconciler_c1d.reconcile()
    assert res_c1d.action == ReconcileAction.HALT_REVIEW_UNTRACKED_BALANCE
    assert res_c1d.untracked_qty == Decimal("5.00")
    assert len(fake_c1d.market_sell_calls) == 0  # JAMAIS vende posição manual!
    assert sm.evaluate_defense(current_equity_usdt="300.00").emergency_halt is True
    db_c1d.close()

    # --- 2. Teste C1-E (Falha de SQLite pós-fill NUNCA interrompe criação da OCO) ---
    db_c1e = Database(tmp_path / "c1e.sqlite")
    db_c1e.migrate()
    fake_c1e = _FakeBinanceExchange()
    mgr_c1e = OcoOrderManager(
        db=db_c1e,
        signed_client=fake_c1e,  # type: ignore[arg-type]
        resilience_cfg=cfg.strategies.execution_and_resilience,
    )
    orig_write_tx = db_c1e.write_tx
    write_count = 0

    def _flaky_write_tx():
        nonlocal write_count
        write_count += 1
        # A 1ª chamada é o PENDING_ENTRY (obrigatória); as chamadas pós-fill simulam falha de I/O no SQLite!
        if write_count >= 2:
            raise sqlite3.OperationalError("Simulated disk full / lock after fill (C1-E)")
        return orig_write_tx()

    monkeypatch.setattr(db_c1e, "write_tx", _flaky_write_tx)
    rep_c1e = mgr_c1e.execute_entry(req)
    assert rep_c1e.executed is True
    assert rep_c1e.status == TradeStatus.PROTECTED.value
    assert len(fake_c1e.oco_calls) == 1  # A OCO foi criada na Binance apesar da falha do SQLite!
    assert mgr_c1e.last_in_memory_context is not None
    assert mgr_c1e.last_in_memory_context.post_fill_db_write_failed is True
    db_c1e.close()

    # --- 3. Teste C2-3 (PaperExecutor + outcome_resolver.py com pior caso intra-vela) ---
    db_shd = Database(tmp_path / "shadow.sqlite")
    db_shd.migrate()
    fake_pub = _FakeBinanceExchange()
    paper = PaperExecutor(
        db=db_shd,
        public_client=fake_pub,  # type: ignore[arg-type]
        sizing_cfg=cfg.strategies.position_sizing,
    )
    shd_rep1 = paper.execute_entry(req)
    assert shd_rep1.executed is True
    assert shd_rep1.status == "OPEN"

    # Segunda tentativa enquanto aberta é bloqueada por Single-Slot (SLOT_OCCUPIED)
    shd_rep2 = paper.execute_entry(req)
    assert shd_rep2.executed is False
    assert shd_rep2.status == "SLOT_OCCUPIED"

    # Vela ambígua em t+1 que toca simultaneamente o Stop (93.0 <= 94.0) e o Target (113.0 >= 112.0) -> SAME_CANDLE_STOP!
    ambiguous_bar = CandleRow(
        symbol="SOLUSDT",
        interval="1h",
        open_time_ms=1_700_000_000_000,
        close_time_ms=1_700_003_600_000,
        open=100.0,
        high=113.0,
        low=93.0,
        close=105.0,
        volume=1000.0,
        quote_volume=100_000.0,
        number_of_trades=500,
        taker_buy_base_volume=500.0,
        taker_buy_quote_volume=50_000.0,
        ingested_at_ms=1_700_003_600_100,
    )
    resolution = paper.evaluate_open_shadow_trade({"SOLUSDT": [ambiguous_bar]})
    assert resolution is not None
    assert resolution.status == "SAME_CANDLE_STOP"
    assert resolution.same_candle_ambiguity is True
    assert resolution.r_gross == -1.0
    assert paper.has_open_shadow_trade() is False

    # Testa também resolve_dual_target_horizon (T1 -> T2) e NoOpExecutor
    clean_bar = CandleRow(
        symbol="SOLUSDT",
        interval="1h",
        open_time_ms=1_700_000_000_000,
        close_time_ms=1_700_003_600_000,
        open=100.0,
        high=115.0,
        low=99.0,
        close=114.0,
        volume=1000.0,
        quote_volume=100_000.0,
        number_of_trades=500,
        taker_buy_base_volume=600.0,
        taker_buy_quote_volume=60_000.0,
        ingested_at_ms=1_700_003_600_100,
    )
    dual_res = resolve_dual_target_horizon(
        signal_close_time_ms=1_700_000_000_000,
        entry_price=100.0,
        stop_price=95.0,
        target1_price=105.0,
        target2_price=110.0,
        horizon_end_ms=1_700_086_400_000,
        candles=[clean_bar],
    )
    assert dual_res.status == "T2"
    assert dual_res.r_gross == pytest.approx(1.5)
    assert NoOpExecutor().execute_entry(req).executed is False
    db_shd.close()
