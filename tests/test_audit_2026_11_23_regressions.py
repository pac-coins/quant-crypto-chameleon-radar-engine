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
