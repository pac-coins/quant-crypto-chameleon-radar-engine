"""Regression test suite for all findings in docs/audit-2026-11-23.md (Cat. 1, 2 and 3)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.core.binance_client import (
    BinanceAPIError,
    BookTicker,
    ExchangeInfoCache,
    IPBannedError,
    MonotonicClock,
    Priority,
    RateLimitedError,
    RequestTimeoutUnknownState,
    SignedBinanceClient,
    SystemStatus,
    WeightBudgetExceeded,
    WeightGovernor,
)
from src.core.config import (
    AnalystConfig,
    BinanceEnv,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_yaml_strict,
)
from src.core.database import CandleRow, Database, SymbolFilters


def _sol_filters(symbol: str = "SOLUSDT", *, oco_allowed: bool = True, status: str = "TRADING") -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        status=status,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("5.00"),
        oco_allowed=oco_allowed,
        fetched_at_ms=1_700_000_000_000,
    )


def _load_cfgs() -> tuple[StrategiesConfig, AnalystConfig]:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    return st, an


def _insert_trade(
    db: Database,
    *,
    status: str,
    symbol: str = "SOLUSDT",
    client_order_id: str = "CHM_SOLUSDT_1700000000000",
    entry_executed_qty: str | None = "10.00",
    oco_order_list_id: int | None = 5001,
    oco_qty: str | None = "10.00",
    oco_stop_price: str | None = "94.00",
    oco_stop_limit_price: str | None = "91.65",
    oco_target_price: str | None = "112.00",
    planned_entry_price: str = "100.00",
    planned_stop_price: str = "94.00",
    planned_target_price: str = "112.00",
    time_barrier_end_ms: int = 1_700_259_200_000,
    created_at_ms: int = 1_700_000_000_000,
) -> int:
    with db.write_tx() as tx:
        cur = tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt, quote_order_qty,
                entry_executed_qty, entry_avg_price, entry_quote_spent,
                oco_order_list_id, oco_qty, oco_stop_price, oco_stop_limit_price, oco_target_price,
                time_barrier_end_ms, created_at_ms, updated_at_ms
            ) VALUES (?, 'TESTNET', ?, 'bull_trend_pullback', 'BULL',
                1700000000000, ?,
                ?, ?, ?,
                2.0, 0.01, '10.00', '1000.00',
                ?, ?, '1000.00',
                ?, ?, ?, ?, ?,
                ?, ?, ?)
            """,
            (
                client_order_id,
                symbol,
                status,
                planned_entry_price,
                planned_stop_price,
                planned_target_price,
                entry_executed_qty,
                planned_entry_price if entry_executed_qty else None,
                oco_order_list_id,
                oco_qty,
                oco_stop_price,
                oco_stop_limit_price,
                oco_target_price,
                time_barrier_end_ms,
                created_at_ms,
                created_at_ms,
            ),
        )
        return int(cur.lastrowid)


def _write_temp_ed25519(path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


# ==============================================================================
# Commit 1: SignedBinanceClient & WeightGovernor (A1-04, A2-03, query helpers)
# ==============================================================================
def test_audit_a1_04_http_503_on_market_buy_raises_unknown_state(tmp_path: Path) -> None:
    pem_path = _write_temp_ed25519(tmp_path / "ed25519.pem")
    session = MagicMock(spec=requests.Session)
    resp_503 = MagicMock(spec=requests.Response)
    resp_503.status_code = 503
    resp_503.headers = {}
    resp_503.json.return_value = {"code": -1001, "msg": "Internal error; unable to process your request."}
    resp_503.text = "Service Unavailable"
    session.request.return_value = resp_503

    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        WeightGovernor(),
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=pem_path,
        testnet=True,
        session=session,
    )
    with pytest.raises(RequestTimeoutUnknownState):
        client.new_market_buy_quote("SOLUSDT", Decimal("1000.00"), "CHM_SOLUSDT_503")

    # Also HTML 502 with code=None on POST must raise RequestTimeoutUnknownState
    resp_502 = MagicMock(spec=requests.Response)
    resp_502.status_code = 502
    resp_502.headers = {}
    resp_502.json.side_effect = ValueError("not json")
    resp_502.text = "<html>502 Bad Gateway</html>"
    session.request.return_value = resp_502
    with pytest.raises(RequestTimeoutUnknownState):
        client.new_oco_sell("SOLUSDT", Decimal("10"), Decimal("112"), Decimal("94"), Decimal("91.65"), "OCO_1")


def test_audit_a2_03_weight_governor_window_and_retry_after(tmp_path: Path) -> None:
    now_s = [59.0]
    gov = WeightGovernor(limit_per_min=6000, high_ceiling=0.75, low_ceiling=0.50, now=lambda: now_s[0])
    gov.update_from_headers({"X-MBX-USED-WEIGHT-1M": "4400"})
    now_s[0] = 61.0  # 2s later: must NOT wipe 4400 on arbitrary monotonic // 60 rollover
    with pytest.raises(WeightBudgetExceeded):
        gov.acquire(50, Priority.LOW)

    # Authoritative lower header from Binance UTC rollover resets window immediately
    now_s[0] = 70.0
    gov.update_from_headers({"X-MBX-USED-WEIGHT-1M": "20"})
    assert gov.used_weight_1m == 20
    gov.acquire(50, Priority.LOW)

    # RateLimitedError & IPBannedError preserve retry_after_s, and governor tracks remaining cooldown
    pem_path = _write_temp_ed25519(tmp_path / "ed25519_rl.pem")
    session = MagicMock(spec=requests.Session)
    resp_429 = MagicMock(spec=requests.Response)
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "15", "X-MBX-USED-WEIGHT-1M": "6000"}
    resp_429.json.return_value = {"code": -1003, "msg": "Too many requests"}
    resp_429.text = "Too many requests"
    session.request.return_value = resp_429

    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        gov,
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=pem_path,
        testnet=True,
        session=session,
    )
    with pytest.raises(RateLimitedError) as rl_info:
        client.get_account()
    assert rl_info.value.retry_after_s == pytest.approx(15.0)
    assert gov.retry_after_remaining_s == pytest.approx(15.0)

    # get_order(order_id=...), get_order_list(orig_client_order_id=...), and get_my_trades()
    ok_resp = MagicMock(spec=requests.Response)
    ok_resp.status_code = 200
    ok_resp.headers = {}
    ok_resp.content = b"[]"
    ok_resp.json.return_value = {"orderId": 42}
    session.request.return_value = ok_resp
    assert client.get_order("SOLUSDT", order_id=42)["orderId"] == 42
    assert client.get_order_list(orig_client_order_id="OCO_SOL_1")["orderId"] == 42
    ok_resp.json.return_value = [{"id": 1, "commission": "0.01", "commissionAsset": "SOL"}]
    assert client.get_my_trades("SOLUSDT", order_id=42)[0]["commissionAsset"] == "SOL"


# ==============================================================================
# Commit 2: OcoOrderManager entry pre-flight, OCO timeout, -2010/TP, and A2-04
# ==============================================================================
def test_audit_a1_03b_oco_post_timeout_recovers_idempotently(tmp_path: Path) -> None:
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import ActiveTradeContext, OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_03b.db")
    db.migrate()
    trade_id = _insert_trade(db, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    class _OcoTimeoutExchange:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.oco_posts = 0

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("100.00"), Decimal("100"), Decimal("100.05"), Decimal("100"))

        def new_oco_sell(self, **kwargs: Any) -> dict[str, Any]:
            self.oco_posts += 1
            raise RequestTimeoutUnknownState("Simulated -1007 timeout on POST /api/v3/orderList/oco")

        def get_open_order_lists(self) -> list[dict[str, Any]]:
            return [{"orderListId": 5001, "symbol": "SOLUSDT", "listOrderStatus": "EXECUTING"}]

    ex = _OcoTimeoutExchange()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    ctx = ActiveTradeContext(
        trade_id=trade_id,
        client_order_id="CHM_SOLUSDT_1700000000000",
        symbol="SOLUSDT",
        base_asset="SOL",
        status=TradeStatus.ENTRY_FILLED.value,
        entry_order_id=1001,
        executed_qty=Decimal("10.00"),
        avg_entry_price=Decimal("100.00"),
        quote_spent=Decimal("1000.00"),
        commission_base_asset=Decimal("0"),
        planned_stop_price=Decimal("94.00"),
        planned_target_price=Decimal("112.00"),
        time_barrier_end_ms=1_700_259_200_000,
    )
    rep = mgr.protect_or_fallback_market(ctx, _sol_filters())
    assert rep.status == TradeStatus.PROTECTED.value
    assert rep.oco_order_list_id == 5001
    assert ex.oco_posts == 1  # Never sent a duplicate OCO!
    db.close()


