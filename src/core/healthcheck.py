"""External dead-man switch heartbeat and Docker container HEALTHCHECK probe."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

logger = logging.getLogger("healthcheck")

DEFAULT_PING_TIMEOUT_S: float = 3.0
DEFAULT_MAX_STALE_SECONDS: int = 9000  # 2h30: tolerates 1h cycle + collection retries without false positive


def ping_healthcheck(
    url: str | None,
    *,
    timeout_s: float = DEFAULT_PING_TIMEOUT_S,
) -> bool:
    """
    Send a minimal, secret-free GET ping to an external heartbeat endpoint (e.g. Healthchecks.io).

    Contract:
    - Disabled when `url` is None or blank (returns False without network I/O).
    - Never raises: timeouts, connection errors, and non-2xx HTTP statuses are caught and logged
      without exposing URL path tokens or credentials.
    - Sends no secrets, cookies, or auth headers.
    """
    if url is None or not url.strip():
        return False

    cleaned = url.strip()
    host = urlsplit(cleaned).hostname or "<unknown-host>"
    try:
        resp = requests.get(
            cleaned,
            timeout=float(timeout_s),
            headers={"User-Agent": "quant-crypto-heartbeat/1.0"},
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning(
            "External healthcheck ping failed (host=%s, error=%s)",
            host,
            type(exc).__name__,
        )
        return False


def check_container_health(
    db_path: Path,
    *,
    max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS,
    now_ms: int | None = None,
) -> tuple[bool, str]:
    """
    Read-only Docker HEALTHCHECK probe (respects ADR-002 single-writer).

    Verifies:
    1. SQLite database exists, is readable in read-only mode, and contains schema_meta.version.
    2. If at least one hourly cycle has been logged in `cycle_health`, the most recent cycle
       is not older than `max_stale_seconds`.
    """
    path = Path(db_path)
    if not path.is_file():
        return False, f"database file not found: {path}"

    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only=ON")
        ver_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        if ver_row is None:
            return False, "schema_meta version missing"

        row = conn.execute(
            "SELECT MAX(cycle_close_time_ms) FROM cycle_health"
        ).fetchone()
        if row is not None and row[0] is not None:
            last_close_ms = int(row[0])
            ref_now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
            age_s = (ref_now_ms - last_close_ms) / 1000.0
            if age_s > float(max_stale_seconds):
                return False, f"last cycle is stale ({age_s:.0f}s > {max_stale_seconds}s)"
        return True, "ok"
    except sqlite3.Error as exc:
        return False, f"sqlite healthcheck error: {exc}"
    finally:
        if conn is not None:
            conn.close()


def main() -> int:
    db_path = Path(os.environ.get("DB_PATH", "/app/data/quant.db"))
    max_stale_s = int(os.environ.get("HEALTHCHECK_MAX_STALE_SECONDS", str(DEFAULT_MAX_STALE_SECONDS)))
    healthy, msg = check_container_health(db_path, max_stale_seconds=max_stale_s)
    if not healthy:
        print(f"UNHEALTHY: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
