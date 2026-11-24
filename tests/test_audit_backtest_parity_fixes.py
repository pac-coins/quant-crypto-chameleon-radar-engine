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


def test_aud05_train_ml_judge_uses_4h_aligned_regime_not_1h_close(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """
    AUD-05 regression: tools/train_ml_judge.py MUST evaluate regime via 4h aligned columns
    (detect_series_from_aligned_frame), NOT 1h close (detect_series).
    We create a deliberate divergence: 1h candles rise linearly (would be BULL on 1h),
    while 4h candles fall steadily (true 4h BEAR). Captured regimes after 4h warmup MUST be BEAR!
    """
    import tools.train_ml_judge as trainer

    db_path = tmp_path / "aud05_ml.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    base_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    rows: list[CandleRow] = []
    # 720 hourly candles rising smoothly (1h close > 1h ema_100)
    for i in range(720):
        o_ms = base_ms + i * ONE_HOUR_MS
        c_ms = o_ms + ONE_HOUR_MS
        px = 100.0 + i * 0.2
        rows.append(
            CandleRow(
                symbol="BTCUSDT",
                interval="1h",
                open_time_ms=o_ms,
                close_time_ms=c_ms,
                open=px - 0.1,
                high=px + 0.5,
                low=px - 0.5,
                close=px,
                volume=100.0,
                quote_volume=100.0 * px,
                number_of_trades=50,
                taker_buy_base_volume=60.0,
                taker_buy_quote_volume=60.0 * px,
                ingested_at_ms=c_ms,
            )
        )
    # 180 4h candles falling steadily (4h close < 4h ema_100 and slope < 0 -> BEAR!)
    for i in range(180):
        o_ms = base_ms + i * 4 * ONE_HOUR_MS
        c_ms = o_ms + 4 * ONE_HOUR_MS
        px = 500.0 - i * 1.5
        rows.append(
            CandleRow(
                symbol="BTCUSDT",
                interval="4h",
                open_time_ms=o_ms,
                close_time_ms=c_ms,
                open=px + 0.5,
                high=px + 1.0,
                low=px - 1.0,
                close=px,
                volume=400.0,
                quote_volume=400.0 * px,
                number_of_trades=200,
                taker_buy_base_volume=150.0,
                taker_buy_quote_volume=150.0 * px,
                ingested_at_ms=c_ms,
            )
        )
    db.upsert_candles(rows)
    db.close()

    observed_regimes: list[tuple[int, MarketRegime, str]] = []

    def _spy_eval(self: Any, sym: str, row: pd.Series, reg: MarketRegime) -> None:
        observed_regimes.append((int(row["close_time_ms"]), reg, str(row.get("btc_regime_4h", ""))))
        return None

    monkeypatch.setattr(trainer.BullTrendPullbackStrategy, "evaluate_row", _spy_eval)
    try:
        trainer.train_ml_judge_from_sqlite(
            db_path=db_path,
            output_path=tmp_path / "unused.json",
        )
    except RuntimeError:
        pass  # expected since _spy_eval returns None (0 trades)

    # After 120 4h warmup bars (480 hours), 4h regime MUST be BEAR (never 1h BULL!)
    post_warmup = [r for ts, r, _ in observed_regimes if ts >= base_ms + 500 * ONE_HOUR_MS]
    post_btc_ctx = [b for ts, _, b in observed_regimes if ts >= base_ms + 500 * ONE_HOUR_MS]
    assert len(post_warmup) > 20
    assert all(r == MarketRegime.BEAR for r in post_warmup)
    assert all(b == MarketRegime.BEAR.value for b in post_btc_ctx)


def test_aud06_fail_closed_when_btc_context_missing_or_stale(tmp_path: Path) -> None:
    """
    AUD-06 regression:
    1. BullTrendPullbackStrategy MUST fail closed (return None) for altcoins when
       btc_rs_12h is missing/NaN or btc_regime_4h is missing/empty/NaN.
    2. QuantOrchestrator.run_once() MUST populate missing BTC context fail-closed
       when BTCUSDT candles are absent/stale so altcoins cannot bypass BTC filters.
    """
    from src.strategies.bull_trend_pullback import BullTrendPullbackStrategy

    strat = BullTrendPullbackStrategy(enabled=True)
    valid_row = pd.Series(
        {
            "close_time_ms": 10_000_000,
            "open": 100.0,
            "high": 104.0,
            "low": 99.5,
            "close": 103.0,
            "ema_9": 102.0,
            "ema_20": 101.0,
            "ema_50": 99.0,
            "ema_100": 96.0,
            "atr_14": 2.0,
            "adx_14_4h": 21.5,
            "rvol_20": 1.65,
            "tbr": 0.56,
            "of_delta_quote": 15_000.0,
            "rs_vol_adjusted_12h": 1.85,
            "btc_rs_12h": 1.10,
            "btc_regime_4h": "BULL",
            "prev_high_12h": 102.5,
        }
    )
    # Sanity: valid BTC context approves SOLUSDT
    assert strat.evaluate_row("SOLUSDT", valid_row, MarketRegime.BULL) is not None

    # Missing / NaN btc_rs_12h MUST reject SOLUSDT (no default=0.0 bypass!)
    row_nan_rs = valid_row.copy()
    row_nan_rs["btc_rs_12h"] = float("nan")
    assert strat.evaluate_row("SOLUSDT", row_nan_rs, MarketRegime.BULL) is None
    row_no_rs = valid_row.drop(labels=["btc_rs_12h"])
    assert strat.evaluate_row("SOLUSDT", row_no_rs, MarketRegime.BULL) is None

    # Missing / empty / NaN btc_regime_4h MUST reject SOLUSDT
    row_empty_reg = valid_row.copy()
    row_empty_reg["btc_regime_4h"] = ""
    assert strat.evaluate_row("SOLUSDT", row_empty_reg, MarketRegime.BULL) is None
    row_no_reg = valid_row.drop(labels=["btc_regime_4h"])
    assert strat.evaluate_row("SOLUSDT", row_no_reg, MarketRegime.BULL) is None

    # BTCUSDT itself in BULL regime STILL works without external BTC comparison
    assert strat.evaluate_row("BTCUSDT", row_no_rs, MarketRegime.BULL) is not None

    # End-to-end orchestrator check: BTCUSDT missing from DB -> SOLUSDT aligned frame gets empty regime & NaN RS
    st_cfg = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    db_path = tmp_path / "aud06.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    end_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    # Seed ONLY SOLUSDT (NO BTCUSDT -> simulates stale/missing BTCUSDT)
    _seed_uptrend(db, "SOLUSDT", end_ms, n_1h=350, n_4h=250)

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
        mode=RunMode.SHADOW_CHAMELEON,
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

    seen_btc_ctx: list[tuple[Any, Any]] = []
    orig_eval = orch.strategy.evaluate_row

    def _capture_eval(sym: str, row: pd.Series, reg: MarketRegime, **kw: Any) -> Any:
        if sym == "SOLUSDT":
            seen_btc_ctx.append((row.get("btc_regime_4h"), row.get("btc_rs_12h")))
        return orig_eval(sym, row, reg, **kw)

    orch.strategy.evaluate_row = _capture_eval  # type: ignore[method-assign]
    rep = orch.run_once(now_ms=end_ms + 3000, skip_collect=True)
    assert rep.unhandled_errors == 0
    assert rep.chameleon_executed is False
    assert len(seen_btc_ctx) == 1
    assert seen_btc_ctx[0][0] == ""
    assert pd.isna(seen_btc_ctx[0][1])
    db.close()


def test_radar_layer3_reads_feature_store_tbr_column() -> None:
    """Radar L3 MUST read FeatureStore's 'tbr' column (previously always None looking only for 'taker_buy_ratio')."""
    from src.radar.structure_ema import score_layers_1_to_3

    an_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    base_rows = [
        {
            "close_time_ms": 1_700_000_000_000 + i * ONE_HOUR_MS,
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.5,
            "atr_14": 1.5,
            "ema_9": 101.0,
            "ema_50": 98.0,
            "ema_200": 95.0,
            "chop_14": 40.0,
            "rvol_20": 2.2,
            "tbr": 0.62,  # FeatureStore canonical column name (no 'taker_buy_ratio' column!)
        }
        for i in range(4)
    ]
    df = pd.DataFrame(base_rows)
    hit = score_layers_1_to_3("SOLUSDT", df, MarketRegime.BULL, an_cfg)
    assert hit is not None
    assert hit.features_snapshot["tbr"] == 0.62
    # High RVOL (13) + strong TBR (12) = 25 (previously capped at 13 because tbr was None!)
    assert hit.score_l3 == 25


def test_gate_report_6b_slippage_uses_l2_depth_vwap_ask(tmp_path: Path) -> None:
    """Gate 6B slippage in tools/gate_report.py MUST incorporate shadow_trades.depth_vwap_ask when present."""
    from tools.gate_report import generate_gate_report

    db_path = tmp_path / "gate6b_vwap.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    t0 = 1_700_000_000_000
    # Candle close = 100.00; top-of-book ask = 100.03 (3 bps <= 8 bps PASS),
    # BUT L2 depth_vwap_ask = 100.15 (15 bps > 8 bps -> MUST FAIL Gate 6B!)
    db.upsert_candles(
        [
            CandleRow(
                symbol="SOLUSDT",
                interval="1h",
                open_time_ms=t0 - ONE_HOUR_MS,
                close_time_ms=t0,
                open=99.0,
                high=100.5,
                low=98.5,
                close=100.0,
                volume=100.0,
                quote_volume=10_000.0,
                number_of_trades=50,
                taker_buy_base_volume=60.0,
                taker_buy_quote_volume=6_000.0,
                ingested_at_ms=t0,
            )
        ]
    )
    with db.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO shadow_trades (
                symbol, strategy, regime, signal_close_time_ms, status,
                decision_ask_price, depth_vwap_ask, book_impact_bps,
                sim_entry_price, stop_price, target_price,
                qty, risk_pct, risk_usdt, virtual_equity_before,
                time_barrier_end_ms, exit_reason, exit_price, r_gross, r_net,
                created_at_ms, closed_at_ms
            ) VALUES (
                'SOLUSDT', 'bull_trend_pullback', 'BULL', ?, 'CLOSED',
                100.03, 100.15, 12.0,
                100.18, 97.0, 106.0,
                '1.5', 0.01, '3.00', '300.00',
                ?, 'TARGET', 106.0, 2.0, 1.9,
                ?, ?
            )
            """,
            (t0, t0 + 72 * ONE_HOUR_MS, t0, t0 + 4 * ONE_HOUR_MS),
        )
    db.close()

    report = generate_gate_report(db_path=db_path)
    c6b = {c["id"]: c for c in report["sections"]["gate_6b"]["criteria"]}
    slip_crit = c6b["gate_6b.mean_slippage_le_0_08pct"]
    assert slip_crit["status"] == "FAIL"
    assert "15.00 bps" in slip_crit["measured"]
    assert "vwap_l2=1/1" in slip_crit["details"]


def test_aud04_causal_simulator_respects_end_time_ms_boundary() -> None:
    """AUD-04 regression: trades opened before end_time_ms MUST NOT read future candles beyond end_time_ms."""
    from src.backtest.causal_simulator import CausalSingleSlotSimulator, CostScenario
    from src.regimes.regime_detector import RegimeDetector
    from src.strategies.base_strategy import BaseStrategy, SignalCandidate

    class _SingleShotStrategy(BaseStrategy):
        def __init__(self, target_ts: int) -> None:
            self._target_ts = target_ts

        @property
        def name(self) -> str:
            return "test_shot"

        @property
        def required_regime(self) -> MarketRegime:
            return MarketRegime.BULL

        def evaluate_row(
            self,
            symbol: str,
            row: pd.Series,
            regime: MarketRegime,
            require_order_flow_filter: bool = False,
        ) -> SignalCandidate | None:
            ts = int(row["close_time_ms"])
            if ts != self._target_ts:
                return None
            return SignalCandidate(
                symbol=symbol,
                close_time_ms=ts,
                regime=MarketRegime.BULL,
                strategy_name=self.name,
                entry_price=100.0,
                stop_price=95.0,
                target_price=110.0,
                atr_14=2.0,
                stop_distance_pct=0.05,
                gross_rr_ratio=2.0,
                time_barrier_hours=24,
                rs_vol_adjusted_12h=2.0,
                order_flow_delta=100.0,
                taker_buy_ratio=0.6,
                has_liquidity_sweep=False,
            )

    t0 = 1_700_000_000_000
    # Bars 0..3 inside end_time_ms (price flat at 101); bar 4 AFTER end_time_ms spikes to 120 (TARGET!)
    df = pd.DataFrame(
        [
            {"close_time_ms": t0 + i * ONE_HOUR_MS, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "atr_14": 2.0}
            for i in range(4)
        ]
        + [
            {"close_time_ms": t0 + 4 * ONE_HOUR_MS, "open": 100.5, "high": 125.0, "low": 100.0, "close": 120.0, "atr_14": 2.0}
        ]
    )
    end_ms = t0 + 3 * ONE_HOUR_MS
    sim = CausalSingleSlotSimulator(cost_scenario=CostScenario.BASE)
    trades = sim.run_multi_asset_simulation(
        aligned_frames={"SOLUSDT": df},
        strategies=[_SingleShotStrategy(target_ts=t0 + 1 * ONE_HOUR_MS)],
        regime_detector=RegimeDetector(),
        ignore_regime_filter=True,
        end_time_ms=end_ms,
    )
    assert len(trades) == 1
    # Previously leaked bar 4 -> exit_reason == 'TARGET' & exit_time_ms == t0 + 4h!
    assert trades[0].exit_time_ms <= end_ms
    assert trades[0].exit_reason == "TIME_BARRIER"



