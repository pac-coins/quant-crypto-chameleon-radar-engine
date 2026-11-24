#!/usr/bin/env python3
"""Offline trainer for MLJudge (B4): fits L2 logistic + Platt calibrator strictly on Train/Validation.

Governance guarantees:
- Opens SQLite strictly read-only (`URI mode=ro` + `PRAGMA query_only=ON`, ADR-002).
- Uses `split_temporal_vaults` (60% Train / 25% Validation, purge=24h, embargo=2%) and
  STRICTLY discards the final 15% Holdout (`close_time_ms > bounds.val_end_ms`) — NEVER touches Holdout.
- Does NOT modify `config/strategies.yaml` or strategy parameters.
- Outputs a SHA-256 signed JSON artifact for silent-observer scoring in Phase 1.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backtest.ablation_and_dsr import compute_afml_sample_uniqueness, split_temporal_vaults
from src.backtest.causal_simulator import CausalSingleSlotSimulator, CostScenario
from src.core.config import FORBIDDEN_EXECUTION_SYMBOLS, StrategiesConfig, load_yaml_strict
from src.data.candle_collector import align_4h_to_1h, align_btc_context
from src.features.feature_store import FeatureStore
from src.features.ml_judge import (
    MLJudge,
    MLJudgeArtifact,
    extract_stationary_features,
    features_to_vector,
    fit_l2_logistic_and_platt,
)
from src.regimes.regime_detector import MarketRegime, RegimeDetector
from src.strategies.bull_trend_pullback import BullTrendPullbackStrategy


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database not found: {resolved}")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=2.0)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _load_candles_df(conn: sqlite3.Connection, symbol: str, interval: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT open_time_ms, close_time_ms, open, high, low, close,
               volume, quote_volume, number_of_trades,
               taker_buy_base_volume, taker_buy_quote_volume
        FROM candles
        WHERE symbol = ? AND interval = ?
        ORDER BY open_time_ms ASC
        """,
        (symbol, interval),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def train_ml_judge_from_sqlite(
    *,
    db_path: Path,
    output_path: Path,
    strategies_path: Path = Path("config/strategies.yaml"),
    l2_reg: float = 1.0,
) -> tuple[MLJudgeArtifact, dict[str, Any]]:
    st_cfg = StrategiesConfig.model_validate(load_yaml_strict(strategies_path))
    regime_det = RegimeDetector(st_cfg.regimes_and_strategies.regime_thresholds_4h)
    strategy = BullTrendPullbackStrategy.from_config(
        st_cfg.regimes_and_strategies.bull_trend_pullback
    )
    fstore = FeatureStore([regime_det, strategy])
    donchian_lb = int(st_cfg.regimes_and_strategies.bull_trend_pullback.donchian_lookback_1h)
    exec_symbols = [
        s for s in st_cfg.universe.symbols if s not in FORBIDDEN_EXECUTION_SYMBOLS
    ]

    conn = _open_readonly(db_path)
    try:
        aligned_by_sym: dict[str, pd.DataFrame] = {}
        all_timestamps: list[int] = []
        for sym in exec_symbols:
            df_1h = _load_candles_df(conn, sym, "1h")
            df_4h = _load_candles_df(conn, sym, "4h")
            if df_1h.empty or df_4h.empty:
                continue
            f1 = fstore.compute_timeframe(df_1h, "1h")
            f4 = fstore.compute_timeframe(df_4h, "4h")
            df_al = align_4h_to_1h(f1, f4)
            if "atr_14" in df_al.columns:
                df_al["rs_vol_adjusted_12h"] = (df_al["close"] - df_al["close"].shift(12)) / df_al[
                    "atr_14"
                ].replace(0.0, float("nan"))
            if donchian_lb > 0 and "high" in df_al.columns:
                df_al[f"prev_high_{donchian_lb}h"] = (
                    df_al["high"].shift(1).rolling(donchian_lb, min_periods=donchian_lb).max()
                )
            aligned_by_sym[sym] = df_al
            all_timestamps.extend(int(t) for t in df_al["close_time_ms"].tolist())
    finally:
        conn.close()

    if not all_timestamps:
        raise RuntimeError("No historical 1h/4h candles found in SQLite database.")

    ml_cfg = st_cfg.ml_judge_and_validation
    bounds = split_temporal_vaults(
        all_timestamps,
        train_pct=0.60,
        val_pct=0.25,
        purge_hours=int(ml_cfg.purge_window_hours),
        embargo_pct=float(ml_cfg.embargo_pct),
    )

    # STRICT ANTI-LEAKAGE: Drop ALL rows beyond val_end_ms (Holdout is NEVER touched!)
    for sym in list(aligned_by_sym):
        df = aligned_by_sym[sym]
        aligned_by_sym[sym] = df[df["close_time_ms"] <= bounds.val_end_ms].reset_index(drop=True)

    btc_df = aligned_by_sym.get("BTCUSDT")
    if btc_df is not None and not btc_df.empty:
        btc_ctx = pd.DataFrame({"close_time_ms": btc_df["close_time_ms"]})
        if "rs_vol_adjusted_12h" in btc_df.columns:
            btc_ctx["rs_12h"] = btc_df["rs_vol_adjusted_12h"]
        btc_reg_map = regime_det.detect_series_from_aligned_frame(btc_df)
        btc_reg_vals = [
            btc_reg_map.get(int(t), MarketRegime.TRANSITION).value
            for t in btc_df["close_time_ms"].tolist()
        ]
        btc_ctx["btc_regime_4h"] = btc_reg_vals
        for sym, df_al in list(aligned_by_sym.items()):
            if sym == "BTCUSDT":
                df_al["btc_rs_12h"] = df_al.get("rs_vol_adjusted_12h", 0.0)
                df_al["btc_regime_4h"] = btc_reg_vals
                continue
            enriched = align_btc_context(df_al, btc_ctx[["close_time_ms", "rs_12h"]])
            if "rs_12h_btc" in enriched.columns:
                enriched["btc_rs_12h"] = enriched["rs_12h_btc"]
            merged_reg = pd.merge_asof(
                enriched.sort_values("close_time_ms"),
                btc_ctx[["close_time_ms", "btc_regime_4h"]].sort_values("close_time_ms"),
                on="close_time_ms",
                direction="backward",
            )
            aligned_by_sym[sym] = merged_reg

    sim = CausalSingleSlotSimulator(cost_scenario=CostScenario.BASE)

    train_records: list[tuple[tuple[float, ...], int, int, int, float, float, float]] = []
    val_records: list[tuple[tuple[float, ...], int, int, int, float, float, float]] = []

    for sym, df_al in aligned_by_sym.items():
        if df_al.empty:
            continue
        reg_map = regime_det.detect_series_from_aligned_frame(df_al)
        for idx in range(len(df_al)):
            row = df_al.iloc[idx]
            ts_ms = int(row["close_time_ms"])
            # Skip purge/embargo gap between Train and Validation
            if bounds.train_end_ms < ts_ms < bounds.val_start_ms:
                continue
            if ts_ms > bounds.val_end_ms:
                continue
            reg = reg_map.get(ts_ms, MarketRegime.TRANSITION)
            sig = strategy.evaluate_row(sym, row, reg)
            if sig is None:
                continue
            future_slice = df_al.iloc[idx + 1 :]
            # Enforce vault boundary so train labels never peek past train_end_ms
            if ts_ms <= bounds.train_end_ms:
                future_slice = future_slice[future_slice["close_time_ms"] <= bounds.train_end_ms]
            else:
                future_slice = future_slice[future_slice["close_time_ms"] <= bounds.val_end_ms]
            if future_slice.empty:
                continue
            trade = sim.resolve_trade_1h_or_1m(sig, future_slice)
            feats = extract_stationary_features(row)
            vec = features_to_vector(feats, rs_vol_adjusted_12h=float(sig.rs_vol_adjusted_12h))
            label = 1 if trade.net_r > 0.0 else 0
            rec = (
                vec,
                label,
                int(trade.entry_time_ms),
                int(trade.exit_time_ms),
                float(sig.stop_distance_pct),
                float(sig.gross_rr_ratio),
                float(trade.net_r),
            )
            if ts_ms <= bounds.train_end_ms:
                train_records.append(rec)
            else:
                val_records.append(rec)

    if len(train_records) < 5 or len(val_records) < 5:
        raise RuntimeError(
            f"Insufficient K9.3 signals in Train/Validation (train={len(train_records)}, val={len(val_records)})."
        )

    train_u = compute_afml_sample_uniqueness([(r[2], r[3]) for r in train_records])
    val_u = compute_afml_sample_uniqueness([(r[2], r[3]) for r in val_records])

    artifact = fit_l2_logistic_and_platt(
        train_x=np.asarray([r[0] for r in train_records], dtype=np.float64),
        train_y=[r[1] for r in train_records],
        val_x=np.asarray([r[0] for r in val_records], dtype=np.float64),
        val_y=[r[1] for r in val_records],
        val_stop_distances_pct=[r[4] for r in val_records],
        val_gross_rr_ratios=[r[5] for r in val_records],
        sizing_cfg=st_cfg.position_sizing,
        ml_cfg=ml_cfg,
        train_weights=train_u.tolist(),
        val_weights=val_u.tolist(),
        regime="BULL",
        l2_reg=float(l2_reg),
    )
    saved_path = artifact.save(output_path)

    # Validation audit summary
    judge = MLJudge.from_artifact(saved_path, ml_cfg, st_cfg.position_sizing)
    grade_r: dict[str, list[float]] = {"A+": [], "B": [], "C": []}
    for vec, _y, _t0, _t1, d_s, r_g, net_r in val_records:
        raw_s = artifact.score_vector(vec)
        verdict = judge.evaluate_signal(raw_score=raw_s, stop_distance_pct=d_s, gross_rr_ratio=r_g)
        grade_r[verdict.grade.value].append(net_r)

    ba_r = grade_r["A+"] + grade_r["B"]
    c_r = grade_r["C"]
    summary = {
        "artifact_path": str(saved_path),
        "sha256": artifact.sha256,
        "holdout_touched": False,
        "train_end_ms": bounds.train_end_ms,
        "val_start_ms": bounds.val_start_ms,
        "val_end_ms": bounds.val_end_ms,
        "holdout_start_ms_excluded": bounds.holdout_start_ms,
        "train_samples": artifact.trained_samples,
        "validation_samples": artifact.validation_samples,
        "ece_out_of_sample": artifact.ece_out_of_sample,
        "net_r_q33_threshold": artifact.net_r_q33_threshold,
        "net_r_q70_threshold": artifact.net_r_q70_threshold,
        "validation_grade_counts": {k: len(v) for k, v in grade_r.items()},
        "validation_mean_r_b_aplus": (sum(ba_r) / len(ba_r)) if ba_r else None,
        "validation_mean_r_c": (sum(c_r) / len(c_r)) if c_r else None,
    }
    return artifact, summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Train calibrated MLJudge artifact strictly on Train/Validation (never touches Holdout)."
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to SQLite DB (opened read-only).")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("models/ml_judge_bull_v1.json"),
        help="Output JSON path for signed MLJudge artifact.",
    )
    parser.add_argument(
        "--strategies",
        type=Path,
        default=Path("config/strategies.yaml"),
        help="Path to frozen strategies.yaml (read-only).",
    )
    parser.add_argument("--l2-reg", type=float, default=1.0, help="L2 regularization strength.")
    args = parser.parse_args(argv)

    _, summary = train_ml_judge_from_sqlite(
        db_path=args.db,
        output_path=args.output,
        strategies_path=args.strategies,
        l2_reg=args.l2_reg,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