def test_audit_a1_11a_price_above_take_profit_exits_at_market_target(tmp_path: Path) -> None:
    from src.execution.executor_protocol import ExitReason, TradeStatus
    from src.execution.oco_order_manager import ActiveTradeContext, OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_11a.db")
    db.migrate()
    trade_id = _insert_trade(db, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    class _ExchangePumpedPastTarget:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.sells: list[Decimal] = []

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("115.00"), Decimal("10"), Decimal("115.05"), Decimal("10"))

        def new_oco_sell(self, **_: Any) -> dict[str, Any]:
            raise BinanceAPIError(
                400,
                -2010,
                "The relationship of the prices for the orders is not correct.",
                "POST /api/v3/orderList/oco",
            )

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sells.append(qty)
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("115.00")), "fills": []}

    ex = _ExchangePumpedPastTarget()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    ctx = ActiveTradeContext(
        trade_id=trade_id,
        client_order_id="CHM_SOLUSDT_1700000000000",
        symbol="SOLUSDT",
        base_asset="SOL",
        status=TradeStatus.ENTRY_FILLED.value,
        entry_order_id=1001,
        executed_qty=Decimal("10.00"),
        avg_entry_price=Decimal("100.00"),
        quote_spent=Decimal("1000.00"),
        commission_base_asset=Decimal("0"),
        planned_stop_price=Decimal("94.00"),
        planned_target_price=Decimal("112.00"),
        time_barrier_end_ms=1_700_259_200_000,
    )
    rep = mgr.protect_or_fallback_market(ctx, _sol_filters())
    assert rep.status == TradeStatus.CLOSED.value
    assert rep.exit_reason == ExitReason.TARGET.value
    assert ex.sells == [Decimal("10.00")]
    db.close()


def test_audit_a1_11b_unrelated_2010_plus_ticker_error_does_not_liquidate_healthy_position(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import ActiveTradeContext, OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_11b.db")
    db.migrate()
    trade_id = _insert_trade(db, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    class _ExchangeUnrelated2010:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.ticker_calls = 0
            self.market_sells: list[Decimal] = []

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            self.ticker_calls += 1
            if self.ticker_calls == 1:
                return BookTicker(symbol, Decimal("105.00"), Decimal("10"), Decimal("105.05"), Decimal("10"))
            raise requests.ConnectionError("transient drop on second ticker call")

        def new_oco_sell(self, **_: Any) -> dict[str, Any]:
            raise BinanceAPIError(400, -2010, "Filter failure: MAX_NUM_ALGO_ORDERS", "POST /api/v3/orderList/oco")

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.market_sells.append(qty)
            return {"executedQty": str(qty), "cummulativeQuoteQty": "1050.00", "fills": []}

    ex = _ExchangeUnrelated2010()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    ctx = ActiveTradeContext(
        trade_id=trade_id,
        client_order_id="CHM_SOLUSDT_1700000000000",
        symbol="SOLUSDT",
        base_asset="SOL",
        status=TradeStatus.ENTRY_FILLED.value,
        entry_order_id=1001,
        executed_qty=Decimal("10.00"),
        avg_entry_price=Decimal("100.00"),
        quote_spent=Decimal("1000.00"),
        commission_base_asset=Decimal("0"),
        planned_stop_price=Decimal("94.00"),
        planned_target_price=Decimal("112.00"),
        time_barrier_end_ms=1_700_259_200_000,
    )
    with pytest.raises(BinanceAPIError):
        mgr.protect_or_fallback_market(ctx, _sol_filters())
    assert ex.market_sells == []
    db.close()


def test_audit_a1_11c_preflight_rejects_lot_failing_oco_stop_limit_notional_or_oco_disabled(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import ExecutionRequest, TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.position_sizer import PositionSizer

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_11c.db")
    db.migrate()
    flt = _sol_filters()
    sizing = PositionSizer(st.position_sizing, st.portfolio).evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="10.00",
        stop_price="9.40",
        target_price="11.20",
        free_usdt="33.00",
        symbol_filters=flt,
    )
    assert sizing.approved is True
    assert sizing.base_qty == Decimal("0.53")

    class _ExchangeMustNotBeBought:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.buy_calls = 0

        def new_market_buy_quote(self, **_: Any) -> dict[str, Any]:
            self.buy_calls += 1
            raise AssertionError("Must reject in pre-flight BEFORE sending market buy!")

    ex = _ExchangeMustNotBeBought()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    req = ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_000_000_000,
        atr_at_signal=0.3,
        time_barrier_end_ms=1_700_259_200_000,
        sizing=sizing,
        symbol_filters=flt,
    )
    rep = mgr.execute_entry(req)
    assert rep.executed is False
    assert rep.status == TradeStatus.ENTRY_REJECTED.value
    assert "MIN_NOTIONAL" in (rep.error_msg or "")
    assert ex.buy_calls == 0

    # Also rejects when oco_allowed=False before buying
    flt_no_oco = _sol_filters(oco_allowed=False)
    sizing_ok = PositionSizer(st.position_sizing, st.portfolio).evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="94.00",
        target_price="112.00",
        free_usdt="6210.00",
        symbol_filters=flt_no_oco,
    )
    req_no_oco = ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_000_000_000,
        atr_at_signal=2.0,
        time_barrier_end_ms=1_700_259_200_000,
        sizing=sizing_ok,
        symbol_filters=flt_no_oco,
    )
    rep_no_oco = mgr.execute_entry(req_no_oco)
    assert rep_no_oco.executed is False
    assert rep_no_oco.status == TradeStatus.ENTRY_REJECTED.value
    assert ex.buy_calls == 0
    db.close()


