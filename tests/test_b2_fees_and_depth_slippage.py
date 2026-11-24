"""Tests for B2: net fee accounting (fees_usdt_equiv) in LIVE and L2 depth VWAP slippage in SHADOW."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.core.binance_client import BookTicker, DepthSnapshot, MonotonicClock, WeightGovernor
from src.core.config import BinanceEnv, StrategiesConfig, load_yaml_strict
from src.core.database import CandleRow, Database, SymbolFilters
from src.core.state_reconciler import ReconcileAction, StateReconciler
from src.execution.executor_protocol import ExecutionRequest, TradeStatus
from src.execution.oco_order_manager import OcoOrderManager, compute_fills_fee_usdt
from src.execution.paper_executor import PaperExecutor, compute_depth_vwap_ask
from src.risk.position_sizer import PositionSizer


def _load_strategies_cfg() -> StrategiesConfig:
    return StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))


def _seed_bnb_candle(db: Database, bnb_close: float = 500.0) -> None:
    db.upsert_candles(
        [
            CandleRow(
                symbol="BNBUSDT",
                interval="1h",
                open_time_ms=1_700_000_000_000,
                close_time_ms=1_700_003_600_000,
                open=bnb_close,
                high=bnb_close + 1.0,
                low=bnb_close - 1.0,
                close=bnb_close,
                volume=100.0,
                quote_volume=100.0 * bnb_close,
                number_of_trades=10,
                taker_buy_base_volume=50.0,
                taker_buy_quote_volume=50.0 * bnb_close,
                ingested_at_ms=1_700_003_600_000,
            )
        ]
    )


def test_b2_compute_fills_fee_usdt_handles_bnb_usdt_and_base_without_double_count() -> None:
    fills = [
        {"commission": "0.001", "commissionAsset": "BNB"},   # 0.001 * 500 = 0.50 USDT
        {"commission": "0.25", "commissionAsset": "USDT"},   # 0.25 USDT
        {"commission": "0.01", "commissionAsset": "SOL"},    # 0.01 * 100 = 1.00 USDT
    ]
    # Entry BUY: exclude base asset (already deducted from sellable qty)
    entry_fee = compute_fills_fee_usdt(
        fills,
        base_asset="SOL",
        base_price_usdt=Decimal("100"),
        bnb_price_usdt=Decimal("500"),
        exclude_base_asset=True,
    )
    assert entry_fee == Decimal("0.75000000")

    # Exit SELL: include all assets
    exit_fee = compute_fills_fee_usdt(
        fills,
        base_asset="SOL",
        base_price_usdt=Decimal("100"),
        bnb_price_usdt=Decimal("500"),
        exclude_base_asset=False,
    )
    assert exit_fee == Decimal("1.75000000")


def test_b2_live_entry_and_reconciler_oco_close_populate_fees_usdt_equiv_and_net_r(tmp_path: Path) -> None:
    st_cfg = _load_strategies_cfg()
    db = Database(tmp_path / "live_fees.db")
    db.connect()
    db.migrate()
    _seed_bnb_candle(db, bnb_close=500.0)

    flt = SymbolFilters(
        symbol="SOLUSDT",
        status="TRADING",
        base_asset="SOL",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("10"),
        oco_allowed=True,
        fetched_at_ms=1_700_003_600_000,
    )

    class _FakeSigned:
        def __init__(self) -> None:
            self.governor = WeightGovernor()
            self.clock = MonotonicClock()
            self.clock.sync(lambda: 1_700_003_600_000, samples=1)
            self.oco_done = False
            self.book_ticker_calls_before_oco = 0
            self.oco_created = False

        def new_market_buy_quote(self, symbol: str, quote_qty: Decimal, client_order_id: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "orderId": 1001,
                "clientOrderId": client_order_id,
                "status": "FILLED",
                "executedQty": "1.00",
                "cummulativeQuoteQty": "100.00",
                "fills": [
                    {"price": "100.00", "qty": "1.00", "commission": "0.0002", "commissionAsset": "BNB"}
                ],  # 0.0002 * 500 = 0.10 USDT
            }

        def get_account(self, omit_zero_balances: bool = True) -> dict[str, Any]:
            if self.oco_done:
                return {"balances": [{"asset": "USDT", "free": "312.00", "locked": "0"}]}
            return {"balances": [{"asset": "SOL", "free": "1.00", "locked": "0"}]}

        def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
            if not self.oco_created and symbol == "BNBUSDT":
                self.book_ticker_calls_before_oco += 1
            px = Decimal("500.00") if symbol == "BNBUSDT" else Decimal("100.00")
            return BookTicker(symbol=symbol, bid=px, bid_qty=Decimal("5"), ask=px, ask_qty=Decimal("5"))

        def new_oco_sell(self, **kwargs: Any) -> dict[str, Any]:
            self.oco_created = True
            return {"orderListId": 5001, "listClientOrderId": kwargs["list_client_order_id"]}

        def get_order_list(self, order_list_id: int) -> dict[str, Any]:
            return {
                "orderListId": order_list_id,
                "listOrderStatus": "ALL_DONE",
                "orderReports": [
                    {
                        "orderId": 2001,
                        "type": "LIMIT_MAKER",
                        "executedQty": "1.00",
                        "cummulativeQuoteQty": "112.00",
                    }
                ],
            }

        def get_my_trades(self, symbol: str, *, order_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
            if order_id == 2001:
                return [
                    {"orderId": 2001, "commission": "0.0003", "commissionAsset": "BNB"}
                ]  # 0.0003 * 500 = 0.15 USDT
            return []

    signed = _FakeSigned()
    oco_mgr = OcoOrderManager(
        db=db,
        signed_client=signed,  # type: ignore[arg-type]
        resilience_cfg=st_cfg.execution_and_resilience,
        environment=BinanceEnv.TESTNET,
        sleep_fn=lambda _: None,
    )
    sizer = PositionSizer(st_cfg.position_sizing, st_cfg.portfolio)
    sizing = sizer.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price=Decimal("100.00"),
        stop_price=Decimal("94.00"),   # risk_unit = 6.00
        target_price=Decimal("112.00"), # +2R gross = +12.00
        free_usdt=Decimal("600.00"),
        tactical_equity_usdt=Decimal("600.00"),
        symbol_filters=flt,
    )
    assert sizing.approved
    req = ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_003_600_000,
        atr_at_signal=2.0,
        time_barrier_end_ms=1_700_262_800_000,
        sizing=sizing,
        symbol_filters=flt,
    )
    rep = oco_mgr.execute_entry(req)
    assert rep.executed
    # Zero network call for BNBUSDT before OCO placement (C1-B / C1-E preserved)
    assert signed.book_ticker_calls_before_oco == 0

    with db.read() as conn:
        t_row = conn.execute("SELECT fees_usdt_equiv FROM trades WHERE trade_id = ?", (rep.trade_id,)).fetchone()
    assert Decimal(str(t_row["fees_usdt_equiv"])) == Decimal("0.10")

    # Now OCO completes at target (+12.00 gross) and reconciler closes trade with entry (0.10) + exit (0.15) = 0.25 fees
    signed.oco_done = True
    rec = StateReconciler(
        db=db,
        signed_client=signed,  # type: ignore[arg-type]
        oco_manager=oco_mgr,
        execution_universe=["SOLUSDT"],
        filters_provider=lambda s: flt,
    )
    res = rec.reconcile()
    assert res.action == ReconcileAction.CLOSED_COMPLETED_OCO

    with db.read() as conn:
        closed = conn.execute(
            "SELECT status, fees_usdt_equiv, realized_pnl_usdt, realized_r FROM trades WHERE trade_id = ?",
            (rep.trade_id,),
        ).fetchone()
    assert closed["status"] == TradeStatus.CLOSED.value
    assert Decimal(str(closed["fees_usdt_equiv"])) == Decimal("0.25")
    assert Decimal(str(closed["realized_pnl_usdt"])) == Decimal("12")
    # Net R = (12.00 - 0.25) / (6.00 * 1.00) = 11.75 / 6.00 = 1.958333...
    assert abs(float(closed["realized_r"]) - (11.75 / 6.0)) < 1e-6
    db.close()


def test_b2_depth_vwap_slippage_and_legacy_v1_migration(tmp_path: Path) -> None:
    st_cfg = _load_strategies_cfg()

    # 1. Pure VWAP helper across 3 ask levels
    asks = [(100.0, 1.0), (100.2, 2.0), (100.5, 5.0)]
    vwap = compute_depth_vwap_ask(asks, Decimal("4.0"))
    # (1*100.0 + 2*100.2 + 1*100.5) / 4 = 400.9 / 4 = 100.225
    assert vwap == Decimal("100.225")
    assert compute_depth_vwap_ask(asks, Decimal("20.0")) is None

    # 2. Legacy v1 DB without depth_vwap_ask / book_impact_bps migrates cleanly
    legacy_path = tmp_path / "legacy_v1.db"
    raw = sqlite3.connect(str(legacy_path))
    raw.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        INSERT INTO schema_meta VALUES ('version', '1');
        CREATE TABLE shadow_trades (
            shadow_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,
            regime TEXT NOT NULL,
            signal_close_time_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            decision_ask_price REAL NOT NULL,
            sim_entry_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            target_price REAL NOT NULL,
            qty TEXT NOT NULL,
            risk_pct REAL NOT NULL,
            risk_usdt TEXT NOT NULL,
            virtual_equity_before TEXT NOT NULL,
            time_barrier_end_ms INTEGER NOT NULL,
            exit_reason TEXT,
            exit_price REAL,
            r_gross REAL,
            r_net REAL,
            ml_grade_silent TEXT,
            radar_signal_id TEXT,
            created_at_ms INTEGER NOT NULL,
            closed_at_ms INTEGER
        );
        """
    )
    raw.commit()
    raw.close()

    db = Database(legacy_path)
    db.connect()
    db.migrate()
    assert Database.verify_backup(legacy_path)

    class _DepthPub:
        def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
            return BookTicker(
                symbol=symbol,
                bid=Decimal("99.99"),
                bid_qty=Decimal("10"),
                ask=Decimal("100.00"),
                ask_qty=Decimal("0.5"),
            )

        def get_depth(self, symbol: str, limit: int = 20) -> DepthSnapshot:
            return DepthSnapshot(
                symbol=symbol,
                last_update_id=1,
                taken_at_ms=1_700_003_600_000,
                requested_limit=limit,
                bids=((99.99, 10.0),),
                asks=((100.00, 0.5), (100.10, 0.5), (100.20, 5.0)),
            )

    flt = SymbolFilters(
        symbol="SOLUSDT",
        status="TRADING",
        base_asset="SOL",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("10"),
        oco_allowed=True,
        fetched_at_ms=1_700_003_600_000,
    )
    sizer = PositionSizer(st_cfg.position_sizing, st_cfg.portfolio)
    sizing = sizer.evaluate_and_size(
        symbol="SOLUSDT",
        entry_price=Decimal("100.00"),
        stop_price=Decimal("97.00"),
        target_price=Decimal("106.00"),
        free_usdt=Decimal("500.00"),
        tactical_equity_usdt=Decimal("500.00"),
        symbol_filters=flt,
    )
    assert sizing.approved
    paper = PaperExecutor(db=db, public_client=_DepthPub(), sizing_cfg=st_cfg.position_sizing)  # type: ignore[arg-type]
    req = ExecutionRequest(
        symbol="SOLUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=1_700_003_600_000,
        atr_at_signal=1.0,
        time_barrier_end_ms=1_700_262_800_000,
        sizing=sizing,
        symbol_filters=flt,
    )
    rep = paper.execute_entry(req, virtual_equity_before=Decimal("500.00"), now_ms=1_700_003_600_000)
    assert rep.executed
    with db.read() as conn:
        row = conn.execute(
            "SELECT decision_ask_price, depth_vwap_ask, book_impact_bps, sim_entry_price FROM shadow_trades"
        ).fetchone()
    assert row["depth_vwap_ask"] is not None and row["depth_vwap_ask"] > 100.0
    assert row["book_impact_bps"] is not None and row["book_impact_bps"] > 0.0
    assert abs(row["decision_ask_price"] - row["depth_vwap_ask"]) < 1e-9
    assert row["sim_entry_price"] >= row["depth_vwap_ask"]
    db.close()
