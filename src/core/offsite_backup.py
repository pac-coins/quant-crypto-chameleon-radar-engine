"""Offsite SQLite backup export (gzip + optional Fernet encryption) via Telegram Bot API (C2-8) and restore CLI."""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken

from src.core.database import Database
from src.notifications.telegram import HttpTelegramTransport

logger = logging.getLogger("offsite_backup")

# Telegram Bot API sendDocument limit (docs/integration-audit.md C2-8)
TELEGRAM_MAX_DOCUMENT_BYTES: Final[int] = 50 * 1024 * 1024

_FORBIDDEN_NAME_PARTS: Final[tuple[str, ...]] = (".env", "secrets", ".pem", ".key")


class OffsiteBackupError(RuntimeError):
    """Raised when an offsite backup artifact fails validation, size limit, encryption, or restoration."""


def _assert_safe_sqlite_backup_file(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_file():
        raise OffsiteBackupError(f"Backup file does not exist: {resolved}")
    if resolved.suffix.lower() != ".db":
        raise OffsiteBackupError(f"Refusing to export non-.db file: {resolved.name}")
    lowered_parts = [p.lower() for p in resolved.parts]
    for bad in _FORBIDDEN_NAME_PARTS:
        if any(bad in part for part in lowered_parts):
            raise OffsiteBackupError(f"Refusing to export sensitive path: {resolved.name}")


def prepare_offsite_artifact(
    verified_backup_path: Path,
    *,
    encryption_key: str | None = None,
    max_bytes: int = TELEGRAM_MAX_DOCUMENT_BYTES,
) -> tuple[Path, bool, int]:
    """
    Compress (gzip) and optionally encrypt (Fernet AES-128-CBC + HMAC-SHA256) a single verified
    SQLite `.db` backup file for offsite transfer.

    Never bundles `.env`, `secrets/`, or key files. Raises OffsiteBackupError if
    `Database.verify_backup()` fails or the resulting artifact exceeds `max_bytes` (50 MB).
    """
    src = Path(verified_backup_path)
    _assert_safe_sqlite_backup_file(src)
    if not Database.verify_backup(src):
        raise OffsiteBackupError(f"Backup failed Database.verify_backup(): {src.name}")

    gz_path = src.with_suffix(src.suffix + ".gz")
    try:
        with src.open("rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    except BaseException:
        if gz_path.exists():
            try:
                gz_path.unlink()
            except OSError:
                pass
        raise

    artifact_path = gz_path
    encrypted = False
    if encryption_key is not None and encryption_key.strip():
        enc_path = src.with_suffix(src.suffix + ".gz.enc")
        try:
            fernet = Fernet(encryption_key.strip().encode("ascii"))
            enc_bytes = fernet.encrypt(gz_path.read_bytes())
            enc_path.write_bytes(enc_bytes)
            artifact_path = enc_path
            encrypted = True
        except BaseException:
            if enc_path.exists():
                try:
                    enc_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            if gz_path.exists():
                try:
                    gz_path.unlink()
                except OSError:
                    pass
    else:
        logger.warning(
            "BACKUP_ENCRYPTION_KEY is empty; exporting gzip-only SQLite backup (%s) without encryption.",
            gz_path.name,
        )

    size_bytes = artifact_path.stat().st_size
    if size_bytes > int(max_bytes):
        if artifact_path.exists():
            artifact_path.unlink()
        raise OffsiteBackupError(
            f"Offsite backup artifact {artifact_path.name} is {size_bytes} bytes "
            f"(exceeds limit of {max_bytes} bytes — check C2-8)."
        )

    return artifact_path, encrypted, size_bytes


def upload_backup_via_telegram(
    verified_backup_path: Path,
    *,
    bot_token: str,
    chat_id: str,
    mode: str,
    encryption_key: str | None = None,
    transport: Any | None = None,
    max_bytes: int = TELEGRAM_MAX_DOCUMENT_BYTES,
) -> dict[str, Any]:
    """
    Prepare and send a verified SQLite backup to Telegram (`sendDocument`).
    Cleans up temporary `.gz`/`.enc` files in `finally` while keeping the local `.db` intact.
    """
    artifact_path, encrypted, size_bytes = prepare_offsite_artifact(
        verified_backup_path,
        encryption_key=encryption_key,
        max_bytes=max_bytes,
    )
    try:
        sender = (
            transport
            if (transport is not None and callable(getattr(transport, "send_document", None)))
            else HttpTelegramTransport(bot_token, timeout_s=30.0)
        )
        enc_label = "Fernet AES+HMAC" if encrypted else "gzip (sem criptografia)"
        size_kb = size_bytes / 1024.0
        caption = (
            f"💾 SQLite Backup [{mode}]\n"
            f"Arquivo: {artifact_path.name} ({size_kb:.1f} KB)\n"
            f"Protecao: {enc_label} | verify_backup=OK"
        )
        msg_id = int(
            sender.send_document(
                chat_id=chat_id,
                file_path=artifact_path,
                caption=caption,
            )
        )
        return {
            "message_id": msg_id,
            "filename": artifact_path.name,
            "size_bytes": size_bytes,
            "encrypted": encrypted,
        }
    finally:
        if artifact_path.exists():
            try:
                artifact_path.unlink()
            except OSError:
                pass


def restore_offsite_artifact(
    artifact_path: Path,
    dest_db_path: Path,
    *,
    encryption_key: str | None = None,
) -> Path:
    """
    Restore a `.db.gz.enc`, `.db.gz`, or `.db` backup artifact to `dest_db_path`,
    validating `Database.verify_backup()` before atomically replacing the target file
    and clearing stale `-wal`/`-shm` sidecars.
    """
    src = Path(artifact_path)
    dst = Path(dest_db_path)
    if not src.is_file():
        raise OffsiteBackupError(f"Backup artifact not found: {src}")

    raw = src.read_bytes()
    name_lower = src.name.lower()

    if name_lower.endswith(".enc"):
        if not encryption_key or not encryption_key.strip():
            raise OffsiteBackupError(
                "Encrypted backup (.enc) requires BACKUP_ENCRYPTION_KEY (or --key)."
            )
        try:
            raw = Fernet(encryption_key.strip().encode("ascii")).decrypt(raw)
        except (InvalidToken, ValueError) as exc:
            raise OffsiteBackupError("Failed to decrypt backup: invalid BACKUP_ENCRYPTION_KEY or corrupted file.") from exc
        name_lower = name_lower[:-4]

    if name_lower.endswith(".gz"):
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise OffsiteBackupError(f"Failed to decompress gzip backup: {exc}") from exc

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst.with_name(f".{dst.name}.restore.tmp")
    try:
        tmp_path.write_bytes(raw)
        if not Database.verify_backup(tmp_path):
            raise OffsiteBackupError(
                f"Restored SQLite failed Database.verify_backup() (PRAGMA integrity_check / schema_meta): {src.name}"
            )
        os.replace(tmp_path, dst)
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = Path(f"{dst}{sidecar_suffix}")
            if sidecar.exists():
                sidecar.unlink()
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    return dst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.core.offsite_backup",
        description="Restore and verify a QuantCrypto SQLite backup (.db, .db.gz, or .db.gz.enc).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_restore = sub.add_parser("restore", help="Restore an offsite backup file to a SQLite database path.")
    p_restore.add_argument("artifact", type=Path, help="Path to downloaded .db, .db.gz, or .db.gz.enc file.")
    p_restore.add_argument("dest_db", type=Path, help="Destination SQLite path (e.g. ./data/quant.db).")
    p_restore.add_argument(
        "--key",
        default=None,
        help="Fernet key (defaults to BACKUP_ENCRYPTION_KEY env var).",
    )

    args = parser.parse_args(argv)
    if args.command == "restore":
        key = args.key or os.environ.get("BACKUP_ENCRYPTION_KEY")
        try:
            out = restore_offsite_artifact(args.artifact, args.dest_db, encryption_key=key)
        except OffsiteBackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"OK: backup restored and verified at {out}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