def test_audit_a2_04_commission_recovery_via_mytrades_and_conservative_estimate_alert(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import ExecutionRequest, TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.position_sizer import PositionSizer

    st, _ = _load_cfgs()
    flt = _sol_filters()
    sizing = PositionSizer(st.position_sizing, st.portfolio).evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="94.00",
        target_price="112.00",
        free_usdt="6210.00",
        symbol_filters=flt,
    )
    req = ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_000_000_000,
        atr_at_signal=2.0,
        time_barrier_end_ms=1_700_259_200_000,
        sizing=sizing,
        symbol_filters=flt,
    )

    # Case 1: get_order has no fills, but myTrades succeeds -> exact 9.99 SOL without estimated alert
    db1 = Database(tmp_path / "a2_04_exact.db")
    db1.migrate()
    alerts1: list[tuple[str, str]] = []

    class _ExchangeWithMyTrades:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.oco_qty: Decimal | None = None

        def new_market_buy_quote(self, **_: Any) -> dict[str, Any]:
            raise RequestTimeoutUnknownState("-1007 timeout")

        def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "orderId": 1001,
                "clientOrderId": orig_client_order_id,
                "status": "FILLED",
                "executedQty": "10.00",
                "cummulativeQuoteQty": "1000.00",
            }

        def get_my_trades(self, symbol: str, *, order_id: int) -> list[dict[str, Any]]:
            return [{"id": 1, "orderId": order_id, "commission": "0.01", "commissionAsset": "SOL"}]

        def get_account(self, **_: Any) -> dict[str, Any]:
            raise BinanceAPIError(503, -1001, "Account temporarily unavailable", "GET /api/v3/account")

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("100.00"), Decimal("10"), Decimal("100.05"), Decimal("10"))

        def new_oco_sell(self, symbol: str, qty: Decimal, **_: Any) -> dict[str, Any]:
            self.oco_qty = qty
            assert qty <= Decimal("9.99")
            return {"orderListId": 5001, "listOrderStatus": "EXECUTING"}

    ex1 = _ExchangeWithMyTrades()
    mgr1 = OcoOrderManager(
        db=db1,
        signed_client=ex1,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        alert_callback=lambda k, t: alerts1.append((k, t)),
        sleep_fn=lambda _: None,
    )
    rep1 = mgr1.execute_entry(req)
    assert rep1.status == TradeStatus.PROTECTED.value
    assert ex1.oco_qty == Decimal("9.99")
    assert alerts1 == []  # Exact via myTrades -> no estimate warning!
    db1.close()

    # Case 2: get_order has no fills, myTrades fails, AND get_account fails after retries ->
    # applies -0.1% estimate (9.99 SOL), persists in SQLite (order_events + system_state), sends Telegram alert!
    db2 = Database(tmp_path / "a2_04_est.db")
    db2.migrate()
    alerts2: list[tuple[str, str]] = []

    class _ExchangeAllThreeExhausted:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.account_calls = 0
            self.my_trades_calls = 0
            self.oco_qty: Decimal | None = None

        def new_market_buy_quote(self, **_: Any) -> dict[str, Any]:
            raise RequestTimeoutUnknownState("-1007 timeout")

        def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "orderId": 1001,
                "clientOrderId": orig_client_order_id,
                "status": "FILLED",
                "executedQty": "10.00",
                "cummulativeQuoteQty": "1000.00",
            }

        def get_my_trades(self, symbol: str, *, order_id: int) -> list[dict[str, Any]]:
            self.my_trades_calls += 1
            raise BinanceAPIError(503, -1001, "myTrades down", "GET /api/v3/myTrades")

        def get_account(self, **_: Any) -> dict[str, Any]:
            self.account_calls += 1
            raise BinanceAPIError(503, -1001, "Account temporarily unavailable", "GET /api/v3/account")

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("100.00"), Decimal("10"), Decimal("100.05"), Decimal("10"))

        def new_oco_sell(self, symbol: str, qty: Decimal, **_: Any) -> dict[str, Any]:
            self.oco_qty = qty
            if qty > Decimal("9.99"):
                raise BinanceAPIError(
                    400, -2010, "Account has insufficient balance for requested action.", "POST /api/v3/orderList/oco"
                )
            return {"orderListId": 5001, "listOrderStatus": "EXECUTING"}

    ex2 = _ExchangeAllThreeExhausted()
    mgr2 = OcoOrderManager(
        db=db2,
        signed_client=ex2,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        alert_callback=lambda k, t: alerts2.append((k, t)),
        sleep_fn=lambda _: None,
    )
    rep2 = mgr2.execute_entry(req)
    assert rep2.status == TradeStatus.PROTECTED.value
    assert ex2.my_trades_calls == 2  # Exhausted retries
    assert ex2.account_calls == 2    # Exhausted retries
    assert ex2.oco_qty == Decimal("9.99")
    assert mgr2.last_in_memory_context is not None
    assert mgr2.last_in_memory_context.commission_estimated is True
    assert db2.get_state("trade_1_oco_qty_estimated") == "9.99"
    with db2.read() as conn:
        ev = conn.execute("SELECT method, endpoint FROM order_events WHERE method = 'FALLBACK_ESTIMATE'").fetchone()
    assert ev is not None and ev["endpoint"] == "local://oco_qty_estimated_fee"
    assert len(alerts2) == 1 and alerts2[0][0] == "OCO_QTY_ESTIMATED"
    db2.close()


# =============================================================================
# Commit 3 — Time-Stop (A1-02, A1-07) & Hanging-Stop Watchdog (A1-05a/b)
# =============================================================================


def test_audit_a1_02_time_stop_must_not_mark_closed_when_cancel_fails_and_oco_still_executing(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_02.db")
    db.migrate()
    trade_id = _insert_trade(
        db, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00"
    )

    class _ExchangeCancelFailsOcoStillOpen:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            raise BinanceAPIError(503, -1001, "Internal error; unable to process", "DELETE /api/v3/orderList")

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "EXECUTING", "orders": []}

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "0.00", "locked": "10.00"}]}

    ex = _ExchangeCancelFailsOcoStillOpen()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )

    with pytest.raises(BinanceAPIError):
        mgr.close_on_time_barrier(
            trade_id=trade_id,
            symbol="SOLUSDT",
            base_asset="SOL",
            oco_order_list_id=5001,
            oco_qty=Decimal("10.00"),
            symbol_filters=_sol_filters(),
        )

    with db.read() as conn:
        row = conn.execute("SELECT status FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
    db.close()
    assert row is not None and row["status"] == TradeStatus.PROTECTED.value


def test_audit_a1_05a_watchdog_cancels_oco_then_uses_remaining_from_oco_on_account_error(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import ExitReason, TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_05a.db")
    db.migrate()
    trade_id = _insert_trade(
        db, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00"
    )

    class _ExchangeWatchdogAccountFails:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.canceled: list[int] = []
            self.market_sells: list[Decimal] = []
            self.was_in_critical_section = False

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("89.00"), Decimal("10"), Decimal("89.10"), Decimal("10"))

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "EXECUTING"}

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            from src.core.binance_client import Priority, WeightBudgetExceeded

            try:
                self.governor.acquire(1, Priority.LOW)
                self.was_in_critical_section = False
            except WeightBudgetExceeded:
                self.was_in_critical_section = True
            self.canceled.append(order_list_id)
            return {"orderListId": order_list_id, "listOrderStatus": "ALL_DONE"}

        def get_account(self, **_: Any) -> dict[str, Any]:
            raise BinanceAPIError(503, -1001, "Temporary error", "GET /api/v3/account")

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.market_sells.append(qty)
            return {"executedQty": str(qty), "cummulativeQuoteQty": "890.00", "fills": []}

    ex = _ExchangeWatchdogAccountFails()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    rep = mgr.check_hanging_stop_watchdog(
        trade_id=trade_id,
        symbol="SOLUSDT",
        base_asset="SOL",
        oco_order_list_id=5001,
        oco_stop_price=Decimal("94.00"),
        oco_stop_limit_price=Decimal("91.65"),
        symbol_filters=_sol_filters(),
    )
    db.close()
    assert ex.was_in_critical_section is True
    assert len(ex.canceled) == 1
    assert rep is not None
    assert rep.status == TradeStatus.CLOSED.value
    assert rep.exit_reason == ExitReason.STOP_WATCHDOG.value
    assert ex.market_sells == [Decimal("10.00")]


