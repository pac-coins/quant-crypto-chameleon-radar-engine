"""Regression tests for AUD-01, AUD-05, and AUD-06 from docs/backtest-edge-audit.md."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import SecretStr

from src.core.binance_client import (
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
from src.core.database import CandleRow, Database, SymbolFilters
from src.main import ONE_HOUR_MS, QuantOrchestrator
from src.notifications.telegram import TelegramOutbox
from src.regimes.regime_detector import MarketRegime


class _StubPublic:
    def __init__(self, now_ms: int) -> None:
        self.governor = WeightGovernor()
        self._now_ms = now_ms

    def get_server_time_ms(self) -> int:
        return self._now_ms

    def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
        return BookTicker(
            symbol=symbol,
            bid=Decimal("350.00"),
            bid_qty=Decimal("10"),
            ask=Decimal("350.05"),
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
                fetched_at_ms=self._now_ms,
            )
            for s in symbols
        }


def _seed_uptrend(
    db: Database,
    symbol: str,
    end_close_ms: int,
    *,
    n_1h: int,
    n_4h: int,
    include_future_4h_crash: bool = False,
) -> None:
    rows: list[CandleRow] = []
    for i in range(n_4h):
        c_ms = end_close_ms - (n_4h - 1 - i) * 4 * ONE_HOUR_MS
        o_ms = c_ms - 4 * ONE_HOUR_MS
        px = 100.0 + i * 1.0
        rows.append(
            CandleRow(
                symbol=symbol,
                interval="4h",
                open_time_ms=o_ms,
                close_time_ms=c_ms,
                open=px - 0.5,
                high=px + 1.0,
                low=px - 1.0,
                close=px,
                volume=400.0,
                quote_volume=400.0 * px,
                number_of_trades=200,
                taker_buy_base_volume=240.0,
                taker_buy_quote_volume=240.0 * px,
                ingested_at_ms=c_ms,
            )
        )
    if include_future_4h_crash:
        # Future 4h candle AFTER end_close_ms: MUST be ignored causally
        fut_o = end_close_ms
        fut_c = end_close_ms + 4 * ONE_HOUR_MS
        rows.append(
            CandleRow(
                symbol=symbol,
                interval="4h",
                open_time_ms=fut_o,
                close_time_ms=fut_c,
                open=350.0,
                high=350.0,
                low=10.0,
                close=10.0,
                volume=9999.0,
                quote_volume=99990.0,
                number_of_trades=500,
                taker_buy_base_volume=100.0,
                taker_buy_quote_volume=1000.0,
                ingested_at_ms=fut_c,
            )
        )
    for i in range(n_1h):
        c_ms = end_close_ms - (n_1h - 1 - i) * ONE_HOUR_MS
        o_ms = c_ms - ONE_HOUR_MS
        px = 260.0 + i * 0.25
        rows.append(
            CandleRow(
                symbol=symbol,
                interval="1h",
                open_time_ms=o_ms,
                close_time_ms=c_ms,
                open=px - 0.2,
                high=px + 0.5,
                low=px - 0.3,
                close=px,
                volume=100.0,
                quote_volume=100.0 * px,
                number_of_trades=50,
                taker_buy_base_volume=60.0,
                taker_buy_quote_volume=60.0 * px,
                ingested_at_ms=c_ms,
            )
        )
    db.upsert_candles(rows)


def test_aud01_run_once_classifies_bull_with_350_1h_and_250_4h_candles(tmp_path: Path) -> None:
    """
    AUD-01 regression: with 350 1h candles and 250 4h candles (previously stuck forever in
    TRANSITION/WARMUP_LT_120), QuantOrchestrator.run_once() MUST classify BULL causally,
    while a symbol with only 80 4h candles STILL falls back to TRANSITION/WARMUP_LT_120.
    """
    st_cfg = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))

    db_path = tmp_path / "aud01.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    end_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    _seed_uptrend(db, "BTCUSDT", end_ms, n_1h=350, n_4h=250, include_future_4h_crash=True)
    _seed_uptrend(db, "SOLUSDT", end_ms, n_1h=350, n_4h=250)
    _seed_uptrend(db, "ADAUSDT", end_ms, n_1h=350, n_4h=80)  # genuinely < 120 4h bars

    pub = _StubPublic(end_ms + 3000)
    clock = MonotonicClock()
    clock.sync(pub.get_server_time_ms, samples=1)
    all_syms = list(
        dict.fromkeys(
            list(st_cfg.universe.symbols) + list(an_cfg.scanner_universe.symbols) + ["BNBUSDT"]
        )
    )
    ex_cache = ExchangeInfoCache(pub, db, all_syms)  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(end_ms + 3000)
    outbox = TelegramOutbox(db, "chat1", transport=None)
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
    )
    rep = orch.run_once(now_ms=end_ms + 3000, skip_collect=True)
    assert rep.unhandled_errors == 0

    with db.read() as conn:
        rows = {
            r["symbol"]: (r["regime"], r["fallback_reason"], int(r["close_time_4h_ms"]))
            for r in conn.execute(
                "SELECT symbol, regime, fallback_reason, close_time_4h_ms FROM regime_history"
            ).fetchall()
        }

    # BTCUSDT & SOLUSDT MUST classify BULL (future crash candle causally ignored!)
    assert rows["BTCUSDT"] == (MarketRegime.BULL.value, None, end_ms)
    assert rows["SOLUSDT"] == (MarketRegime.BULL.value, None, end_ms)
    # ADAUSDT has only 80 4h candles -> MUST preserve WARMUP_LT_120 guard
    assert rows["ADAUSDT"][0] == MarketRegime.TRANSITION.value
    assert rows["ADAUSDT"][1] == "WARMUP_LT_120"
    db.close()
