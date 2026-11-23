"""Tests for SQLite backup encryption check, offsite Telegram delivery (<50 MB), and restore CLI (Part B)."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.fernet import Fernet

from src.core.binance_client import ExchangeInfoCache, MonotonicClock, WeightGovernor
from src.core.config import (
    AnalystConfig,
    BinanceEnv,
    ConfigError,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_config,
    load_yaml_strict,
)
from src.core.database import Database, SymbolFilters
from src.core.offsite_backup import (
    OffsiteBackupError,
    main as offsite_cli_main,
    prepare_offsite_artifact,
    restore_offsite_artifact,
    upload_backup_via_telegram,
)
from src.main import QuantOrchestrator
from src.notifications.telegram import HttpTelegramTransport, TelegramOutbox
from src.radar.orderbook_inspector import OrderbookInspector


def _sol_filters(symbol: str = "SOLUSDT") -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        status="TRADING",
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("5.00"),
        oco_allowed=True,
        fetched_at_ms=1_700_000_000_000,
    )


def test_local_backup_to_is_plaintext_sqlite_not_encrypted(tmp_path: Path) -> None:
    """Confirms Database.backup_to() produces an unencrypted SQLite file (despite strategies.yaml:31 comment)."""
    db = Database(tmp_path / "live.db")
    db.connect()
    db.migrate()
    db.set_state("canary_key", "canary_value_plain")
    bkp = db.backup_to(tmp_path / "backups")
    db.close()

    assert Database.verify_backup(bkp) is True
    raw = bkp.read_bytes()
    assert raw.startswith(b"SQLite format 3\x00")
    assert b"canary_value_plain" in raw


def test_offsite_backup_disabled_by_default_and_invalid_key_rejected(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env_default = tmp_path / ".env.default"
    env_default.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "TELEGRAM_CHAT_ID=1\n",
        encoding="utf-8",
    )
    cfg = load_config(
        env_file=env_default,
        strategies_path=repo_root / "config" / "strategies.yaml",
        analyst_path=repo_root / "config" / "analyst.yaml",
    )
    assert cfg.runtime.offsite_backup_enabled is False
    assert cfg.runtime.backup_encryption_key is None

    env_bad_key = tmp_path / ".env.badkey"
    env_bad_key.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "TELEGRAM_CHAT_ID=1\n"
        "BACKUP_ENCRYPTION_KEY=not-a-valid-fernet-key\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(
            env_file=env_bad_key,
            strategies_path=repo_root / "config" / "strategies.yaml",
            analyst_path=repo_root / "config" / "analyst.yaml",
        )


def test_offsite_backup_enabled_without_encryption_key_fails_boot(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    # Empty BACKUP_ENCRYPTION_KEY with OFFSITE_BACKUP_ENABLED=true -> boot fails with clear error
    env_empty_key = tmp_path / ".env.offsite_empty_key"
    env_empty_key.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "TELEGRAM_CHAT_ID=1\n"
        "OFFSITE_BACKUP_ENABLED=true\n"
        "BACKUP_ENCRYPTION_KEY=\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"OFFSITE_BACKUP_ENABLED=true.*BACKUP_ENCRYPTION_KEY"):
        load_config(
            env_file=env_empty_key,
            strategies_path=repo_root / "config" / "strategies.yaml",
            analyst_path=repo_root / "config" / "analyst.yaml",
        )

    # Omitted BACKUP_ENCRYPTION_KEY with OFFSITE_BACKUP_ENABLED=true -> boot fails with clear error
    env_missing_key = tmp_path / ".env.offsite_missing_key"
    env_missing_key.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "TELEGRAM_CHAT_ID=1\n"
        "OFFSITE_BACKUP_ENABLED=true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"OFFSITE_BACKUP_ENABLED=true.*BACKUP_ENCRYPTION_KEY"):
        load_config(
            env_file=env_missing_key,
            strategies_path=repo_root / "config" / "strategies.yaml",
            analyst_path=repo_root / "config" / "analyst.yaml",
        )

    # Valid Fernet key with OFFSITE_BACKUP_ENABLED=true -> boot succeeds
    valid_key = Fernet.generate_key().decode("ascii")
    env_ok = tmp_path / ".env.offsite_ok"
    env_ok.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "TELEGRAM_CHAT_ID=1\n"
        "OFFSITE_BACKUP_ENABLED=true\n"
        f"BACKUP_ENCRYPTION_KEY={valid_key}\n",
        encoding="utf-8",
    )
    cfg = load_config(
        env_file=env_ok,
        strategies_path=repo_root / "config" / "strategies.yaml",
        analyst_path=repo_root / "config" / "analyst.yaml",
    )
    assert cfg.runtime.offsite_backup_enabled is True
    assert cfg.runtime.backup_encryption_key is not None
    assert cfg.runtime.backup_encryption_key.get_secret_value() == valid_key


def test_prepare_upload_and_restore_encrypted_and_gzip_backups_never_bundle_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "quant.db")
    db.connect()
    db.migrate()
    db.set_state("restore_probe", "exact_state_42")
    bkp = db.backup_to(tmp_path / "backups")
    db.close()

    # Refuses sensitive files / non-.db paths
    secret_env = tmp_path / ".env"
    secret_env.write_text("BINANCE_API_KEY=leak\n", encoding="utf-8")
    with pytest.raises(OffsiteBackupError):
        prepare_offsite_artifact(secret_env)

    secret_dir_db = tmp_path / "secrets" / "keys.db"
    secret_dir_db.parent.mkdir(parents=True, exist_ok=True)
    secret_dir_db.write_bytes(bkp.read_bytes())
    with pytest.raises(OffsiteBackupError):
        prepare_offsite_artifact(secret_dir_db)

    # 1. Encrypted Fernet artifact: ciphertext has no plaintext SQLite header or state string
    key = Fernet.generate_key().decode("ascii")
    enc_artifact, is_enc, enc_size = prepare_offsite_artifact(bkp, encryption_key=key)
    assert is_enc is True
    assert enc_artifact.name.endswith(".db.gz.enc")
    enc_bytes = enc_artifact.read_bytes()
    assert enc_size == len(enc_bytes)
    assert b"SQLite format 3" not in enc_bytes
    assert b"exact_state_42" not in enc_bytes

    # Wrong key fails restoration
    with pytest.raises(OffsiteBackupError):
        restore_offsite_artifact(
            enc_artifact,
            tmp_path / "restored_bad.db",
            encryption_key=Fernet.generate_key().decode("ascii"),
        )

    # Right key restores and clears stale -wal/-shm sidecars
    restored_enc_path = tmp_path / "restored_enc" / "quant.db"
    restored_enc_path.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{restored_enc_path}-wal").write_bytes(b"stale-wal")
    Path(f"{restored_enc_path}-shm").write_bytes(b"stale-shm")
    rc = offsite_cli_main(["restore", str(enc_artifact), str(restored_enc_path), "--key", key])
    assert rc == 0
    assert not Path(f"{restored_enc_path}-wal").exists()
    assert not Path(f"{restored_enc_path}-shm").exists()
    db_r = Database(restored_enc_path)
    db_r.connect()
    assert db_r.get_state("restore_probe") == "exact_state_42"
    db_r.close()
    enc_artifact.unlink()

    # 2. Gzip-only artifact + HttpTelegramTransport.send_document mocked
    uploaded: list[dict[str, Any]] = []
    transport = HttpTelegramTransport("123:TOKEN", timeout_s=5.0)

    def _fake_post(url: str, *, data: Any = None, files: Any = None, timeout: float = 0.0) -> MagicMock:
        fname, fh, _ctype = files["document"]
        content = fh.read()
        uploaded.append({"url": url, "data": data, "filename": fname, "bytes": content, "timeout": timeout})
        resp = MagicMock(spec=requests.Response)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"ok": True, "result": {"message_id": 777}}
        return resp

    monkeypatch.setattr(transport._session, "post", _fake_post)
    meta = upload_backup_via_telegram(
        bkp,
        bot_token="123:TOKEN",
        chat_id="-100999",
        mode="SHADOW_CHAMELEON",
        encryption_key=key,
        transport=transport,
    )
    assert meta["message_id"] == 777
    assert meta["encrypted"] is True
    assert len(uploaded) == 1
    assert uploaded[0]["url"].endswith("/sendDocument")
    assert uploaded[0]["data"]["chat_id"] == "-100999"
    assert uploaded[0]["filename"].endswith(".db.gz.enc")
    # Temporary artifact cleaned up while original .db remains intact
    assert bkp.is_file()
    assert not bkp.with_suffix(".db.gz.enc").exists()

    # Restore directly from the exact bytes received by Telegram
    downloaded = tmp_path / "downloaded_from_tg.db.gz.enc"
    downloaded.write_bytes(uploaded[0]["bytes"])
    restored_tg = restore_offsite_artifact(downloaded, tmp_path / "restored_tg.db", encryption_key=key)
    assert Database.verify_backup(restored_tg) is True


def test_prepare_offsite_artifact_enforces_50mb_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "big.db")
    db.connect()
    db.migrate()
    bkp = db.backup_to(tmp_path / "backups")
    db.close()

    with pytest.raises(OffsiteBackupError, match="exceeds limit"):
        prepare_offsite_artifact(bkp, max_bytes=10)
    assert not bkp.with_suffix(".db.gz").exists()


def test_run_once_offsite_upload_failure_logs_and_alerts_without_crashing_cycle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    db = Database(tmp_path / "offsite_fail.db")
    db.connect()
    db.migrate()

    now_ms = 1_700_500_003_000

    class _PubStub:
        governor = WeightGovernor()

        def get_server_time_ms(self) -> int:
            return now_ms

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

    class _FailingDocTransport:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.doc_attempts = 0

        def send_message(self, *, chat_id: str, text: str, reply_to_message_id: int | None = None) -> int:
            self.messages.append(text)
            return len(self.messages)

        def send_document(self, *, chat_id: str, file_path: Path, caption: str | None = None) -> int:
            self.doc_attempts += 1
            raise requests.Timeout("Telegram upload timed out")

    pub = _PubStub()
    clock = MonotonicClock()
    clock.sync(pub.get_server_time_ms)
    all_syms = list(dict.fromkeys(list(st.universe.symbols) + list(an.scanner_universe.symbols) + ["BNBUSDT"]))
    ex_cache = ExchangeInfoCache(pub, db, all_syms)  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(now_ms)
    transport = _FailingDocTransport()

    settings = RuntimeSettings(
        mode=RunMode.ANALYST_ONLY,
        binance_env=BinanceEnv.TESTNET,
        telegram_bot_token="123:TOKEN",
        telegram_chat_id="12345",
        db_path=tmp_path / "offsite_fail.db",
        backup_dir=tmp_path / "bkp",
        offsite_backup_enabled=True,
        backup_encryption_key=Fernet.generate_key().decode("ascii"),
    )
    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st,
        analyst_cfg=an,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=None,
        clock=clock,
        exchange_cache=ex_cache,
        outbox=TelegramOutbox(db, "12345", transport=transport),  # type: ignore[arg-type]
        orderbook_inspector=OrderbookInspector(an.layer_4_orderbook_live, sleep_fn=lambda _: None),
        sleep_fn=lambda _: None,
    )

    with caplog.at_level(logging.ERROR, logger="quant_orchestrator"):
        rep = orch.run_once(now_ms=now_ms, skip_collect=True)

    with db.read() as conn:
        err_row = conn.execute(
            "SELECT kind, payload FROM telegram_outbox WHERE kind = 'BACKUP_ERROR'"
        ).fetchone()
    db.close()

    assert rep.backup_created is not None and Database.verify_backup(rep.backup_created)
    assert rep.unhandled_errors == 0  # Offsite upload failure never crashes the cycle!
    assert transport.doc_attempts == 1
    assert err_row is not None and "backup offsite" in str(err_row["payload"]).lower()
    assert any("Offsite SQLite backup upload failed" in r.getMessage() for r in caplog.records)