def test_audit_a1_05a_watchdog_insufficient_balance_retries_once_then_halt_review_with_alert(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import ExitReason, TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.shadow_manager import ShadowManager

    st, _ = _load_cfgs()

    # Case 1: First sell fails with -2010 insufficient balance, balance re-read shows 7.50 SOL -> single retry succeeds!
    db1 = Database(tmp_path / "a1_05a_retry_ok.db")
    db1.migrate()
    tid1 = _insert_trade(
        db1, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00"
    )
    alerts1: list[tuple[str, str]] = []

    class _ExchangeRetrySucceeds:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.account_calls = 0
            self.sell_attempts: list[Decimal] = []

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("89.00"), Decimal("10"), Decimal("89.10"), Decimal("10"))

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "EXECUTING"}

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "ALL_DONE"}

        def get_account(self, **_: Any) -> dict[str, Any]:
            self.account_calls += 1
            if self.account_calls <= 2:
                raise BinanceAPIError(503, -1001, "Account down initially", "GET /api/v3/account")
            return {"balances": [{"asset": "SOL", "free": "7.50", "locked": "0.00"}]}

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sell_attempts.append(qty)
            if qty > Decimal("7.50"):
                raise BinanceAPIError(
                    400, -2010, "Account has insufficient balance for requested action.", "POST /api/v3/order"
                )
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("89.00")), "fills": []}

    ex1 = _ExchangeRetrySucceeds()
    mgr1 = OcoOrderManager(
        db=db1,
        signed_client=ex1,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        alert_callback=lambda k, t: alerts1.append((k, t)),
        sleep_fn=lambda _: None,
    )
    rep1 = mgr1.check_hanging_stop_watchdog(
        trade_id=tid1,
        symbol="SOLUSDT",
        base_asset="SOL",
        oco_order_list_id=5001,
        oco_stop_price=Decimal("94.00"),
        oco_stop_limit_price=Decimal("91.65"),
        symbol_filters=_sol_filters(),
    )
    assert rep1 is not None and rep1.status == TradeStatus.CLOSED.value
    assert rep1.exit_reason == ExitReason.STOP_WATCHDOG.value
    assert ex1.sell_attempts == [Decimal("10.00"), Decimal("7.50")]
    assert alerts1 == []
    db1.close()

    # Case 2: Retry ALSO fails with -2010 insufficient balance -> MUST enter HALT_REVIEW + emergency_halt + alert!
    db2 = Database(tmp_path / "a1_05a_retry_halt.db")
    db2.migrate()
    tid2 = _insert_trade(
        db2, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00"
    )
    alerts2: list[tuple[str, str]] = []
    sm2 = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db2)

    class _ExchangeRetryStillInsufficient:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.account_calls = 0
            self.sell_attempts: list[Decimal] = []

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("89.00"), Decimal("10"), Decimal("89.10"), Decimal("10"))

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "EXECUTING"}

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "ALL_DONE"}

        def get_account(self, **_: Any) -> dict[str, Any]:
            self.account_calls += 1
            if self.account_calls <= 2:
                raise BinanceAPIError(503, -1001, "Account down initially", "GET /api/v3/account")
            return {"balances": [{"asset": "SOL", "free": "8.00", "locked": "0.00"}]}

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sell_attempts.append(qty)
            raise BinanceAPIError(
                400, -2010, "Account has insufficient balance for requested action.", "POST /api/v3/order"
            )

    ex2 = _ExchangeRetryStillInsufficient()
    mgr2 = OcoOrderManager(
        db=db2,
        signed_client=ex2,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        alert_callback=lambda k, t: alerts2.append((k, t)),
        shadow_manager=sm2,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(BinanceAPIError):
        mgr2.check_hanging_stop_watchdog(
            trade_id=tid2,
            symbol="SOLUSDT",
            base_asset="SOL",
            oco_order_list_id=5001,
            oco_stop_price=Decimal("94.00"),
            oco_stop_limit_price=Decimal("91.65"),
            symbol_filters=_sol_filters(),
        )

    assert ex2.sell_attempts == [Decimal("10.00"), Decimal("8.00")]  # Tried once more, never looped!
    with db2.read() as conn:
        row2 = conn.execute("SELECT status FROM trades WHERE trade_id = ?", (tid2,)).fetchone()
    assert row2 is not None and row2["status"] == TradeStatus.HALT_REVIEW.value
    assert db2.get_state("emergency_halt") == "1"
    assert sm2.evaluate_defense(current_equity_usdt="300.00").emergency_halt is True
    assert len(alerts2) == 1 and alerts2[0][0] == "KILL_SWITCH"
    db2.close()


def test_audit_a1_05b_watchdog_must_not_sell_untracked_manual_balance_beyond_oco_qty(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_05b.db")
    db.migrate()
    trade_id = _insert_trade(
        db, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00"
    )

    class _ExchangeWithManualSol:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.sold_qty: Decimal | None = None

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("89.00"), Decimal("10"), Decimal("89.10"), Decimal("10"))

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "EXECUTING"}

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "ALL_DONE"}

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "15.00", "locked": "0.00"}]}

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sold_qty = qty
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("89.00")), "fills": []}

    ex = _ExchangeWithManualSol()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    mgr.check_hanging_stop_watchdog(
        trade_id=trade_id,
        symbol="SOLUSDT",
        base_asset="SOL",
        oco_order_list_id=5001,
        oco_stop_price=Decimal("94.00"),
        oco_stop_limit_price=Decimal("91.65"),
        symbol_filters=_sol_filters(),
    )
    db.close()
    assert ex.sold_qty == Decimal("10.00")


def test_audit_a1_07_time_stop_get_order_list_has_no_order_reports_and_oversells(
    tmp_path: Path,
) -> None:
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_07.db")
    db.migrate()
    trade_id = _insert_trade(
        db, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00"
    )

    class _ExchangeRealGetOrderListSchema:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.market_sells: list[Decimal] = []

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            raise BinanceAPIError(400, -2011, "Order list does not exist.", "DELETE /api/v3/orderList")

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            # Schema oficial de GET /api/v3/orderList na Binance: sem "orderReports"
            return {
                "orderListId": order_list_id,
                "contingencyType": "OCO",
                "listStatusType": "ALL_DONE",
                "listOrderStatus": "ALL_DONE",
                "orders": [
                    {"symbol": "SOLUSDT", "orderId": 11, "clientOrderId": "tp_leg"},
                    {"symbol": "SOLUSDT", "orderId": 12, "clientOrderId": "sl_leg"},
                ],
            }

        def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "orderId": 11 if orig_client_order_id == "tp_leg" else 12,
                "executedQty": "10.00" if orig_client_order_id == "tp_leg" else "0.00",
                "cummulativeQuoteQty": "1120.00" if orig_client_order_id == "tp_leg" else "0.00",
                "status": "FILLED" if orig_client_order_id == "tp_leg" else "EXPIRED",
            }

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "2.00", "locked": "0.00"}]}

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.market_sells.append(qty)
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("112.00")), "fills": []}

    ex = _ExchangeRealGetOrderListSchema()
    mgr = OcoOrderManager(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    rep = mgr.close_on_time_barrier(
        trade_id=trade_id,
        symbol="SOLUSDT",
        base_asset="SOL",
        oco_order_list_id=5001,
        oco_qty=Decimal("10.00"),
        symbol_filters=_sol_filters(),
    )
    with db.read() as conn:
        row = conn.execute(
            "SELECT status, exit_avg_price, realized_pnl_usdt FROM trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
    db.close()
    assert ex.market_sells == []
    assert rep.status == TradeStatus.CLOSED.value
    assert row is not None and row["status"] == TradeStatus.CLOSED.value
    assert row["exit_avg_price"] == "112"
    assert row["realized_pnl_usdt"] == "120"


# =============================================================================
# Commit 4 — StateReconciler (A1-01, A1-03a, A1-06, A1-10, A2-01, A2-02)
# =============================================================================


def test_audit_a1_01_reconciler_heals_closing_and_never_returns_clean_for_halt_review(
    tmp_path: Path,
) -> None:
    from src.core.state_reconciler import ReconcileAction, StateReconciler
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.shadow_manager import ShadowManager

    st, _ = _load_cfgs()

    # Case 1: CLOSING with 10.00 SOL free (OCO already canceled) -> liquidates remainder and closes trade
    db = Database(tmp_path / "a1_01_closing.db")
    db.migrate()
    tid = _insert_trade(db, status=TradeStatus.CLOSING.value, oco_order_list_id=5001, oco_qty="10.00")

    class _ExchangeAfterCancelBeforeSell:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.market_sells: list[Decimal] = []

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]}

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "ALL_DONE", "orders": []}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("98.00"), Decimal("10"), Decimal("98.05"), Decimal("10"))

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.market_sells.append(qty)
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("98.00")), "fills": []}

    ex = _ExchangeAfterCancelBeforeSell()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res = rec.reconcile()
    with db.read() as conn:
        row = conn.execute("SELECT status FROM trades WHERE trade_id = ?", (tid,)).fetchone()
    db.close()
    assert res.action == ReconcileAction.RECOVERED_CLOSING_TO_CLOSED
    assert ex.market_sells == [Decimal("10.00")]
    assert row is not None and row["status"] == TradeStatus.CLOSED.value

    # Case 2: HALT_REVIEW -> never returns CLEAN and keeps emergency_halt active
    db2 = Database(tmp_path / "a1_01_halt.db")
    db2.migrate()
    _insert_trade(db2, status=TradeStatus.HALT_REVIEW.value, oco_order_list_id=5001, oco_qty="10.00")
    sm2 = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db2)

    class _ExchangeEmptyForHalt:
        governor = WeightGovernor()
        clock = MonotonicClock()

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "0.00", "locked": "10.00"}]}

    ex2 = _ExchangeEmptyForHalt()
    mgr2 = OcoOrderManager(
        db=db2, signed_client=ex2, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec2 = StateReconciler(
        db=db2,
        signed_client=ex2,  # type: ignore[arg-type]
        oco_manager=mgr2,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
        shadow_manager=sm2,
    )
    res2 = rec2.reconcile()
    assert res2.action == ReconcileAction.HALT_REVIEW_PERSISTENT
    assert sm2.evaluate_defense(current_equity_usdt="300.00").emergency_halt is True
    db2.close()


def test_audit_a1_03_reconciler_heals_after_c1e_when_oco_already_exists_on_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from src.core.state_reconciler import ReconcileAction, StateReconciler
    from src.execution.executor_protocol import ExecutionRequest, TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.position_sizer import PositionSizer

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_03.db")
    db.migrate()

    class _RealisticOcoExchange:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.free_sol = Decimal("10.00")
            self.locked_sol = Decimal("0.00")
            self.open_order_lists: list[dict[str, Any]] = []

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("100.00"), Decimal("100"), Decimal("100.05"), Decimal("100"))

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": str(self.free_sol), "locked": str(self.locked_sol)}]}

        def new_market_buy_quote(self, symbol: str, quote_qty: Decimal, client_order_id: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "orderId": 1001,
                "clientOrderId": client_order_id,
                "status": "FILLED",
                "executedQty": "10.00",
                "cummulativeQuoteQty": "1000.00",
                "fills": [{"price": "100.00", "qty": "10.00", "commission": "0", "commissionAsset": "BNB"}],
            }

        def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "orderId": 1001,
                "clientOrderId": orig_client_order_id,
                "status": "FILLED",
                "executedQty": "10.00",
                "cummulativeQuoteQty": "1000.00",
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
            if qty <= Decimal("0") or qty > self.free_sol:
                raise BinanceAPIError(400, -1013, "Filter failure: LOT_SIZE", "POST /api/v3/orderList/oco")
            self.free_sol -= qty
            self.locked_sol += qty
            ol = {
                "orderListId": 5001,
                "listClientOrderId": list_client_order_id,
                "listOrderStatus": "EXECUTING",
                "symbol": symbol,
            }
            self.open_order_lists.append(ol)
            return ol

        def get_open_order_lists(self) -> list[dict[str, Any]]:
            return list(self.open_order_lists)

    ex = _RealisticOcoExchange()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    flt = _sol_filters()
    sizing = PositionSizer(st.position_sizing, st.portfolio).evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="94.00",
        target_price="112.00",
        free_usdt="6210.00",
        symbol_filters=flt,
    )
    req = ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_000_000_000,
        atr_at_signal=2.0,
        time_barrier_end_ms=1_700_259_200_000,
        sizing=sizing,
        symbol_filters=flt,
    )

    orig_write_tx = db.write_tx
    writes = 0

    def _flaky_write_tx():
        nonlocal writes
        writes += 1
        if writes >= 2:
            raise sqlite3.OperationalError("Simulated post-fill disk error (C1-E)")
        return orig_write_tx()

    monkeypatch.setattr(db, "write_tx", _flaky_write_tx)
    rep = mgr.execute_entry(req)
    assert rep.status == TradeStatus.PROTECTED.value
    assert ex.locked_sol == Decimal("10.00")

    # SQLite recovers while still stuck in PENDING_ENTRY; StateReconciler MUST attach OCO #5001 cleanly!
    monkeypatch.setattr(db, "write_tx", orig_write_tx)
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res = rec.reconcile()
    with db.read() as conn:
        row = conn.execute(
            "SELECT status, oco_order_list_id, oco_qty FROM trades WHERE trade_id = 1"
        ).fetchone()
    db.close()
    assert res.action == ReconcileAction.HEALED_EXISTING_OCO
    assert row is not None
    assert row["status"] == TradeStatus.PROTECTED.value
    assert row["oco_order_list_id"] == 5001
    assert row["oco_qty"] == "10"


