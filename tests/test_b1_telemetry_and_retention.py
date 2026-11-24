"""Tests for B1: SQLite telemetry persistence, preventive BNB/commission alerts, and local backup rotation."""

from __future__ import annotations

import os
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
from src.core.database import CandleRow, Database, SymbolFilters
from src.execution.executor_protocol import ExecutionRequest
from src.execution.paper_executor import PaperExecutor
from src.main import ONE_DAY_MS, ONE_HOUR_MS, QuantOrchestrator
from src.notifications.telegram import TelegramOutbox
from src.risk.position_sizer import PositionSizer
from tools.gate_report import generate_gate_report


def _load_cfgs() -> tuple[StrategiesConfig, AnalystConfig]:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    return st, an


def _seed_candles(db: Database, symbols: list[str], end_ms: int) -> None:
    rows: list[CandleRow] = []
    for sym in symbols:
        px = 100.0 if sym != "BNBUSDT" else 500.0
        for i in range(150):
            o_ms = end_ms - (150 - i) * ONE_HOUR_MS
            c_ms = o_ms + ONE_HOUR_MS
            p = px + i * 0.1
            rows.append(
                CandleRow(
                    symbol=sym,
                    interval="1h",
                    open_time_ms=o_ms,
                    close_time_ms=c_ms,
                    open=p,
                    high=p + 0.5,
                    low=p - 0.5,
                    close=p + 0.2,
                    volume=100.0,
                    quote_volume=100.0 * p,
                    number_of_trades=50,
                    taker_buy_base_volume=55.0,
                    taker_buy_quote_volume=55.0 * p,
                    ingested_at_ms=c_ms,
                )
            )
        for i in range(130):
            o_ms = end_ms - (130 - i) * 4 * ONE_HOUR_MS
            c_ms = o_ms + 4 * ONE_HOUR_MS
            p = px + i * 0.4
            rows.append(
                CandleRow(
                    symbol=sym,
                    interval="4h",
                    open_time_ms=o_ms,
                    close_time_ms=c_ms,
                    open=p,
                    high=p + 1.0,
                    low=p - 1.0,
                    close=p + 0.5,
                    volume=400.0,
                    quote_volume=400.0 * p,
                    number_of_trades=200,
                    taker_buy_base_volume=220.0,
                    taker_buy_quote_volume=220.0 * p,
                    ingested_at_ms=c_ms,
                )
            )
    db.upsert_candles(rows)


class _DummyTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        self.sent.append(text)
        return len(self.sent)


class _FakePublicClient:
    def __init__(self, now_ms: int, bnb_price: Decimal = Decimal("500.0")) -> None:
        self.governor = WeightGovernor()
        self._now_ms = now_ms
        self._bnb_price = bnb_price

    def get_server_time_ms(self) -> int:
        return self._now_ms

    def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
        px = self._bnb_price if symbol == "BNBUSDT" else Decimal("100.0")
        return BookTicker(
            symbol=symbol,
            bid=px,
            bid_qty=Decimal("10"),
            ask=px + Decimal("0.01"),
            ask_qty=Decimal("10"),
        )

    def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
        out: dict[str, SymbolFilters] = {}
        for s in symbols:
            base = s.replace("USDT", "")
            out[s] = SymbolFilters(
                symbol=s,
                status="TRADING",
                base_asset=base,
                quote_asset="USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                min_notional=Decimal("10"),
                oco_allowed=True,
                fetched_at_ms=self._now_ms,
            )
        return out


