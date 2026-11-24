"""Tests for B5: read-only Radar L4 ablation, Chameleon x Radar convergence, and offline MLJudge ablation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from src.core.database import Database
from tools.radar_ai_audit import format_audit_table, generate_radar_ai_audit


def _populate_radar_audit_db(db_path: Path, n_signals: int = 60) -> None:
    db = Database(db_path)
    db.connect()
    db.migrate()
    base_ms = 1_700_000_000_000

    with db.write_tx() as tx:
        for i in range(n_signals):
            sig_id = f"SIG_{i:04d}"
            ts = base_ms + i * 3_600_000
            l4_good = (i % 2 == 0)
            ob_valid = 1 if i < int(n_signals * 0.85) else 0
            score_l4 = 20 if (ob_valid and l4_good) else 0
            obi = 0.35 if (ob_valid and l4_good) else -0.10
            ask_wall = 0 if l4_good else 1
            grade = "A+" if score_l4 > 0 else "B"
            outcome_r = 1.4 if (ob_valid and l4_good) else -0.8
            feats = json.dumps({"rvol_20": 1.8 if l4_good else 1.2, "tbr": 0.60 if l4_good else 0.51})
            tx.execute(
                """
                INSERT INTO virtual_signals (
                    signal_id, symbol, signal_close_time_ms, emitted_at_ms, setup, regime_4h,
                    score_total, score_l1, score_l2, score_l3, score_l4, grade,
                    entry_ref_price, candle_close_price, stop_price, target1_price, target2_price,
                    atr14, horizon_end_ms, cost_roundtrip_pct, features_json,
                    ob_valid, ob_band_eff_pct, ob_obi_mean, ob_ask_wall_before_t2,
                    status, same_candle_ambiguity, resolved_at_ms,
                    outcome_r_gross, outcome_r_net, mfe_r, mae_r
                ) VALUES (
                    ?, 'SOLUSDT', ?, ?, 'PULLBACK', 'BULL',
                    ?, 20, 20, 20, ?, ?,
                    100.0, 99.9, 97.0, 104.5, 107.5,
                    1.0, ?, 0.0021, ?,
                    ?, 0.012, ?, ?,
                    'T2', 0, ?,
                    ?, ?, 1.8, -0.3
                )
                """,
                (
                    sig_id,
                    ts,
                    ts + 5000,
                    60 + score_l4,
                    score_l4,
                    grade,
                    ts + 24 * 3_600_000,
                    feats,
                    ob_valid,
                    obi if ob_valid else None,
                    ask_wall if ob_valid else None,
                    ts + 10 * 3_600_000,
                    outcome_r + 0.05,
                    outcome_r,
                ),
            )
            if ob_valid:
                tx.execute(
                    """
                    INSERT INTO orderbook_snapshots (
                        signal_id, seq, symbol, requested_limit, taken_at_ms, last_update_id,
                        best_bid, best_ask, mid, spread_bps, bid_coverage_pct, ask_coverage_pct,
                        band_eff_pct, bid_notional_band, ask_notional_band, obi, impact_bps_ref, buckets_json
                    ) VALUES (?, 0, 'SOLUSDT', 500, ?, 1, 99.98, 100.00, 99.99, 2.0, 0.02, 0.02, 0.012, 100000, 70000, ?, 1.5, '{}')
                    """,
                    (sig_id, ts + 5000, obi),
                )
                tx.execute(
                    """
                    INSERT INTO orderbook_walls (
                        signal_id, side, bucket_low, bucket_high, notional_mean, median_multiple, persistence
                    ) VALUES (?, 'BID', 99.5, 99.6, 50000.0, 5.2, 3)
                    """,
                    (sig_id,),
                )

        # Also add 1 OPEN and 1 INVALID_GAP row (must be excluded from resolved stats)
        tx.execute(
            """
            INSERT INTO virtual_signals (
                signal_id, symbol, signal_close_time_ms, emitted_at_ms, setup, regime_4h,
                score_total, score_l1, score_l2, score_l3, score_l4, grade,
                entry_ref_price, candle_close_price, stop_price, target1_price, target2_price,
                atr14, horizon_end_ms, cost_roundtrip_pct, features_json, ob_valid, status
            ) VALUES (
                'SIG_OPEN', 'SOLUSDT', ?, ?, 'PULLBACK', 'BULL',
                70, 20, 20, 20, 10, 'A',
                100.0, 99.9, 97.0, 104.5, 107.5,
                1.0, ?, 0.0021, '{}', 1, 'OPEN'
            ), (
                'SIG_GAP', 'SOLUSDT', ?, ?, 'PULLBACK', 'BULL',
                70, 20, 20, 20, 10, 'A',
                100.0, 99.9, 97.0, 104.5, 107.5,
                1.0, ?, 0.0021, '{}', 1, 'INVALID_GAP'
            )
            """,
            (
                base_ms + 999 * 3_600_000,
                base_ms + 999 * 3_600_000,
                base_ms + 1023 * 3_600_000,
                base_ms + 998 * 3_600_000,
                base_ms + 998 * 3_600_000,
                base_ms + 1022 * 3_600_000,
            ),
        )

        # Add shadow_trades with and without matched radar_signal_id
        tx.execute(
            """
            INSERT INTO shadow_trades (
                symbol, strategy, regime, signal_close_time_ms, status,
                decision_ask_price, sim_entry_price, stop_price, target_price,
                qty, risk_pct, risk_usdt, virtual_equity_before, time_barrier_end_ms,
                exit_reason, exit_price, r_gross, r_net, radar_signal_id, created_at_ms, closed_at_ms
            ) VALUES (
                'SOLUSDT', 'bull_trend_pullback', 'BULL', ?, 'CLOSED',
                100.0, 100.03, 97.0, 106.0, '1.0', 0.01, '3.00', '300.00', ?,
                'TARGET', 106.0, 2.0, 1.9, 'SIG_0000', ?, ?
            ), (
                'BTCUSDT', 'bull_trend_pullback', 'BULL', ?, 'CLOSED',
                100.0, 100.03, 97.0, 106.0, '1.0', 0.01, '3.00', '300.00', ?,
                'STOP', 97.0, -1.0, -1.05, NULL, ?, ?
            )
            """,
            (
                base_ms,
                base_ms + 72 * 3_600_000,
                base_ms,
                base_ms + 5 * 3_600_000,
                base_ms + 10 * 3_600_000,
                base_ms + 82 * 3_600_000,
                base_ms + 10 * 3_600_000,
                base_ms + 15 * 3_600_000,
            ),
        )
    db.close()


def test_b5_radar_ai_audit_readonly_ablation_convergence_and_verdict(tmp_path: Path) -> None:
    ro_dir = tmp_path / "ro_snap"
    ro_dir.mkdir()
    db_path = ro_dir / "quant.db"
    _populate_radar_audit_db(db_path, n_signals=60)

    # Lock directory read-only to prove strict ADR-002 mode=ro compliance
    os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        # With default min_l4_sample=150 -> verdict must be INSUFFICIENT_SAMPLE
        rep_strict = generate_radar_ai_audit(db_path=db_path, min_l4_sample=150)
        assert rep_strict["read_only"] is True
        assert rep_strict["affects_execution"] is False
        assert rep_strict["summary"]["resolved_signals"] == 60
        assert rep_strict["summary"]["open_signals"] == 1
        assert rep_strict["summary"]["invalid_gap_signals"] == 1
        assert rep_strict["layer4_ablation"]["verdict"] == "INSUFFICIENT_SAMPLE"

        # With lowered threshold (n=40) -> L4 positive signals clearly beat zero -> PROMISING
        rep_eval = generate_radar_ai_audit(db_path=db_path, min_l4_sample=40, min_ablation_sample=30)
        assert rep_eval["layer4_ablation"]["verdict"] == "PROMISING"
        assert rep_eval["layer4_ablation"]["delta_l4_pos_vs_zero"]["ci95_lo"] > 0.0

        # Convergence check
        conv = rep_eval["chameleon_radar_convergence"]["shadow_trades"]
        assert conv["with_radar"]["n"] == 1
        assert conv["without_radar"]["n"] == 1
        assert conv["with_radar"]["mean_r_net"] > conv["without_radar"]["mean_r_net"]

        # Offline MLJudge OHLCV vs OHLCV+L4 ablation ran cleanly
        ml_ab = rep_eval["ml_judge_l4_ablation"]
        assert ml_ab["status"] == "EVALUATED"
        assert "ece_val" in ml_ab["ohlcv_only"]
        assert "ece_val" in ml_ab["ohlcv_plus_l4"]

        tbl = format_audit_table(rep_eval)
        assert "PROMISING" in tbl
    finally:
        os.chmod(ro_dir, stat.S_IRWXU)
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