def test_audit_a1_06_reconciler_protected_canceling_executing_oco_must_protect_unlocked_qty_too(
    tmp_path: Path,
) -> None:
    from src.core.state_reconciler import ReconcileAction, StateReconciler
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_06.db")
    db.migrate()
    _insert_trade(db, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00")

    class _SplitBalanceExchange:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.free_sol = Decimal("4.00")
            self.locked_sol = Decimal("6.00")
            self.oco_qtys: list[Decimal] = []

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": str(self.free_sol), "locked": str(self.locked_sol)}]}

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "EXECUTING"}

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            self.free_sol += self.locked_sol
            self.locked_sol = Decimal("0.00")
            return {"orderListId": order_list_id, "listOrderStatus": "ALL_DONE"}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("100.00"), Decimal("10"), Decimal("100.05"), Decimal("10"))

        def new_oco_sell(self, symbol: str, qty: Decimal, **_: Any) -> dict[str, Any]:
            self.oco_qtys.append(qty)
            self.free_sol -= qty
            self.locked_sol += qty
            return {"orderListId": 5002, "listOrderStatus": "EXECUTING"}

    ex = _SplitBalanceExchange()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res1 = rec.reconcile()
    assert res1.action == ReconcileAction.REPROTECTED_PARTIAL_OCO_RESIDUAL
    assert ex.oco_qtys == [Decimal("10.00")]
    assert res1.protected_qty == Decimal("10.00")
    res2 = rec.reconcile()
    db.close()
    assert res2.action == ReconcileAction.CLEAN


def test_audit_a1_10_reconciler_c1d_must_fail_closed_when_book_ticker_errors(
    tmp_path: Path,
) -> None:
    from src.core.state_reconciler import ReconcileAction, StateReconciler
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.shadow_manager import ShadowManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a1_10.db")
    db.migrate()
    sm = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db)

    class _ExchangeManualBalanceTickerError:
        governor = WeightGovernor()
        clock = MonotonicClock()

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "5.00", "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            raise BinanceAPIError(503, -1001, "Ticker temporarily unavailable", "GET /api/v3/ticker/bookTicker")

    ex = _ExchangeManualBalanceTickerError()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
        shadow_manager=sm,
    )
    res = rec.reconcile()
    assert res.action == ReconcileAction.HALT_REVIEW_UNTRACKED_BALANCE
    assert sm.evaluate_defense(current_equity_usdt="300.00").emergency_halt is True
    db.close()


def test_audit_a2_01_reconciler_auth_error_2015_and_3_failures_trigger_emergency_halt(
    tmp_path: Path,
) -> None:
    from src.core.state_reconciler import StateReconciler
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.shadow_manager import ShadowManager

    st, _ = _load_cfgs()

    # Case 1: Immediate halt on -2015 in get_order_list (no longer swallowed!)
    db = Database(tmp_path / "a2_01_auth.db")
    db.migrate()
    _insert_trade(db, status=TradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00")
    sm = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db)

    class _ExchangeAuthErrorOnOrderList:
        governor = WeightGovernor()
        clock = MonotonicClock()

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "0.00", "locked": "10.00"}]}

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            raise BinanceAPIError(401, -2015, "Invalid API-key, IP, or permissions for action.", "GET /api/v3/orderList")

    ex = _ExchangeAuthErrorOnOrderList()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
        shadow_manager=sm,
    )
    with pytest.raises(BinanceAPIError):
        rec.reconcile()
    assert sm.evaluate_defense(current_equity_usdt="300.00").emergency_halt is True
    db.close()

    # Case 2: 3 consecutive transient 503 failures trigger halt on the 3rd failure
    db2 = Database(tmp_path / "a2_01_streak.db")
    db2.migrate()
    sm2 = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db2)

    class _ExchangeTransient503:
        governor = WeightGovernor()
        clock = MonotonicClock()

        def get_account(self, **_: Any) -> dict[str, Any]:
            raise BinanceAPIError(503, -1001, "Internal error", "GET /api/v3/account")

    ex2 = _ExchangeTransient503()
    mgr2 = OcoOrderManager(
        db=db2, signed_client=ex2, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec2 = StateReconciler(
        db=db2,
        signed_client=ex2,  # type: ignore[arg-type]
        oco_manager=mgr2,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
        shadow_manager=sm2,
    )
    for i in range(3):
        with pytest.raises(BinanceAPIError):
            rec2.reconcile()
        expected_halt = i == 2
        assert sm2.evaluate_defense(current_equity_usdt="300.00").emergency_halt is expected_halt
    db2.close()


def test_audit_a2_02_completed_oco_records_real_exit_reason_pnl_and_shadow_outcome(
    tmp_path: Path,
) -> None:
    from src.core.state_reconciler import ReconcileAction, StateReconciler
    from src.execution.executor_protocol import ExitReason, TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager
    from src.risk.shadow_manager import ShadowManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a2_02.db")
    db.migrate()
    _insert_trade(
        db,
        status=TradeStatus.PROTECTED.value,
        oco_order_list_id=5001,
        oco_qty="10.00",
        planned_entry_price="100.00",
        planned_stop_price="94.00",
        planned_target_price="112.00",
    )
    sm = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db)

    class _ExchangeCompletedTargetOco:
        governor = WeightGovernor()
        clock = MonotonicClock()

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": "0.00", "locked": "0.00"}]}

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {
                "orderListId": order_list_id,
                "listOrderStatus": "ALL_DONE",
                "orders": [
                    {"symbol": "SOLUSDT", "orderId": 11, "clientOrderId": "tp_leg"},
                    {"symbol": "SOLUSDT", "orderId": 12, "clientOrderId": "sl_leg"},
                ],
            }

        def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
            if orig_client_order_id == "tp_leg":
                return {
                    "symbol": symbol,
                    "orderId": 11,
                    "type": "LIMIT_MAKER",
                    "status": "FILLED",
                    "executedQty": "10.00",
                    "cummulativeQuoteQty": "1120.00",
                }
            return {
                "symbol": symbol,
                "orderId": 12,
                "type": "STOP_LOSS_LIMIT",
                "status": "EXPIRED",
                "executedQty": "0.00",
                "cummulativeQuoteQty": "0.00",
            }

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("112.50"), Decimal("10"), Decimal("112.55"), Decimal("10"))

    ex = _ExchangeCompletedTargetOco()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
        shadow_manager=sm,
    )
    res = rec.reconcile()
    assert res.action == ReconcileAction.CLOSED_COMPLETED_OCO
    with db.read() as conn:
        row = conn.execute(
            "SELECT exit_reason, exit_avg_price, exit_quote_received, realized_pnl_usdt, realized_r "
            "FROM trades WHERE trade_id = 1"
        ).fetchone()
    db.close()
    assert row is not None
    assert row["exit_reason"] == ExitReason.TARGET.value
    assert row["exit_avg_price"] == "112"
    assert row["exit_quote_received"] == "1120"
    assert row["realized_pnl_usdt"] == "120"
    assert abs(float(row["realized_r"]) - 2.0) < 1e-6


