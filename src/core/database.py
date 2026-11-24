from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, Iterator, Sequence

import pandas as pd

from src.core.config import Interval

SCHEMA_VERSION: Final[int] = 1
PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
    "PRAGMA wal_autocheckpoint=1000",
)

SCHEMA_DDL: Final[str] = """
-- ===================== Metadados e estado =====================
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS system_state (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
) WITHOUT ROWID;

-- ===================== Dados de mercado =====================
CREATE TABLE IF NOT EXISTS candles (
    symbol                 TEXT    NOT NULL,
    interval               TEXT    NOT NULL CHECK (interval IN ('1m','1h','4h')),
    open_time_ms           INTEGER NOT NULL,
    close_time_ms          INTEGER NOT NULL,
    open                   REAL    NOT NULL,
    high                   REAL    NOT NULL,
    low                    REAL    NOT NULL,
    close                  REAL    NOT NULL,
    volume                 REAL    NOT NULL,
    quote_volume           REAL    NOT NULL,
    number_of_trades       INTEGER NOT NULL,
    taker_buy_base_volume  REAL    NOT NULL,
    taker_buy_quote_volume REAL    NOT NULL,
    ingested_at_ms         INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, open_time_ms),
    CHECK (close_time_ms > open_time_ms),
    CHECK (high >= low AND volume >= 0 AND taker_buy_base_volume <= volume + 1e-9)
) WITHOUT ROWID;
CREATE UNIQUE INDEX IF NOT EXISTS ux_candles_close ON candles(symbol, interval, close_time_ms);

CREATE TABLE IF NOT EXISTS exchange_filters (
    symbol        TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    base_asset    TEXT NOT NULL,
    quote_asset   TEXT NOT NULL,
    tick_size     TEXT NOT NULL,
    step_size     TEXT NOT NULL,
    min_qty       TEXT NOT NULL,
    min_notional  TEXT NOT NULL,
    oco_allowed   INTEGER NOT NULL CHECK (oco_allowed IN (0,1)),
    fetched_at_ms INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS regime_history (
    symbol             TEXT    NOT NULL,
    close_time_4h_ms   INTEGER NOT NULL,
    regime             TEXT    NOT NULL CHECK (regime IN ('BEAR','BULL','SIDEWAYS','TRANSITION')),
    close_4h           REAL,
    ema100_4h          REAL,
    ema100_slope_4h    REAL,
    chop14_4h          REAL,
    adx14_4h           REAL,
    fallback_reason    TEXT,
    PRIMARY KEY (symbol, close_time_4h_ms)
) WITHOUT ROWID;

-- ===================== Radar: sinais virtuais e livro =====================
CREATE TABLE IF NOT EXISTS virtual_signals (
    signal_id             TEXT PRIMARY KEY,
    symbol                TEXT    NOT NULL,
    signal_close_time_ms  INTEGER NOT NULL,
    emitted_at_ms         INTEGER NOT NULL,
    setup                 TEXT    NOT NULL,
    regime_4h             TEXT    NOT NULL CHECK (regime_4h IN ('BEAR','BULL','SIDEWAYS','TRANSITION')),
    score_total           INTEGER NOT NULL CHECK (score_total BETWEEN 0 AND 100),
    score_l1              INTEGER NOT NULL, score_l2 INTEGER NOT NULL,
    score_l3              INTEGER NOT NULL, score_l4 INTEGER NOT NULL,
    grade                 TEXT    NOT NULL CHECK (grade IN ('B','A','A+')),
    entry_ref_price       REAL    NOT NULL,
    candle_close_price    REAL    NOT NULL,
    stop_price            REAL    NOT NULL,
    target1_price         REAL    NOT NULL,
    target2_price         REAL    NOT NULL,
    atr14                 REAL    NOT NULL,
    horizon_end_ms        INTEGER NOT NULL,
    cost_roundtrip_pct    REAL    NOT NULL,
    features_json         TEXT    NOT NULL,
    ob_valid              INTEGER NOT NULL CHECK (ob_valid IN (0,1)),
    ob_band_eff_pct       REAL,
    ob_obi_mean           REAL,
    ob_bid_wall_price     REAL, ob_bid_wall_notional REAL,
    ob_ask_wall_price     REAL, ob_ask_wall_notional REAL,
    ob_ask_wall_before_t2 INTEGER CHECK (ob_ask_wall_before_t2 IN (0,1)),
    telegram_message_id   INTEGER,
    status                TEXT    NOT NULL DEFAULT 'OPEN'
                          CHECK (status IN ('OPEN','T1_THEN_T2','T1_THEN_STOP','T1_THEN_TIME','T2','STOP','TIME','INVALID_GAP')),
    same_candle_ambiguity INTEGER NOT NULL DEFAULT 0,
    resolved_at_ms        INTEGER,
    outcome_r_gross       REAL,
    outcome_r_net         REAL,
    mfe_r                 REAL,
    mae_r                 REAL,
    UNIQUE (symbol, signal_close_time_ms, setup)
);
CREATE INDEX IF NOT EXISTS ix_vs_open ON virtual_signals(status) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id         TEXT    NOT NULL REFERENCES virtual_signals(signal_id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL CHECK (seq IN (0,1,2)),
    symbol            TEXT    NOT NULL,
    requested_limit   INTEGER NOT NULL,
    taken_at_ms       INTEGER NOT NULL,
    last_update_id    INTEGER NOT NULL,
    best_bid          REAL    NOT NULL,
    best_ask          REAL    NOT NULL,
    mid               REAL    NOT NULL,
    spread_bps        REAL    NOT NULL,
    bid_coverage_pct  REAL    NOT NULL,
    ask_coverage_pct  REAL    NOT NULL,
    band_eff_pct      REAL    NOT NULL,
    bid_notional_band REAL    NOT NULL,
    ask_notional_band REAL    NOT NULL,
    obi               REAL,
    impact_bps_ref    REAL,
    buckets_json      TEXT    NOT NULL,
    UNIQUE (signal_id, seq)
);

CREATE TABLE IF NOT EXISTS orderbook_walls (
    signal_id         TEXT    NOT NULL REFERENCES virtual_signals(signal_id) ON DELETE CASCADE,
    side              TEXT    NOT NULL CHECK (side IN ('BID','ASK')),
    bucket_low        REAL    NOT NULL,
    bucket_high       REAL    NOT NULL,
    notional_mean     REAL    NOT NULL,
    median_multiple   REAL    NOT NULL,
    persistence       INTEGER NOT NULL CHECK (persistence BETWEEN 1 AND 3),
    PRIMARY KEY (signal_id, side, bucket_low)
) WITHOUT ROWID;

-- ===================== Chameleon: shadow e live =====================
CREATE TABLE IF NOT EXISTS shadow_trades (
    shadow_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                TEXT    NOT NULL,
    strategy              TEXT    NOT NULL,
    regime                TEXT    NOT NULL,
    signal_close_time_ms  INTEGER NOT NULL,
    status                TEXT    NOT NULL CHECK (status IN ('OPEN','CLOSED')),
    decision_ask_price    REAL    NOT NULL,
    sim_entry_price       REAL    NOT NULL,
    stop_price            REAL    NOT NULL,
    target_price          REAL    NOT NULL,
    qty                   TEXT    NOT NULL,
    risk_pct              REAL    NOT NULL,
    risk_usdt             TEXT    NOT NULL,
    virtual_equity_before TEXT    NOT NULL,
    time_barrier_end_ms   INTEGER NOT NULL,
    exit_reason           TEXT CHECK (exit_reason IN ('TARGET','STOP','TIME','SAME_CANDLE_STOP') OR exit_reason IS NULL),
    exit_price            REAL,
    r_gross               REAL,
    r_net                 REAL,
    ml_grade_silent       TEXT CHECK (ml_grade_silent IN ('A+','B','C') OR ml_grade_silent IS NULL),
    radar_signal_id       TEXT REFERENCES virtual_signals(signal_id),
    created_at_ms         INTEGER NOT NULL,
    closed_at_ms          INTEGER,
    UNIQUE (strategy, symbol, signal_close_time_ms)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shadow_single_slot ON shadow_trades((1)) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS trades (
    trade_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id        TEXT    NOT NULL UNIQUE,
    environment            TEXT    NOT NULL CHECK (environment IN ('TESTNET','PRODUCTION')),
    symbol                 TEXT    NOT NULL,
    strategy               TEXT    NOT NULL,
    regime                 TEXT    NOT NULL,
    signal_close_time_ms   INTEGER NOT NULL,
    status                 TEXT    NOT NULL CHECK (status IN
                           ('PENDING_ENTRY','ENTRY_FILLED','PROTECTED','CLOSING','CLOSED','ENTRY_REJECTED','HALT_REVIEW')),
    planned_entry_price    TEXT    NOT NULL,
    planned_stop_price     TEXT    NOT NULL,
    planned_target_price   TEXT    NOT NULL,
    atr_at_signal          REAL    NOT NULL,
    risk_pct               REAL    NOT NULL,
    risk_usdt              TEXT    NOT NULL,
    quote_order_qty        TEXT,
    entry_order_id         INTEGER,
    entry_executed_qty     TEXT,
    entry_avg_price        TEXT,
    entry_quote_spent      TEXT,
    commission_base_asset  TEXT    NOT NULL DEFAULT '0',
    oco_order_list_id      INTEGER,
    oco_list_client_id     TEXT UNIQUE,
    oco_qty                TEXT,
    oco_stop_price         TEXT,
    oco_stop_limit_price   TEXT,
    oco_target_price       TEXT,
    time_barrier_end_ms    INTEGER NOT NULL,
    exit_reason            TEXT CHECK (exit_reason IN
                           ('TARGET','STOP','TIME','FALLBACK_MARKET_2010','STOP_WATCHDOG','RECONCILER') OR exit_reason IS NULL),
    exit_avg_price         TEXT,
    exit_quote_received    TEXT,
    fees_usdt_equiv        TEXT,
    realized_pnl_usdt      TEXT,
    realized_r             REAL,
    slippage_entry_bps     REAL,
    ml_grade_silent        TEXT CHECK (ml_grade_silent IN ('A+','B','C') OR ml_grade_silent IS NULL),
    ml_p_cal_silent        REAL,
    radar_signal_id        TEXT REFERENCES virtual_signals(signal_id),
    created_at_ms          INTEGER NOT NULL,
    updated_at_ms          INTEGER NOT NULL,
    closed_at_ms           INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_single_slot ON trades((1))
    WHERE status IN ('PENDING_ENTRY','ENTRY_FILLED','PROTECTED','CLOSING','HALT_REVIEW');
CREATE INDEX IF NOT EXISTS ix_trades_created ON trades(created_at_ms);

CREATE TABLE IF NOT EXISTS order_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER REFERENCES trades(trade_id),
    client_order_id TEXT,
    method          TEXT    NOT NULL,
    endpoint        TEXT    NOT NULL,
    http_status     INTEGER,
    binance_code    INTEGER,
    request_json    TEXT    NOT NULL,
    response_json   TEXT,
    ts_ms           INTEGER NOT NULL
);

-- ===================== Observabilidade =====================
CREATE TABLE IF NOT EXISTS cycle_health (
    cycle_close_time_ms INTEGER PRIMARY KEY,
    mode                TEXT    NOT NULL,
    clock_offset_ms     REAL    NOT NULL,
    clock_rtt_ms        REAL    NOT NULL,
    used_weight_1m_peak INTEGER NOT NULL,
    stale_symbols       TEXT    NOT NULL DEFAULT '[]',
    gaps_detected       INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER NOT NULL,
    unhandled_errors    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telegram_outbox (
    msg_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                TEXT    NOT NULL,
    payload             TEXT    NOT NULL,
    reply_to_signal_id  TEXT REFERENCES virtual_signals(signal_id),
    status              TEXT    NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    telegram_message_id INTEGER,
    created_at_ms       INTEGER NOT NULL,
    sent_at_ms          INTEGER
);
"""


