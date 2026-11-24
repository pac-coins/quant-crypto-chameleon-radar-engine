"""Unit tests for tools/guided_gate_switch.py (exclusively Gate 6A -> Gate 6B)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from src.core.database import CycleHealth, Database
from tools.guided_gate_switch import (
    CONFIRMATION_PHRASE,
    main,
    run_guided_gate_switch,
)

ONE_HOUR_MS = 3_600_000


def _seed_passing_6a_fixtures(tmp_path: Path, *, n_cycles: int = 168, emergency_halt: str = "0") -> dict[str, Any]:
    """Create a complete passing Gate 6A setup (SQLite, log, restore drill JSON, Ed25519 key, .env)."""
    db_path = tmp_path / "quant_6a.db"
    db = Database(db_path)
    db.connect()
    db.migrate()
    base_ms = 1_700_000_000_000 - (1_700_000_000_000 % ONE_HOUR_MS)
    for i in range(n_cycles):
        db.log_cycle_health(
            CycleHealth(
                cycle_close_time_ms=base_ms + i * ONE_HOUR_MS,
                mode="LIVE_CHAMELEON_PHASE_1",
                clock_offset_ms=30.0,
                clock_rtt_ms=12.0,
                used_weight_1m_peak=90,
                stale_symbols=(),
                gaps_detected=0,
                duration_ms=350,
                unhandled_errors=0,
            )
        )
    db.set_state("emergency_halt", emergency_halt)
    db.set_state("kill_switch_reason", "NONE" if emergency_halt == "0" else "MANUAL_HALT")
    db.set_state("tactical_hwm_usdt", "300.00")
    db.set_state("current_equity_usdt", "298.50")
    db.set_state("bnb_free_usdt_equiv", "12.50")
    db.set_state("reconcile_fail_streak", "0")
    db.close()

    log_file = tmp_path / "bot_6a.log"
    log_file.write_text("2026-11-24 INFO cycle complete\n", encoding="utf-8")

    restore_json = tmp_path / "restore_drill_6a.json"
    restore_json.write_text(
        json.dumps(
            {
                "status": "PASS",
                "backup_path": "quant_backup_test.db",
                "verified_at_utc": "2026-11-24T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    key_file = tmp_path / "ed25519.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nDUMMY\n-----END PRIVATE KEY-----\n", encoding="utf-8")
    os.chmod(key_file, 0o600)

    fernet_key = Fernet.generate_key().decode("ascii")
    tg_token = "111222:SUPER_SECRET_TG_TOKEN_GUIDED"
    api_key = "SUPER_SECRET_TESTNET_API_KEY_12345"
    hc_url = "https://hc-ping.com/secret-uuid-987654"

    env_file = tmp_path / ".env"
    env_text = (
        "# Gate 6A Testnet config\n"
        "MODE=LIVE_CHAMELEON_PHASE_1\n"
        "BINANCE_ENV=TESTNET\n"
        f"BINANCE_API_KEY={api_key}\n"
        f"BINANCE_ED25519_PRIVATE_KEY_PATH={key_file}\n"
        f"TELEGRAM_BOT_TOKEN={tg_token}\n"
        "TELEGRAM_CHAT_ID=444555666\n"
        f"DB_PATH={db_path}\n"
        f"HEALTHCHECK_URL={hc_url}\n"
        "OFFSITE_BACKUP_ENABLED=true\n"
        f"BACKUP_ENCRYPTION_KEY={fernet_key}\n"
    )
    env_file.write_text(env_text, encoding="utf-8")
    os.chmod(env_file, 0o600)

    return {
        "db_path": db_path,
        "log_file": log_file,
        "restore_json": restore_json,
        "env_file": env_file,
        "orig_env_text": env_text,
        "secrets": [tg_token, api_key, fernet_key, "secret-uuid-987654"],
    }


@pytest.mark.parametrize("bad_target", ["phase1", "phase2", "PHASE1", "production", "live", "6a"])
def test_refuses_phase1_phase2_and_any_non_6b_target(tmp_path: Path, bad_target: str) -> None:
    fx = _seed_passing_6a_fixtures(tmp_path)
    lines: list[str] = []

    res = run_guided_gate_switch(
        target=bad_target,
        db_path=fx["db_path"],
        env_file=fx["env_file"],
        log_file=fx["log_file"],
        restore_drill_report=fx["restore_json"],
        input_fn=lambda _: CONFIRMATION_PHRASE,
        output_fn=lines.append,
    )

    assert res.switched is False
    assert res.exit_code == 2
    assert "EXCLUSIVAMENTE" in res.reason
    assert "Fase 1" in res.reason and "Fase 2" in res.reason
    assert fx["env_file"].read_text(encoding="utf-8") == fx["orig_env_text"]


def test_refuses_when_any_gate_6a_criterion_is_unknown(tmp_path: Path) -> None:
    """Omitting --restore-drill-report produces UNKNOWN on gate_6a.restore_drill_validated -> MUST block."""
    fx = _seed_passing_6a_fixtures(tmp_path)
    prompted = False

    def _never_prompt(_: str) -> str:
        nonlocal prompted
        prompted = True
        return CONFIRMATION_PHRASE

    res = run_guided_gate_switch(
        target="6b",
        db_path=fx["db_path"],
        env_file=fx["env_file"],
        log_file=fx["log_file"],
        restore_drill_report=None,  # causes UNKNOWN!
        input_fn=_never_prompt,
        output_fn=lambda _: None,
    )

    assert res.switched is False
    assert res.exit_code == 1
    assert prompted is False
    assert any("UNKNOWN" in item for item in res.blocking_items)
    assert fx["env_file"].read_text(encoding="utf-8") == fx["orig_env_text"]


def test_refuses_when_gate_6a_or_kill_switch_fails(tmp_path: Path) -> None:
    """Fewer than 168 cycles OR active emergency_halt MUST refuse without prompting or editing .env."""
    fx = _seed_passing_6a_fixtures(tmp_path, n_cycles=120, emergency_halt="1")
    lines: list[str] = []

    res = run_guided_gate_switch(
        target="6b",
        db_path=fx["db_path"],
        env_file=fx["env_file"],
        log_file=fx["log_file"],
        restore_drill_report=fx["restore_json"],
        input_fn=lambda _: CONFIRMATION_PHRASE,
        output_fn=lines.append,
    )

    assert res.switched is False
    assert res.exit_code == 1
    assert any("FAIL" in item for item in res.blocking_items)
    assert fx["env_file"].read_text(encoding="utf-8") == fx["orig_env_text"]


def test_refuses_when_preflight_fails(tmp_path: Path) -> None:
    """Missing Ed25519 key triggers Preflight 6A FAIL -> MUST block."""
    fx = _seed_passing_6a_fixtures(tmp_path)
    (tmp_path / "ed25519.pem").unlink()

    res = run_guided_gate_switch(
        target="6b",
        db_path=fx["db_path"],
        env_file=fx["env_file"],
        log_file=fx["log_file"],
        restore_drill_report=fx["restore_json"],
        input_fn=lambda _: CONFIRMATION_PHRASE,
        output_fn=lambda _: None,
    )

    assert res.switched is False
    assert res.exit_code == 1
    assert any("PREFLIGHT FAIL" in item for item in res.blocking_items)
    assert fx["env_file"].read_text(encoding="utf-8") == fx["orig_env_text"]


def test_aborts_cleanly_if_confirmation_phrase_does_not_match(tmp_path: Path) -> None:
    fx = _seed_passing_6a_fixtures(tmp_path)
    lines: list[str] = []

    res = run_guided_gate_switch(
        target="6b",
        db_path=fx["db_path"],
        env_file=fx["env_file"],
        log_file=fx["log_file"],
        restore_drill_report=fx["restore_json"],
        input_fn=lambda _: "sim",  # wrong confirmation
        output_fn=lines.append,
    )

    assert res.switched is False
    assert res.exit_code == 1
    assert res.backup_path is None
    assert fx["env_file"].read_text(encoding="utf-8") == fx["orig_env_text"]
    assert not list(tmp_path.glob(".env.bak.*"))


def test_switches_to_6b_after_all_pass_and_explicit_confirmation(tmp_path: Path) -> None:
    fx = _seed_passing_6a_fixtures(tmp_path)
    lines: list[str] = []

    res = run_guided_gate_switch(
        target="6b",
        db_path=fx["db_path"],
        env_file=fx["env_file"],
        log_file=fx["log_file"],
        restore_drill_report=fx["restore_json"],
        input_fn=lambda _: CONFIRMATION_PHRASE,
        output_fn=lines.append,
    )

    assert res.switched is True
    assert res.exit_code == 0
    assert res.backup_path is not None and res.backup_path.is_file()
    assert res.post_preflight_overall in ("PASS", "WARN")

    # Backup preserves original content & 0600 permissions
    assert res.backup_path.read_text(encoding="utf-8") == fx["orig_env_text"]
    assert stat.S_IMODE(res.backup_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(fx["env_file"].stat().st_mode) == 0o600

    # Updated .env switches to SHADOW_CHAMELEON, strips Binance keys (ADR-010), preserves Telegram/backup
    updated = fx["env_file"].read_text(encoding="utf-8")
    assert "MODE=SHADOW_CHAMELEON" in updated
    assert "SHADOW_INITIAL_USDT=300.00" in updated
    assert "BINANCE_API_KEY" not in updated
    assert "BINANCE_ED25519_PRIVATE_KEY_PATH" not in updated
    assert "TELEGRAM_BOT_TOKEN=" in updated
    assert "BACKUP_ENCRYPTION_KEY=" in updated

    # Zero secrets printed in output
    full_output = "\n".join(lines)
    for sec in fx["secrets"]:
        assert sec not in full_output


def test_cli_entrypoint_refuses_phase1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--to", "phase1"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "RECUSADO" in out