def test_audit_a2_02b_completed_oco_dust_revalued_at_bid_avoids_false_halt(
    tmp_path: Path,
) -> None:
    from src.core.state_reconciler import ReconcileAction, StateReconciler
    from src.execution.executor_protocol import TradeStatus
    from src.execution.oco_order_manager import OcoOrderManager

    st, _ = _load_cfgs()
    db = Database(tmp_path / "a2_02b.db")
    db.migrate()
    _insert_trade(
        db,
        status=TradeStatus.PROTECTED.value,
        oco_order_list_id=5001,
        oco_qty="10.00",
        planned_entry_price="100.00",
    )

    class _ExchangeCompletedWithPumpedDust:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.free_sol = Decimal("0.04")
            self.market_sells: list[Decimal] = []

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": str(self.free_sol), "locked": "0.00"}]}

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "ALL_DONE"}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("150.00"), Decimal("10"), Decimal("150.10"), Decimal("10"))

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.market_sells.append(qty)
            self.free_sol -= qty
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("150.00")), "fills": []}

    ex = _ExchangeCompletedWithPumpedDust()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res1 = rec.reconcile()
    assert res1.action == ReconcileAction.CLOSED_COMPLETED_OCO
    assert ex.market_sells == [Decimal("0.04")]
    res2 = rec.reconcile()
    db.close()
    assert res2.action == ReconcileAction.CLEAN


# =============================================================================
# Commit 5 — Orchestrator & CandleCollector (A1-08, A1-09, A2-05a, A2-05b)
# =============================================================================


def test_audit_a1_08_run_once_must_exclude_stale_symbols_from_chameleon_entries(
    tmp_path: Path,
) -> None:
    from src.core.binance_client import ExchangeInfoCache
    from src.core.config import BinanceEnv, RunMode, RuntimeSettings
    from src.core.database import CandleRow
    from src.data.candle_collector import CollectionReport
    from src.main import QuantOrchestrator
    from src.notifications.telegram import TelegramOutbox
    from src.radar.orderbook_inspector import OrderbookInspector
    from src.regimes.regime_detector import MarketRegime
    from src.strategies.base_strategy import SignalCandidate

    st, an = _load_cfgs()
    db = Database(tmp_path / "a1_08.db")
    db.connect()
    db.migrate()

    t_prev = 1_700_510_400_000
    t_now = t_prev + 3_600_000
    rows_1h: list[CandleRow] = []
    rows_4h: list[CandleRow] = []
    # BTCUSDT has fresh candle at t_now; SOLUSDT is stale (last candle is t_prev)
    for sym, end_t in (("BTCUSDT", t_now), ("SOLUSDT", t_prev)):
        for i in range(250):
            cl = end_t - (249 - i) * 3_600_000
            op = cl - 3_600_000
            px = 100.0 + i * 0.5
            rows_1h.append(
                CandleRow(sym, "1h", op, cl, px - 0.2, px + 0.5, px - 0.5, px, 1000.0, 100000.0, 100, 600.0, 60000.0, cl)
            )
        for i in range(150):
            cl = t_prev - (149 - i) * 14_400_000
            op = cl - 14_400_000
            px = 100.0 + i * 1.5
            rows_4h.append(
                CandleRow(sym, "4h", op, cl, px - 0.2, px + 0.8, px - 0.4, px, 4000.0, 400000.0, 400, 2400.0, 240000.0, cl)
            )
    db.upsert_candles(rows_1h + rows_4h)

    class _PubStub:
        governor = WeightGovernor()

        def get_server_time_ms(self) -> int:
            return t_now + 3000

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("224.50"), Decimal("100"), Decimal("224.60"), Decimal("100"))

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

    class _StaleCollector:
        def run_cycle(self, symbols: Any) -> list[CollectionReport]:
            return [
                CollectionReport(
                    interval="1h",
                    expected_close_time_ms=t_now,
                    ok=["BTCUSDT"],
                    stale=["SOLUSDT"],
                )
            ]

    pub = _PubStub()
    clock = MonotonicClock()
    clock.sync(pub.get_server_time_ms)
    all_syms = list(dict.fromkeys(list(st.universe.symbols) + list(an.scanner_universe.symbols) + ["BNBUSDT"]))
    ex_cache = ExchangeInfoCache(pub, db, all_syms)  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(t_now)

    settings = RuntimeSettings(
        mode=RunMode.SHADOW_CHAMELEON,
        binance_env=BinanceEnv.TESTNET,
        telegram_bot_token="123:ABC",
        telegram_chat_id="1",
        db_path=tmp_path / "a1_08.db",
        backup_dir=tmp_path / "bkp",
    )
    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=None,
        clock=clock,
        exchange_cache=ex_cache,
        outbox=TelegramOutbox(db, "1", transport=None),
        collector=_StaleCollector(),  # type: ignore[arg-type]
        orderbook_inspector=OrderbookInspector(an.layer_4_orderbook_live, sleep_fn=lambda _: None),
        sleep_fn=lambda _: None,
    )

    def _emit_if_called(sym: str, row: Any, reg: MarketRegime) -> SignalCandidate | None:
        if sym != "SOLUSDT":
            return None
        return SignalCandidate(
            symbol="SOLUSDT",
            close_time_ms=int(row["close_time_ms"]),
            regime=MarketRegime.BULL,
            strategy_name="bull_trend_pullback",
            entry_price=224.5,
            stop_price=210.0,
            target_price=260.0,
            atr_14=5.0,
            stop_distance_pct=(224.5 - 210.0) / 224.5,
            gross_rr_ratio=(260.0 - 224.5) / (224.5 - 210.0),
            time_barrier_hours=72,
            rs_vol_adjusted_12h=2.0,
            order_flow_delta=100.0,
            taker_buy_ratio=0.58,
            has_liquidity_sweep=False,
        )

    orch.strategy.evaluate_row = _emit_if_called  # type: ignore[method-assign]
    rep = orch.run_once(now_ms=t_now + 3000, skip_collect=False)
    db.close()
    assert rep.chameleon_executed is False
    assert rep.cycle_close_time_ms == t_now


