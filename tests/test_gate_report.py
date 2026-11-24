"""Comprehensive unit tests for tools/gate_report.py (PASS, FAIL, UNKNOWN for every criterion + read-only ADR-002)."""

from __future__ import annotations

import json
import os
import stat
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.core.database import CycleHealth, Database
from tools.gate_report import (
    ONE_DAY_MS,
    ONE_HOUR_MS,
    format_report_table,
    generate_gate_report,
    main as gate_report_main,
)


def _fresh_db(path: Path) -> Database:
    db = Database(path)
    db.connect()
    db.migrate()
    return db


def _criteria_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for sec in report["sections"].values():
        for c in sec["criteria"]:
            out[c["id"]] = c
    return out


def _seed_cycles(
    db: Database,
    *,
    mode: str,
    count: int,
    start_ms: int = 1_700_000_000_000,
    step_ms: int = ONE_HOUR_MS,
    offset_ms: float = 25.0,
    unhandled_at: set[int] | None = None,
    offset_override: dict[int, float] | None = None,
    gap_after_idx: int | None = None,
) -> None:
    unhandled_at = unhandled_at or set()
    offset_override = offset_override or {}
    cur_ts = start_ms
    for i in range(count):
        if gap_after_idx is not None and i == gap_after_idx:
            cur_ts += step_ms  # injects an extra 1h gap
        db.log_cycle_health(
            CycleHealth(
                cycle_close_time_ms=cur_ts,
                mode=mode,
                clock_offset_ms=offset_override.get(i, offset_ms),
                clock_rtt_ms=15.0,
                used_weight_1m_peak=40,
                stale_symbols=(),
                gaps_detected=0,
                duration_ms=450,
                unhandled_errors=1 if i in unhandled_at else 0,
            )
        )
        cur_ts += step_ms


def _insert_prod_trade(
    db: Database,
    *,
    idx: int,
    environment: str = "PRODUCTION",
    status: str = "CLOSED",
    created_at_ms: int,
    closed_at_ms: int | None,
    realized_pnl_usdt: str | None = "10.00",
    fees_usdt_equiv: str | None = "0.50",
    realized_r: float | None = 0.5,
    slippage_entry_bps: float | None = 4.0,
    ml_grade_silent: str | None = "B",
    ml_p_cal_silent: float | None = 0.60,
) -> int:
    with db.write_tx() as tx:
        cur = tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt,
                time_barrier_end_ms,
                fees_usdt_equiv, realized_pnl_usdt, realized_r, slippage_entry_bps,
                ml_grade_silent, ml_p_cal_silent,
                created_at_ms, updated_at_ms, closed_at_ms
            ) VALUES (
                ?, ?, 'SOLUSDT', 'bull_trend_pullback', 'BULL',
                ?, ?,
                '100.00', '94.00', '112.00',
                2.0, 0.01, '10.00',
                ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?
            )
            """,
            (
                f"CHM_SOLUSDT_{environment}_{idx}_{created_at_ms}",
                environment,
                created_at_ms,
                status,
                created_at_ms + 72 * ONE_HOUR_MS,
                fees_usdt_equiv,
                realized_pnl_usdt,
                realized_r,
                slippage_entry_bps,
                ml_grade_silent,
                ml_p_cal_silent,
                created_at_ms,
                closed_at_ms if closed_at_ms is not None else created_at_ms,
                closed_at_ms,
            ),
        )
        return int(cur.lastrowid)


def _insert_shadow_trade_with_candle(
    db: Database,
    *,
    idx: int,
    symbol: str = "SOLUSDT",
    signal_close_time_ms: int,
    candle_close: float | None = 100.0,
    decision_ask: float = 100.04,
    status: str = "CLOSED",
) -> None:
    if candle_close is not None:
        with db.write_tx() as tx:
            tx.execute(
                """
                INSERT OR REPLACE INTO candles (
                    symbol, interval, open_time_ms, close_time_ms,
                    open, high, low, close, volume, quote_volume,
                    number_of_trades, taker_buy_base_volume, taker_buy_quote_volume,
                    ingested_at_ms
                ) VALUES (?, '1h', ?, ?, ?, ?, ?, ?, 1000.0, 100000.0, 500, 600.0, 60000.0, ?)
                """,
                (
                    symbol,
                    signal_close_time_ms - ONE_HOUR_MS,
                    signal_close_time_ms,
                    candle_close,
                    candle_close * 1.01,
                    candle_close * 0.99,
                    candle_close,
                    signal_close_time_ms,
                ),
            )
    with db.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO shadow_trades (
                symbol, strategy, regime, signal_close_time_ms, status,
                decision_ask_price, sim_entry_price, stop_price, target_price,
                qty, risk_pct, risk_usdt, virtual_equity_before,
                time_barrier_end_ms, exit_reason, exit_price, r_gross, r_net,
                ml_grade_silent, created_at_ms, closed_at_ms
            ) VALUES (
                ?, 'bull_trend_pullback', 'BULL', ?, ?,
                ?, ?, 94.0, 112.0,
                '1.0', 0.01, '3.00', '300.00',
                ?, ?, 105.0, 0.8, 0.7,
                'B', ?, ?
            )
            """,
            (
                symbol,
                signal_close_time_ms,
                status,
                decision_ask,
                decision_ask * 1.0003,
                signal_close_time_ms + 72 * ONE_HOUR_MS,
                "TARGET" if status == "CLOSED" else None,
                signal_close_time_ms,
                signal_close_time_ms + 4 * ONE_HOUR_MS if status == "CLOSED" else None,
            ),
        )


