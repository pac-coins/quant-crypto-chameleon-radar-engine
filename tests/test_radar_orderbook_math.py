from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.auditing.virtual_auditor import VirtualAuditor
from src.core.binance_client import (
    BookTicker,
    DepthSnapshot,
    ExchangeInfoCache,
    MonotonicClock,
    Priority,
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
from src.main import QuantOrchestrator
from src.notifications.telegram import TelegramOutbox
from src.radar.confluence_scorer import score_and_build_signal
from src.radar.orderbook_inspector import OrderbookInspector, evaluate_snapshots_for_symbol
from src.radar.structure_ema import Layer123Score
from src.regimes.regime_detector import MarketRegime

ONE_HOUR_MS = 3_600_000
FOUR_HOURS_MS = 4 * ONE_HOUR_MS


def _make_settings(tmp_path: Path, mode: RunMode = RunMode.SHADOW_CHAMELEON) -> RuntimeSettings:
    return RuntimeSettings(
        mode=mode,
        binance_env=BinanceEnv.TESTNET,
        telegram_bot_token="123456:TEST_TOKEN",
        telegram_chat_id="999999",
        db_path=tmp_path / "quant.db",
        backup_dir=tmp_path / "backups",
    )


def _seed_bull_candles(db: Database, symbol: str, end_close_ms: int, n_1h: int = 240, n_4h: int = 160) -> None:
    rows: list[CandleRow] = []
    # 4h candles: steady uptrend -> BULL
    start_4h_open = end_close_ms - n_4h * FOUR_HOURS_MS
    for i in range(n_4h):
        op_ms = start_4h_open + i * FOUR_HOURS_MS
        cl_ms = op_ms + FOUR_HOURS_MS
        base = 100.0 + i * 0.6
        rows.append(
            CandleRow(
                symbol=symbol,
                interval="4h",
                open_time_ms=op_ms,
                close_time_ms=cl_ms,
                open=base,
                high=base + 1.2,
                low=base - 0.3,
                close=base + 0.9,
                volume=1000.0 + i * 5.0,
                quote_volume=(1000.0 + i * 5.0) * base,
                number_of_trades=500,
                taker_buy_base_volume=600.0 + i * 3.0,
                taker_buy_quote_volume=(600.0 + i * 3.0) * base,
                ingested_at_ms=cl_ms + 500,
            )
        )

    # 1h candles: steady uptrend with last candle strong bullish breakout
    start_1h_open = end_close_ms - n_1h * ONE_HOUR_MS
    for i in range(n_1h):
        op_ms = start_1h_open + i * ONE_HOUR_MS
        cl_ms = op_ms + ONE_HOUR_MS
        base = 150.0 + i * 0.25
        is_last = i == n_1h - 1
        vol = 5000.0 if is_last else 1000.0
        tb_vol = 3500.0 if is_last else 520.0
        rows.append(
            CandleRow(
                symbol=symbol,
                interval="1h",
                open_time_ms=op_ms,
                close_time_ms=cl_ms,
                open=base,
                high=base + (3.0 if is_last else 0.6),
                low=base - 0.2,
                close=base + (2.6 if is_last else 0.3),
                volume=vol,
                quote_volume=vol * base,
                number_of_trades=900 if is_last else 200,
                taker_buy_base_volume=tb_vol,
                taker_buy_quote_volume=tb_vol * base,
                ingested_at_ms=cl_ms + 500,
            )
        )
    db.upsert_candles(rows)


def test_gate6_c1a_symmetric_band_eff_and_c21_anchored_walls() -> None:
    analyst_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    l4_cfg = analyst_cfg.layer_4_orderbook_live

    # 1. C1-A: Truncated book (bid coverage only 0.12% < min_valid_band_pct 0.30%)
    truncated = DepthSnapshot(
        symbol="BTCUSDT",
        last_update_id=1,
        taken_at_ms=1_000_000,
        requested_limit=1000,
        bids=((99990.0, 5.0), (99900.0, 5.0), (99880.0, 50.0)),  # ~0.12% depth
        asks=((100010.0, 1.0), (100500.0, 2.0), (101500.0, 2.0)),  # ~1.50% depth
    )
    insp_invalid = evaluate_snapshots_for_symbol([truncated, truncated, truncated], l4_cfg)
    assert insp_invalid.ob_valid is False
    assert insp_invalid.obi_mean is None
    assert insp_invalid.score_l4 == 0
    assert insp_invalid.walls == ()

    # 2. C2-1: Anchored 0.1% buckets on mid_0 + persistent BID Whale Wall (and 1-snap spoof ignored)
    # mid_0 = 100.0 -> bucket_width = 0.10. Bucket -3 is [99.70, 99.80).
    def _make_snap(seq: int, mid_shift: float, include_spoof: bool) -> DepthSnapshot:
        bids = [
            (99.95 + mid_shift, 1.0),
            (99.85 + mid_shift, 1.0),
            (99.75, 200.0),  # Persistent BID whale wall anchored at 99.75 (~$19,950 vs ~$100 median)
            (99.65, 1.0),
            (99.45, 1.0),
            (98.40, 1.0),
        ]
        asks = [
            (100.05 + mid_shift, 1.0),
            (100.15 + mid_shift, 1.0),
            (100.25, 1.0),
            (100.45, 1.0),
            (101.60, 1.0),
        ]
        if include_spoof:
            asks.append((100.35, 300.0))  # Appears ONLY in seq=0 (spoof -> persistence=1)
        return DepthSnapshot(
            symbol="SOLUSDT",
            last_update_id=10 + seq,
            taken_at_ms=1_000_000 + seq * 5000,
            requested_limit=500,
            bids=tuple(bids),
            asks=tuple(asks),
        )

    snaps = [_make_snap(0, 0.0, True), _make_snap(1, 0.02, False), _make_snap(2, -0.02, False)]
    insp = evaluate_snapshots_for_symbol(snaps, l4_cfg)
    assert insp.ob_valid is True
    assert insp.band_eff_pct == pytest.approx(0.015, rel=1e-4)
    assert insp.obi_mean is not None and insp.obi_mean > 0.18
    assert insp.score_l4 >= 15
    assert any(w.side == "BID" and w.persistence == 3 for w in insp.walls)
    # Spoof ask wall (only 1 snapshot) must NOT be promoted
    assert not any(w.side == "ASK" for w in insp.walls)
    # C2-8: compact JSON < 1 KB
    assert all(len(s.buckets_json.encode("utf-8")) < 1024 for s in insp.snapshots)


def test_gate6_virtual_auditor_c22_c23_c27_and_telegram_outbox(tmp_path: Path) -> None:
    db = Database(tmp_path / "audit.db")
    db.connect()
    db.migrate()

    strategies_cfg = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    analyst_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))

    t0 = 1_700_000_000_000
    l123 = Layer123Score(
        symbol="SOLUSDT",
        signal_close_time_ms=t0,
        regime_4h=MarketRegime.BULL,
        score_l1=22,
        score_l2=20,
        score_l3=20,
        subtotal_l1_l3=62,
        close_price=100.0,
        low_price=99.0,
        atr14=2.0,
        setup="RADAR_BULL_ENGULFING",
        features_snapshot={"close": 100.0},
    )
    sig = score_and_build_signal(
        l123,
        None,
        entry_ref_price=100.5,  # C2-2: ask price
        emitted_at_ms=t0 + 3000,
        analyst_cfg=analyst_cfg,
        strategies_cfg=strategies_cfg,
    )
    assert sig is None  # score 62 < min_score_to_alert_telegram (65)

    # Build with score >= 65 so it passes threshold cleanly
    l123_a = Layer123Score(
        symbol="SOLUSDT",
        signal_close_time_ms=t0,
        regime_4h=MarketRegime.BULL,
        score_l1=24,
        score_l2=22,
        score_l3=22,
        subtotal_l1_l3=68,
        close_price=100.0,
        low_price=99.0,
        atr14=2.0,
        setup="RADAR_BULL_ENGULFING",
        features_snapshot={"close": 100.0},
    )
    sig_a = score_and_build_signal(
        l123_a,
        None,
        entry_ref_price=100.5,  # C2-2: ask price
        emitted_at_ms=t0 + 3000,
        analyst_cfg=analyst_cfg,
        strategies_cfg=strategies_cfg,
    )
    assert sig_a is not None and sig_a.grade == "B"
    assert sig_a.horizon_end_ms == t0 + 24 * ONE_HOUR_MS  # C2-7
    expected_cost = (
        strategies_cfg.position_sizing.roundtrip_fee_assumption_pct
        + strategies_cfg.position_sizing.stress_slippage_assumption_pct
    )
    assert sig_a.cost_roundtrip_pct == pytest.approx(expected_cost, rel=1e-9)  # C2-4

    auditor = VirtualAuditor(db)
    assert auditor.record_signal(sig_a) is True
    assert auditor.record_signal(sig_a) is False  # Idempotent

    # Insert t+1 candle hitting BOTH stop and target2 -> worst-case T1_THEN_STOP / STOP (C2-3)
    db.upsert_candles(
        [
            CandleRow(
                symbol="SOLUSDT",
                interval="1h",
                open_time_ms=t0,
                close_time_ms=t0 + ONE_HOUR_MS,
                open=100.5,
                high=sig_a.target2_price + 1.0,
                low=sig_a.stop_price - 1.0,
                close=101.0,
                volume=1000.0,
                quote_volume=100_000.0,
                number_of_trades=100,
                taker_buy_base_volume=600.0,
                taker_buy_quote_volume=60_000.0,
                ingested_at_ms=t0 + ONE_HOUR_MS + 100,
            )
        ]
    )
    resolved = auditor.resolve_open_signals(t0 + ONE_HOUR_MS + 500)
    assert len(resolved) == 1
    assert resolved[0].same_candle_ambiguity is True
    assert resolved[0].status == "STOP"

    # Second signal with a candle gap -> INVALID_GAP (C2-7)
    t1 = t0 + 10 * ONE_HOUR_MS
    l123_gap = Layer123Score(
        symbol="ETHUSDT",
        signal_close_time_ms=t1,
        regime_4h=MarketRegime.BULL,
        score_l1=25,
        score_l2=25,
        score_l3=25,
        subtotal_l1_l3=75,
        close_price=2000.0,
        low_price=1980.0,
        atr14=20.0,
        setup="RADAR_SWEEP_REVERSAL",
        features_snapshot={"close": 2000.0},
    )
    sig_gap = score_and_build_signal(
        l123_gap,
        None,
        entry_ref_price=2001.0,
        emitted_at_ms=t1 + 3000,
        analyst_cfg=analyst_cfg,
        strategies_cfg=strategies_cfg,
    )
    assert sig_gap is not None and sig_gap.grade == "A"
    assert auditor.record_signal(sig_gap) is True
    # Insert candle at t1 + 2h (missing t1 + 0h..1h -> gap!)
    db.upsert_candles(
        [
            CandleRow(
                symbol="ETHUSDT",
                interval="1h",
                open_time_ms=t1 + 2 * ONE_HOUR_MS,
                close_time_ms=t1 + 3 * ONE_HOUR_MS,
                open=2001.0,
                high=2010.0,
                low=1995.0,
                close=2005.0,
                volume=500.0,
                quote_volume=1_000_000.0,
                number_of_trades=50,
                taker_buy_base_volume=300.0,
                taker_buy_quote_volume=600_000.0,
                ingested_at_ms=t1 + 3 * ONE_HOUR_MS + 100,
            )
        ]
    )
    resolved_gap = auditor.resolve_open_signals(t1 + 3 * ONE_HOUR_MS + 500)
    assert len(resolved_gap) == 1
    assert resolved_gap[0].status == "INVALID_GAP"

    md = auditor.generate_weekly_scorecard_markdown(t0, t1 + 5 * ONE_HOUR_MS)
    assert "Invalid Gaps:** 1" in md

    # Telegram Outbox + reply_to_message_id threading
    sent_calls: list[dict[str, Any]] = []

    class _FakeTgTransport:
        def __init__(self) -> None:
            self._next_id = 700

        def send_message(self, *, chat_id: str, text: str, reply_to_message_id: int | None = None) -> int:
            self._next_id += 1
            sent_calls.append(
                {"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id, "id": self._next_id}
            )
            return self._next_id

    outbox = TelegramOutbox(db, "999999", transport=_FakeTgTransport(), max_attempts=2)
    outbox.enqueue("RADAR_SIGNAL", "Signal A", reply_to_signal_id=sig_a.signal_id, now_ms=t0 + 4000)
    stats1 = outbox.flush(now_ms=t0 + 5000)
    assert stats1.sent == 1
    outbox.enqueue("RADAR_OUTCOME", "Resolved", reply_to_signal_id=sig_a.signal_id, now_ms=t0 + 6000)
    stats2 = outbox.flush(now_ms=t0 + 7000)
    assert stats2.sent == 1
    assert sent_calls[1]["reply_to"] == sent_calls[0]["id"]
    db.close()


class _FakePublicClient:
    def __init__(self, call_log: list[str]) -> None:
        self.governor = WeightGovernor()
        self.call_log = call_log

    def get_server_time_ms(self) -> int:
        return 1_700_500_000_000

    def get_book_ticker(self, symbol: str, *, priority: Priority = Priority.HIGH) -> BookTicker:
        self.call_log.append(f"book_ticker:{symbol}")
        return BookTicker(
            symbol=symbol,
            bid=Decimal("209.90"),
            bid_qty=Decimal("10"),
            ask=Decimal("210.00"),
            ask_qty=Decimal("10"),
        )

    def get_depth(self, symbol: str, limit: int = 500) -> DepthSnapshot:
        self.call_log.append(f"depth:{symbol}")
        return DepthSnapshot(
            symbol=symbol,
            last_update_id=1,
            taken_at_ms=1_700_500_000_000,
            requested_limit=limit,
            bids=((209.8, 100.0), (208.0, 100.0), (205.0, 100.0)),
            asks=((210.2, 50.0), (212.0, 50.0), (215.0, 50.0)),
        )

    def get_exchange_info(self, symbols: list[str]) -> dict[str, SymbolFilters]:
        return {
            sym: SymbolFilters(
                symbol=sym,
                status="TRADING",
                base_asset=sym.replace("USDT", ""),
                quote_asset="USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.01"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("5.00"),
                oco_allowed=True,
                fetched_at_ms=1_700_500_000_000,
            )
            for sym in symbols
        }


class _FakeSignedClient:
    def __init__(self, maintenance_status: int = 0) -> None:
        self.governor = WeightGovernor()
        self.maintenance_status = maintenance_status

    def sapi_system_status(self) -> SystemStatus:
        return SystemStatus(
            normal=(self.maintenance_status == 0),
            msg="ok" if self.maintenance_status == 0 else "maint",
            mocked=False,
        )

    def get_account(self, *, omit_zero_balances: bool = True) -> dict[str, Any]:
        return {
            "balances": [
                {"asset": "USDT", "free": "300.00", "locked": "0"},
                {"asset": "BNB", "free": "0.05", "locked": "0"},
            ]
        }


def test_gate6_orchestrator_c29_order_maintenance_and_halt_blocks(tmp_path: Path) -> None:
    strategies_cfg = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    analyst_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))

    db = Database(tmp_path / "orch.db")
    db.connect()
    db.migrate()

    end_close_ms = 1_700_500_000_000
    for sym in ("BTCUSDT", "SOLUSDT"):
        _seed_bull_candles(db, sym, end_close_ms)

    call_log: list[str] = []
    pub = _FakePublicClient(call_log)
    signed = _FakeSignedClient(maintenance_status=1)  # Maintenance active!
    clock = MonotonicClock()
    clock.sync(pub.get_server_time_ms)
    all_syms = list(dict.fromkeys(list(strategies_cfg.universe.symbols) + list(analyst_cfg.scanner_universe.symbols) + ["BNBUSDT"]))
    ex_cache = ExchangeInfoCache(pub, db, all_syms)
    ex_cache.refresh_if_stale(end_close_ms)

    outbox = TelegramOutbox(db, "999999", transport=None)
    orch = QuantOrchestrator(
        settings=_make_settings(tmp_path, RunMode.SHADOW_CHAMELEON),
        strategies_cfg=strategies_cfg,
        analyst_cfg=analyst_cfg,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=signed,  # type: ignore[arg-type]
        clock=clock,
        exchange_cache=ex_cache,
        outbox=outbox,
        orderbook_inspector=OrderbookInspector(analyst_cfg.layer_4_orderbook_live, sleep_fn=lambda _: None),
        sleep_fn=lambda _: None,
    )

    # 1. Maintenance blocks Chameleon entries, Radar still runs, daily backup created
    rep1 = orch.run_once(now_ms=end_close_ms + 3000, skip_collect=True)
    assert rep1.maintenance_blocked is True
    assert rep1.chameleon_executed is False
    assert rep1.backup_created is not None and Database.verify_backup(rep1.backup_created)

    # 2. Emergency Halt blocks Chameleon entries even when maintenance is cleared
    signed.maintenance_status = 0
    db.set_state("emergency_halt", "1")
    rep2 = orch.run_once(now_ms=end_close_ms + 4000, skip_collect=True)
    assert rep2.emergency_halted is True
    assert rep2.chameleon_executed is False

    # 3. Verify cycle_health was logged
    with db.read() as conn:
        hc = conn.execute("SELECT COUNT(*) FROM cycle_health").fetchone()[0]
    assert hc >= 1
    db.close()