def test_audit_a1_09_radar_symbol_kline_failure_must_not_block_live_time_stop(
    tmp_path: Path,
) -> None:
    from src.core.binance_client import ExchangeInfoCache, SystemStatus
    from src.core.config import BinanceEnv, RunMode, RuntimeSettings
    from src.execution.executor_protocol import TradeStatus
    from src.main import QuantOrchestrator
    from src.notifications.telegram import TelegramOutbox
    from src.radar.orderbook_inspector import OrderbookInspector

    st, an = _load_cfgs()
    db = Database(tmp_path / "a1_09.db")
    db.connect()
    db.migrate()

    now_ms = 1_700_300_003_000
    _insert_trade(
        db,
        status=TradeStatus.PROTECTED.value,
        symbol="SOLUSDT",
        oco_order_list_id=5001,
        oco_qty="10.00",
        time_barrier_end_ms=now_ms - 10_000,
    )

    class _PubRadarFails:
        governor = WeightGovernor()

        def get_server_time_ms(self) -> int:
            return now_ms

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

        def get_klines(self, symbol: str, interval: str, **_: Any) -> list[list[Any]]:
            if symbol == "DOGEUSDT":
                raise BinanceAPIError(503, -1001, "Transient radar symbol error", "GET /api/v3/klines")
            cl = (now_ms // 3_600_000) * 3_600_000
            op = cl - 3_600_000
            return [[op, "100", "101", "99", "100", "10", cl - 1, "1000", 5, "5", "500", "0"]]

    class _SignedLive:
        def __init__(self, gov: WeightGovernor, clk: MonotonicClock) -> None:
            self.governor = gov
            self.clock = clk
            self.canceled: list[int] = []
            self.sold: list[Decimal] = []

        def sapi_system_status(self) -> SystemStatus:
            return SystemStatus(normal=True, msg="normal", mocked=True)

        def get_account(self, **_: Any) -> dict[str, Any]:
            if self.canceled:
                return {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}, {"asset": "USDT", "free": "300.00", "locked": "0.00"}, {"asset": "BNB", "free": "0.1", "locked": "0.00"}]}
            return {"balances": [{"asset": "SOL", "free": "0.00", "locked": "10.00"}, {"asset": "USDT", "free": "300.00", "locked": "0.00"}, {"asset": "BNB", "free": "0.1", "locked": "0.00"}]}

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {"orderListId": order_list_id, "listOrderStatus": "EXECUTING"}

        def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
            self.canceled.append(order_list_id)
            return {
                "orderListId": order_list_id,
                "listOrderStatus": "ALL_DONE",
                "orderReports": [{"executedQty": "0.00"}, {"executedQty": "0.00"}],
            }

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sold.append(qty)
            return {"executedQty": str(qty), "cummulativeQuoteQty": "1000.00", "fills": []}

    pub = _PubRadarFails()
    clock = MonotonicClock()
    clock.sync(pub.get_server_time_ms)
    signed = _SignedLive(pub.governor, clock)
    all_syms = list(dict.fromkeys(list(st.universe.symbols) + list(an.scanner_universe.symbols) + ["BNBUSDT"]))
    ex_cache = ExchangeInfoCache(pub, db, all_syms)  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(now_ms)

    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key="k",
        telegram_bot_token="123:ABC",
        telegram_chat_id="1",
        db_path=tmp_path / "a1_09.db",
        backup_dir=tmp_path / "bkp",
    )
    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=signed,  # type: ignore[arg-type]
        clock=clock,
        exchange_cache=ex_cache,
        outbox=TelegramOutbox(db, "1", transport=None),
        orderbook_inspector=OrderbookInspector(an.layer_4_orderbook_live, sleep_fn=lambda _: None),
        sleep_fn=lambda _: None,
    )
    orch.run_once(now_ms=now_ms, skip_collect=False)
    with db.read() as conn:
        row = conn.execute("SELECT status FROM trades WHERE trade_id = 1").fetchone()
    db.close()
    assert signed.canceled == [5001]
    assert signed.sold == [Decimal("10.00")]
    assert row is not None and row["status"] == TradeStatus.CLOSED.value


def test_audit_a2_05a_rejected_entries_must_not_trigger_daily_trade_cap(tmp_path: Path) -> None:
    from src.core.binance_client import ExchangeInfoCache
    from src.core.config import BinanceEnv, RunMode, RuntimeSettings
    from src.execution.executor_protocol import TradeStatus
    from src.main import QuantOrchestrator
    from src.notifications.telegram import TelegramOutbox

    st, an = _load_cfgs()
    db = Database(tmp_path / "a2_05a.db")
    db.connect()
    db.migrate()
    now_ms = 1_700_010_000_000
    for i in range(6):
        _insert_trade(
            db,
            status=TradeStatus.ENTRY_REJECTED.value,
            client_order_id=f"REJ_{i}",
            entry_executed_qty=None,
            oco_order_list_id=None,
            oco_qty=None,
            created_at_ms=now_ms - 1000 * (i + 1),
        )

    class _PubStub:
        governor = WeightGovernor()

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

    pub = _PubStub()
    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key="k",
        telegram_bot_token="123:ABC",
        telegram_chat_id="1",
        db_path=tmp_path / "a2_05a.db",
        backup_dir=tmp_path / "bkp",
    )
    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=None,
        clock=MonotonicClock(),
        exchange_cache=ExchangeInfoCache(pub, db, ["SOLUSDT"]),  # type: ignore[arg-type]
        outbox=TelegramOutbox(db, "1", transport=None),
        sleep_fn=lambda _: None,
    )
    count_24h = orch._count_trades_last_24h(now_ms)
    db.close()
    assert count_24h == 0


def test_audit_a2_05b_bnb_ticker_error_must_not_fabricate_10usd_reserve(tmp_path: Path) -> None:
    import requests as _requests

    from src.core.binance_client import ExchangeInfoCache
    from src.core.config import BinanceEnv, RunMode, RuntimeSettings
    from src.core.database import CandleRow
    from src.main import QuantOrchestrator
    from src.notifications.telegram import TelegramOutbox

    st, an = _load_cfgs()
    db = Database(tmp_path / "a2_05b.db")
    db.connect()
    db.migrate()

    class _PubBnbTickerFails:
        governor = WeightGovernor()

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            raise _requests.ConnectionError("ticker down")

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

    class _SignedDepletedBnb:
        governor = WeightGovernor()
        clock = MonotonicClock()

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {
                "balances": [
                    {"asset": "USDT", "free": "300.00", "locked": "0.00"},
                    {"asset": "BNB", "free": "0.00001", "locked": "0.00"},
                ]
            }

    pub = _PubBnbTickerFails()
    signed = _SignedDepletedBnb()
    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key="k",
        telegram_bot_token="123:ABC",
        telegram_chat_id="1",
        db_path=tmp_path / "a2_05b.db",
        backup_dir=tmp_path / "bkp",
    )
    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=signed,  # type: ignore[arg-type]
        clock=signed.clock,
        exchange_cache=ExchangeInfoCache(pub, db, ["SOLUSDT"]),  # type: ignore[arg-type]
        outbox=TelegramOutbox(db, "1", transport=None),
        sleep_fn=lambda _: None,
    )
    # No candles -> fail-closed 0
    _, _, bnb_usdt_no_candle = orch._get_live_balances()
    assert bnb_usdt_no_candle == Decimal("0")

    # With BNBUSDT candle at $600 -> 0.00001 * 600 = $0.006 (< $1.00, triggers BNB_FEE_RESERVE_DEPLETED!)
    db.upsert_candles(
        [
            CandleRow(
                "BNBUSDT", "1h", 1_700_000_000_000, 1_700_003_600_000,
                600.0, 605.0, 595.0, 600.0, 100.0, 60000.0, 50, 50.0, 30000.0, 1_700_003_600_000
            )
        ]
    )
    _, _, bnb_usdt_candle = orch._get_live_balances()
    db.close()
    assert bnb_usdt_candle == Decimal("0.00600")
    assert bnb_usdt_candle < Decimal("1.00")


# =============================================================================
# Commit 6 — Structural Compliance (A3-01a, A3-01b, A3-01c)
# =============================================================================