def test_gate_report_empty_db_all_criteria_unknown_and_strictly_readonly(tmp_path: Path) -> None:
    """Empty migrated DB must report UNKNOWN on EVERY criterion (never PASS by omission),
    and must work on a chmod 0444 file inside a chmod 0555 directory (ADR-002 read-only)."""
    ro_dir = tmp_path / "ro_dir"
    ro_dir.mkdir()
    db_path = ro_dir / "empty.db"
    db = _fresh_db(db_path)
    db.close()

    # Enforce OS-level read-only permissions on both file and parent directory
    os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    try:
        report = generate_gate_report(db_path)
        assert report["read_only"] is True
        assert report["schema_version"] == "1"
        assert not Path(f"{db_path}-wal").exists()
        assert not Path(f"{db_path}-shm").exists()

        cmap = _criteria_map(report)
        assert len(cmap) == 16  # 3 (6A) + 3 (6B) + 5 (F1->F2) + 5 (KS)
        for cid, c in cmap.items():
            assert c["status"] == "UNKNOWN", f"{cid} should be UNKNOWN on empty DB, got {c}"
        for sec_name, sec in report["sections"].items():
            assert sec["status"] == "UNKNOWN", f"Section {sec_name} must be UNKNOWN"
    finally:
        os.chmod(ro_dir, stat.S_IRWXU)
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    # Also verify live concurrent WAL reading while bot writer connection is open
    # AND confirm any write attempt on _open_sqlite_readonly fails with OperationalError.
    from tools.gate_report import _open_sqlite_readonly

    live_path = tmp_path / "live_wal.db"
    live_db = _fresh_db(live_path)
    try:
        live_db.set_state("emergency_halt", "0")
        live_db.set_state("kill_switch_reason", "NONE")
        rep_live = generate_gate_report(live_path)
        assert _criteria_map(rep_live)["kill_switches.emergency_halt"]["status"] == "PASS"

        ro_conn = _open_sqlite_readonly(live_path)
        try:
            with pytest.raises(Exception):
                ro_conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('illegal_write', '1')"
                )
        finally:
            ro_conn.close()
    finally:
        live_db.close()


