"""End-to-end tests for tools/restore_drill.sh (plain .db, encrypted .db.gz.enc, corrupt fail, cleanup, no writes to ./data or ./secrets)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

from src.core.database import CycleHealth, Database
from src.core.offsite_backup import prepare_offsite_artifact
from tools.gate_report import generate_gate_report

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "restore_drill.sh"


def _build_rich_backup(tmp_path: Path) -> Path:
    db_path = tmp_path / "src_live.db"
    db = Database(db_path)
    db.connect()
    db.migrate()
    db.set_state("emergency_halt", "0")
    db.set_state("kill_switch_reason", "NONE")
    db.set_state("tactical_hwm_usdt", "1050.00")
    db.set_state("reconcile_fail_streak", "0")
    db.log_cycle_health(
        CycleHealth(
            cycle_close_time_ms=1_700_000_000_000,
            mode="LIVE_CHAMELEON_PHASE_1",
            clock_offset_ms=12.5,
            clock_rtt_ms=18.0,
            used_weight_1m_peak=35,
            stale_symbols=(),
            gaps_detected=0,
            duration_ms=420,
            unhandled_errors=0,
        )
    )
    with db.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO candles (
                symbol, interval, open_time_ms, close_time_ms,
                open, high, low, close, volume, quote_volume,
                number_of_trades, taker_buy_base_volume, taker_buy_quote_volume,
                ingested_at_ms
            ) VALUES ('SOLUSDT', '1h', 1699996400000, 1700000000000, 100.0, 102.0, 99.0, 101.0, 500.0, 50500.0, 200, 300.0, 30300.0, 1700000003000)
            """
        )
        # 1 CLOSED historical trade + 1 active PROTECTED live trade
        tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt,
                time_barrier_end_ms, realized_pnl_usdt, realized_r,
                created_at_ms, updated_at_ms, closed_at_ms
            ) VALUES (
                'CHM_SOLUSDT_HIST1', 'TESTNET', 'SOLUSDT', 'bull_trend_pullback', 'BULL',
                1699900000000, 'CLOSED',
                '95.00', '90.00', '105.00',
                1.5, 0.01, '10.00',
                1700159200000, '20.00', 2.0,
                1699900000000, 1699920000000, 1699920000000
            )
            """
        )
        tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt,
                entry_order_id, entry_executed_qty, entry_avg_price, entry_quote_spent,
                oco_order_list_id, oco_list_client_id, oco_qty,
                oco_stop_price, oco_stop_limit_price, oco_target_price,
                time_barrier_end_ms, created_at_ms, updated_at_ms
            ) VALUES (
                'CHM_SOLUSDT_ACTIVE1', 'TESTNET', 'SOLUSDT', 'bull_trend_pullback', 'BULL',
                1700000000000, 'PROTECTED',
                '101.00', '95.00', '113.00',
                2.0, 0.01, '10.00',
                1001, '10.00', '101.00', '1010.00',
                5001, 'OCO_SOLUSDT_2_1700000000000', '10.00',
                '95.00', '92.62', '113.00',
                1700259200000, 1700000000000, 1700000000000
            )
            """
        )
        # 1 OPEN shadow trade
        tx.execute(
            """
            INSERT INTO shadow_trades (
                symbol, strategy, regime, signal_close_time_ms, status,
                decision_ask_price, sim_entry_price, stop_price, target_price,
                qty, risk_pct, risk_usdt, virtual_equity_before,
                time_barrier_end_ms, created_at_ms
            ) VALUES (
                'BTCUSDT', 'bull_trend_pullback', 'BULL', 1700000000000, 'OPEN',
                50000.0, 50015.0, 48000.0, 54000.0,
                '0.01', 0.01, '3.00', '300.00',
                1700259200000, 1700000000000
            )
            """
        )
    bkp = db.backup_to(tmp_path / "backups")
    db.close()
    return bkp


