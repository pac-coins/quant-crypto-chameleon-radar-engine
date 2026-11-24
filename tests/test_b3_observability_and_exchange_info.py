"""Tests for B3: weekly Radar scorecard scheduling, VPS disk alerts, and -1121 isolation in exchangeInfo."""

from __future__ import annotations

from collections import namedtuple
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from src.core.binance_client import (
    BinanceAPIError,
    BookTicker,
    ExchangeInfoCache,
    MonotonicClock,
    WeightGovernor,
)
from src.core.config import (
    AnalystConfig,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_yaml_strict,
)
from src.core.database import Database, SymbolFilters
from src.main import ONE_DAY_MS, ONE_HOUR_MS, ONE_WEEK_MS, QuantOrchestrator
from src.notifications.telegram import TelegramOutbox

_DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


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


class _DelistedRadarClient:
    def __init__(self, now_ms: int, invalid_symbols: set[str]) -> None:
        self.governor = WeightGovernor()
        self._now_ms = now_ms
        self._invalid = set(invalid_symbols)

    def get_server_time_ms(self) -> int:
        return self._now_ms

    def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
        return BookTicker(
            symbol=symbol,
            bid=Decimal("100.0"),
            bid_qty=Decimal("10"),
            ask=Decimal("100.1"),
            ask_qty=Decimal("10"),
        )

    def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
        syms = list(symbols)
        if any(s in self._invalid for s in syms):
            raise BinanceAPIError(400, -1121, "Invalid symbol.", "GET /api/v3/exchangeInfo")
        return {
            s: SymbolFilters(
                symbol=s,
                status="TRADING",
                base_asset=s.replace("USDT", ""),
                quote_asset="USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                min_notional=Decimal("10"),
                oco_allowed=True,
                fetched_at_ms=self._now_ms,
            )
            for s in syms
        }


def test_b3_exchange_info_isolates_delisted_radar_symbol_without_breaking_execution(tmp_path: Path) -> None:
    st_cfg, an_cfg = _load_cfgs()
    db = Database(tmp_path / "b3_ex.db")
    db.connect()
    db.migrate()

    now_ms = 1_700_000_000_000
    all_syms = list(
        dict.fromkeys(list(st_cfg.universe.symbols) + list(an_cfg.scanner_universe.symbols))
    )
    client = _DelistedRadarClient(now_ms, invalid_symbols={"OPUSDT"})
    cache = ExchangeInfoCache(
        client,  # type: ignore[arg-type]
        db,
        all_syms,
        priority_symbols=st_cfg.universe.symbols,
    )
    refreshed = cache.refresh_if_stale(now_ms)
    assert refreshed is True
    assert cache.last_invalid_symbols == ("OPUSDT",)
    for exec_sym in st_cfg.universe.symbols:
        assert cache.get(exec_sym).status == "TRADING"

    # Fail-closed if an execution (priority) symbol itself is invalid (-1121)
    bad_exec_client = _DelistedRadarClient(now_ms, invalid_symbols={"BTCUSDT"})
    bad_cache = ExchangeInfoCache(
        bad_exec_client,  # type: ignore[arg-type]
        db,
        all_syms,
        priority_symbols=st_cfg.universe.symbols,
    )
    with pytest.raises(BinanceAPIError) as exc_info:
        bad_cache.refresh_if_stale(now_ms)
    assert exc_info.value.code == -1121
    db.close()


def test_b3_weekly_radar_scorecard_disk_warning_and_exchange_info_alert_in_orchestrator(
    tmp_path: Path,
) -> None:
    st_cfg, an_cfg = _load_cfgs()
    db_path = tmp_path / "b3_orch.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    now_ms = 1_700_000_000_000 - (1_700_000_000_000 % ONE_HOUR_MS)
    all_syms = list(
        dict.fromkeys(list(st_cfg.universe.symbols) + list(an_cfg.scanner_universe.symbols))
    )
    pub = _DelistedRadarClient(now_ms, invalid_symbols={"OPUSDT"})
    clock = MonotonicClock()
    clock.sync(lambda: now_ms, samples=1)
    ex_cache = ExchangeInfoCache(
        pub,  # type: ignore[arg-type]
        db,
        all_syms,
        priority_symbols=st_cfg.universe.symbols,
    )
    transport = _DummyTransport()
    outbox = TelegramOutbox(db, "chat1", transport=transport)
    settings = RuntimeSettings(
        mode=RunMode.ANALYST_ONLY,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
    )

    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st_cfg,
        analyst_cfg=an_cfg,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=None,
        clock=clock,
        exchange_cache=ex_cache,
        outbox=outbox,
        sleep_fn=lambda _: None,
        disk_usage_fn=lambda _: _DiskUsage(total=100 * 1024**3, used=78 * 1024**3, free=22 * 1024**3),
    )

    # Cycle 0: isolates OPUSDT with 0 unhandled errors, alerts DISK_USAGE_WARNING (>=70%) and EXCHANGE_INFO_WARNING,
    # seeds weekly scorecard baseline (no premature scorecard on day 0).
    rep0 = orch.run_once(now_ms=now_ms, skip_collect=True)
    assert rep0.unhandled_errors == 0
    assert db.get_state("disk_usage_pct") == "78.00"
    with db.read() as conn:
        kinds0 = [r["kind"] for r in conn.execute("SELECT kind FROM telegram_outbox").fetchall()]
    assert kinds0.count("EXCHANGE_INFO_WARNING") == 1
    assert kinds0.count("DISK_USAGE_WARNING") == 1
    assert kinds0.count("RADAR_WEEKLY_SCORECARD") == 0

    # Cycle 1 (6 days later): still no weekly scorecard
    orch.run_once(now_ms=now_ms + 6 * ONE_DAY_MS, skip_collect=True)
    with db.read() as conn:
        kinds1 = [r["kind"] for r in conn.execute("SELECT kind FROM telegram_outbox").fetchall()]
    assert kinds1.count("RADAR_WEEKLY_SCORECARD") == 0

    # Cycle 2 (7 days after baseline): emits weekly scorecard exactly once
    orch.run_once(now_ms=now_ms + ONE_WEEK_MS, skip_collect=True)
    with db.read() as conn:
        rows2 = conn.execute(
            "SELECT kind, payload FROM telegram_outbox WHERE kind = 'RADAR_WEEKLY_SCORECARD'"
        ).fetchall()
    assert len(rows2) == 1
    assert "Radar Weekly Scorecard" in rows2[0]["payload"]

    # Cycle 3 (1h later): does NOT duplicate weekly scorecard
    orch.run_once(now_ms=now_ms + ONE_WEEK_MS + ONE_HOUR_MS, skip_collect=True)
    with db.read() as conn:
        count3 = conn.execute(
            "SELECT COUNT(*) FROM telegram_outbox WHERE kind = 'RADAR_WEEKLY_SCORECARD'"
        ).fetchone()[0]
    assert count3 == 1
    db.close()
