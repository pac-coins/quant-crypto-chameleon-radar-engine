#!/usr/bin/env python3
"""Read-only audit tool for Radar virtual_signals, L4 orderbook snapshots, and MLJudge ablation (B5).

Strict invariants:
- Opens SQLite ONLY in read-only mode (`URI mode=ro` + `PRAGMA query_only=ON`, ADR-002).
- Strictly analytical: NEVER alters execution or `config/strategies.yaml` (ADR-004).
- Reports `INSUFFICIENT_SAMPLE` / `UNKNOWN` whenever data is below minimum thresholds.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backtest.ablation_and_dsr import compute_block_bootstrap_ci95
from src.core.config import StrategiesConfig, load_yaml_strict
from src.features.ml_judge import MLJudge, fit_l2_logistic_and_platt


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database not found: {resolved}")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.OperationalError as exc:
        conn.close()
        if "readonly" in str(exc).lower() and not Path(f"{resolved}-wal").exists():
            imm = sqlite3.connect(
                f"file:{resolved.as_posix()}?mode=ro&immutable=1", uri=True, timeout=2.0
            )
            imm.execute("PRAGMA query_only=ON")
            imm.row_factory = sqlite3.Row
            return imm
        raise


def _group_stats(
    vals: Sequence[float],
    mfe: Sequence[float] = (),
    mae: Sequence[float] = (),
    *,
    seed: int = 42,
) -> dict[str, Any]:
    clean = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    n = len(clean)
    if n == 0:
        return {
            "n": 0,
            "win_rate_pct": None,
            "mean_r_net": None,
            "ci95_lo": None,
            "ci95_hi": None,
            "mean_mfe_r": None,
            "mean_mae_r": None,
        }
    wins = sum(1 for v in clean if v > 0.0)
    mean_r = sum(clean) / n
    ci_lo, ci_hi = compute_block_bootstrap_ci95(clean, block_size=min(5, n), n_bootstraps=500, seed=seed)
    clean_mfe = [float(x) for x in mfe if x is not None and math.isfinite(float(x))]
    clean_mae = [float(x) for x in mae if x is not None and math.isfinite(float(x))]
    return {
        "n": n,
        "win_rate_pct": round((wins / n) * 100.0, 2),
        "mean_r_net": round(mean_r, 4),
        "ci95_lo": round(ci_lo, 4),
        "ci95_hi": round(ci_hi, 4),
        "mean_mfe_r": round(sum(clean_mfe) / len(clean_mfe), 4) if clean_mfe else None,
        "mean_mae_r": round(sum(clean_mae) / len(clean_mae), 4) if clean_mae else None,
    }


def _bootstrap_diff_ci95(
    group_a: Sequence[float],
    group_b: Sequence[float],
    *,
    n_bootstraps: int = 500,
    seed: int = 42,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if not group_a or not group_b:
        return None, None, None
    a = np.asarray(group_a, dtype=np.float64)
    b = np.asarray(group_b, dtype=np.float64)
    point = float(np.mean(a) - np.mean(b))
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_bootstraps, dtype=np.float64)
    for i in range(n_bootstraps):
        sa = rng.choice(a, size=len(a), replace=True)
        sb = rng.choice(b, size=len(b), replace=True)
        diffs[i] = float(np.mean(sa) - np.mean(sb))
    return point, float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def _offline_ml_ablation(
    resolved_rows: Sequence[dict[str, Any]],
    *,
    strategies_path: Path,
    min_samples: int = 30,
) -> dict[str, Any]:
    """Compare offline MLJudge with OHLCV-only vs OHLCV + L4 features on resolved virtual_signals."""
    valid = [r for r in resolved_rows if r["ob_valid"] == 1 and r["outcome_r_net"] is not None]
    if len(valid) < min_samples:
        return {
            "status": "UNKNOWN",
            "reason": f"Need >= {min_samples} resolved signals with ob_valid=1 (have {len(valid)}).",
            "n_samples": len(valid),
        }

    st_cfg = StrategiesConfig.model_validate(load_yaml_strict(strategies_path))
    # Sort chronologically for strict causal split (60% train / 40% val)
    valid.sort(key=lambda r: int(r["signal_close_time_ms"]))
    split_idx = int(len(valid) * 0.60)
    if split_idx < 10 or (len(valid) - split_idx) < 10:
        return {
            "status": "UNKNOWN",
            "reason": f"Insufficient chronological train/val split (n={len(valid)}).",
            "n_samples": len(valid),
        }

    x_ohlcv: list[list[float]] = []
    x_plus_l4: list[list[float]] = []
    y_bin: list[int] = []
    net_rs: list[float] = []
    stop_dists: list[float] = []
    gross_rrs: list[float] = []

    for r in valid:
        try:
            feats = json.loads(str(r["features_json"] or "{}"))
        except json.JSONDecodeError:
            feats = {}
        entry = max(float(r["entry_ref_price"]), 1e-9)
        stop = float(r["stop_price"])
        t2 = float(r["target2_price"])
        atr = float(r["atr14"])
        d_stop = max((entry - stop) / entry, 1e-4)
        g_rr = max((t2 - entry) / max(entry - stop, 1e-9), 0.5)

        base_vec = [
            atr / entry,
            float(feats.get("rvol_20", 1.0)),
            float(feats.get("tbr", 0.5)),
            float(r["score_l1"]),
            float(r["score_l2"]),
            float(r["score_l3"]),
        ]
        l4_vec = [
            float(r["ob_obi_mean"] or 0.0),
            float(r["avg_spread_bps"] or 0.0),
            float(r["avg_impact_bps_ref"] or 0.0),
            float(r["ob_ask_wall_before_t2"] or 0),
        ]
        x_ohlcv.append(base_vec)
        x_plus_l4.append(base_vec + l4_vec)
        nr = float(r["outcome_r_net"])
        net_rs.append(nr)
        y_bin.append(1 if nr > 0.0 else 0)
        stop_dists.append(d_stop)
        gross_rrs.append(g_rr)

    def _eval_model(mat: list[list[float]]) -> dict[str, Any]:
        arr = np.asarray(mat, dtype=np.float64)
        art = fit_l2_logistic_and_platt(
            train_x=arr[:split_idx],
            train_y=y_bin[:split_idx],
            val_x=arr[split_idx:],
            val_y=y_bin[split_idx:],
            val_stop_distances_pct=stop_dists[split_idx:],
            val_gross_rr_ratios=gross_rrs[split_idx:],
            sizing_cfg=st_cfg.position_sizing,
            ml_cfg=st_cfg.ml_judge_and_validation,
        )
        judge = MLJudge(
            st_cfg.ml_judge_and_validation,
            st_cfg.position_sizing,
            artifact=art,
            ece_out_of_sample=art.ece_out_of_sample,
            net_r_q33_threshold=art.net_r_q33_threshold,
            net_r_q70_threshold=art.net_r_q70_threshold,
        )
        judge.calibrator.a = art.platt_a
        judge.calibrator.b = art.platt_b
        ba_vals: list[float] = []
        c_vals: list[float] = []
        for idx in range(split_idx, len(valid)):
            raw_s = art.score_vector(arr[idx].tolist())
            v = judge.evaluate_signal(
                raw_score=raw_s,
                stop_distance_pct=stop_dists[idx],
                gross_rr_ratio=gross_rrs[idx],
            )
            if v.grade.value in ("B", "A+"):
                ba_vals.append(net_rs[idx])
            else:
                c_vals.append(net_rs[idx])
        return {
            "ece_val": round(art.ece_out_of_sample, 4),
            "n_val_b_aplus": len(ba_vals),
            "n_val_c": len(c_vals),
            "mean_r_b_aplus": round(sum(ba_vals) / len(ba_vals), 4) if ba_vals else None,
            "mean_r_c": round(sum(c_vals) / len(c_vals), 4) if c_vals else None,
        }

    m_ohlcv = _eval_model(x_ohlcv)
    m_l4 = _eval_model(x_plus_l4)
    return {
        "status": "EVALUATED",
        "n_samples": len(valid),
        "train_n": split_idx,
        "val_n": len(valid) - split_idx,
        "ohlcv_only": m_ohlcv,
        "ohlcv_plus_l4": m_l4,
        "ece_delta_l4_minus_ohlcv": round(m_l4["ece_val"] - m_ohlcv["ece_val"], 4),
        "adr_004_note": "Strictly offline research; layer_4_orderbook_live.affects_execution remains False.",
    }


def generate_radar_ai_audit(
    *,
    db_path: Path,
    strategies_path: Path = Path("config/strategies.yaml"),
    min_l4_sample: int = 150,
    min_ablation_sample: int = 30,
) -> dict[str, Any]:
    conn = _open_readonly(db_path)
    try:
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "virtual_signals" not in tables:
            return {
                "read_only": True,
                "affects_execution": False,
                "status": "UNKNOWN",
                "reason": "virtual_signals table not found.",
            }

        snap_agg: dict[str, dict[str, float]] = {}
        if "orderbook_snapshots" in tables:
            for r in conn.execute(
                """
                SELECT signal_id,
                       AVG(spread_bps) AS avg_spread_bps,
                       AVG(impact_bps_ref) AS avg_impact_bps_ref,
                       AVG(band_eff_pct) AS avg_band_eff_pct
                FROM orderbook_snapshots
                GROUP BY signal_id
                """
            ).fetchall():
                snap_agg[str(r["signal_id"])] = {
                    "avg_spread_bps": float(r["avg_spread_bps"]) if r["avg_spread_bps"] is not None else 0.0,
                    "avg_impact_bps_ref": float(r["avg_impact_bps_ref"]) if r["avg_impact_bps_ref"] is not None else 0.0,
                    "avg_band_eff_pct": float(r["avg_band_eff_pct"]) if r["avg_band_eff_pct"] is not None else 0.0,
                }

        wall_counts: dict[str, int] = {}
        if "orderbook_walls" in tables:
            for r in conn.execute(
                "SELECT signal_id, COUNT(*) AS cnt FROM orderbook_walls WHERE persistence >= 2 GROUP BY signal_id"
            ).fetchall():
                wall_counts[str(r["signal_id"])] = int(r["cnt"])

        all_vs = [dict(r) for r in conn.execute("SELECT * FROM virtual_signals").fetchall()]
        for r in all_vs:
            agg = snap_agg.get(str(r["signal_id"]), {})
            r["avg_spread_bps"] = agg.get("avg_spread_bps")
            r["avg_impact_bps_ref"] = agg.get("avg_impact_bps_ref")
            r["avg_band_eff_pct"] = agg.get("avg_band_eff_pct")
            r["persistent_walls"] = wall_counts.get(str(r["signal_id"]), 0)

        open_cnt = sum(1 for r in all_vs if r["status"] == "OPEN")
        gap_cnt = sum(1 for r in all_vs if r["status"] == "INVALID_GAP")
        ambig_cnt = sum(1 for r in all_vs if int(r["same_candle_ambiguity"] or 0) == 1)
        resolved = [
            r
            for r in all_vs
            if r["status"] not in ("OPEN", "INVALID_GAP") and r["outcome_r_net"] is not None
        ]

        # 1. Stratification by grade
        by_grade = {}
        for g in ("A+", "A", "B"):
            sub = [r for r in resolved if r["grade"] == g]
            by_grade[g] = _group_stats(
                [r["outcome_r_net"] for r in sub],
                [r["mfe_r"] for r in sub],
                [r["mae_r"] for r in sub],
            )

        # 2. Layer 4 ablation
        l4_valid = [r for r in resolved if int(r["ob_valid"] or 0) == 1]
        l4_invalid = [r for r in resolved if int(r["ob_valid"] or 0) == 0]
        l4_pos = [r for r in l4_valid if int(r["score_l4"] or 0) > 0]
        l4_zero = [r for r in resolved if int(r["score_l4"] or 0) == 0]
        obi_bull = [r for r in l4_valid if r["ob_obi_mean"] is not None and float(r["ob_obi_mean"]) >= 0.18]
        obi_nonbull = [r for r in l4_valid if r["ob_obi_mean"] is not None and float(r["ob_obi_mean"]) < 0.18]
        ask_wall_yes = [r for r in l4_valid if int(r["ob_ask_wall_before_t2"] or 0) == 1]
        ask_wall_no = [r for r in l4_valid if int(r["ob_ask_wall_before_t2"] or 0) == 0]

        delta_pt, delta_lo, delta_hi = _bootstrap_diff_ci95(
            [float(r["outcome_r_net"]) for r in l4_pos],
            [float(r["outcome_r_net"]) for r in l4_zero],
        )

        if len(l4_valid) < int(min_l4_sample):
            l4_verdict = "INSUFFICIENT_SAMPLE"
        elif delta_lo is not None and delta_lo > 0.0:
            l4_verdict = "PROMISING"
        else:
            l4_verdict = "NO_EDGE"

        # 3. Chameleon x Radar convergence (live trades & shadow_trades)
        convergence: dict[str, Any] = {}
        for tbl, r_col in (("trades", "realized_r"), ("shadow_trades", "r_net")):
            if tbl not in tables:
                convergence[tbl] = {"status": "UNKNOWN", "with_radar": _group_stats([]), "without_radar": _group_stats([])}
                continue
            rows = conn.execute(
                f"""
                SELECT t.{r_col} AS r_val,
                       COALESCE(t.radar_signal_id, v.signal_id) AS matched_signal_id
                FROM {tbl} t
                LEFT JOIN virtual_signals v
                  ON v.symbol = t.symbol AND v.signal_close_time_ms = t.signal_close_time_ms
                WHERE t.status = 'CLOSED' AND t.{r_col} IS NOT NULL
                """
            ).fetchall()
            with_r = [float(r["r_val"]) for r in rows if r["matched_signal_id"] is not None]
            without_r = [float(r["r_val"]) for r in rows if r["matched_signal_id"] is None]
            c_pt, c_lo, c_hi = _bootstrap_diff_ci95(with_r, without_r)
            convergence[tbl] = {
                "with_radar": _group_stats(with_r),
                "without_radar": _group_stats(without_r),
                "delta_mean_r": round(c_pt, 4) if c_pt is not None else None,
                "delta_ci95_lo": round(c_lo, 4) if c_lo is not None else None,
                "delta_ci95_hi": round(c_hi, 4) if c_hi is not None else None,
            }

        # 4. Offline MLJudge OHLCV vs OHLCV+L4 ablation
        ml_ablation = _offline_ml_ablation(
            resolved,
            strategies_path=strategies_path,
            min_samples=min_ablation_sample,
        )

        return {
            "read_only": True,
            "affects_execution": False,
            "summary": {
                "total_signals": len(all_vs),
                "resolved_signals": len(resolved),
                "open_signals": open_cnt,
                "invalid_gap_signals": gap_cnt,
                "same_candle_ambiguity": ambig_cnt,
                "overall": _group_stats(
                    [r["outcome_r_net"] for r in resolved],
                    [r["mfe_r"] for r in resolved],
                    [r["mae_r"] for r in resolved],
                ),
            },
            "by_grade": by_grade,
            "layer4_ablation": {
                "verdict": l4_verdict,
                "min_l4_sample_threshold": int(min_l4_sample),
                "ob_valid_1": _group_stats([r["outcome_r_net"] for r in l4_valid]),
                "ob_valid_0": _group_stats([r["outcome_r_net"] for r in l4_invalid]),
                "score_l4_gt_0": _group_stats([r["outcome_r_net"] for r in l4_pos]),
                "score_l4_eq_0": _group_stats([r["outcome_r_net"] for r in l4_zero]),
                "delta_l4_pos_vs_zero": {
                    "mean_diff": round(delta_pt, 4) if delta_pt is not None else None,
                    "ci95_lo": round(delta_lo, 4) if delta_lo is not None else None,
                    "ci95_hi": round(delta_hi, 4) if delta_hi is not None else None,
                },
                "obi_bullish_ge_0_18": _group_stats([r["outcome_r_net"] for r in obi_bull]),
                "obi_nonbullish": _group_stats([r["outcome_r_net"] for r in obi_nonbull]),
                "ask_wall_before_t2": _group_stats([r["outcome_r_net"] for r in ask_wall_yes]),
                "no_ask_wall_before_t2": _group_stats([r["outcome_r_net"] for r in ask_wall_no]),
            },
            "chameleon_radar_convergence": convergence,
            "ml_judge_l4_ablation": ml_ablation,
        }
    finally:
        conn.close()


def format_audit_table(report: dict[str, Any]) -> str:
    if report.get("status") == "UNKNOWN":
        return f"RADAR AI AUDIT: UNKNOWN ({report.get('reason')})"
    s = report["summary"]
    l4 = report["layer4_ablation"]
    lines = [
        "=== Radar & MLJudge Read-Only Audit (ADR-002 / ADR-004) ===",
        f"Total={s['total_signals']} | Resolved={s['resolved_signals']} | Open={s['open_signals']} | InvalidGap={s['invalid_gap_signals']}",
        f"L4 Verdict: {l4['verdict']} (ob_valid n={l4['ob_valid_1']['n']} / min={l4['min_l4_sample_threshold']})",
        f"L4 Delta (score_l4>0 vs =0): {l4['delta_l4_pos_vs_zero']['mean_diff']} (CI95: [{l4['delta_l4_pos_vs_zero']['ci95_lo']}, {l4['delta_l4_pos_vs_zero']['ci95_hi']}])",
        f"MLJudge Offline Ablation: {report['ml_judge_l4_ablation'].get('status')}",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of Radar virtual_signals, L4 orderbook snapshots, and MLJudge ablation."
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to SQLite DB (read-only).")
    parser.add_argument(
        "--strategies",
        type=Path,
        default=Path("config/strategies.yaml"),
        help="Path to strategies.yaml.",
    )
    parser.add_argument("--min-l4-sample", type=int, default=150)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    rep = generate_radar_ai_audit(
        db_path=args.db,
        strategies_path=args.strategies,
        min_l4_sample=args.min_l4_sample,
    )
    print(format_audit_table(rep))
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