def test_gate_6a_pass_fail_and_unknown_variants(tmp_path: Path) -> None:
    # 1. PASS: 168 consecutive clean cycles, |offset| <= 500ms, clean log, PASS restore report
    db_pass_path = tmp_path / "g6a_pass.db"
    db = _fresh_db(db_pass_path)
    _seed_cycles(db, mode="LIVE_CHAMELEON_PHASE_1", count=168, offset_ms=-499.5)
    db.close()

    clean_log = tmp_path / "bot_clean.log"
    clean_log.write_text(
        "2026-11-23 00:00:04 [INFO] quant_orchestrator: Cycle complete\n"
        "2026-11-23 01:00:04 [WARNING] healthcheck: transient timeout\n",
        encoding="utf-8",
    )
    drill_pass = tmp_path / "drill_pass.json"
    drill_pass.write_text(
        json.dumps({"status": "PASS", "backup_path": "bkp.db", "verified_at_utc": "2026-11-23T00:00:00Z"}),
        encoding="utf-8",
    )

    rep_pass = generate_gate_report(
        db_pass_path, log_file=clean_log, restore_drill_report=drill_pass
    )
    assert rep_pass["sections"]["gate_6a"]["status"] == "PASS"
    cmap = _criteria_map(rep_pass)
    assert cmap["gate_6a.cycles_168_no_unhandled"]["status"] == "PASS"
    assert cmap["gate_6a.clock_offset_within_500ms"]["status"] == "PASS"
    assert cmap["gate_6a.disaster_restore_drill"]["status"] == "PASS"

    # 2. UNKNOWN when --log-file or --restore-drill-report omitted (never PASS by omission!)
    rep_no_log = generate_gate_report(db_pass_path)
    cmap_nl = _criteria_map(rep_no_log)
    assert cmap_nl["gate_6a.cycles_168_no_unhandled"]["status"] == "UNKNOWN"
    assert cmap_nl["gate_6a.disaster_restore_drill"]["status"] == "UNKNOWN"
    assert rep_no_log["sections"]["gate_6a"]["status"] == "UNKNOWN"

    # 3. FAIL when log contains Traceback / Unhandled error even if DB unhandled_errors == 0
    bad_log = tmp_path / "bot_bad.log"
    bad_log.write_text(
        "2026-11-23 02:00:04 [ERROR] quant_orchestrator: Unhandled error during hourly cycle: boom\n"
        "Traceback (most recent call last):\n"
        "  File 'src/main.py', line 1038\n"
        "RuntimeError: boom\n",
        encoding="utf-8",
    )
    drill_fail = tmp_path / "drill_fail.json"
    drill_fail.write_text(json.dumps({"status": "FAIL", "backup_path": "bad.db"}), encoding="utf-8")
    rep_log_fail = generate_gate_report(
        db_pass_path, log_file=bad_log, restore_drill_report=drill_fail
    )
    cmap_lf = _criteria_map(rep_log_fail)
    assert cmap_lf["gate_6a.cycles_168_no_unhandled"]["status"] == "FAIL"
    assert cmap_lf["gate_6a.disaster_restore_drill"]["status"] == "FAIL"
    assert rep_log_fail["sections"]["gate_6a"]["status"] == "FAIL"

    # 4. FAIL on 1h gap breaking 168 consecutive streak AND on |offset| > 500ms
    db_gap_path = tmp_path / "g6a_gap.db"
    db_g = _fresh_db(db_gap_path)
    _seed_cycles(
        db_g,
        mode="LIVE_CHAMELEON_PHASE_1",
        count=170,
        gap_after_idx=85,  # splits into 85 + 85 (< 168 consecutive)
        offset_override={10: 505.2},
    )
    db_g.close()
    rep_gap = generate_gate_report(db_gap_path, log_file=clean_log, restore_drill_report=drill_pass)
    cmap_g = _criteria_map(rep_gap)
    assert cmap_g["gate_6a.cycles_168_no_unhandled"]["status"] == "FAIL"
    assert cmap_g["gate_6a.clock_offset_within_500ms"]["status"] == "FAIL"

    # 5. FAIL on DB unhandled_errors > 0
    db_err_path = tmp_path / "g6a_dberr.db"
    db_e = _fresh_db(db_err_path)
    _seed_cycles(db_e, mode="LIVE_CHAMELEON_PHASE_1", count=168, unhandled_at={100})
    db_e.close()
    rep_err = generate_gate_report(db_err_path, log_file=clean_log, restore_drill_report=drill_pass)
    assert _criteria_map(rep_err)["gate_6a.cycles_168_no_unhandled"]["status"] == "FAIL"


