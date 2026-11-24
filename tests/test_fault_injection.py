"""Fault-injection test suite for Binance errors (-1007, -2010, -1021, 429, 418),
process crashes (fill->OCO, OCO->SQLite, 4-step Time-Stop), SQLite locks,
and TelegramOutbox/Orchestrator resilience (Part C).

Invariants asserted across all execution tests:
  1. Never leave an open exchange position without a stop (OCO or market exit).
  2. Never submit duplicate buy, OCO, or sell orders.

Any test exposing an unfixed defect is marked `@pytest.mark.xfail(strict=True)`
rather than patched in this commit.
"""

from __future__ import annotations

import sqlite3
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
    to_decimal,
)
from src.core.config import (
    AnalystConfig,
    BinanceEnv,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_yaml_strict,
)
from src.core.database import Database, SymbolFilters
from src.core.state_reconciler import ReconcileAction, StateReconciler
from src.execution.executor_protocol import ExecutionRequest, ExitReason, TradeStatus
from src.execution.oco_order_manager import ActiveTradeContext, OcoOrderManager
from src.main import QuantOrchestrator
from src.notifications.telegram import HttpTelegramTransport, TelegramOutbox
from src.radar.orderbook_inspector import OrderbookInspector
from src.risk.position_sizer import PositionSizer
from src.risk.shadow_manager import ShadowManager


def _load_cfgs() -> tuple[StrategiesConfig, AnalystConfig]:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    return st, an


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


def _make_ed25519_pem(tmp_path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    pem = tmp_path / "fi_ed25519.pem"
    pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return pem


def _make_req(st: StrategiesConfig) -> ExecutionRequest:
    flt = _sol_filters()
    sizing = PositionSizer(st.position_sizing, st.portfolio).evaluate_and_size(
        symbol="SOLUSDT",
        entry_price="100.00",
        stop_price="94.00",
        target_price="112.00",
        free_usdt="6210.00",
        symbol_filters=flt,
    )
    assert sizing.approved
    return ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_000_000_000,
        atr_at_signal=2.0,
        time_barrier_end_ms=1_700_259_200_000,
        sizing=sizing,
        symbol_filters=flt,
    )


