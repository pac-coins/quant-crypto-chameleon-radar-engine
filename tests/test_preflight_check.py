"""Tests for tools/preflight_check.py (read-only gate transition validator)."""

from __future__ import annotations

import json
import os
from collections import namedtuple
from pathlib import Path

from cryptography.fernet import Fernet

from src.core.config import PHASE_1_LIVE_CONFIRMATION, sha256_file
from src.features.ml_judge import MLJudgeArtifact
from tools.preflight_check import main as preflight_main
from tools.preflight_check import run_preflight_check

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def _write_valid_artifact(path: Path, *, ece: float = 0.025) -> Path:
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
        weights=(0.0, 0.0, 0.0, -1.0, 0.0, 0.5),
        intercept=0.2,
        platt_a=-1.0,
        platt_b=0.0,
        ece_out_of_sample=ece,
        net_r_q33_threshold=0.05,
        net_r_q70_threshold=0.25,
        trained_samples=120,
        validation_samples=60,
        created_at_utc="2026-11-24T00:00:00+00:00",
    ).with_computed_sha256()
    return art.save(path)


def test_preflight_phase1_production_full_pass_with_app_path_mapping(tmp_path: Path) -> None:
    """Validates Phase 1 PRODUCTION with Docker /app/... paths mapped cleanly to host_root."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "data").mkdir()

    key_file = tmp_path / "secrets" / "ed25519.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n", encoding="utf-8")
    os.chmod(key_file, 0o600)

    _write_valid_artifact(tmp_path / "models" / "ml_judge_bull_v1.json", ece=0.02)
    fernet_key = Fernet.generate_key().decode("utf-8")
    st_sha = sha256_file(Path("config/strategies.yaml"))

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MODE=LIVE_CHAMELEON_PHASE_1",
                "BINANCE_ENV=PRODUCTION",
                "BINANCE_API_KEY=secret_api_key_do_not_leak",
                "BINANCE_ED25519_PRIVATE_KEY_PATH=/app/secrets/ed25519.pem",
                f"LIVE_CONFIRMATION={PHASE_1_LIVE_CONFIRMATION}",
                f"APPROVED_STRATEGIES_SHA256={st_sha}",
                "TELEGRAM_BOT_TOKEN=12345:super_secret_tg_token",
                "TELEGRAM_CHAT_ID=99999",
                "DB_PATH=/app/data/quant.db",
                "BACKUP_DIR=/app/data/backups",
                "BACKUP_RETENTION_DAYS=14",
                "WATCHDOG_POLL_INTERVAL_SECONDS=20",
                "HEALTHCHECK_URL=https://hc-ping.com/secret-uuid-12345",
                "OFFSITE_BACKUP_ENABLED=true",
                f"BACKUP_ENCRYPTION_KEY={fernet_key}",
                "ML_JUDGE_ARTIFACT_PATH=/app/models/ml_judge_bull_v1.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rep = run_preflight_check(
        env_file=env_file,
        target_gate="phase1",
        host_root=tmp_path,
        disk_usage_fn=lambda _: _DiskUsage(total=100 * 1024**3, used=40 * 1024**3, free=60 * 1024**3),
    )
    assert rep["overall"] == "PASS"
    assert rep["summary"]["fail"] == 0
    assert rep["summary"]["warn"] == 0

    # Ensure zero secrets appear anywhere in serialized output
    serialized = json.dumps(rep)
    assert "secret_api_key_do_not_leak" not in serialized
    assert "super_secret_tg_token" not in serialized
    assert "secret-uuid-12345" not in serialized
    assert fernet_key not in serialized


def test_preflight_blocks_insecure_production_defaults_and_bad_permissions(tmp_path: Path) -> None:
    """Production without HEALTHCHECK_URL, OFFSITE_BACKUP_ENABLED, or ML_JUDGE_ARTIFACT_PATH must FAIL."""
    key_file = tmp_path / "ed25519.pem"
    key_file.write_text("dummy-key", encoding="utf-8")
    os.chmod(key_file, 0o644)  # overly permissive -> WARN

    st_sha = sha256_file(Path("config/strategies.yaml"))
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MODE=LIVE_CHAMELEON_PHASE_1",
                "BINANCE_ENV=PRODUCTION",
                "BINANCE_API_KEY=k",
                f"BINANCE_ED25519_PRIVATE_KEY_PATH={key_file}",
                f"LIVE_CONFIRMATION={PHASE_1_LIVE_CONFIRMATION}",
                f"APPROVED_STRATEGIES_SHA256={st_sha}",
                "TELEGRAM_BOT_TOKEN=tg",
                "TELEGRAM_CHAT_ID=1",
                "OFFSITE_BACKUP_ENABLED=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rep = run_preflight_check(
        env_file=env_file,
        target_gate="phase1",
        host_root=tmp_path,
        disk_usage_fn=lambda _: _DiskUsage(total=100 * 1024**3, used=90 * 1024**3, free=10 * 1024**3),
    )
    assert rep["overall"] == "FAIL"
    by_id = {c["check_id"]: c["status"] for c in rep["checks"]}
    assert by_id["PF-04"] == "WARN"  # chmod 0644
    assert by_id["PF-05"] == "FAIL"  # missing HEALTHCHECK_URL
    assert by_id["PF-06"] == "FAIL"  # OFFSITE_BACKUP_ENABLED=false
    assert by_id["PF-07"] == "FAIL"  # missing ML_JUDGE_ARTIFACT_PATH
    assert by_id["PF-09"] == "FAIL"  # disk 90% >= 85%


def test_preflight_gate_6b_shadow_mode_cli_and_json_out(tmp_path: Path) -> None:
    """Gate 6B (SHADOW_CHAMELEON) without Binance keys passes non-strict CLI and writes JSON."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MODE=SHADOW_CHAMELEON",
                "TELEGRAM_BOT_TOKEN=tg",
                "TELEGRAM_CHAT_ID=1",
                "HEALTHCHECK_URL=https://hc-ping.com/ok",
                f"BACKUP_ENCRYPTION_KEY={Fernet.generate_key().decode('utf-8')}",
                "OFFSITE_BACKUP_ENABLED=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_json = tmp_path / "preflight.json"
    rc = preflight_main(
        [
            "--env-file",
            str(env_file),
            "--target-gate",
            "6b",
            "--host-root",
            str(tmp_path),
            "--json-out",
            str(out_json),
        ]
    )
    assert rc == 0  # WARN on optional ML artifact in 6B does not fail non-strict
    data = json.loads(out_json.read_text(encoding="utf-8"))
    by_id = {c["check_id"]: c["status"] for c in data["checks"]}
    assert by_id["PF-01"] == "PASS"
    assert by_id["PF-02"] == "PASS"
    assert by_id["PF-04"] == "PASS"
    assert by_id["PF-07"] == "WARN"