def test_restore_drill_plain_db_pass_cleanup_and_gate_report_integration(tmp_path: Path) -> None:
    bkp = _build_rich_backup(tmp_path)
    isolated_tmp = tmp_path / "drill_tmp"
    isolated_tmp.mkdir()
    report_json = tmp_path / "drill_report.json"

    # Poison parent environment with live-looking vars to prove restore_drill.sh scrubs them
    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": str(isolated_tmp),
            "MODE": "LIVE_CHAMELEON_PHASE_1",
            "BINANCE_API_KEY": "MUST_BE_SCRUBBED",
            "BINANCE_ED25519_PRIVATE_KEY_PATH": "/nonexistent/secret.pem",
            "DB_PATH": str(REPO_ROOT / "data" / "must_never_touch.db"),
        }
    )

    data_before = set((REPO_ROOT / "data").iterdir()) if (REPO_ROOT / "data").exists() else None
    secrets_before = set((REPO_ROOT / "secrets").iterdir()) if (REPO_ROOT / "secrets").exists() else None

    proc = subprocess.run(
        [str(SCRIPT), str(bkp), "--report-json", str(report_json)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "RESTORE DRILL: PASS" in proc.stdout

    # Temporary directory MUST be completely cleaned up
    assert list(isolated_tmp.glob("quant_restore_drill.*")) == []

    # ./data and ./secrets MUST be untouched
    data_after = set((REPO_ROOT / "data").iterdir()) if (REPO_ROOT / "data").exists() else None
    secrets_after = set((REPO_ROOT / "secrets").iterdir()) if (REPO_ROOT / "secrets").exists() else None
    assert data_after == data_before
    assert secrets_after == secrets_before

    # Verify JSON report contents & integration with tools/gate_report.py
    payload = json.loads(report_json.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    ver = payload["verification"]
    assert ver["integrity_check"] == "ok"
    assert ver["safe_mode"] == "ANALYST_ONLY"
    assert ver["signed_client_present"] is False
    assert ver["table_counts"]["candles"] == 1
    assert ver["table_counts"]["trades"] == 2
    assert ver["table_counts"]["shadow_trades"] == 1
    assert ver["table_counts"]["cycle_health"] == 1
    assert ver["active_live_trade"]["status"] == "PROTECTED"
    assert ver["active_live_trade"]["symbol"] == "SOLUSDT"
    assert ver["open_shadow_trade"]["symbol"] == "BTCUSDT"

    gate_rep = generate_gate_report(bkp, restore_drill_report=report_json)
    drill_crit = next(
        c
        for c in gate_rep["sections"]["gate_6a"]["criteria"]
        if c["id"] == "gate_6a.disaster_restore_drill"
    )
    assert drill_crit["status"] == "PASS"


def test_restore_drill_encrypted_artifact_and_corrupt_fail_and_forbidden_paths(tmp_path: Path) -> None:
    bkp = _build_rich_backup(tmp_path)
    key = Fernet.generate_key().decode("ascii")
    enc_path, is_enc, _ = prepare_offsite_artifact(bkp, encryption_key=key)
    assert is_enc is True

    isolated_tmp = tmp_path / "drill_tmp2"
    isolated_tmp.mkdir()
    env = os.environ.copy()
    env["TMPDIR"] = str(isolated_tmp)

    # 1. Encrypted .db.gz.enc with valid --key -> PASS
    ok_proc = subprocess.run(
        [str(SCRIPT), str(enc_path), "--key", key],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_proc.returncode == 0, f"stdout={ok_proc.stdout}\nstderr={ok_proc.stderr}"
    assert "RESTORE DRILL: PASS" in ok_proc.stdout
    assert list(isolated_tmp.glob("quant_restore_drill.*")) == []

    # 2. Encrypted .db.gz.enc with wrong --key -> FAIL + cleanup
    bad_key_proc = subprocess.run(
        [str(SCRIPT), str(enc_path), "--key", Fernet.generate_key().decode("ascii")],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_key_proc.returncode != 0
    assert "RESTORE DRILL: FAIL" in bad_key_proc.stdout
    assert list(isolated_tmp.glob("quant_restore_drill.*")) == []

    # 3. Corrupted SQLite file -> FAIL + FAIL JSON report + cleanup
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"NOT A VALID SQLITE DATABASE" * 100)
    fail_json = tmp_path / "corrupt_report.json"
    corrupt_proc = subprocess.run(
        [str(SCRIPT), str(corrupt_db), "--report-json", str(fail_json)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert corrupt_proc.returncode != 0
    assert "RESTORE DRILL: FAIL" in corrupt_proc.stdout
    assert json.loads(fail_json.read_text(encoding="utf-8"))["status"] == "FAIL"
    assert list(isolated_tmp.glob("quant_restore_drill.*")) == []

    # 4. Refuses writing --report-json inside ./data or ./secrets
    for forbidden_target in (REPO_ROOT / "data" / "illegal.json", REPO_ROOT / "secrets" / "illegal.json"):
        refuse_proc = subprocess.run(
            [str(SCRIPT), str(bkp), "--report-json", str(forbidden_target)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert refuse_proc.returncode != 0
        assert "RESTORE DRILL: FAIL" in refuse_proc.stdout
        assert not forbidden_target.exists()