def _insert_trade(
    db: Database,
    *,
    status: str,
    symbol: str = "SOLUSDT",
    client_order_id: str = "CHM_SOLUSDT_FI1",
    entry_executed_qty: str | None = "10.00",
    oco_order_list_id: int | None = 5001,
    oco_list_client_id: str | None = "OCO_SOLUSDT_1_1700000000000",
    oco_qty: str | None = "10.00",
    time_barrier_end_ms: int = 1_700_259_200_000,
) -> int:
    with db.write_tx() as tx:
        cur = tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt, quote_order_qty,
                entry_order_id, entry_executed_qty, entry_avg_price, entry_quote_spent,
                commission_base_asset,
                oco_order_list_id, oco_list_client_id, oco_qty,
                oco_stop_price, oco_stop_limit_price, oco_target_price,
                time_barrier_end_ms, created_at_ms, updated_at_ms
            ) VALUES (
                ?, 'TESTNET', ?, 'bull_trend_pullback', 'BULL',
                1700000000000, ?,
                '100.00', '94.00', '112.00',
                2.0, 0.01, '10.00', '1000.00',
                1001, ?, '100.00', '1000.00',
                '0',
                ?, ?, ?,
                '94.00', '91.65', '112.00',
                ?, 1700000000000, 1700000000000
            )
            """,
            (
                client_order_id,
                symbol,
                status,
                entry_executed_qty,
                oco_order_list_id,
                oco_list_client_id,
                oco_qty,
                time_barrier_end_ms,
            ),
        )
        return int(cur.lastrowid)


def _mock_response(
    status_code: int,
    payload: dict[str, Any] | list[Any],
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


# =============================================================================
# 1. Binance Fault Injection: -1007, -2010, -1021, 429, 418
# =============================================================================


def test_fi_binance_1007_timeout_on_buy_and_oco_no_duplicate_and_always_protected(
    tmp_path: Path,
) -> None:
    """-1007 on POST /api/v3/order AND POST /api/v3/orderList/oco (real SignedBinanceClient):
    must recover idempotently with 1 buy, 1 OCO, and protected position (never naked, never duplicate)."""
    st, _ = _load_cfgs()
    db = Database(tmp_path / "fi_1007.db")
    db.migrate()

    session = MagicMock(spec=requests.Session)
    buy_posts = 0
    oco_posts = 0
    oco_accepted_coid: list[str] = []

    def _dispatch(method: str, url: str, **kwargs: Any) -> MagicMock:
        nonlocal buy_posts, oco_posts
        if method == "POST" and "/api/v3/orderList/oco" in url:
            oco_posts += 1
            # Extract listClientOrderId
            from urllib.parse import parse_qs, urlsplit

            coid = parse_qs(urlsplit(url).query)["listClientOrderId"][0]
            oco_accepted_coid.append(coid)
            # Binance matched the OCO but gateway timed out with -1007
            return _mock_response(504, {"code": -1007, "msg": "Timeout waiting for response from backend server."})
        if method == "POST" and "/api/v3/order" in url:
            buy_posts += 1
            # Binance filled the buy but gateway timed out with -1007
            return _mock_response(504, {"code": -1007, "msg": "Timeout waiting for response from backend server."})
        if method == "GET" and "/api/v3/orderList" in url:
            return _mock_response(
                200,
                {
                    "orderListId": 5001,
                    "listClientOrderId": oco_accepted_coid[-1],
                    "listOrderStatus": "EXECUTING",
                    "symbol": "SOLUSDT",
                },
            )
        if method == "GET" and "/api/v3/order" in url:
            return _mock_response(
                200,
                {
                    "symbol": "SOLUSDT",
                    "orderId": 1001,
                    "status": "FILLED",
                    "executedQty": "10.00",
                    "cummulativeQuoteQty": "1000.00",
                },
            )
        if method == "GET" and "/api/v3/myTrades" in url:
            return _mock_response(200, [{"id": 1, "orderId": 1001, "commission": "0", "commissionAsset": "BNB"}])
        if method == "GET" and "/api/v3/account" in url:
            return _mock_response(200, {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]})
        if method == "GET" and "/api/v3/ticker/bookTicker" in url:
            return _mock_response(
                200,
                {"symbol": "SOLUSDT", "bidPrice": "100.00", "bidQty": "50", "askPrice": "100.05", "askQty": "50"},
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    session.request.side_effect = _dispatch
    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        WeightGovernor(),
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=_make_ed25519_pem(tmp_path),
        testnet=True,
        session=session,
    )
    mgr = OcoOrderManager(
        db=db,
        signed_client=client,
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda _: None,
    )
    rep = mgr.execute_entry(_make_req(st))
    db.close()

    assert rep.executed is True
    assert rep.status == TradeStatus.PROTECTED.value
    assert buy_posts == 1  # Invariant: never duplicate buy
    assert oco_posts == 1  # Invariant: never duplicate OCO when accepted on exchange


def test_fi_binance_1007_timeout_on_emergency_market_sell_missing_2013_retries_sell(
    tmp_path: Path,
) -> None:
    st, _ = _load_cfgs()
    db = Database(tmp_path / "fi_1007_emerg.db")
    db.migrate()
    tid = _insert_trade(db, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    class _ExchangeTimeoutThenMissingOnEmergencySell:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.sell_attempts = 0
            self.sell_client_ids: list[str] = []
            self.free_sol = Decimal("10.00")

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": str(self.free_sol), "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            # Price already breached stop (93 < 94) -> triggers emergency market sell
            return BookTicker(symbol, Decimal("93.00"), Decimal("10"), Decimal("93.05"), Decimal("10"))

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sell_attempts += 1
            self.sell_client_ids.append(client_order_id)
            if self.sell_attempts == 1:
                raise RequestTimeoutUnknownState("-1007 before matching engine")
            self.free_sol -= qty
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("93.00")), "fills": []}

        def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
            raise BinanceAPIError(400, -2013, "Order does not exist.", "GET /api/v3/order")

    ex = _ExchangeTimeoutThenMissingOnEmergencySell()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    ctx = ActiveTradeContext(
        trade_id=tid,
        client_order_id="CHM_SOLUSDT_FI1",
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
    db.close()
    assert rep.status == TradeStatus.CLOSED.value
    assert ex.free_sol == Decimal("0.00")
    assert ex.sell_attempts == 2
    assert len(set(ex.sell_client_ids)) == 2
    assert all(len(coid) <= 36 for coid in ex.sell_client_ids)

    # Companion invariant: when -1007 happens AFTER matching engine filled the sell,
    # get_order recovers the filled sell and NEVER sends a duplicate market sell.
    db2 = Database(tmp_path / "fi_1007_emerg_filled.db")
    db2.migrate()
    tid2 = _insert_trade(db2, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    class _ExchangeTimeoutAfterFilledEmergencySell:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.sell_attempts = 0
            self.free_sol = Decimal("10.00")
            self.filled_by_coid: dict[str, dict[str, Any]] = {}

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": str(self.free_sol), "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            return BookTicker(symbol, Decimal("93.00"), Decimal("10"), Decimal("93.05"), Decimal("10"))

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sell_attempts += 1
            self.free_sol -= qty
            self.filled_by_coid[client_order_id] = {
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * Decimal("93.00")),
                "fills": [],
            }
            raise RequestTimeoutUnknownState("-1007 after matching engine filled sell")

        def get_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any]:
            return self.filled_by_coid[orig_client_order_id]

    ex2 = _ExchangeTimeoutAfterFilledEmergencySell()
    mgr2 = OcoOrderManager(
        db=db2, signed_client=ex2, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    ctx2 = ActiveTradeContext(
        trade_id=tid2,
        client_order_id="CHM_SOLUSDT_FI1",
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
    rep2 = mgr2.protect_or_fallback_market(ctx2, _sol_filters())
    db2.close()
    assert rep2.status == TradeStatus.CLOSED.value
    assert ex2.free_sol == Decimal("0.00")
    assert ex2.sell_attempts == 1  # Zero duplicate sell!


def test_fi_binance_2010_price_breach_stop_and_target_exits_cleanly_without_naked_position(
    tmp_path: Path,
) -> None:
    st, _ = _load_cfgs()

    # Case A: -2010 stop trigger race (bid was 95 on pre-check, dropped to 93 before POST OCO)
    db1 = Database(tmp_path / "fi_2010_stop.db")
    db1.migrate()
    tid1 = _insert_trade(db1, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    class _Exchange2010RaceStop:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.free_sol = Decimal("10.00")
            self.sells: list[Decimal] = []
            self.ticks = 0

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": str(self.free_sol), "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            self.ticks += 1
            px = Decimal("95.00") if self.ticks == 1 else Decimal("93.00")
            return BookTicker(symbol, px, Decimal("10"), px + Decimal("0.05"), Decimal("10"))

        def new_oco_sell(self, **_: Any) -> dict[str, Any]:
            raise BinanceAPIError(400, -2010, "Order would trigger immediately.", "POST /api/v3/orderList/oco")

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sells.append(qty)
            self.free_sol -= qty
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("93.00")), "fills": []}

    ex1 = _Exchange2010RaceStop()
    mgr1 = OcoOrderManager(
        db=db1, signed_client=ex1, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    ctx1 = ActiveTradeContext(
        trade_id=tid1, client_order_id="CHM_1", symbol="SOLUSDT", base_asset="SOL",
        status=TradeStatus.ENTRY_FILLED.value, entry_order_id=1001, executed_qty=Decimal("10.00"),
        avg_entry_price=Decimal("100.00"), quote_spent=Decimal("1000.00"), commission_base_asset=Decimal("0"),
        planned_stop_price=Decimal("94.00"), planned_target_price=Decimal("112.00"), time_barrier_end_ms=1_700_259_200_000,
    )
    rep1 = mgr1.protect_or_fallback_market(ctx1, _sol_filters())
    db1.close()
    assert rep1.status == TradeStatus.CLOSED.value
    assert rep1.exit_reason == ExitReason.FALLBACK_MARKET_2010.value
    assert ex1.sells == [Decimal("10.00")] and ex1.free_sol == Decimal("0.00")

    # Case B: -2010 price relationship race above take profit (bid jumped 111 -> 113 before POST OCO)
    db2 = Database(tmp_path / "fi_2010_tp.db")
    db2.migrate()
    tid2 = _insert_trade(db2, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    class _Exchange2010RaceTarget:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.free_sol = Decimal("10.00")
            self.sells: list[Decimal] = []
            self.ticks = 0

        def get_account(self, **_: Any) -> dict[str, Any]:
            return {"balances": [{"asset": "SOL", "free": str(self.free_sol), "locked": "0.00"}]}

        def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
            self.ticks += 1
            px = Decimal("111.00") if self.ticks == 1 else Decimal("113.00")
            return BookTicker(symbol, px, Decimal("10"), px + Decimal("0.05"), Decimal("10"))

        def new_oco_sell(self, **_: Any) -> dict[str, Any]:
            raise BinanceAPIError(
                400, -2010, "The relationship of the prices for the orders is not correct.", "POST /api/v3/orderList/oco"
            )

        def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
            self.sells.append(qty)
            self.free_sol -= qty
            return {"executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal("113.00")), "fills": []}

    ex2 = _Exchange2010RaceTarget()
    mgr2 = OcoOrderManager(
        db=db2, signed_client=ex2, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    ctx2 = ActiveTradeContext(
        trade_id=tid2, client_order_id="CHM_2", symbol="SOLUSDT", base_asset="SOL",
        status=TradeStatus.ENTRY_FILLED.value, entry_order_id=1001, executed_qty=Decimal("10.00"),
        avg_entry_price=Decimal("100.00"), quote_spent=Decimal("1000.00"), commission_base_asset=Decimal("0"),
        planned_stop_price=Decimal("94.00"), planned_target_price=Decimal("112.00"), time_barrier_end_ms=1_700_259_200_000,
    )
    rep2 = mgr2.protect_or_fallback_market(ctx2, _sol_filters())
    db2.close()
    assert rep2.status == TradeStatus.CLOSED.value
    assert rep2.exit_reason == ExitReason.TARGET.value
    assert ex2.sells == [Decimal("10.00")] and ex2.free_sol == Decimal("0.00")


def test_fi_binance_1021_on_entry_rejects_cleanly_without_duplicate(tmp_path: Path) -> None:
    st, _ = _load_cfgs()
    db = Database(tmp_path / "fi_1021_entry.db")
    db.migrate()

    session = MagicMock(spec=requests.Session)
    session.request.return_value = _mock_response(
        400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
    )
    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        WeightGovernor(),
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=_make_ed25519_pem(tmp_path),
        testnet=True,
        session=session,
    )
    mgr = OcoOrderManager(
        db=db, signed_client=client, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None
    )
    rep = mgr.execute_entry(_make_req(st))
    db.close()

    assert rep.executed is False
    assert rep.status == TradeStatus.ENTRY_REJECTED.value
    assert session.request.call_count == 1  # Zero duplicate order


def test_fi_binance_1021_after_fill_on_oco_must_resync_and_protect(tmp_path: Path) -> None:
    st, _ = _load_cfgs()
    db = Database(tmp_path / "fi_1021_oco.db")
    db.migrate()

    session = MagicMock(spec=requests.Session)
    oco_attempts = 0
    time_syncs = 0
    oco_timestamps: list[int] = []

    def _dispatch(method: str, url: str, **kwargs: Any) -> MagicMock:
        nonlocal oco_attempts, time_syncs
        if method == "GET" and "/api/v3/time" in url:
            time_syncs += 1
            return _mock_response(200, {"serverTime": 1_700_000_005_000})
        if method == "POST" and "/api/v3/orderList/oco" in url:
            from urllib.parse import parse_qs, urlsplit

            oco_attempts += 1
            ts = int(parse_qs(urlsplit(url).query)["timestamp"][0])
            oco_timestamps.append(ts)
            if oco_attempts == 1:
                return _mock_response(
                    400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
                )
            return _mock_response(200, {"orderListId": 5001, "listOrderStatus": "EXECUTING"})
        if method == "POST" and "/api/v3/order" in url:
            return _mock_response(
                200,
                {
                    "symbol": "SOLUSDT",
                    "orderId": 1001,
                    "status": "FILLED",
                    "executedQty": "10.00",
                    "cummulativeQuoteQty": "1000.00",
                    "fills": [{"price": "100.00", "qty": "10.00", "commission": "0", "commissionAsset": "BNB"}],
                },
            )
        if method == "GET" and "/api/v3/account" in url:
            return _mock_response(200, {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]})
        if method == "GET" and "/api/v3/ticker/bookTicker" in url:
            return _mock_response(
                200,
                {"symbol": "SOLUSDT", "bidPrice": "100.00", "bidQty": "50", "askPrice": "100.05", "askQty": "50"},
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    session.request.side_effect = _dispatch
    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        WeightGovernor(),
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=_make_ed25519_pem(tmp_path),
        testnet=True,
        session=session,
    )
    mgr = OcoOrderManager(
        db=db, signed_client=client, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None
    )
    rep = mgr.execute_entry(_make_req(st))
    db.close()

    assert rep.status == TradeStatus.PROTECTED.value
    assert oco_attempts == 2
    assert time_syncs == 1
    assert oco_timestamps[1] > oco_timestamps[0]  # Resynced against serverTime (+5000 ms)


def test_fi_binance_1021_on_emergency_market_sell_resyncs_and_closes(tmp_path: Path) -> None:
    """-1021 on emergency market sell (stop-breach fallback) resyncs clock at CRITICAL and closes cleanly."""
    st, _ = _load_cfgs()
    db = Database(tmp_path / "fi_1021_sell.db")
    db.migrate()
    tid = _insert_trade(db, status=TradeStatus.ENTRY_FILLED.value, oco_order_list_id=None, oco_qty=None)

    session = MagicMock(spec=requests.Session)
    sell_attempts = 0
    time_syncs = 0

    def _dispatch(method: str, url: str, **kwargs: Any) -> MagicMock:
        nonlocal sell_attempts, time_syncs
        if method == "GET" and "/api/v3/time" in url:
            time_syncs += 1
            return _mock_response(200, {"serverTime": 1_700_000_005_000})
        if method == "GET" and "/api/v3/account" in url:
            return _mock_response(200, {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]})
        if method == "GET" and "/api/v3/ticker/bookTicker" in url:
            return _mock_response(
                200,
                {"symbol": "SOLUSDT", "bidPrice": "93.00", "bidQty": "50", "askPrice": "93.05", "askQty": "50"},
            )
        if method == "POST" and "/api/v3/order" in url:
            sell_attempts += 1
            if sell_attempts == 1:
                return _mock_response(
                    400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
                )
            return _mock_response(
                200,
                {
                    "symbol": "SOLUSDT",
                    "orderId": 9001,
                    "status": "FILLED",
                    "executedQty": "10.00",
                    "cummulativeQuoteQty": "930.00",
                    "fills": [],
                },
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    session.request.side_effect = _dispatch
    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        WeightGovernor(),
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=_make_ed25519_pem(tmp_path),
        testnet=True,
        session=session,
    )
    mgr = OcoOrderManager(
        db=db, signed_client=client, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None
    )
    ctx = ActiveTradeContext(
        trade_id=tid,
        client_order_id="CHM_SOLUSDT_FI1",
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
    with client.governor.critical_section():
        rep = mgr.protect_or_fallback_market(ctx, _sol_filters())
    db.close()

    assert rep.status == TradeStatus.CLOSED.value
    assert rep.exit_reason == ExitReason.FALLBACK_MARKET_2010.value
    assert sell_attempts == 2
    assert time_syncs == 1


def test_fi_binance_429_rate_limit_suspends_non_critical_while_oco_retries_and_protects(
    tmp_path: Path,
) -> None:
    """HTTP 429 on POST /api/v3/orderList/oco suspends LOW/HIGH priorities on WeightGovernor,
    while CRITICAL OCO honors retry_after_s, retries once, and protects the position without duplicate buys."""
    st, _ = _load_cfgs()
    db = Database(tmp_path / "fi_429.db")
    db.migrate()

    fake_now = [100.0]
    gov = WeightGovernor(now=lambda: fake_now[0])
    slept: list[float] = []
    session = MagicMock(spec=requests.Session)
    buy_posts = 0
    oco_posts = 0

    def _dispatch(method: str, url: str, **kwargs: Any) -> MagicMock:
        nonlocal buy_posts, oco_posts
        if method == "POST" and "/api/v3/orderList/oco" in url:
            oco_posts += 1
            if oco_posts == 1:
                return _mock_response(
                    429,
                    {"code": -1003, "msg": "Too many requests"},
                    headers={"Retry-After": "2", "X-MBX-USED-WEIGHT-1M": "5900"},
                )
            return _mock_response(200, {"orderListId": 5001, "listOrderStatus": "EXECUTING"})
        if method == "POST" and "/api/v3/order" in url:
            buy_posts += 1
            return _mock_response(
                200,
                {
                    "symbol": "SOLUSDT",
                    "orderId": 1001,
                    "status": "FILLED",
                    "executedQty": "10.00",
                    "cummulativeQuoteQty": "1000.00",
                    "fills": [{"price": "100.00", "qty": "10.00", "commission": "0", "commissionAsset": "BNB"}],
                },
            )
        if method == "GET" and "/api/v3/account" in url:
            return _mock_response(200, {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]})
        if method == "GET" and "/api/v3/ticker/bookTicker" in url:
            return _mock_response(
                200,
                {"symbol": "SOLUSDT", "bidPrice": "100.00", "bidQty": "50", "askPrice": "100.05", "askQty": "50"},
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    session.request.side_effect = _dispatch
    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        gov,
        MonotonicClock(mono=lambda: fake_now[0], wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=_make_ed25519_pem(tmp_path),
        testnet=True,
        session=session,
    )
    mgr = OcoOrderManager(
        db=db,
        signed_client=client,
        resilience_cfg=st.execution_and_resilience,
        sleep_fn=lambda s: slept.append(s),
    )
    rep = mgr.execute_entry(_make_req(st))
    db.close()

    assert rep.status == TradeStatus.PROTECTED.value
    assert buy_posts == 1
    assert oco_posts == 2
    assert slept == [2.0]
    # Non-critical tiers remain suspended during 429 cooldown
    with pytest.raises(WeightBudgetExceeded):
        gov.acquire(1, Priority.HIGH)
    with pytest.raises(WeightBudgetExceeded):
        gov.acquire(1, Priority.LOW)


def test_fi_binance_418_ip_ban_triggers_emergency_halt_and_alert(tmp_path: Path) -> None:
    st, _ = _load_cfgs()

    # Subcase A: HTTP 418 on execute_entry -> raises IPBannedError, triggers emergency_halt + KILL_SWITCH alert
    db = Database(tmp_path / "fi_418_entry.db")
    db.migrate()
    sm = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db)
    alerts: list[tuple[str, str]] = []

    fake_now = [100.0]
    gov = WeightGovernor(now=lambda: fake_now[0])
    session = MagicMock(spec=requests.Session)
    session.request.return_value = _mock_response(
        418,
        {"code": -1003, "msg": "Way too many requests; IP banned until ..."},
        headers={"Retry-After": "120"},
    )
    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        gov,
        MonotonicClock(mono=lambda: fake_now[0], wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=_make_ed25519_pem(tmp_path),
        testnet=True,
        session=session,
    )
    mgr = OcoOrderManager(
        db=db,
        signed_client=client,
        resilience_cfg=st.execution_and_resilience,
        alert_callback=lambda k, t: alerts.append((k, t)),
        shadow_manager=sm,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(IPBannedError):
        mgr.execute_entry(_make_req(st))

    # WeightGovernor blocks all further calls (including CRITICAL) during ban
    with pytest.raises(WeightBudgetExceeded):
        gov.acquire(1, Priority.CRITICAL)

    assert sm.evaluate_defense(current_equity_usdt="300.00").emergency_halt is True
    assert any(k == "KILL_SWITCH" for k, _ in alerts)
    assert session.request.call_count == 1  # Zero duplicate buy
    db.close()

    # Subcase B: HTTP 418 on POST /api/v3/orderList/oco (after fill) -> triggers emergency_halt + KILL_SWITCH alert
    db2 = Database(tmp_path / "fi_418_oco.db")
    db2.migrate()
    sm2 = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db2)
    alerts2: list[tuple[str, str]] = []

    fake_now2 = [100.0]
    gov2 = WeightGovernor(now=lambda: fake_now2[0])
    session2 = MagicMock(spec=requests.Session)

    def _dispatch_oco_418(method: str, url: str, **kwargs: Any) -> MagicMock:
        if method == "POST" and "/api/v3/orderList/oco" in url:
            return _mock_response(
                418,
                {"code": -1003, "msg": "Way too many requests; IP banned"},
                headers={"Retry-After": "120"},
            )
        if method == "POST" and "/api/v3/order" in url:
            return _mock_response(
                200,
                {
                    "symbol": "SOLUSDT",
                    "orderId": 1001,
                    "status": "FILLED",
                    "executedQty": "10.00",
                    "cummulativeQuoteQty": "1000.00",
                    "fills": [{"price": "100.00", "qty": "10.00", "commission": "0", "commissionAsset": "BNB"}],
                },
            )
        if method == "GET" and "/api/v3/account" in url:
            return _mock_response(200, {"balances": [{"asset": "SOL", "free": "10.00", "locked": "0.00"}]})
        if method == "GET" and "/api/v3/ticker/bookTicker" in url:
            return _mock_response(
                200,
                {"symbol": "SOLUSDT", "bidPrice": "100.00", "bidQty": "50", "askPrice": "100.05", "askQty": "50"},
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    session2.request.side_effect = _dispatch_oco_418
    client2 = SignedBinanceClient(
        "https://testnet.binance.vision",
        gov2,
        MonotonicClock(mono=lambda: fake_now2[0], wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=_make_ed25519_pem(tmp_path),
        testnet=True,
        session=session2,
    )
    mgr2 = OcoOrderManager(
        db=db2,
        signed_client=client2,
        resilience_cfg=st.execution_and_resilience,
        alert_callback=lambda k, t: alerts2.append((k, t)),
        shadow_manager=sm2,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(IPBannedError):
        mgr2.execute_entry(_make_req(st))

    with pytest.raises(WeightBudgetExceeded):
        gov2.acquire(1, Priority.CRITICAL)
    assert sm2.evaluate_defense(current_equity_usdt="300.00").emergency_halt is True
    assert any(k == "KILL_SWITCH" for k, _ in alerts2)
    db2.close()


# =============================================================================
# 2. Process Crash Simulation (fill->OCO, OCO->SQLite, 4-step Time-Stop)
# =============================================================================


class _StatefulSpotExchange:
    """In-memory exchange simulator tracking balances, orders, and OCOs across simulated process restarts."""

    def __init__(self) -> None:
        self.governor = WeightGovernor()
        self.clock = MonotonicClock()
        self.free_sol = Decimal("0.00")
        self.locked_sol = Decimal("0.00")
        self.bid = Decimal("100.00")
        self.ask = Decimal("100.05")
        self.buy_calls: list[str] = []
        self.oco_calls: list[dict[str, Any]] = []
        self.sell_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[int] = []
        self.orders_by_coid: dict[str, dict[str, Any]] = {}
        self.order_lists_by_id: dict[int, dict[str, Any]] = {}
        self.crash_on: str | None = None

    def get_book_ticker(self, symbol: str, **_: Any) -> BookTicker:
        return BookTicker(symbol, self.bid, Decimal("100"), self.ask, Decimal("100"))

    def get_account(self, **_: Any) -> dict[str, Any]:
        return {
            "balances": [
                {"asset": "USDT", "free": "500.00", "locked": "0.00"},
                {"asset": "SOL", "free": str(self.free_sol), "locked": str(self.locked_sol)},
                {"asset": "BNB", "free": "0.50", "locked": "0.00"},
            ]
        }

    def new_market_buy_quote(self, symbol: str, quote_qty: Decimal, client_order_id: str) -> dict[str, Any]:
        self.buy_calls.append(client_order_id)
        self.free_sol += Decimal("10.00")
        order = {
            "symbol": symbol,
            "orderId": 1001,
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": "10.00",
            "cummulativeQuoteQty": "1000.00",
            "fills": [{"price": "100.00", "qty": "10.00", "commission": "0", "commissionAsset": "BNB"}],
        }
        self.orders_by_coid[client_order_id] = order
        if self.crash_on == "after_fill_before_oco":
            raise RuntimeError("SIMULATED_CRASH: process died between fill and OCO")
        return order

    def get_order(self, symbol: str, *, orig_client_order_id: str | None = None, order_id: int | None = None) -> dict[str, Any]:
        if orig_client_order_id and orig_client_order_id in self.orders_by_coid:
            return self.orders_by_coid[orig_client_order_id]
        raise BinanceAPIError(400, -2013, "Order does not exist.", "GET /api/v3/order")

    def new_oco_sell(
        self,
        symbol: str,
        qty: Decimal,
        take_profit: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal | None,
        list_client_order_id: str,
    ) -> dict[str, Any]:
        assert qty > Decimal("0") and qty <= self.free_sol
        self.free_sol -= qty
        self.locked_sol += qty
        ol_id = 5000 + len(self.oco_calls) + 1
        ol = {
            "orderListId": ol_id,
            "listClientOrderId": list_client_order_id,
            "listOrderStatus": "EXECUTING",
            "symbol": symbol,
            "qty": qty,
        }
        self.oco_calls.append(ol)
        self.order_lists_by_id[ol_id] = ol
        if self.crash_on == "after_oco_before_db":
            raise RuntimeError("SIMULATED_CRASH: process died between OCO creation and SQLite commit")
        return ol

    def get_order_list(
        self, order_list_id: int | None = None, *, orig_client_order_id: str | None = None
    ) -> dict[str, Any]:
        if order_list_id is not None and order_list_id in self.order_lists_by_id:
            return self.order_lists_by_id[order_list_id]
        if orig_client_order_id is not None:
            for ol in self.order_lists_by_id.values():
                if ol.get("listClientOrderId") == orig_client_order_id:
                    return ol
        raise BinanceAPIError(400, -2013, "Order list does not exist.", "GET /api/v3/orderList")

    def get_open_order_lists(self) -> list[dict[str, Any]]:
        return [ol for ol in self.order_lists_by_id.values() if ol.get("listOrderStatus") == "EXECUTING"]

    def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
        self.cancel_calls.append(order_list_id)
        ol = self.order_lists_by_id[order_list_id]
        if ol["listOrderStatus"] == "EXECUTING":
            qty = ol["qty"]
            self.locked_sol -= qty
            self.free_sol += qty
            ol["listOrderStatus"] = "ALL_DONE"
            ol["orderReports"] = [{"executedQty": "0.00"}, {"executedQty": "0.00"}]
        if self.crash_on == "after_timestop_cancel_before_sell":
            raise RuntimeError("SIMULATED_CRASH: process died after Time-Stop cancel before market sell")
        return ol

    def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
        assert qty > Decimal("0") and qty <= self.free_sol
        self.free_sol -= qty
        rec = {
            "symbol": symbol,
            "orderId": 9000 + len(self.sell_calls) + 1,
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": str(qty),
            "cummulativeQuoteQty": str((qty * self.bid).quantize(Decimal("0.01"))),
            "fills": [{"price": str(self.bid), "qty": str(qty), "commission": "0", "commissionAsset": "BNB"}],
        }
        self.sell_calls.append({"qty": qty, "client_order_id": client_order_id})
        self.orders_by_coid[client_order_id] = rec
        if self.crash_on == "after_timestop_sell_before_db":
            raise RuntimeError("SIMULATED_CRASH: process died after Time-Stop market sell before SQLite CLOSED")
        return rec


def test_fi_process_crash_between_fill_and_oco_recovered_on_reboot(tmp_path: Path) -> None:
    st, _ = _load_cfgs()
    db_path = tmp_path / "crash_fill_oco.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    ex = _StatefulSpotExchange()
    ex.crash_on = "after_fill_before_oco"
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="SIMULATED_CRASH"):
        mgr.execute_entry(_make_req(st))
    db.close()

    # Exchange has 10 SOL free without stop; SQLite has PENDING_ENTRY. Reboot process:
    assert ex.free_sol == Decimal("10.00") and ex.locked_sol == Decimal("0.00")
    ex.crash_on = None
    db_reboot = Database(db_path)
    db_reboot.connect()
    mgr_reboot = OcoOrderManager(
        db=db_reboot, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db_reboot,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr_reboot,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res = rec.reconcile()
    with db_reboot.read() as conn:
        row = conn.execute("SELECT status, oco_order_list_id FROM trades WHERE trade_id = 1").fetchone()
    db_reboot.close()

    assert res.action == ReconcileAction.RECOVERED_PENDING_TO_PROTECTED
    assert len(ex.buy_calls) == 1  # Never duplicate buy
    assert len(ex.oco_calls) == 1  # Exactly 1 OCO placed
    assert ex.free_sol == Decimal("0.00") and ex.locked_sol == Decimal("10.00")  # Fully protected!
    assert row is not None and row["status"] == TradeStatus.PROTECTED.value and row["oco_order_list_id"] == 5001


def test_fi_process_crash_between_oco_and_sqlite_write_healed_without_duplicate(
    tmp_path: Path,
) -> None:
    st, _ = _load_cfgs()
    db_path = tmp_path / "crash_oco_db.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    ex = _StatefulSpotExchange()
    ex.crash_on = "after_oco_before_db"
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="SIMULATED_CRASH"):
        mgr.execute_entry(_make_req(st))
    db.close()

    # Exchange already has OCO #5001 (locked=10.00); SQLite still says ENTRY_FILLED! Reboot:
    ex.crash_on = None
    db_reboot = Database(db_path)
    db_reboot.connect()
    mgr_reboot = OcoOrderManager(
        db=db_reboot, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rec = StateReconciler(
        db=db_reboot,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr_reboot,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res = rec.reconcile()
    with db_reboot.read() as conn:
        row = conn.execute("SELECT status, oco_order_list_id FROM trades WHERE trade_id = 1").fetchone()
    db_reboot.close()

    assert res.action == ReconcileAction.HEALED_EXISTING_OCO
    assert len(ex.buy_calls) == 1  # Zero duplicate buy
    assert len(ex.oco_calls) == 1  # Zero duplicate OCO
    assert ex.locked_sol == Decimal("10.00") and ex.free_sol == Decimal("0.00")
    assert row is not None and row["status"] == TradeStatus.PROTECTED.value and row["oco_order_list_id"] == 5001


def test_fi_process_crash_during_4step_time_stop_healed_without_naked_or_duplicate(
    tmp_path: Path,
) -> None:
    st, _ = _load_cfgs()

    # Subcase A: Crash after Step 1 (OCO canceled -> 10 SOL free without stop) before Step 4 sell
    db_a_path = tmp_path / "crash_ts_a.db"
    db_a = Database(db_a_path)
    db_a.connect()
    db_a.migrate()
    ex_a = _StatefulSpotExchange()
    mgr_a = OcoOrderManager(
        db=db_a, signed_client=ex_a, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rep_a = mgr_a.execute_entry(_make_req(st))
    assert rep_a.status == TradeStatus.PROTECTED.value and rep_a.trade_id is not None

    ex_a.crash_on = "after_timestop_cancel_before_sell"
    with pytest.raises(RuntimeError, match="SIMULATED_CRASH"):
        mgr_a.close_on_time_barrier(
            trade_id=rep_a.trade_id,
            symbol="SOLUSDT",
            base_asset="SOL",
            oco_order_list_id=5001,
            oco_qty=Decimal("10.00"),
            symbol_filters=_sol_filters(),
        )
    db_a.close()
    assert ex_a.free_sol == Decimal("10.00") and len(ex_a.sell_calls) == 0

    ex_a.crash_on = None
    db_a_boot = Database(db_a_path)
    db_a_boot.connect()
    rec_a = StateReconciler(
        db=db_a_boot,
        signed_client=ex_a,  # type: ignore[arg-type]
        oco_manager=OcoOrderManager(
            db=db_a_boot, signed_client=ex_a, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
        ),
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res_a = rec_a.reconcile()
    db_a_boot.close()
    assert res_a.action == ReconcileAction.RECOVERED_CLOSING_TO_CLOSED
    assert ex_a.free_sol == Decimal("0.00") and ex_a.locked_sol == Decimal("0.00")
    assert len(ex_a.sell_calls) == 1

    # Subcase B: Crash after Step 4 market sell filled before SQLite CLOSED commit
    db_b_path = tmp_path / "crash_ts_b.db"
    db_b = Database(db_b_path)
    db_b.connect()
    db_b.migrate()
    ex_b = _StatefulSpotExchange()
    mgr_b = OcoOrderManager(
        db=db_b, signed_client=ex_b, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rep_b = mgr_b.execute_entry(_make_req(st))
    assert rep_b.trade_id is not None

    ex_b.crash_on = "after_timestop_sell_before_db"
    with pytest.raises(RuntimeError, match="SIMULATED_CRASH"):
        mgr_b.close_on_time_barrier(
            trade_id=rep_b.trade_id,
            symbol="SOLUSDT",
            base_asset="SOL",
            oco_order_list_id=5001,
            oco_qty=Decimal("10.00"),
            symbol_filters=_sol_filters(),
        )
    db_b.close()
    assert ex_b.free_sol == Decimal("0.00") and len(ex_b.sell_calls) == 1

    ex_b.crash_on = None
    db_b_boot = Database(db_b_path)
    db_b_boot.connect()
    rec_b = StateReconciler(
        db=db_b_boot,
        signed_client=ex_b,  # type: ignore[arg-type]
        oco_manager=OcoOrderManager(
            db=db_b_boot, signed_client=ex_b, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
        ),
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res_b = rec_b.reconcile()
    db_b_boot.close()
    assert res_b.action == ReconcileAction.RECOVERED_CLOSING_TO_CLOSED
    assert len(ex_b.sell_calls) == 1  # Never duplicate sell!

    # Subcase C: Crash while CLOSING but OCO is still EXECUTING on exchange
    db_c = Database(tmp_path / "crash_ts_c.db")
    db_c.connect()
    db_c.migrate()
    ex_c = _StatefulSpotExchange()
    mgr_c = OcoOrderManager(
        db=db_c, signed_client=ex_c, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rep_c = mgr_c.execute_entry(_make_req(st))
    assert rep_c.trade_id is not None
    with db_c.write_tx() as tx:
        tx.execute("UPDATE trades SET status = 'CLOSING' WHERE trade_id = ?", (rep_c.trade_id,))
    rec_c = StateReconciler(
        db=db_c,
        signed_client=ex_c,  # type: ignore[arg-type]
        oco_manager=mgr_c,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res_c = rec_c.reconcile()
    db_c.close()
    assert res_c.action == ReconcileAction.RECOVERED_CLOSING_TO_CLOSED
    assert ex_c.cancel_calls == [5001]
    assert len(ex_c.sell_calls) == 1
    assert ex_c.free_sol == Decimal("0.00") and ex_c.locked_sol == Decimal("0.00")


# =============================================================================
# 3. SQLite Locked Fault Injection + Outbox & Orchestrator Failure Paths
# =============================================================================


def test_fi_sqlite_locked_before_buy_aborts_cleanly_zero_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, _ = _load_cfgs()
    db = Database(tmp_path / "locked_pre_buy.db")
    db.connect()
    db.migrate()

    ex = _StatefulSpotExchange()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )

    def _locked_tx():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "write_tx", _locked_tx)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        mgr.execute_entry(_make_req(st))
    db.close()

    assert ex.buy_calls == []  # Mandatory PENDING_ENTRY failed -> zero order sent!
    assert ex.oco_calls == []


def test_fi_sqlite_locked_after_fill_still_places_oco_and_heals_on_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, _ = _load_cfgs()
    db = Database(tmp_path / "locked_post_fill.db")
    db.connect()
    db.migrate()

    ex = _StatefulSpotExchange()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )

    orig_write_tx = db.write_tx
    writes = 0

    def _locked_after_pending():
        nonlocal writes
        writes += 1
        if writes >= 2:
            raise sqlite3.OperationalError("database is locked")
        return orig_write_tx()

    monkeypatch.setattr(db, "write_tx", _locked_after_pending)
    rep = mgr.execute_entry(_make_req(st))
    assert rep.status == TradeStatus.PROTECTED.value
    assert len(ex.buy_calls) == 1
    assert len(ex.oco_calls) == 1
    assert ex.locked_sol == Decimal("10.00") and ex.free_sol == Decimal("0.00")

    # Unlock SQLite -> reconciler attaches OCO #5001 without duplicate order
    monkeypatch.setattr(db, "write_tx", orig_write_tx)
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res = rec.reconcile()
    db.close()
    assert res.action == ReconcileAction.HEALED_EXISTING_OCO
    assert len(ex.buy_calls) == 1 and len(ex.oco_calls) == 1


def test_fi_sqlite_locked_during_reconciler_pending_entry_must_still_protect_filled_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, _ = _load_cfgs()
    db = Database(tmp_path / "locked_reconcile.db")
    db.connect()
    db.migrate()
    tid = _insert_trade(
        db,
        status=TradeStatus.PENDING_ENTRY.value,
        client_order_id="CHM_SOLUSDT_LOCKED",
        entry_executed_qty=None,
        oco_order_list_id=None,
        oco_list_client_id=None,
        oco_qty=None,
    )
    sm = ShadowManager(st.drawdown_and_shadow_protection, st.portfolio, db=db)

    ex = _StatefulSpotExchange()
    ex.new_market_buy_quote("SOLUSDT", Decimal("1000.00"), "CHM_SOLUSDT_LOCKED")
    assert ex.free_sol == Decimal("10.00") and ex.locked_sol == Decimal("0.00")

    orig_write_tx = db.write_tx

    def _locked_tx():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "write_tx", _locked_tx)
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
    res_locked = rec.reconcile()

    # Invariant (C1-E): even if SQLite is locked, the exchange position MUST be protected by an OCO!
    assert res_locked.action == ReconcileAction.RECOVERED_PENDING_TO_PROTECTED
    assert len(ex.oco_calls) == 1
    assert ex.locked_sol == Decimal("10.00") and ex.free_sol == Decimal("0.00")

    # Unlock SQLite -> next reconcile heals DB to PROTECTED without creating a duplicate OCO!
    monkeypatch.setattr(db, "write_tx", orig_write_tx)
    res_healed = rec.reconcile()
    with db.read() as conn:
        row = conn.execute("SELECT status, oco_order_list_id FROM trades WHERE trade_id = ?", (tid,)).fetchone()
    db.close()

    assert res_healed.action == ReconcileAction.HEALED_EXISTING_OCO
    assert len(ex.oco_calls) == 1  # Zero duplicate OCO
    assert row is not None and row["status"] == TradeStatus.PROTECTED.value and row["oco_order_list_id"] == 5001


def test_fi_sqlite_locked_during_time_stop_still_exits_and_reconciler_heals_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st, _ = _load_cfgs()
    db = Database(tmp_path / "locked_ts.db")
    db.connect()
    db.migrate()

    ex = _StatefulSpotExchange()
    mgr = OcoOrderManager(
        db=db, signed_client=ex, resilience_cfg=st.execution_and_resilience, sleep_fn=lambda _: None  # type: ignore[arg-type]
    )
    rep = mgr.execute_entry(_make_req(st))
    assert rep.trade_id is not None

    orig_write_tx = db.write_tx
    monkeypatch.setattr(db, "write_tx", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    ts_rep = mgr.close_on_time_barrier(
        trade_id=rep.trade_id,
        symbol="SOLUSDT",
        base_asset="SOL",
        oco_order_list_id=5001,
        oco_qty=Decimal("10.00"),
        symbol_filters=_sol_filters(),
    )
    assert ts_rep.status == TradeStatus.CLOSED.value
    assert ex.free_sol == Decimal("0.00") and ex.locked_sol == Decimal("0.00")
    assert len(ex.sell_calls) == 1

    # Unlock SQLite -> reconciler closes stale PROTECTED row cleanly without duplicate sell
    monkeypatch.setattr(db, "write_tx", orig_write_tx)
    rec = StateReconciler(
        db=db,
        signed_client=ex,  # type: ignore[arg-type]
        oco_manager=mgr,
        execution_universe=st.universe.symbols,
        filters_provider=_sol_filters,
    )
    res = rec.reconcile()
    db.close()
    assert res.action == ReconcileAction.CLOSED_COMPLETED_OCO
    assert len(ex.sell_calls) == 1


def test_fi_telegram_outbox_http_retries_and_live_run_once_auth_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers HttpTelegramTransport.send_message, TelegramOutbox retry->FAILED, and live run_once -2015 halt."""
    # 1. HttpTelegramTransport + TelegramOutbox exhaustion
    db_tg = Database(tmp_path / "tg_outbox.db")
    db_tg.connect()
    db_tg.migrate()
    transport = HttpTelegramTransport("123:TOKEN", timeout_s=2.0)

    def _fail_post(url: str, **kwargs: Any) -> MagicMock:
        return _mock_response(200, {"ok": False, "description": "Bad Request"})

    monkeypatch.setattr(transport._session, "post", _fail_post)
    outbox = TelegramOutbox(db_tg, "999", transport=transport, max_attempts=2)
    outbox.enqueue("ALERT", "test message", now_ms=1000)

    s1 = outbox.flush(now_ms=2000)
    assert s1.sent == 0 and s1.failed == 0 and s1.pending_remaining == 1
    s2 = outbox.flush(now_ms=3000)
    assert s2.sent == 0 and s2.failed == 1 and s2.pending_remaining == 0

    # Succeeding send_message path
    monkeypatch.setattr(
        transport._session,
        "post",
        lambda url, **kw: _mock_response(200, {"ok": True, "result": {"message_id": 42}}),
    )
    outbox.enqueue("ALERT", "recovered", now_ms=4000)
    s3 = outbox.flush(now_ms=5000)
    assert s3.sent == 1 and s3.failed == 0 and s3.pending_remaining == 0
    db_tg.close()

    # 2. Live run_once with -2015 in StateReconciler -> halts, blocks entries, enqueues KILL_SWITCH
    # Local imports ensure identical class objects in sys.modules even after test_gate4e's notebook reload.
    from src.core.binance_client import (
        BinanceAPIError as _LiveBinanceAPIError,
        ExchangeInfoCache as _LiveExchangeInfoCache,
        MonotonicClock as _LiveClock,
        SystemStatus as _LiveSystemStatus,
        WeightGovernor as _LiveGovernor,
    )
    from src.core.config import (
        AnalystConfig as _LiveAnalystConfig,
        BinanceEnv as _LiveBinanceEnv,
        RunMode as _LiveRunMode,
        RuntimeSettings as _LiveRuntimeSettings,
        StrategiesConfig as _LiveStrategiesConfig,
        load_yaml_strict as _load_yaml_strict,
    )
    from src.core.database import Database as _LiveDatabase, SymbolFilters as _LiveSymbolFilters
    from src.execution.executor_protocol import TradeStatus as _LiveTradeStatus
    from src.main import QuantOrchestrator as _LiveOrchestrator
    from src.notifications.telegram import TelegramOutbox as _LiveOutbox
    from src.radar.orderbook_inspector import OrderbookInspector as _LiveInspector

    st = _LiveStrategiesConfig.model_validate(_load_yaml_strict(Path("config/strategies.yaml")))
    an = _LiveAnalystConfig.model_validate(_load_yaml_strict(Path("config/analyst.yaml")))
    db = _LiveDatabase(tmp_path / "live_2015.db")
    db.connect()
    db.migrate()
    now_ms = 1_700_500_003_000
    _insert_trade(db, status=_LiveTradeStatus.PROTECTED.value, oco_order_list_id=5001, oco_qty="10.00")

    def _live_filters(symbol: str = "SOLUSDT") -> _LiveSymbolFilters:
        return _LiveSymbolFilters(
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

    class _PubStub:
        governor = _LiveGovernor()

        def get_server_time_ms(self) -> int:
            return now_ms

        def get_exchange_info(self, symbols: Any) -> dict[str, _LiveSymbolFilters]:
            return {s: _live_filters(s) for s in symbols}

    class _SignedAuthRevoked:
        def __init__(self, gov: _LiveGovernor, clk: _LiveClock) -> None:
            self.governor = gov
            self.clock = clk

        def sapi_system_status(self) -> _LiveSystemStatus:
            return _LiveSystemStatus(normal=True, msg="ok", mocked=True)

        def get_account(self, **_: Any) -> dict[str, Any]:
            raise _LiveBinanceAPIError(401, -2015, "Invalid API-key, IP, or permissions.", "GET /api/v3/account")

    pub = _PubStub()
    clock = _LiveClock()
    clock.sync(pub.get_server_time_ms)
    signed = _SignedAuthRevoked(pub.governor, clock)
    all_syms = list(dict.fromkeys(list(st.universe.symbols) + list(an.scanner_universe.symbols) + ["BNBUSDT"]))
    ex_cache = _LiveExchangeInfoCache(pub, db, all_syms)  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(now_ms)

    settings = _LiveRuntimeSettings(
        mode=_LiveRunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=_LiveBinanceEnv.TESTNET,
        binance_api_key="k",
        telegram_bot_token="123:ABC",
        telegram_chat_id="1",
        db_path=tmp_path / "live_2015.db",
        backup_dir=tmp_path / "bkp",
    )
    orch = _LiveOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=signed,  # type: ignore[arg-type]
        clock=clock,
        exchange_cache=ex_cache,
        outbox=_LiveOutbox(db, "1", transport=None),
        orderbook_inspector=_LiveInspector(an.layer_4_orderbook_live, sleep_fn=lambda _: None),
        sleep_fn=lambda _: None,
    )
    rep = orch.run_once(now_ms=now_ms, skip_collect=True)
    with db.read() as conn:
        ks = conn.execute("SELECT kind FROM telegram_outbox WHERE kind = 'KILL_SWITCH'").fetchone()
    db.close()

    assert rep.emergency_halted is True
    assert rep.chameleon_executed is False
    assert rep.unhandled_errors >= 1
    assert ks is not None