class _FakeSignedClient(_FakePublicClient):
    def __init__(
        self,
        now_ms: int,
        *,
        free_usdt: Decimal = Decimal("300.00"),
        free_bnb: Decimal = Decimal("0.008"),  # 0.008 * 500 = $4.00 (< $5 warn, >= $2 halt)
        taker_commission: str = "0.0015",  # > 0.10% triggers commission warning
    ) -> None:
        super().__init__(now_ms)
        self.free_usdt = free_usdt
        self.free_bnb = free_bnb
        self.taker_commission = taker_commission

    def sapi_system_status(self) -> SystemStatus:
        return SystemStatus(normal=True, msg="normal", mocked=True)

    def get_account(self, omit_zero_balances: bool = True) -> dict[str, Any]:
        return {
            "balances": [
                {"asset": "USDT", "free": format(self.free_usdt, "f"), "locked": "0"},
                {"asset": "BNB", "free": format(self.free_bnb, "f"), "locked": "0"},
            ]
        }

    def get_account_commission(self, symbol: str) -> dict[str, Any]:
        return {"standardCommission": {"maker": "0.0010", "taker": self.taker_commission}}

    def get_open_order_lists(self) -> list[dict[str, Any]]:
        return []


def test_b1_live_telemetry_regime_history_and_rate_limited_warnings(tmp_path: Path) -> None:
    st_cfg, an_cfg = _load_cfgs()
    db_path = tmp_path / "quant.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    now_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    symbols = list(
        dict.fromkeys(list(st_cfg.universe.symbols) + list(an_cfg.scanner_universe.symbols) + ["BNBUSDT"])
    )
    _seed_candles(db, symbols, now_ms)

    pub = _FakePublicClient(now_ms)
    signed = _FakeSignedClient(now_ms, free_usdt=Decimal("320.50"), free_bnb=Decimal("0.008"), taker_commission="0.0015")
    clock = MonotonicClock()
    clock.sync(lambda: now_ms, samples=1)
    ex_cache = ExchangeInfoCache(pub, db, symbols)
    ex_cache.refresh_if_stale(now_ms)

    transport = _DummyTransport()
    outbox = TelegramOutbox(db, "chat1", transport=transport)
    key_file = tmp_path / "ed25519.pem"
    key_file.write_text("dummy-key", encoding="utf-8")

    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key=SecretStr("testkey"),
        binance_ed25519_private_key_path=key_file,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        backup_retention_days=3,
    )

    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st_cfg,
        analyst_cfg=an_cfg,
        db=db,
        public_client=pub,
        signed_client=signed,
        clock=clock,
        exchange_cache=ex_cache,
        outbox=outbox,
        sleep_fn=lambda _: None,
    )

    # First cycle: should persist telemetry, regime_history, and emit BNB + commission warnings once
    orch.run_once(now_ms=now_ms, skip_collect=True)

    assert db.get_state("current_equity_usdt") == "320.50"
    assert db.get_state("initial_tactical_equity_usdt") == "320.50"
    assert Decimal(db.get_state("bnb_free_usdt_equiv") or "0") == Decimal("4.000")

    with db.read() as conn:
        rh_count = conn.execute("SELECT COUNT(*) FROM regime_history").fetchone()[0]
        kinds = [
            r["kind"]
            for r in conn.execute("SELECT kind FROM telegram_outbox ORDER BY msg_id ASC").fetchall()
        ]
    assert rh_count == len(symbols)
    assert kinds.count("BNB_LOW_WARNING") == 1
    assert kinds.count("COMMISSION_WARNING") == 1

    # Second cycle 1 hour later with lower equity: initial_tactical_equity_usdt stays unchanged,
    # current_equity_usdt updates, and rate-limited warnings do NOT fire again within 24h.
    signed.free_usdt = Decimal("315.00")
    orch.run_once(now_ms=now_ms + ONE_HOUR_MS, skip_collect=True)
    assert db.get_state("initial_tactical_equity_usdt") == "320.50"
    assert db.get_state("current_equity_usdt") == "315.00"
    with db.read() as conn:
        kinds2 = [r["kind"] for r in conn.execute("SELECT kind FROM telegram_outbox").fetchall()]
    assert kinds2.count("BNB_LOW_WARNING") == 1
    assert kinds2.count("COMMISSION_WARNING") == 1

    # Third cycle > 24h later: warnings may fire once more
    orch.run_once(now_ms=now_ms + ONE_DAY_MS + ONE_HOUR_MS, skip_collect=True)
    with db.read() as conn:
        kinds3 = [r["kind"] for r in conn.execute("SELECT kind FROM telegram_outbox").fetchall()]
    assert kinds3.count("BNB_LOW_WARNING") == 2
    assert kinds3.count("COMMISSION_WARNING") == 2

    # Verify gate_report.py now resolves KS-1 and KS-2 as PASS instead of UNKNOWN!
    report = generate_gate_report(db_path=db_path)
    ks_map = {c["id"]: c["status"] for c in report["sections"]["kill_switches"]["criteria"]}
    assert ks_map["kill_switches.ks1_hard_stop_drawdown_12pct"] == "PASS"
    assert ks_map["kill_switches.ks2_bnb_fee_reserve_2usdt"] == "PASS"

    db.close()