def test_gate_6b_pass_fail_and_unknown_variants(tmp_path: Path) -> None:
    base_ms = 1_700_000_000_000

    # 1. PASS: 31 days of SHADOW_CHAMELEON cycles, 8 trades (~1.8/week), slippage 0.04% <= 0.08%
    db_pass_path = tmp_path / "g6b_pass.db"
    db = _fresh_db(db_pass_path)
    _seed_cycles(db, mode="SHADOW_CHAMELEON", count=2, start_ms=base_ms, step_ms=31 * ONE_DAY_MS)
    for i in range(8):
        _insert_shadow_trade_with_candle(
            db,
            idx=i,
            signal_close_time_ms=base_ms + (i + 1) * 3 * ONE_DAY_MS,
            candle_close=100.0,
            decision_ask=100.04,  # +0.04% (4 bps)
        )
    db.close()

    rep_pass = generate_gate_report(db_pass_path)
    assert rep_pass["sections"]["gate_6b"]["status"] == "PASS"
    cmap = _criteria_map(rep_pass)
    assert cmap["gate_6b.duration_or_sample"]["status"] == "PASS"
    assert cmap["gate_6b.mean_slippage_le_0_08pct"]["status"] == "PASS"
    assert cmap["gate_6b.weekly_trade_frequency"]["status"] == "PASS"

    # 2. UNKNOWN slippage when shadow_trades have NO matching 1h candle (never fake PASS!)
    #    AND UNKNOWN weekly frequency when window < 7 days
    db_unk_path = tmp_path / "g6b_unk.db"
    db_u = _fresh_db(db_unk_path)
    _seed_cycles(db_u, mode="SHADOW_CHAMELEON", count=2, start_ms=base_ms, step_ms=3 * ONE_DAY_MS)
    _insert_shadow_trade_with_candle(
        db_u,
        idx=0,
        signal_close_time_ms=base_ms + ONE_DAY_MS,
        candle_close=None,  # No matching 1h candle
        decision_ask=100.04,
    )
    db_u.close()

    rep_u = generate_gate_report(db_unk_path)
    cmap_u = _criteria_map(rep_u)
    assert cmap_u["gate_6b.duration_or_sample"]["status"] == "FAIL"  # 3 days, 1 trade
    assert cmap_u["gate_6b.mean_slippage_le_0_08pct"]["status"] == "UNKNOWN"
    assert cmap_u["gate_6b.weekly_trade_frequency"]["status"] == "UNKNOWN"

    # 3. FAIL: high slippage (0.15% > 0.08%), short duration (10 days, 12 trades -> 8.4/wk > 7.0)
    db_fail_path = tmp_path / "g6b_fail.db"
    db_f = _fresh_db(db_fail_path)
    _seed_cycles(db_f, mode="SHADOW_CHAMELEON", count=2, start_ms=base_ms, step_ms=10 * ONE_DAY_MS)
    for i in range(12):
        _insert_shadow_trade_with_candle(
            db_f,
            idx=i,
            signal_close_time_ms=base_ms + (i + 1) * 18 * ONE_HOUR_MS,
            candle_close=100.0,
            decision_ask=100.15,  # +0.15% (15 bps) > 0.08%
        )
    db_f.close()

    rep_f = generate_gate_report(db_fail_path)
    cmap_f = _criteria_map(rep_f)
    assert cmap_f["gate_6b.duration_or_sample"]["status"] == "FAIL"
    assert cmap_f["gate_6b.mean_slippage_le_0_08pct"]["status"] == "FAIL"
    assert cmap_f["gate_6b.weekly_trade_frequency"]["status"] == "FAIL"
    assert rep_f["sections"]["gate_6b"]["status"] == "FAIL"

    # 4. PASS duration_or_sample via >= 25 trades even when < 30 days
    db_25_path = tmp_path / "g6b_25trades.db"
    db_25 = _fresh_db(db_25_path)
    _seed_cycles(db_25, mode="SHADOW_CHAMELEON", count=2, start_ms=base_ms, step_ms=28 * ONE_DAY_MS)
    for i in range(25):
        _insert_shadow_trade_with_candle(
            db_25,
            idx=i,
            signal_close_time_ms=base_ms + (i + 1) * ONE_DAY_MS,
            candle_close=100.0,
            decision_ask=100.05,
        )
    db_25.close()
    rep_25 = generate_gate_report(db_25_path)
    assert _criteria_map(rep_25)["gate_6b.duration_or_sample"]["status"] == "PASS"


