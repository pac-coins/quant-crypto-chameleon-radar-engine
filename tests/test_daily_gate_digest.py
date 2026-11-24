"""Unit tests for tools/daily_gate_digest.py (read-only gate digest + Telegram + cron)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pytest

from src.core.database import CycleHealth, Database
from tools.daily_gate_digest import (
    build_cron_example,
    compute_gate_day_progress,
    main,
    run_daily_digest,
)

ONE_HOUR_MS = 3_600_000
ONE_DAY_MS = 86_400_000


class _FakeTelegramTransport:
    def __init__(self, *, fail_with: Optional[str] = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with = fail_with

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        self.sent.append((chat_id, text))
        return 4242


def _create_db_with_6a_cycles(
    db_path: Path,
    *,
    n_cycles: int,
    mode: str = "LIVE_CHAMELEON_PHASE_1",
) -> int:
    db = Database(db_path)
    db.connect()
    db.migrate()
    base_ms = 1_700_000_000_000 - (1_700_000_000_000 % ONE_HOUR_MS)
    for i in range(n_cycles):
        db.log_cycle_health(
            CycleHealth(
                cycle_close_time_ms=base_ms + i * ONE_HOUR_MS,
                mode=mode,
                clock_offset_ms=25.0,
                clock_rtt_ms=15.0,
                used_weight_1m_peak=120,
                stale_symbols=(),
                gaps_detected=0,
                duration_ms=450,
                unhandled_errors=0,
            )
        )
    db.set_state("emergency_halt", "0")
    db.set_state("kill_switch_reason", "NONE")
    db.set_state("tactical_hwm_usdt", "300.00")
    db.set_state("current_equity_usdt", "300.00")
    db.set_state("bnb_free_usdt_equiv", "10.00")
    db.set_state("reconcile_fail_streak", "0")
    db.close()
    return base_ms + (n_cycles - 1) * ONE_HOUR_MS


def test_daily_digest_6a_day_progress_and_telegram_send(tmp_path: Path) -> None:
    db_path = tmp_path / "quant_6a.db"
    # 75 hourly cycles = 74 hours = ~3.08 days -> Day 4 / 7
    _create_db_with_6a_cycles(db_path, n_cycles=75, mode="LIVE_CHAMELEON_PHASE_1")

    log_file = tmp_path / "bot.log"
    log_file.write_text("INFO cycle ok\nINFO cycle ok\n", encoding="utf-8")

    env_file = tmp_path / ".env"
    secret_tok = "123456:SUPER_SECRET_TELEGRAM_TOKEN_XYZ"
    secret_api = "BINANCE_SECRET_API_KEY_999"
    env_file.write_text(
        f"MODE=LIVE_CHAMELEON_PHASE_1\n"
        f"BINANCE_ENV=TESTNET\n"
        f"BINANCE_API_KEY={secret_api}\n"
        f"TELEGRAM_BOT_TOKEN={secret_tok}\n"
        f"TELEGRAM_CHAT_ID=987654321\n",
        encoding="utf-8",
    )

    transport = _FakeTelegramTransport()
    res = run_daily_digest(
        db_path=db_path,
        gate="auto",
        env_file=env_file,
        log_file=log_file,
        dry_run=False,
        transport=transport,
    )

    assert res["gate"] == "6a"
    assert res["sent"] is True
    assert res["telegram_message_id"] == 4242
    assert res["day_progress"]["current_day"] == 4
    assert res["day_progress"]["target_days"] == 7

    assert len(transport.sent) == 1
    chat_id, msg = transport.sent[0]
    assert chat_id == "987654321"
    assert "Gate 6A (Testnet)" in msg
    assert "Dia 4 / 7" in msg
    assert "PASS" in msg
    assert "FAIL" in msg  # 75 < 168 cycles
    assert "UNKNOWN" in msg  # restore_drill_report omitted
    assert secret_tok not in msg
    assert secret_api not in msg


def test_daily_digest_6b_dry_run_and_readonly_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ro_dir = tmp_path / "ro_data"
    ro_dir.mkdir()
    db_path = ro_dir / "quant_6b.db"
    # 265 cycles = 11.0 days -> Day 12 / 30
    _create_db_with_6a_cycles(db_path, n_cycles=265, mode="SHADOW_CHAMELEON")

    env_file = tmp_path / ".env"
    secret_tok = "999999:DO_NOT_LEAK_TOKEN_ABC"
    env_file.write_text(
        f"MODE=SHADOW_CHAMELEON\n"
        f"TELEGRAM_BOT_TOKEN={secret_tok}\n"
        f"TELEGRAM_CHAT_ID=111222\n",
        encoding="utf-8",
    )

    # Lock directory read-only to prove zero SQLite write attempts (ADR-002)
    os.chmod(ro_dir, 0o555)
    try:
        rc = main(
            [
                "--db",
                str(db_path),
                "--env-file",
                str(env_file),
                "--gate",
                "auto",
                "--dry-run",
            ]
        )
    finally:
        os.chmod(ro_dir, 0o755)

    captured = capsys.readouterr()
    assert rc == 0
    assert "Gate 6B (Shadow)" in captured.out
    assert "Dia 12 / 30" in captured.out
    assert secret_tok not in captured.out
    assert secret_tok not in captured.err


def test_daily_digest_redacts_token_on_http_error(tmp_path: Path) -> None:
    db_path = tmp_path / "quant_err.db"
    _create_db_with_6a_cycles(db_path, n_cycles=10)

    secret_tok = "777777:LEAKABLE_BOT_TOKEN_QWERTY"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"MODE=LIVE_CHAMELEON_PHASE_1\n"
        f"BINANCE_ENV=TESTNET\n"
        f"TELEGRAM_BOT_TOKEN={secret_tok}\n"
        f"TELEGRAM_CHAT_ID=55555\n",
        encoding="utf-8",
    )

    failing_transport = _FakeTelegramTransport(
        fail_with=f"502 Bad Gateway for url: https://api.telegram.org/bot{secret_tok}/sendMessage"
    )
    with pytest.raises(RuntimeError) as exc_info:
        run_daily_digest(
            db_path=db_path,
            gate="6a",
            env_file=env_file,
            dry_run=False,
            transport=failing_transport,
        )

    err_text = str(exc_info.value)
    assert secret_tok not in err_text
    assert "[REDACTED]" in err_text


def test_print_cron_generates_safe_vm_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "--print-cron",
            "--gate",
            "6b",
            "--cron-hour",
            "8",
            "--repo-dir",
            "/srv/quant-bot",
            "--db",
            "data/quant.db",
            "--env-file",
            ".env",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "5 8 * * *" in out
    assert "cd /srv/quant-bot" in out
    assert "docker compose logs --no-color" in out
    assert "tools/daily_gate_digest.py" in out
    assert "--gate 6b" in out
    assert "TELEGRAM_BOT_TOKEN" not in out


def test_empty_db_reports_day_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    db = Database(db_path)
    db.connect()
    db.migrate()
    db.close()

    prog = compute_gate_day_progress(db_path, "6a")
    assert prog.current_day == 0
    assert prog.target_days == 7
    assert prog.elapsed_days is None

    # Also validate build_cron_example with restore_drill_report
    snippet = build_cron_example(
        repo_dir=Path("/opt/bot"),
        gate="6a",
        restore_drill_report="/tmp/restore_drill_6a.json",
    )
    assert "--restore-drill-report /tmp/restore_drill_6a.json" in snippet
