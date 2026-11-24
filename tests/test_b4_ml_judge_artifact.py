"""Tests for B4: calibrated MLJudge artifact training, SHA-256 verification, and silent observer wiring."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
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
from src.features.ml_judge import (
    MLJudge,
    MLJudgeArtifact,
    extract_stationary_features,
    features_to_vector,
    fit_l2_logistic_and_platt,
)
from src.main import ONE_HOUR_MS, QuantOrchestrator
from src.notifications.telegram import TelegramOutbox
from src.regimes.regime_detector import MarketRegime
from src.risk.position_sizer import SignalGrade
from src.strategies.base_strategy import SignalCandidate


def _load_cfgs() -> tuple[StrategiesConfig, AnalystConfig]:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    return st, an


def test_b4_fit_l2_logistic_platt_and_artifact_sha256_integrity(tmp_path: Path) -> None:
    st_cfg, _ = _load_cfgs()
    rng = np.random.default_rng(7)
    n_train, n_val = 400, 300

    # Synthetic separable 6-feature problem (last feature & rvol positively correlated with win)
    x_tr = rng.normal(0.0, 1.0, size=(n_train, 6))
    logits_tr = 1.4 * x_tr[:, 2] + 1.2 * x_tr[:, 5] - 0.8 * x_tr[:, 3]
    probs_tr = 1.0 / (1.0 + np.exp(-logits_tr))
    y_tr = (rng.uniform(0.0, 1.0, size=n_train) < probs_tr).astype(int)

    x_va = rng.normal(0.0, 1.0, size=(n_val, 6))
    logits_va = 1.4 * x_va[:, 2] + 1.2 * x_va[:, 5] - 0.8 * x_va[:, 3]
    probs_va = 1.0 / (1.0 + np.exp(-logits_va))
    y_va = (rng.uniform(0.0, 1.0, size=n_val) < probs_va).astype(int)

    art = fit_l2_logistic_and_platt(
        train_x=x_tr,
        train_y=y_tr.tolist(),
        val_x=x_va,
        val_y=y_va.tolist(),
        val_stop_distances_pct=[0.03] * n_val,
        val_gross_rr_ratios=[2.0] * n_val,
        sizing_cfg=st_cfg.position_sizing,
        ml_cfg=st_cfg.ml_judge_and_validation,
    )
    assert art.ece_out_of_sample <= 0.08
    assert art.net_r_q33_threshold < art.net_r_q70_threshold
    assert len(art.sha256) == 64

    out_path = tmp_path / "models" / "ml_judge_bull_v1.json"
    art.save(out_path)
    loaded = MLJudgeArtifact.load(out_path, verify_sha256=True)
    assert loaded.sha256 == art.sha256

    # Loaded MLJudge must produce non-degenerate C, B, A+ distribution on validation signals
    judge = MLJudge.from_artifact(out_path, st_cfg.ml_judge_and_validation, st_cfg.position_sizing)
    counts = {"C": 0, "B": 0, "A+": 0}
    for row_x in x_va:
        raw = loaded.score_vector(row_x.tolist())
        v = judge.evaluate_signal(raw_score=raw, stop_distance_pct=0.03, gross_rr_ratio=2.0)
        counts[v.grade.value] += 1
        assert v.use_ml_filter_active is False  # Phase 1 frozen config keeps use_ml_filter=False
    assert counts["C"] > 30
    assert counts["B"] > 20
    assert counts["A+"] > 30

    # Tampering with artifact fails SHA-256 check
    raw_json = json.loads(out_path.read_text(encoding="utf-8"))
    raw_json["intercept"] = raw_json["intercept"] + 9.9
    out_path.write_text(json.dumps(raw_json), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        MLJudgeArtifact.load(out_path, verify_sha256=True)


def test_b4_orchestrator_loads_artifact_scores_stationary_features_and_stays_silent_in_phase1(
    tmp_path: Path,
) -> None:
    st_cfg, an_cfg = _load_cfgs()

    # Create a calibrated artifact that strongly assigns Grade C when chop_14 is high
    art = MLJudgeArtifact(
        schema_version=1,
        regime="BULL",
        feature_names=(
            "atr_pct",
            "dist_ema100_atr",
            "rvol_20",
            "chop_14",
            "btc_regime_ret",
            "rs_vol_adjusted_12h",
        ),
        feature_means=(0.01, 1.0, 1.5, 40.0, 0.5, 1.8),
        feature_stds=(0.005, 0.5, 0.5, 5.0, 0.5, 0.5),
        weights=(0.0, 0.0, 0.0, -3.0, 0.0, 0.0),
        intercept=-1.5,
        platt_a=-1.0,
        platt_b=0.0,
        ece_out_of_sample=0.02,
        net_r_q33_threshold=0.05,
        net_r_q70_threshold=0.30,
        trained_samples=100,
        validation_samples=60,
        created_at_utc="2026-11-24T00:00:00+00:00",
    ).with_computed_sha256()
    art_path = tmp_path / "ml_judge_bull_v1.json"
    art.save(art_path)

    db_path = tmp_path / "b4_shadow.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    now_ms = 1_700_000_000_000
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
        fetched_at_ms=now_ms,
    )

    class _Pub:
        governor = WeightGovernor()

        def get_server_time_ms(self) -> int:
            return now_ms

        def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
            return BookTicker(
                symbol=symbol,
                bid=Decimal("99.95"),
                bid_qty=Decimal("10"),
                ask=Decimal("100.00"),
                ask_qty=Decimal("10"),
            )

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: flt for s in symbols}

    pub = _Pub()
    clock = MonotonicClock()
    clock.sync(lambda: now_ms, samples=1)
    ex_cache = ExchangeInfoCache(pub, db, ["SOLUSDT"])  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(now_ms)
    outbox = TelegramOutbox(db, "chat1", transport=None)

    settings = RuntimeSettings(
        mode=RunMode.SHADOW_CHAMELEON,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        ml_judge_artifact_path=art_path,
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
    assert orch.ml_judge.artifact is not None

    # Stub strategy to emit a valid K9.3-like signal on a high-chop row (which artifact grades as C!)
    def _stub_eval(sym: str, row: pd.Series, reg: MarketRegime) -> SignalCandidate | None:
        if sym != "SOLUSDT":
            return None
        return SignalCandidate(
            symbol="SOLUSDT",
            close_time_ms=now_ms,
            regime=MarketRegime.BULL,
            strategy_name="bull_trend_pullback",
            entry_price=100.0,
            stop_price=97.0,
            target_price=106.0,
            atr_14=1.0,
            stop_distance_pct=0.03,
            gross_rr_ratio=2.0,
            time_barrier_hours=72,
            rs_vol_adjusted_12h=2.5,  # High RS -> old untrained calibrator would wrongly say A+ (0.924)!
            order_flow_delta=1000.0,
            taker_buy_ratio=0.58,
            has_liquidity_sweep=False,
        )

    orch.strategy.evaluate_row = _stub_eval  # type: ignore[method-assign]
    aligned = {
        "SOLUSDT": pd.DataFrame(
            [
                {
                    "close_time_ms": now_ms,
                    "close": 100.0,
                    "atr_14": 1.0,
                    "ema_100": 99.0,
                    "rvol_20": 1.5,
                    "chop_14_4h": 48.0,  # high chop -> artifact scores negative -> Grade C!
                    "btc_rs_12h": 0.5,
                }
            ]
        )
    }

    executed, sym = orch._execute_chameleon_step(
        now_ms=now_ms,
        cycle_close_ms=now_ms,
        aligned_frames=aligned,
        regimes={"SOLUSDT": MarketRegime.BULL},
        entries_blocked=False,
    )
    # Phase 1 silent observer: MUST STILL EXECUTE even though MLJudge graded C!
    assert executed is True
    assert sym == "SOLUSDT"

    with db.read() as conn:
        row = conn.execute(
            "SELECT ml_grade_silent, ml_p_cal_silent FROM shadow_trades LIMIT 1"
        ).fetchone()
    assert row["ml_grade_silent"] == SignalGrade.C.value
    assert row["ml_p_cal_silent"] is not None and float(row["ml_p_cal_silent"]) < 0.20

    # Also verify extract_stationary_features picks up btc_rs_12h fallback
    f = extract_stationary_features(aligned["SOLUSDT"].iloc[-1])
    assert abs(f.btc_regime_ret - 0.5) < 1e-9
    assert len(features_to_vector(f, 2.5)) == 6
    db.close()


def test_b4_train_ml_judge_cli_excludes_holdout_and_opens_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.train_ml_judge as trainer

    db_path = tmp_path / "hist.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    # Seed 600 hourly candles + 160 4h candles for BTCUSDT (enough for 120 4h warmup + train/val/holdout)
    base_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    rows: list[CandleRow] = []
    for i in range(600):
        o = base_ms + i * ONE_HOUR_MS
        c = o + ONE_HOUR_MS
        px = 100.0 + i * 0.25 + (1.5 if i % 7 == 0 else -0.2)
        rows.append(
            CandleRow(
                symbol="BTCUSDT",
                interval="1h",
                open_time_ms=o,
                close_time_ms=c,
                open=px - 0.5,
                high=px + 2.0,
                low=px - 1.5,
                close=px,
                volume=200.0 if i % 7 == 0 else 100.0,
                quote_volume=(200.0 if i % 7 == 0 else 100.0) * px,
                number_of_trades=100,
                taker_buy_base_volume=130.0 if i % 7 == 0 else 50.0,
                taker_buy_quote_volume=(130.0 if i % 7 == 0 else 50.0) * px,
                ingested_at_ms=c,
            )
        )
    for i in range(160):
        o = base_ms + i * 4 * ONE_HOUR_MS
        c = o + 4 * ONE_HOUR_MS
        px = 100.0 + i * 1.0
        rows.append(
            CandleRow(
                symbol="BTCUSDT",
                interval="4h",
                open_time_ms=o,
                close_time_ms=c,
                open=px - 0.5,
                high=px + 1.0,
                low=px - 1.0,
                close=px,
                volume=400.0,
                quote_volume=400.0 * px,
                number_of_trades=200,
                taker_buy_base_volume=240.0,
                taker_buy_quote_volume=240.0 * px,
                ingested_at_ms=c,
            )
        )
    db.upsert_candles(rows)
    db.close()

    # Stub evaluate_row so we generate deterministic train+val signals AND verify holdout is never passed
    seen_timestamps: list[int] = []

    def _mock_eval(self: Any, sym: str, row: pd.Series, reg: MarketRegime) -> SignalCandidate | None:
        ts = int(row["close_time_ms"])
        seen_timestamps.append(ts)
        if (ts // ONE_HOUR_MS) % 5 != 0:
            return None
        c_p = float(row["close"])
        return SignalCandidate(
            symbol=sym,
            close_time_ms=ts,
            regime=MarketRegime.BULL,
            strategy_name="bull_trend_pullback",
            entry_price=c_p,
            stop_price=c_p - 3.0,
            target_price=c_p + 6.0,
            atr_14=1.0,
            stop_distance_pct=3.0 / c_p,
            gross_rr_ratio=2.0,
            time_barrier_hours=24,
            rs_vol_adjusted_12h=2.0,
            order_flow_delta=100.0,
            taker_buy_ratio=0.58,
            has_liquidity_sweep=False,
        )

    monkeypatch.setattr(trainer.BullTrendPullbackStrategy, "evaluate_row", _mock_eval)

    out_json = tmp_path / "models" / "ml_judge_bull_v1.json"
    art, summary = trainer.train_ml_judge_from_sqlite(
        db_path=db_path,
        output_path=out_json,
    )
    assert out_json.is_file()
    assert summary["holdout_touched"] is False
    assert art.trained_samples >= 5
    assert art.validation_samples >= 5
    # Verify NO timestamp in the Holdout region was EVER passed to evaluate_row!
    assert seen_timestamps and max(seen_timestamps) <= summary["val_end_ms"]
    assert summary["val_end_ms"] < summary["holdout_start_ms_excluded"]
    loaded = MLJudgeArtifact.load(out_json, verify_sha256=True)
    assert loaded.sha256 == art.sha256