def test_phase_1_to_2_all_5_requirements_pass_fail_and_unknown(tmp_path: Path) -> None:
    base_ms = 1_700_000_000_000

    # 1. Full PASS across all 5 requirements:
    #    - 60 PRODUCTION closed trades spanning 65 days
    #    - 40 Grade B/A+ (30 wins +2.0R/+20 USDT, 10 losses -1.0R/-10 USDT, p_cal=0.75 -> exact winrate 0.75 -> ECE=0.0)
    #    - 20 Grade C    (5 wins +1.0R/+10 USDT, 15 losses -1.0R/-10 USDT, p_cal=0.25 -> exact winrate 0.25 -> ECE=0.0)
    #    - Total PnL > 0, PF = 650 / 250 = 2.60 >= 1.15, mean R = +0.667R >= +0.10R, MaxDD < 10%
    #    - 0 orphans, slippage = 5.0 bps (0.05%) <= 0.10%
    db_pass_path = tmp_path / "p12_pass.db"
    db = _fresh_db(db_pass_path)
    db.set_state("initial_tactical_equity_usdt", "1000.00")

    # Interleave wins and losses so drawdown stays tiny (< 3%)
    specs: list[tuple[str, float, str, float]] = []
    for i in range(30):
        specs.append(("A+" if i < 15 else "B", 0.75, "20.00", 2.0))
    for _ in range(10):
        specs.append(("B", 0.75, "-10.00", -1.0))
    for _ in range(5):
        specs.append(("C", 0.25, "10.00", 1.0))
    for _ in range(15):
        specs.append(("C", 0.25, "-10.00", -1.0))
    # Interleave positives and negatives cleanly
    pos = [s for s in specs if s[3] > 0]
    neg = [s for s in specs if s[3] < 0]
    ordered: list[tuple[str, float, str, float]] = []
    while pos or neg:
        if pos:
            ordered.append(pos.pop(0))
        if neg:
            ordered.append(neg.pop(0))

    for i, (grade, p_cal, pnl, r_val) in enumerate(ordered):
        c_ms = base_ms + i * ONE_DAY_MS
        cl_ms = c_ms + 6 * ONE_DAY_MS if i == len(ordered) - 1 else c_ms + 6 * ONE_HOUR_MS
        _insert_prod_trade(
            db,
            idx=i,
            created_at_ms=c_ms,
            closed_at_ms=cl_ms,
            realized_pnl_usdt=pnl,
            fees_usdt_equiv="0.00",
            realized_r=r_val,
            slippage_entry_bps=5.0,
            ml_grade_silent=grade,
            ml_p_cal_silent=p_cal,
        )
    db.close()

    rep_pass = generate_gate_report(db_pass_path)
    assert rep_pass["sections"]["phase_1_to_2"]["status"] == "PASS"
    cmap = _criteria_map(rep_pass)
    for k in (
        "phase_1_to_2.req1_sample_and_days",
        "phase_1_to_2.req2_profit_and_pf",
        "phase_1_to_2.req3_expectancy_and_max_dd",
        "phase_1_to_2.req4_zero_orphans_and_slippage",
        "phase_1_to_2.req5_silent_ai_proof",
    ):
        assert cmap[k]["status"] == "PASS", f"{k}: {cmap[k]}"

    # 2. UNKNOWN checks:
    #    - Testnet-only trades must leave PRODUCTION Phase 1->2 as UNKNOWN
    #    - Missing initial equity must make req3 UNKNOWN (even with high mean R)
    #    - Missing ml_grade_silent / slippage_entry_bps / realized_pnl_usdt -> UNKNOWN
    db_unk_path = tmp_path / "p12_unk.db"
    db_u = _fresh_db(db_unk_path)
    _insert_prod_trade(
        db_u,
        idx=0,
        environment="PRODUCTION",
        created_at_ms=base_ms,
        closed_at_ms=base_ms + ONE_HOUR_MS,
        realized_pnl_usdt=None,
        realized_r=0.5,
        slippage_entry_bps=None,
        ml_grade_silent=None,
        ml_p_cal_silent=None,
    )
    db_u.close()

    rep_u = generate_gate_report(db_unk_path)
    cmap_u = _criteria_map(rep_u)
    assert cmap_u["phase_1_to_2.req1_sample_and_days"]["status"] == "FAIL"  # 1 < 60
    assert cmap_u["phase_1_to_2.req2_profit_and_pf"]["status"] == "UNKNOWN"
    assert cmap_u["phase_1_to_2.req3_expectancy_and_max_dd"]["status"] == "UNKNOWN"  # no init equity
    assert cmap_u["phase_1_to_2.req4_zero_orphans_and_slippage"]["status"] == "UNKNOWN"
    assert cmap_u["phase_1_to_2.req5_silent_ai_proof"]["status"] == "UNKNOWN"

    # 3. FAIL checks across all 5 requirements:
    #    - 10 trades over 10 days -> req1 FAIL
    #    - PF < 1.15 -> req2 FAIL
    #    - MaxDD >= 10% (loss streak of -150 USDT on 1000 USDT equity = 15% DD) -> req3 FAIL
    #    - Historical HALT_REVIEW in order_events + high slippage 15 bps -> req4 FAIL
    #    - Grade C outperforms Grade B/A+ AND bad calibration ECE > 0.05 -> req5 FAIL
    db_fail_path = tmp_path / "p12_fail.db"
    db_f = _fresh_db(db_fail_path)
    db_f.set_state("initial_tactical_equity_usdt", "1000.00")
    t_ids: list[int] = []
    for i in range(10):
        # First 5 lose -30 USDT each (-15% DD) with grade B & overconfident p_cal=0.95
        # Last 5 win +32 USDT each with grade C & p_cal=0.10 (C beats B + huge ECE)
        is_loss = i < 5
        tid = _insert_prod_trade(
            db_f,
            idx=i,
            created_at_ms=base_ms + i * ONE_DAY_MS,
            closed_at_ms=base_ms + i * ONE_DAY_MS + ONE_HOUR_MS,
            realized_pnl_usdt="-30.00" if is_loss else "32.00",
            fees_usdt_equiv="0.00",
            realized_r=-1.0 if is_loss else 1.5,
            slippage_entry_bps=15.0,  # 0.15% > 0.10%
            ml_grade_silent="B" if is_loss else "C",
            ml_p_cal_silent=0.95 if is_loss else 0.10,
        )
        t_ids.append(tid)
    # Record a historical HALT_REVIEW event on trade 0
    with db_f.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO order_events (
                trade_id, client_order_id, method, endpoint,
                http_status, binance_code, request_json, response_json, ts_ms
            ) VALUES (?, 'HALT_0', 'HALT_REVIEW', 'local://watchdog_halt_review', 0, NULL, '{}', '{}', ?)
            """,
            (t_ids[0], base_ms),
        )
    db_f.close()

    rep_f = generate_gate_report(db_fail_path)
    assert rep_f["sections"]["phase_1_to_2"]["status"] == "FAIL"
    cmap_f = _criteria_map(rep_f)
    for k in (
        "phase_1_to_2.req1_sample_and_days",
        "phase_1_to_2.req2_profit_and_pf",
        "phase_1_to_2.req3_expectancy_and_max_dd",
        "phase_1_to_2.req4_zero_orphans_and_slippage",
        "phase_1_to_2.req5_silent_ai_proof",
    ):
        assert cmap_f[k]["status"] == "FAIL", f"{k}: {cmap_f[k]}"


def test_kill_switches_pass_fail_and_unknown_variants(tmp_path: Path) -> None:
    base_ms = 1_700_000_000_000

    # 1. All 5 kill-switch checks PASS
    db_pass_path = tmp_path / "ks_pass.db"
    db = _fresh_db(db_pass_path)
    db.set_state("emergency_halt", "0")
    db.set_state("kill_switch_reason", "NONE")
    db.set_state("tactical_hwm_usdt", "1000.00")
    db.set_state("current_equity_usdt", "950.00")  # 5% DD (< 12%)
    db.set_state("bnb_free_usdt_equiv", "6.50")  # >= 2.00 USDT
    db.set_state("reconcile_fail_streak", "0")
    _seed_cycles(db, mode="LIVE_CHAMELEON_PHASE_1", count=5, start_ms=base_ms)
    # 10 ENTRY_REJECTED inside 24h must NOT trip KS-4 (matches src/main.py:298)
    for i in range(10):
        _insert_prod_trade(
            db,
            idx=i,
            status="ENTRY_REJECTED",
            created_at_ms=base_ms + i * 60_000,
            closed_at_ms=None,
            realized_pnl_usdt=None,
            realized_r=None,
        )
    db.close()

    rep_pass = generate_gate_report(db_pass_path)
    assert rep_pass["sections"]["kill_switches"]["status"] == "PASS"
    for c in rep_pass["sections"]["kill_switches"]["criteria"]:
        assert c["status"] == "PASS", c

    # 2. All 5 kill-switch checks FAIL
    db_fail_path = tmp_path / "ks_fail.db"
    db_f = _fresh_db(db_fail_path)
    db_f.set_state("emergency_halt", "1")
    db_f.set_state("kill_switch_reason", "RECONCILIATION_OR_AUTH_FAILURE")
    db_f.set_state("tactical_hwm_usdt", "1000.00")
    db_f.set_state("current_equity_usdt", "870.00")  # 13% DD >= 12%
    db_f.set_state("bnb_free_usdt_equiv", "1.50")  # < 2.00 USDT
    db_f.set_state("reconcile_fail_streak", "3")
    # 6 non-rejected trades within 24h -> trips KS-4
    for i in range(6):
        _insert_prod_trade(
            db_f,
            idx=i,
            status="CLOSED",
            created_at_ms=base_ms + i * ONE_HOUR_MS,
            closed_at_ms=base_ms + i * ONE_HOUR_MS + 1000,
        )
    db_f.close()

    rep_fail = generate_gate_report(db_fail_path)
    assert rep_fail["sections"]["kill_switches"]["status"] == "FAIL"
    for c in rep_fail["sections"]["kill_switches"]["criteria"]:
        assert c["status"] == "FAIL", c

    # 3. KS-3 also FAILS on HTTP 418 or auth code -2015 in order_events even if streak == 0
    db_418_path = tmp_path / "ks_418.db"
    db_4 = _fresh_db(db_418_path)
    db_4.set_state("reconcile_fail_streak", "0")
    with db_4.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO order_events (
                trade_id, client_order_id, method, endpoint,
                http_status, binance_code, request_json, response_json, ts_ms
            ) VALUES (NULL, 'C1', 'POST', '/api/v3/order', 418, -1003, '{}', '{}', ?)
            """,
            (base_ms,),
        )
    db_4.close()
    rep_4 = generate_gate_report(db_418_path)
    assert _criteria_map(rep_4)["kill_switches.ks3_reconciliation_or_auth"]["status"] == "FAIL"


def test_gate_report_cli_outputs_both_table_and_valid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "cli.db"
    db = _fresh_db(db_path)
    db.close()

    json_out = tmp_path / "report.json"
    rc = gate_report_main(["--db", str(db_path), "--format", "both", "--json-out", str(json_out)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "QUANTCRYPTO CHAMELEON" in out
    assert "--- JSON ---" in out
    json_part = out.split("--- JSON ---", 1)[1].strip()
    parsed = json.loads(json_part)
    assert parsed["read_only"] is True
    assert set(parsed["sections"].keys()) == {"gate_6a", "gate_6b", "phase_1_to_2", "kill_switches"}
    assert json.loads(json_out.read_text(encoding="utf-8")) == parsed

    # Table formatter includes measured & threshold headers
    tbl = format_report_table(parsed)
    assert "MEDIDO" in tbl and "LIMIAR" in tbl
