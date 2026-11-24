"""Tests for B6: single-thread sub-hourly hanging-stop watchdog during inter-cycle wait."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from src.core.binance_client import (
    BookTicker,
    ExchangeInfoCache,
    MonotonicClock,
    SystemStatus,
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
from src.core.database import Database, SymbolFilters
from src.execution.executor_protocol import ExitReason, TradeStatus
from src.main import ONE_HOUR_MS, QuantOrchestrator
from src.notifications.telegram import TelegramOutbox


def _load_cfgs() -> tuple[StrategiesConfig, AnalystConfig]:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    return st, an


class _DummyTransport:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        self.messages.append(text)
        return len(self.messages)


class _FakeWatchdogSignedClient:
    def __init__(self, clock: MonotonicClock, crash_at_ms: int) -> None:
        self.governor = WeightGovernor()
        self.clock = clock
        self.crash_at_ms = crash_at_ms
        self.book_ticker_calls = 0
        self.cancel_calls = 0
        self.sell_calls = 0

    def get_server_time_ms(self) -> int:
        return self.clock.server_now_ms()

    def sapi_system_status(self) -> SystemStatus:
        return SystemStatus(normal=True, msg="normal", mocked=True)

    def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
        self.book_ticker_calls += 1
        # Simulate flash-crash jumping past stopLimitPrice (91.65) at crash_at_ms
        bid = Decimal("88.00") if self.clock.server_now_ms() >= self.crash_at_ms else Decimal("99.50")
        return BookTicker(
            symbol=symbol,
            bid=bid,
            bid_qty=Decimal("10"),
            ask=bid + Decimal("0.05"),
            ask_qty=Decimal("10"),
        )

    def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
        return {
            s: SymbolFilters(
                symbol=s,
                status="TRADING",
                base_asset=s.replace("USDT", ""),
                quote_asset="USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.01"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("10"),
                oco_allowed=True,
                fetched_at_ms=self.clock.server_now_ms(),
            )
            for s in symbols
        }

    def get_order_list(self, order_list_id: int) -> dict[str, Any]:
        return {
            "orderListId": order_list_id,
            "listOrderStatus": "EXECUTING",
            "orderReports": [
                {"orderId": 10, "executedQty": "0", "cummulativeQuoteQty": "0"},
                {"orderId": 11, "executedQty": "0", "cummulativeQuoteQty": "0"},
            ],
        }

    def cancel_order_list(self, symbol: str, order_list_id: int) -> dict[str, Any]:
        self.cancel_calls += 1
        return {
            "orderListId": order_list_id,
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"orderId": 10, "executedQty": "0", "cummulativeQuoteQty": "0"},
                {"orderId": 11, "executedQty": "0", "cummulativeQuoteQty": "0"},
            ],
        }

    def get_account(self, omit_zero_balances: bool = True) -> dict[str, Any]:
        return {"balances": [{"asset": "SOL", "free": "1.00", "locked": "0"}]}

    def new_market_sell(self, symbol: str, qty: Decimal, client_order_id: str) -> dict[str, Any]:
        self.sell_calls += 1
        return {
            "symbol": symbol,
            "orderId": 9999,
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": str(qty),
            "cummulativeQuoteQty": "88.00",
            "fills": [{"price": "88.00", "qty": str(qty), "commission": "0", "commissionAsset": "USDT"}],
        }


def test_b6_subhourly_watchdog_liquidates_flash_crash_within_20s_and_stops_polling_when_flat(
    tmp_path: Path,
) -> None:
    st_cfg, an_cfg = _load_cfgs()
    db_path = tmp_path / "b6.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    # Fake monotonic clock starting at XX:00:05 UTC
    base_hour_ms = 1_700_006_400_000  # divisible by 3_600_000
    sim_time_s = [float(base_hour_ms) / 1000.0 + 5.0]
    clock = MonotonicClock(mono=lambda: sim_time_s[0], wall=lambda: sim_time_s[0])
    clock.sync(lambda: int(sim_time_s[0] * 1000), samples=1)

    crash_at_ms = base_hour_ms + 120_000  # flash-crash at XX:02:00 UTC
    signed = _FakeWatchdogSignedClient(clock, crash_at_ms=crash_at_ms)
    ex_cache = ExchangeInfoCache(signed, db, ["SOLUSDT"])  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(clock.server_now_ms())

    # Insert an open PROTECTED trade with stopLimitPrice = 91.65
    with db.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt, quote_order_qty,
                entry_order_id, entry_executed_qty, entry_avg_price, entry_quote_spent,
                oco_order_list_id, oco_list_client_id, oco_qty,
                oco_stop_price, oco_stop_limit_price, oco_target_price,
                time_barrier_end_ms, created_at_ms, updated_at_ms
            ) VALUES (
                'CHM_SOL_1', 'TESTNET', 'SOLUSDT', 'bull_trend_pullback', 'BULL',
                ?, 'PROTECTED',
                '100.00', '94.00', '112.00',
                2.0, 0.01, '6.00', '100.00',
                1001, '1.00', '100.00', '100.00',
                5001, 'OCO_SOL_1', '1.00',
                '94.00', '91.65', '112.00',
                ?, ?, ?
            )
            """,
            (base_hour_ms, base_hour_ms + 72 * ONE_HOUR_MS, base_hour_ms, base_hour_ms),
        )

    transport = _DummyTransport()
    outbox = TelegramOutbox(db, "chat1", transport=transport)
    key_file = tmp_path / "k.pem"
    key_file.write_text("dummy", encoding="utf-8")
    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key=SecretStr("key"),
        binance_ed25519_private_key_path=key_file,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        watchdog_poll_interval_seconds=20.0,
    )

    liquidated_at_ms: list[int] = []

    def _advance_sleep(s: float) -> None:
        sim_time_s[0] += s
        if signed.sell_calls > 0 and not liquidated_at_ms:
            liquidated_at_ms.append(clock.server_now_ms())

    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st_cfg,
        analyst_cfg=an_cfg,
        db=db,
        public_client=signed,  # type: ignore[arg-type]
        signed_client=signed,  # type: ignore[arg-type]
        clock=clock,
        exchange_cache=ex_cache,
        outbox=outbox,
        sleep_fn=_advance_sleep,
    )

    target_boundary = orch.wait_for_next_cycle_with_watchdog(poll_interval_s=20.0)
    assert target_boundary == base_hour_ms + ONE_HOUR_MS
    assert signed.cancel_calls == 1
    assert signed.sell_calls == 1
    # Liquidated within <= 25s of flash-crash (NOT waiting ~58 min for next hourly boundary!)
    assert liquidated_at_ms and (liquidated_at_ms[0] - crash_at_ms) <= 25_000
    # Stopped polling bookTicker once trade became flat (about 7 ticks during first 2 min, not 180 ticks for whole hour)
    assert signed.book_ticker_calls <= 10

    with db.read() as conn:
        t_row = conn.execute("SELECT status, exit_reason FROM trades WHERE client_order_id = 'CHM_SOL_1'").fetchone()
        alerts = [r["kind"] for r in conn.execute("SELECT kind FROM telegram_outbox").fetchall()]
    assert t_row["status"] == TradeStatus.CLOSED.value
    assert t_row["exit_reason"] == ExitReason.STOP_WATCHDOG.value
    assert "STOP_WATCHDOG" in alerts
    db.close()