def test_audit_a3_01a_main_must_not_import_live_executor_modules_outside_live(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, src.main; "
                "print('src.execution.oco_order_manager' in sys.modules, 'src.core.state_reconciler' in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "False False"

    # Also verify instantiating QuantOrchestrator in SHADOW_CHAMELEON still does NOT import live modules
    code = f"""
import sys
from pathlib import Path
from src.core.binance_client import ExchangeInfoCache, MonotonicClock, WeightGovernor
from src.core.config import AnalystConfig, BinanceEnv, RunMode, RuntimeSettings, StrategiesConfig, load_yaml_strict
from src.core.database import Database
from src.main import QuantOrchestrator
from src.notifications.telegram import TelegramOutbox

st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
db = Database(Path({str(tmp_path / 'shadow_lazy.db')!r}))
db.connect(); db.migrate()
class _P:
    governor = WeightGovernor()
    def get_exchange_info(self, syms): return {{}}
pub = _P()
settings = RuntimeSettings(
    mode=RunMode.SHADOW_CHAMELEON,
    binance_env=BinanceEnv.TESTNET,
    telegram_bot_token="123:ABC",
    telegram_chat_id="1",
    db_path=Path({str(tmp_path / 'shadow_lazy.db')!r}),
    backup_dir=Path({str(tmp_path / 'bkp')!r}),
)
orch = QuantOrchestrator(
    settings=settings, strategies_cfg=st, analyst_cfg=an, db=db,
    public_client=pub, signed_client=object(), clock=MonotonicClock(),
    exchange_cache=ExchangeInfoCache(pub, db, []), outbox=TelegramOutbox(db, "1", transport=None),
)
db.close()
print('src.execution.oco_order_manager' in sys.modules, 'src.core.state_reconciler' in sys.modules, orch.oco_manager is None, orch.reconciler is None)
"""
    proc2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc2.stdout.strip() == "False False True True"


def test_audit_a3_01b_live_radar_card_includes_c1d_execution_basket_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pandas as pd

    from src.core.binance_client import ExchangeInfoCache
    from src.core.config import BinanceEnv, RunMode, RuntimeSettings
    from src.main import QuantOrchestrator
    from src.notifications.telegram import TelegramOutbox, format_radar_signal_alert
    from src.radar.confluence_scorer import RadarSignal
    from src.radar.structure_ema import Layer123Score
    from src.regimes.regime_detector import MarketRegime

    def _make_sig(symbol: str) -> RadarSignal:
        return RadarSignal(
            signal_id=f"{symbol}_1700000000000",
            symbol=symbol,
            signal_close_time_ms=1_700_000_000_000,
            emitted_at_ms=1_700_000_005_000,
            setup="PULLBACK",
            regime_4h="BULL",
            score_total=80,
            score_l1=20,
            score_l2=20,
            score_l3=20,
            score_l4=20,
            grade="A",
            entry_ref_price=100.0,
            candle_close_price=100.0,
            stop_price=95.0,
            target1_price=107.5,
            target2_price=112.5,
            atr14=2.0,
            horizon_end_ms=1_700_086_400_000,
            cost_roundtrip_pct=0.0021,
            features_snapshot={},
            orderbook=None,
            ob_ask_wall_before_t2=False,
        )

    # Direct formatter behavior (backward-compatible default False + True adds C1-D warning)
    card_default = format_radar_signal_alert(_make_sig("SOLUSDT"))
    assert "Ativo gerido pelo executor" not in card_default
    card_live = format_radar_signal_alert(_make_sig("SOLUSDT"), live_managed_asset=True)
    assert "⚠️ Ativo gerido pelo executor — não opere nesta conta." in card_live

    # Integration through QuantOrchestrator._execute_radar_step in LIVE mode:
    # SOLUSDT (in execution_universe) gets C1-D footer; DOGEUSDT (Radar-only) does NOT!
    st, an = _load_cfgs()
    db = Database(tmp_path / "a3_01b.db")
    db.connect()
    db.migrate()

    class _Pub:
        governor = WeightGovernor()

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("100.00"), Decimal("10"), Decimal("100.05"), Decimal("10"))

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

    pub = _Pub()
    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key="k",
        telegram_bot_token="123:ABC",
        telegram_chat_id="1",
        db_path=tmp_path / "a3_01b.db",
        backup_dir=tmp_path / "bkp",
    )
    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=None,
        clock=MonotonicClock(),
        exchange_cache=ExchangeInfoCache(pub, db, ["SOLUSDT"]),  # type: ignore[arg-type]
        outbox=TelegramOutbox(db, "1", transport=None),
        sleep_fn=lambda _: None,
    )
    monkeypatch.setattr(orch.orderbook_inspector, "inspect_batch", lambda syms, cli: {})

    import src.main as main_mod

    monkeypatch.setattr(
        main_mod,
        "score_layers_1_to_3",
        lambda sym, df, reg, cfg: Layer123Score(
            symbol=sym,
            signal_close_time_ms=1_700_000_000_000,
            regime_4h=MarketRegime.BULL,
            score_l1=25,
            score_l2=25,
            score_l3=25,
            subtotal_l1_l3=75,
            close_price=100.0,
            low_price=98.0,
            atr14=2.0,
            setup="PULLBACK",
            features_snapshot={},
        )
        if sym in ("SOLUSDT", "DOGEUSDT")
        else None,
    )
    monkeypatch.setattr(
        main_mod,
        "score_and_build_signal",
        lambda hit, ob, **kwargs: _make_sig(hit.symbol),
    )

    dummy_df = pd.DataFrame([{"close_time_ms": 1_700_000_000_000, "close": 100.0}])
    emitted, _ = orch._execute_radar_step(
        now_ms=1_700_000_005_000,
        aligned_frames={"SOLUSDT": dummy_df, "DOGEUSDT": dummy_df},
        regimes={"SOLUSDT": MarketRegime.BULL, "DOGEUSDT": MarketRegime.BULL},
    )
    assert emitted == 2
    with db.read() as conn:
        rows = conn.execute(
            "SELECT reply_to_signal_id, payload FROM telegram_outbox WHERE kind = 'RADAR_SIGNAL'"
        ).fetchall()
    db.close()
    by_sig = {str(r["reply_to_signal_id"]): str(r["payload"]) for r in rows}
    assert "⚠️ Ativo gerido pelo executor — não opere nesta conta." in by_sig["SOLUSDT_1700000000000"]
    assert "Ativo gerido pelo executor" not in by_sig["DOGEUSDT_1700000000000"]


def test_audit_a3_01c_radar_signal_id_backfilled_on_trade_preserving_fk(
    tmp_path: Path,
) -> None:
    from src.core.binance_client import ExchangeInfoCache
    from src.core.config import BinanceEnv, RunMode, RuntimeSettings
    from src.execution.executor_protocol import TradeStatus
    from src.main import QuantOrchestrator
    from src.notifications.telegram import TelegramOutbox
    from src.radar.confluence_scorer import RadarSignal

    st, an = _load_cfgs()
    db = Database(tmp_path / "a3_01c.db")
    db.connect()
    db.migrate()

    close_ms = 1_700_000_000_000
    tid = _insert_trade(
        db,
        status=TradeStatus.PROTECTED.value,
        symbol="SOLUSDT",
        oco_order_list_id=5001,
        oco_qty="10.00",
    )

    class _Pub:
        governor = WeightGovernor()

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

    pub = _Pub()
    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key="k",
        telegram_bot_token="123:ABC",
        telegram_chat_id="1",
        db_path=tmp_path / "a3_01c.db",
        backup_dir=tmp_path / "bkp",
    )
    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=None,
        clock=MonotonicClock(),
        exchange_cache=ExchangeInfoCache(pub, db, ["SOLUSDT"]),  # type: ignore[arg-type]
        outbox=TelegramOutbox(db, "1", transport=None),
        sleep_fn=lambda _: None,
    )
    sig = RadarSignal(
        signal_id="SOLUSDT_1700000000000",
        symbol="SOLUSDT",
        signal_close_time_ms=close_ms,
        emitted_at_ms=close_ms + 5000,
        setup="PULLBACK",
        regime_4h="BULL",
        score_total=80,
        score_l1=20,
        score_l2=20,
        score_l3=20,
        score_l4=20,
        grade="A",
        entry_ref_price=100.0,
        candle_close_price=100.0,
        stop_price=95.0,
        target1_price=107.5,
        target2_price=112.5,
        atr14=2.0,
        horizon_end_ms=close_ms + 86_400_000,
        cost_roundtrip_pct=0.0021,
        features_snapshot={},
        orderbook=None,
        ob_ask_wall_before_t2=False,
    )
    assert orch.virtual_auditor.record_signal(sig) is True
    orch._backfill_trade_radar_signal_id(
        symbol="SOLUSDT",
        cycle_close_ms=close_ms,
        radar_signal_id=sig.signal_id,
    )
    with db.read() as conn:
        row = conn.execute("SELECT radar_signal_id FROM trades WHERE trade_id = ?", (tid,)).fetchone()
    db.close()
    assert row is not None and row["radar_signal_id"] == "SOLUSDT_1700000000000"