def test_b1_paper_executor_updates_shadow_virtual_equity_on_close(tmp_path: Path) -> None:
    st_cfg, _ = _load_cfgs()
    db = Database(tmp_path / "shadow.db")
    db.connect()
    db.migrate()

    now_ms = 1_700_000_000_000
    pub = _FakePublicClient(now_ms)
    sizer = PositionSizer(st_cfg.position_sizing, st_cfg.portfolio)
    flt = pub.get_exchange_info(["BTCUSDT"])["BTCUSDT"]
    sizing = sizer.evaluate_and_size(
        symbol="BTCUSDT",
        entry_price=Decimal("100.01"),
        stop_price=Decimal("97.00"),
        target_price=Decimal("106.03"),
        free_usdt=Decimal("300.00"),
        tactical_equity_usdt=Decimal("300.00"),
        symbol_filters=flt,
    )
    assert sizing.approved

    paper = PaperExecutor(db=db, public_client=pub, sizing_cfg=st_cfg.position_sizing)
    req = ExecutionRequest(
        symbol="BTCUSDT",
        strategy="bull_trend_pullback",
        regime="BULL",
        signal_close_time_ms=now_ms,
        atr_at_signal=1.0,
        time_barrier_end_ms=now_ms + 72 * ONE_HOUR_MS,
        sizing=sizing,
        symbol_filters=flt,
    )
    rep = paper.execute_entry(req, virtual_equity_before=Decimal("300.00"), now_ms=now_ms)
    assert rep.executed

    # Candle hits target (+2R)
    bar = CandleRow(
        symbol="BTCUSDT",
        interval="1h",
        open_time_ms=now_ms,
        close_time_ms=now_ms + ONE_HOUR_MS,
        open=100.5,
        high=107.0,
        low=99.5,
        close=106.5,
        volume=100.0,
        quote_volume=10000.0,
        number_of_trades=100,
        taker_buy_base_volume=60.0,
        taker_buy_quote_volume=6000.0,
        ingested_at_ms=now_ms + ONE_HOUR_MS,
    )
    res = paper.evaluate_open_shadow_trade({"BTCUSDT": [bar]})
    assert res is not None and res.status == "TARGET"
    assert db.get_state("initial_shadow_equity_usdt") == "300.00"
    updated_eq = Decimal(db.get_state("shadow_virtual_equity_usdt") or "0")
    assert updated_eq > Decimal("300.00")
    db.close()


def test_b1_prune_old_backups_keeps_latest_n_and_preserves_other_files(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    keep_unrelated = bdir / "notes.txt"
    keep_unrelated.write_text("do not delete", encoding="utf-8")

    created: list[Path] = []
    for i in range(6):
        p = bdir / f"quant_backup_2026110{i}_000000_00000{i}.db"
        p.write_bytes(b"sqlite")
        # Set deterministic increasing mtime
        os.utime(p, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
        created.append(p)

    removed = Database.prune_old_backups(bdir, keep_latest=3, prefix="quant")
    assert len(removed) == 3
    remaining = sorted(p.name for p in bdir.glob("quant_backup_*.db"))
    assert remaining == [p.name for p in created[-3:]]
    assert keep_unrelated.exists()