def test_b6_wait_loop_makes_zero_ticker_calls_when_flat_and_honors_sigterm(tmp_path: Path) -> None:
    st_cfg, an_cfg = _load_cfgs()
    db_path = tmp_path / "b6_flat.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    base_hour_ms = 1_700_006_400_000
    sim_time_s = [float(base_hour_ms) / 1000.0 + 10.0]
    clock = MonotonicClock(mono=lambda: sim_time_s[0], wall=lambda: sim_time_s[0])
    clock.sync(lambda: int(sim_time_s[0] * 1000), samples=1)

    signed = _FakeWatchdogSignedClient(clock, crash_at_ms=base_hour_ms)
    ex_cache = ExchangeInfoCache(signed, db, ["SOLUSDT"])  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(clock.server_now_ms())
    outbox = TelegramOutbox(db, "chat1", transport=_DummyTransport())
    key_file = tmp_path / "k.pem"
    key_file.write_text("dummy", encoding="utf-8")
    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key=SecretStr("key"),
        binance_ed25519_private_key_path=key_file,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
    )

    sleeps: list[float] = []
    stop_flag = [False]

    def _sleep(s: float) -> None:
        sleeps.append(s)
        sim_time_s[0] += s
        if len(sleeps) >= 2:
            stop_flag[0] = True

    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st_cfg,
        analyst_cfg=an_cfg,
        db=db,
        public_client=signed,  # type: ignore[arg-type]
        signed_client=signed,  # type: ignore[arg-type]
        clock=clock,
        exchange_cache=ex_cache,
        outbox=outbox,
        sleep_fn=_sleep,
    )

    orch.wait_for_next_cycle_with_watchdog(stop_requested_fn=lambda: stop_flag[0], poll_interval_s=20.0)
    assert signed.book_ticker_calls == 0
    assert len(sleeps) == 2
    assert all(s <= 5.0 for s in sleeps)
    db.close()