class DatabaseError(RuntimeError):
    """Base error for SQLite database operations."""


class WrongThreadError(DatabaseError):
    """Raised when write_tx() is invoked from a thread other than the registered writer thread."""


@dataclass(frozen=True, slots=True)
class CandleRow:
    symbol: str
    interval: Interval
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    number_of_trades: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float
    ingested_at_ms: int


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    oco_allowed: bool
    fetched_at_ms: int


@dataclass(frozen=True, slots=True)
class CycleHealth:
    cycle_close_time_ms: int
    mode: str
    clock_offset_ms: float
    clock_rtt_ms: float
    used_weight_1m_peak: int
    stale_symbols: tuple[str, ...]
    gaps_detected: int
    duration_ms: int
    unhandled_errors: int


class Database:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._conn: sqlite3.Connection | None = None
        self._owner_thread_id: int | None = None
        self._in_write_tx: bool = False

    def _apply_pragmas(self, conn: sqlite3.Connection, *, read_only: bool = False) -> None:
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            mode_row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            actual_mode = str(mode_row[0]).lower() if mode_row else ""
            if actual_mode != "wal":
                raise DatabaseError(
                    f"Failed to enable SQLite WAL mode on {self.path} (got {actual_mode!r})."
                )
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")

    def connect(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._owner_thread_id = threading.get_ident()
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn, read_only=False)
        self._conn = conn

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        finally:
            self._conn.close()
            self._conn = None

    def _ensure_connected(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._ensure_connected()
        current_tid = threading.get_ident()
        if self._owner_thread_id is not None and current_tid != self._owner_thread_id:
            raise WrongThreadError(
                f"write_tx() called from thread {current_tid}, "
                f"but Database writer owner thread is {self._owner_thread_id} (ADR-002)."
            )
        if self._in_write_tx:
            yield conn
            return

        conn.execute("BEGIN IMMEDIATE")
        self._in_write_tx = True
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            self._in_write_tx = False

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        self._ensure_connected()
        if (
            self._owner_thread_id == threading.get_ident()
            and self._conn is not None
            and not self._in_write_tx
        ):
            yield self._conn
            return

        ro_conn = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        ro_conn.row_factory = sqlite3.Row
        try:
            self._apply_pragmas(ro_conn, read_only=True)
            ro_conn.execute("PRAGMA query_only=ON")
            yield ro_conn
        finally:
            ro_conn.close()

    def migrate(self) -> int:
        conn = self._ensure_connected()
        current_tid = threading.get_ident()
        if self._owner_thread_id is not None and current_tid != self._owner_thread_id:
            raise WrongThreadError("migrate() must run on the owner writer thread.")
        statements = [
            stmt.strip()
            for stmt in SCHEMA_DDL.split(";")
            if stmt.strip()
        ]
        with self.write_tx() as tx:
            for stmt in statements:
                tx.execute(stmt)
            tx.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        return SCHEMA_VERSION

    # --- candles ---
    def upsert_candles(self, rows: Sequence[CandleRow]) -> int:
        if not rows:
            return 0
        sql = """
            INSERT INTO candles (
                symbol, interval, open_time_ms, close_time_ms,
                open, high, low, close,
                volume, quote_volume, number_of_trades,
                taker_buy_base_volume, taker_buy_quote_volume, ingested_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, open_time_ms) DO UPDATE SET
                close_time_ms          = excluded.close_time_ms,
                open                   = excluded.open,
                high                   = excluded.high,
                low                    = excluded.low,
                close                  = excluded.close,
                volume                 = excluded.volume,
                quote_volume           = excluded.quote_volume,
                number_of_trades       = excluded.number_of_trades,
                taker_buy_base_volume  = excluded.taker_buy_base_volume,
                taker_buy_quote_volume = excluded.taker_buy_quote_volume,
                ingested_at_ms         = excluded.ingested_at_ms
        """
        params = [
            (
                r.symbol,
                r.interval,
                int(r.open_time_ms),
                int(r.close_time_ms),
                float(r.open),
                float(r.high),
                float(r.low),
                float(r.close),
                float(r.volume),
                float(r.quote_volume),
                int(r.number_of_trades),
                float(r.taker_buy_base_volume),
                float(r.taker_buy_quote_volume),
                int(r.ingested_at_ms),
            )
            for r in rows
        ]
        with self.write_tx() as tx:
            tx.executemany(sql, params)
        return len(params)

    def last_open_time(self, symbol: str, interval: Interval) -> int | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT MAX(open_time_ms) AS max_open FROM candles WHERE symbol = ? AND interval = ?",
                (symbol, interval),
            ).fetchone()
            if row is None or row["max_open"] is None:
                return None
            return int(row["max_open"])

    def load_candles(
        self,
        symbol: str,
        interval: Interval,
        *,
        limit: int,
        until_close_time_ms: int | None = None,
    ) -> pd.DataFrame:
        cols = [
            "symbol",
            "interval",
            "open_time_ms",
            "close_time_ms",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "number_of_trades",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
            "ingested_at_ms",
        ]
        if until_close_time_ms is None:
            sql = """
                SELECT symbol, interval, open_time_ms, close_time_ms,
                       open, high, low, close,
                       volume, quote_volume, number_of_trades,
                       taker_buy_base_volume, taker_buy_quote_volume, ingested_at_ms
                FROM (
                    SELECT * FROM candles
                    WHERE symbol = ? AND interval = ?
                    ORDER BY close_time_ms DESC
                    LIMIT ?
                )
                ORDER BY close_time_ms ASC
            """
            args: tuple[object, ...] = (symbol, interval, int(limit))
        else:
            sql = """
                SELECT symbol, interval, open_time_ms, close_time_ms,
                       open, high, low, close,
                       volume, quote_volume, number_of_trades,
                       taker_buy_base_volume, taker_buy_quote_volume, ingested_at_ms
                FROM (
                    SELECT * FROM candles
                    WHERE symbol = ? AND interval = ? AND close_time_ms <= ?
                    ORDER BY close_time_ms DESC
                    LIMIT ?
                )
                ORDER BY close_time_ms ASC
            """
            args = (symbol, interval, int(until_close_time_ms), int(limit))

        with self.read() as conn:
            rows = conn.execute(sql, args).fetchall()

        if not rows:
            df = pd.DataFrame(columns=cols)
        else:
            df = pd.DataFrame([dict(r) for r in rows], columns=cols)

        float_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ]
        int_cols = ["open_time_ms", "close_time_ms", "number_of_trades", "ingested_at_ms"]
        for c in float_cols:
            df[c] = df[c].astype("float64")
        for c in int_cols:
            df[c] = df[c].astype("int64")
        return df.reset_index(drop=True)

    def close_times(self, symbol: str, interval: Interval, since_ms: int) -> list[int]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT close_time_ms FROM candles "
                "WHERE symbol = ? AND interval = ? AND close_time_ms >= ? "
                "ORDER BY close_time_ms ASC",
                (symbol, interval, int(since_ms)),
            ).fetchall()
            return [int(r["close_time_ms"]) for r in rows]

    # --- exchange filters ---
    def upsert_exchange_filters(self, filters: Sequence[SymbolFilters]) -> None:
        if not filters:
            return
        sql = """
            INSERT INTO exchange_filters (
                symbol, status, base_asset, quote_asset,
                tick_size, step_size, min_qty, min_notional,
                oco_allowed, fetched_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                status        = excluded.status,
                base_asset    = excluded.base_asset,
                quote_asset   = excluded.quote_asset,
                tick_size     = excluded.tick_size,
                step_size     = excluded.step_size,
                min_qty       = excluded.min_qty,
                min_notional  = excluded.min_notional,
                oco_allowed   = excluded.oco_allowed,
                fetched_at_ms = excluded.fetched_at_ms
        """
        params = [
            (
                f.symbol,
                f.status,
                f.base_asset,
                f.quote_asset,
                format(f.tick_size, "f"),
                format(f.step_size, "f"),
                format(f.min_qty, "f"),
                format(f.min_notional, "f"),
                1 if f.oco_allowed else 0,
                int(f.fetched_at_ms),
            )
            for f in filters
        ]
        with self.write_tx() as tx:
            tx.executemany(sql, params)

    def get_exchange_filters(self, symbol: str) -> SymbolFilters | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM exchange_filters WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            if row is None:
                return None
            return SymbolFilters(
                symbol=str(row["symbol"]),
                status=str(row["status"]),
                base_asset=str(row["base_asset"]),
                quote_asset=str(row["quote_asset"]),
                tick_size=Decimal(str(row["tick_size"])),
                step_size=Decimal(str(row["step_size"])),
                min_qty=Decimal(str(row["min_qty"])),
                min_notional=Decimal(str(row["min_notional"])),
                oco_allowed=bool(row["oco_allowed"]),
                fetched_at_ms=int(row["fetched_at_ms"]),
            )

    # --- estado / saúde ---
    def get_state(self, key: str) -> str | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row is not None else None

    def set_state(self, key: str, value: str | Decimal) -> None:
        serialized = format(value, "f") if isinstance(value, Decimal) else str(value)
        now_ms = int(time.time() * 1000)
        with self.write_tx() as tx:
            tx.execute(
                "INSERT INTO system_state(key, value, updated_at_ms) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at_ms = excluded.updated_at_ms",
                (key, serialized, now_ms),
            )

    def log_cycle_health(self, h: CycleHealth) -> None:
        with self.write_tx() as tx:
            tx.execute(
                """
                INSERT INTO cycle_health (
                    cycle_close_time_ms, mode, clock_offset_ms, clock_rtt_ms,
                    used_weight_1m_peak, stale_symbols, gaps_detected,
                    duration_ms, unhandled_errors
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_close_time_ms) DO UPDATE SET
                    mode                = excluded.mode,
                    clock_offset_ms     = excluded.clock_offset_ms,
                    clock_rtt_ms        = excluded.clock_rtt_ms,
                    used_weight_1m_peak = excluded.used_weight_1m_peak,
                    stale_symbols       = excluded.stale_symbols,
                    gaps_detected       = excluded.gaps_detected,
                    duration_ms         = excluded.duration_ms,
                    unhandled_errors    = excluded.unhandled_errors
                """,
                (
                    int(h.cycle_close_time_ms),
                    str(h.mode),
                    float(h.clock_offset_ms),
                    float(h.clock_rtt_ms),
                    int(h.used_weight_1m_peak),
                    json.dumps(list(h.stale_symbols)),
                    int(h.gaps_detected),
                    int(h.duration_ms),
                    int(h.unhandled_errors),
                ),
            )

    def upsert_regime_history(
        self,
        *,
        symbol: str,
        close_time_4h_ms: int,
        regime: str,
        close_4h: float | None,
        ema100_4h: float | None,
        ema100_slope_4h: float | None,
        chop14_4h: float | None,
        adx14_4h: float | None,
        fallback_reason: str | None = None,
    ) -> None:
        with self.write_tx() as tx:
            tx.execute(
                """
                INSERT INTO regime_history (
                    symbol, close_time_4h_ms, regime, close_4h, ema100_4h,
                    ema100_slope_4h, chop14_4h, adx14_4h, fallback_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, close_time_4h_ms) DO UPDATE SET
                    regime          = excluded.regime,
                    close_4h        = excluded.close_4h,
                    ema100_4h       = excluded.ema100_4h,
                    ema100_slope_4h = excluded.ema100_slope_4h,
                    chop14_4h       = excluded.chop14_4h,
                    adx14_4h        = excluded.adx14_4h,
                    fallback_reason = excluded.fallback_reason
                """,
                (
                    str(symbol),
                    int(close_time_4h_ms),
                    str(regime),
                    close_4h,
                    ema100_4h,
                    ema100_slope_4h,
                    chop14_4h,
                    adx14_4h,
                    fallback_reason,
                ),
            )

    # --- backup (TODO Cat.1) ---
    def backup_to(self, dest_dir: Path) -> Path:
        conn = self._ensure_connected()
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        dest_path = dest_dir / f"{self.path.stem}_backup_{ts}_{time.time_ns() % 1_000_000:06d}.db"
        dst_conn = sqlite3.connect(str(dest_path))
        try:
            conn.backup(dst_conn, pages=-1)
        finally:
            dst_conn.close()
        return dest_path

    @staticmethod
    def prune_old_backups(
        dest_dir: Path,
        *,
        keep_latest: int = 14,
        prefix: str | None = None,
    ) -> list[Path]:
        """Remove local `*_backup_*.db` files beyond `keep_latest` newest entries (B1)."""
        keep = max(1, int(keep_latest))
        base = Path(dest_dir)
        if not base.is_dir():
            return []
        pattern = f"{prefix}_backup_*.db" if prefix else "*_backup_*.db"
        candidates = [p for p in base.glob(pattern) if p.is_file()]
        candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        removed: list[Path] = []
        for old_path in candidates[keep:]:
            try:
                old_path.unlink()
                removed.append(old_path)
            except OSError:
                continue
        return removed

    @staticmethod
    def verify_backup(path: Path) -> bool:
        path = Path(path)
        if not path.is_file():
            return False
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                return False
            ver_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            if ver_row is None or int(ver_row[0]) != SCHEMA_VERSION:
                return False
            return True
        except (sqlite3.Error, ValueError):
            return False
        finally:
            conn.close()
