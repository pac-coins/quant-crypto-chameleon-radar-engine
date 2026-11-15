from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from src.core.binance_client import (
    ExchangeInfoCache,
    IPBannedError,
    MonotonicClock,
    Priority,
    PublicBinanceClient,
    RateLimitedError,
    RequestTimeoutUnknownState,
    SignedBinanceClient,
    WeightBudgetExceeded,
    WeightGovernor,
    build_clients,
)
from src.core.config import (
    BinanceEnv,
    ConfigError,
    PHASE_1_LIVE_CONFIRMATION,
    RunMode,
    load_config,
    load_yaml_strict,
    sha256_file,
    validate_mode_invariants,
)
from src.core.database import (
    CandleRow,
    CycleHealth,
    Database,
    WrongThreadError,
)


def _write_ed25519_pem(path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    path.write_bytes(pem)
    return path


# ==============================================================================
# Gate 1 (c): Restauração real de backup via sqlite3.backup() + integrity_check
# ==============================================================================
def test_gate1c_sqlite_native_backup_and_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "live.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    db.set_state("high_water_mark_usdt", Decimal("345.67"))
    db.upsert_candles(
        [
            CandleRow(
                symbol="BTCUSDT",
                interval="1h",
                open_time_ms=1_767_225_600_000,
                close_time_ms=1_767_229_200_000,
                open=90000.0,
                high=91000.0,
                low=89500.0,
                close=90500.0,
                volume=100.0,
                quote_volume=9050000.0,
                number_of_trades=5000,
                taker_buy_base_volume=60.0,
                taker_buy_quote_volume=5430000.0,
                ingested_at_ms=1_767_229_203_000,
            )
        ]
    )
    db.log_cycle_health(
        CycleHealth(
            cycle_close_time_ms=1_767_229_200_000,
            mode="ANALYST_ONLY",
            clock_offset_ms=-12.5,
            clock_rtt_ms=18.0,
            used_weight_1m_peak=286,
            stale_symbols=(),
            gaps_detected=0,
            duration_ms=420,
            unhandled_errors=0,
        )
    )

    backup_file = db.backup_to(tmp_path / "backups")
    assert backup_file.is_file()
    assert Database.verify_backup(backup_file) is True

    db.close()

    # Open the backup in a clean Database instance and verify all data is intact
    restored = Database(backup_file)
    restored.connect()
    assert restored.get_state("high_water_mark_usdt") == "345.67"
    df = restored.load_candles("BTCUSDT", "1h", limit=5)
    assert len(df) == 1
    assert df.iloc[0]["close"] == pytest.approx(90500.0)
    restored.close()


# ==============================================================================
# Gate 1 (d): WeightGovernor (C1-B) — tetos por prioridade e critical_section()
# ==============================================================================
def test_gate1d_weight_governor_ceilings_and_critical_section() -> None:
    fake_now = [1000.0]
    gov = WeightGovernor(
        limit_per_min=6000,
        high_ceiling=0.75,  # 4500
        low_ceiling=0.50,   # 3000
        now=lambda: fake_now[0],
    )

    # Report 3100 used weight from Binance header (> 50% LOW ceiling, < 75% HIGH ceiling)
    gov.update_from_headers({"X-MBX-USED-WEIGHT-1M": "3100"})
    assert gov.used_weight_1m == 3100

    # LOW priority (Radar depth) MUST be rejected at 3100
    with pytest.raises(WeightBudgetExceeded):
        gov.acquire(25, Priority.LOW)

    # HIGH priority (klines) MUST still pass at 3100
    gov.acquire(2, Priority.HIGH)
    assert gov.used_weight_1m == 3102

    #CRITICAL priority (orders/OCO) MUST pass even above 4500
    gov.update_from_headers({"X-MBX-USED-WEIGHT-1M": "5500"})
    with pytest.raises(WeightBudgetExceeded):
        gov.acquire(2, Priority.HIGH)
    gov.acquire(1, Priority.CRITICAL)
    assert gov.used_weight_1m == 5501

    # Advance to next minute window: resets used_weight_1m
    fake_now[0] += 61.0
    assert gov.used_weight_1m == 0

    # Inside critical_section(), both LOW and HIGH are blocked while CRITICAL passes
    with gov.critical_section():
        with pytest.raises(WeightBudgetExceeded):
            gov.acquire(2, Priority.HIGH)
        with pytest.raises(WeightBudgetExceeded):
            gov.acquire(25, Priority.LOW)
        gov.acquire(1, Priority.CRITICAL)

    # After exiting critical_section(), HIGH and LOW work again
    gov.acquire(2, Priority.HIGH)
    gov.acquire(25, Priority.LOW)


# ==============================================================================
# Gate 1 (e): validate_mode_invariants (C1-C, ADR-004, ADR-010)
# ==============================================================================
def test_gate1e_mode_invariants_and_yaml_guards(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    strategies_path = repo_root / "config" / "strategies.yaml"
    analyst_path = repo_root / "config" / "analyst.yaml"

    # 1. Valid ANALYST_ONLY config (no Binance keys)
    env_analyst = tmp_path / ".env.analyst"
    env_analyst.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123456:ABC\n"
        "TELEGRAM_CHAT_ID=999999\n",
        encoding="utf-8",
    )
    cfg = load_config(
        env_file=env_analyst,
        strategies_path=strategies_path,
        analyst_path=analyst_path,
    )
    assert cfg.runtime.mode == RunMode.ANALYST_ONLY
    assert "BNBUSDT" not in cfg.execution_universe
    assert "BNBUSDT" in cfg.market_data_universe
    assert cfg.market_data_base_url == "https://data-api.binance.vision"
    assert cfg.trading_base_url is None

    # 2. ANALYST_ONLY or SHADOW_CHAMELEON with BINANCE_API_KEY must raise ConfigError
    env_bad_analyst = tmp_path / ".env.bad_analyst"
    env_bad_analyst.write_text(
        "MODE=ANALYST_ONLY\n"
        "BINANCE_API_KEY=secret_key_should_fail\n"
        "TELEGRAM_BOT_TOKEN=123456:ABC\n"
        "TELEGRAM_CHAT_ID=999999\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="forbids Binance credentials"):
        load_config(
            env_file=env_bad_analyst,
            strategies_path=strategies_path,
            analyst_path=analyst_path,
        )

    # 3. LIVE with BNBUSDT in strategies.symbols must raise ConfigError (C1-C)
    pem_path = _write_ed25519_pem(tmp_path / "ed25519.pem")
    bad_strategies = tmp_path / "strategies_bnb.yaml"
    bad_strategies.write_text(
        strategies_path.read_text(encoding="utf-8").replace(
            '- "AVAXUSDT"', '- "AVAXUSDT"\n    - "BNBUSDT"'
        ),
        encoding="utf-8",
    )
    env_live_testnet = tmp_path / ".env.live_testnet"
    env_live_testnet.write_text(
        "MODE=LIVE_CHAMELEON_PHASE_1\n"
        "BINANCE_ENV=TESTNET\n"
        "BINANCE_API_KEY=testnet_key\n"
        f"BINANCE_ED25519_PRIVATE_KEY_PATH={pem_path}\n"
        "TELEGRAM_BOT_TOKEN=123456:ABC\n"
        "TELEGRAM_CHAT_ID=999999\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="BNBUSDT"):
        load_config(
            env_file=env_live_testnet,
            strategies_path=bad_strategies,
            analyst_path=analyst_path,
        )

    # 4. analyst.yaml with affects_execution: true must fail validation (ADR-004)
    bad_analyst = tmp_path / "analyst_bad.yaml"
    bad_analyst.write_text(
        analyst_path.read_text(encoding="utf-8").replace(
            "affects_execution: false", "affects_execution: true"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(
            env_file=env_analyst,
            strategies_path=strategies_path,
            analyst_path=bad_analyst,
        )

    # 5. LIVE + PRODUCTION requires exact LIVE_CONFIRMATION and SHA-256 match
    strat_hash = sha256_file(strategies_path)
    env_live_prod_ok = tmp_path / ".env.live_prod"
    env_live_prod_ok.write_text(
        "MODE=LIVE_CHAMELEON_PHASE_1\n"
        "BINANCE_ENV=PRODUCTION\n"
        "BINANCE_API_KEY=prod_key\n"
        f"BINANCE_ED25519_PRIVATE_KEY_PATH={pem_path}\n"
        f"LIVE_CONFIRMATION={PHASE_1_LIVE_CONFIRMATION}\n"
        f"APPROVED_STRATEGIES_SHA256={strat_hash}\n"
        "TELEGRAM_BOT_TOKEN=123456:ABC\n"
        "TELEGRAM_CHAT_ID=999999\n",
        encoding="utf-8",
    )
    cfg_live = load_config(
        env_file=env_live_prod_ok,
        strategies_path=strategies_path,
        analyst_path=analyst_path,
    )
    assert cfg_live.is_live is True
    assert cfg_live.trading_base_url == "https://api.binance.com"
    validate_mode_invariants(cfg_live)

    # Duplicate YAML keys are rejected
    dup_yaml = tmp_path / "dup.yaml"
    dup_yaml.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Duplicate YAML key"):
        load_yaml_strict(dup_yaml)


# ==============================================================================
# Gate 1 (f): Single-Slot parcial no SQLite + WrongThreadError + leitor não bloqueia escrita
# ==============================================================================
def test_gate1f_single_slot_unique_index_wrong_thread_and_wal_concurrency(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "slot.db")
    db.connect()
    db.migrate()

    insert_trade_sql = """
        INSERT INTO trades (
            client_order_id, environment, symbol, strategy, regime,
            signal_close_time_ms, status,
            planned_entry_price, planned_stop_price, planned_target_price,
            atr_at_signal, risk_pct, risk_usdt,
            time_barrier_end_ms, created_at_ms, updated_at_ms
        ) VALUES (?, 'TESTNET', ?, 'bull_trend_pullback', 'BULL',
                  1767225600000, ?, '100', '95', '112.5',
                  2.5, 0.01, '3.00', 1767312000000, 1767225603000, 1767225603000)
    """

    with db.write_tx() as tx:
        tx.execute(insert_trade_sql, ("CHM_SOLUSDT_1", "SOLUSDT", "PENDING_ENTRY"))

    # A second open trade (even on a different symbol and different open status) MUST fail
    with pytest.raises(sqlite3.IntegrityError):
        with db.write_tx() as tx:
            tx.execute(insert_trade_sql, ("CHM_ETHUSDT_2", "ETHUSDT", "PROTECTED"))

    # Once the first trade transitions to CLOSED, a new open trade is permitted
    with db.write_tx() as tx:
        tx.execute("UPDATE trades SET status = 'CLOSED' WHERE client_order_id = 'CHM_SOLUSDT_1'")
        tx.execute(insert_trade_sql, ("CHM_ETHUSDT_2", "ETHUSDT", "PROTECTED"))

    # Calling write_tx() from a secondary thread MUST raise WrongThreadError (ADR-002)
    thread_err: list[BaseException] = []

    def _foreign_writer() -> None:
        try:
            with db.write_tx() as tx:
                tx.execute("UPDATE trades SET status = 'CLOSED'")
        except BaseException as exc:  # noqa: BLE001
            thread_err.append(exc)

    t = threading.Thread(target=_foreign_writer)
    t.start()
    t.join()
    assert len(thread_err) == 1
    assert isinstance(thread_err[0], WrongThreadError)

    # A reader holding an active read cursor on another connection does NOT block write_tx in WAL
    reader_ready = threading.Event()
    release_reader = threading.Event()

    def _long_reader() -> None:
        with db.read() as ro_conn:
            _ = ro_conn.execute("SELECT * FROM trades").fetchall()
            reader_ready.set()
            release_reader.wait(timeout=5.0)

    rt = threading.Thread(target=_long_reader)
    rt.start()
    assert reader_ready.wait(timeout=5.0)

    # Writer commits while reader is still open
    db.set_state("probe_while_reading", "ok")
    release_reader.set()
    rt.join()
    assert db.get_state("probe_while_reading") == "ok"

    db.close()


# ==============================================================================
# Complemento Bloco 1: MonotonicClock (Cristian RTT/2), SignedBinanceClient Ed25519,
# timeout -1007, mock /sapi/* na Testnet e ExchangeInfoCache
# ==============================================================================
def test_block1_clock_ed25519_sapi_mock_and_timeout_handling(tmp_path: Path) -> None:
    # 1. Cristian RTT/2 clock sync picks minimum-RTT sample
    mono_samples = iter([10.000, 10.040, 20.000, 20.010, 30.000, 30.050])
    server_times = iter([1_700_010_025, 1_700_020_008, 1_700_030_030])
    clock = MonotonicClock(safety_margin_ms=200, mono=lambda: next(mono_samples))
    best = clock.sync(lambda: next(server_times), samples=3)
    assert best.rtt_ms == pytest.approx(10.0)
    # Midpoint of 2nd sample was 20005.0 ms; server was 1_700_020_008 -> offset = 1_700_000_003.0
    assert best.offset_ms == pytest.approx(1_700_000_003.0)

    # 2. SignedBinanceClient Ed25519 signature, /sapi/* Testnet mock, and POST timeout -> RequestTimeoutUnknownState
    pem_path = _write_ed25519_pem(tmp_path / "signer.pem")
    gov = WeightGovernor()
    session = MagicMock(spec=requests.Session)
    fixed_clock = MonotonicClock(safety_margin_ms=200, mono=lambda: 100.0, wall=lambda: 1700000.0)

    signed = SignedBinanceClient(
        "https://testnet.binance.vision",
        gov,
        fixed_clock,
        api_key="test_api_key",
        ed25519_private_key_path=pem_path,
        testnet=True,
        session=session,
    )

    # /sapi/* on TESTNET is mocked without hitting the network (TODO Cat.2)
    sys_status = signed.sapi_system_status()
    assert sys_status.normal is True and sys_status.mocked is True
    transfer_res = signed.sapi_transfer_main_to_funding("USDT", Decimal("10.00"))
    assert transfer_res["mocked"] is True and transfer_res["amount"] == "10"
    session.request.assert_not_called()

    # Timeout during POST raises RequestTimeoutUnknownState (never auto-retries)
    session.request.side_effect = requests.Timeout("read timed out")
    with pytest.raises(RequestTimeoutUnknownState):
        signed.new_market_buy_quote("SOLUSDT", Decimal("25.00"), "CHM_SOLUSDT_100")
    assert session.request.call_count == 1

    # HTTP 429 and 418 mapping
    resp_429 = MagicMock(spec=requests.Response)
    resp_429.status_code = 429
    resp_429.headers = {"X-MBX-USED-WEIGHT-1M": "6000", "Retry-After": "30"}
    resp_429.json.return_value = {"code": -1003, "msg": "Too many requests"}
    resp_429.text = "Too many requests"
    session.request.side_effect = None
    session.request.return_value = resp_429

    with pytest.raises(RateLimitedError):
        signed.get_server_time_ms()

    resp_418 = MagicMock(spec=requests.Response)
    resp_418.status_code = 418
    resp_418.headers = {"Retry-After": "120"}
    resp_418.json.return_value = {"code": -1003, "msg": "Way too many requests; IP banned"}
    resp_418.text = "IP banned"
    session.request.return_value = resp_418
    with pytest.raises(IPBannedError):
        signed.get_account()

    # 3. ExchangeInfoCache persists to SQLite and serves from cache
    db = Database(tmp_path / "cache.db")
    db.connect()
    db.migrate()

    pub_session = MagicMock(spec=requests.Session)
    resp_info = MagicMock(spec=requests.Response)
    resp_info.status_code = 200
    resp_info.headers = {"X-MBX-USED-WEIGHT-1M": "20"}
    resp_info.content = b"{}"
    resp_info.json.return_value = {
        "serverTime": 1_767_225_600_000,
        "symbols": [
            {
                "symbol": "SOLUSDT",
                "status": "TRADING",
                "baseAsset": "SOL",
                "quoteAsset": "USDT",
                "ocoAllowed": True,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
                ],
            }
        ],
    }
    pub_session.request.return_value = resp_info
    pub_client = PublicBinanceClient("https://data-api.binance.vision", WeightGovernor(), session=pub_session)

    cache = ExchangeInfoCache(pub_client, db, ["SOLUSDT"], max_age_s=3600)
    assert cache.refresh_if_stale(1_767_225_600_000) is True
    assert cache.refresh_if_stale(1_767_225_610_000) is False  # cached
    f = cache.get("SOLUSDT")
    assert f.tick_size == Decimal("0.01")
    assert f.step_size == Decimal("0.001")
    assert f.min_notional == Decimal("5")

    # build_clients smoke check for ANALYST_ONLY
    repo_root = Path(__file__).resolve().parents[1]
    env_analyst = tmp_path / ".env.build"
    env_analyst.write_text(
        "MODE=ANALYST_ONLY\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\n",
        encoding="utf-8",
    )
    cfg = load_config(
        env_file=env_analyst,
        strategies_path=repo_root / "config" / "strategies.yaml",
        analyst_path=repo_root / "config" / "analyst.yaml",
    )
    pub_c, sig_c, _, _ = build_clients(cfg)
    assert isinstance(pub_c, PublicBinanceClient)
    assert sig_c is None

    db.close()
