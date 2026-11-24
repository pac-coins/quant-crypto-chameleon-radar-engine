"""Tests for external dead-man switch heartbeat and Docker HEALTHCHECK probe (Part A)."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

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
from src.core.database import CycleHealth, Database, SymbolFilters
from src.core.healthcheck import check_container_health, ping_healthcheck
from src.main import QuantOrchestrator
from src.notifications.telegram import TelegramOutbox
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


def _make_orchestrator(
    tmp_path: Path,
    *,
    healthcheck_url: str | None,
    clock_sync_fails: bool = False,
) -> tuple[QuantOrchestrator, Database]:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    db = Database(tmp_path / "hc.db")
    db.connect()
    db.migrate()

    now_ms = 1_700_500_003_000

    class _PubStub:
        governor = WeightGovernor()

        def get_server_time_ms(self) -> int:
            if clock_sync_fails:
                raise requests.ConnectionError("clock endpoint unreachable")
            return now_ms

        def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
            return {s: _sol_filters(s) for s in symbols}

    pub = _PubStub()
    clock = MonotonicClock()
    if not clock_sync_fails:
        clock.sync(pub.get_server_time_ms)
    all_syms = list(dict.fromkeys(list(st.universe.symbols) + list(an.scanner_universe.symbols) + ["BNBUSDT"]))
    ex_cache = ExchangeInfoCache(pub, db, all_syms)  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(now_ms)

    settings = RuntimeSettings(
        mode=RunMode.ANALYST_ONLY,
        binance_env=BinanceEnv.TESTNET,
        telegram_bot_token="999999:SUPER_SECRET_TG_TOKEN",
        telegram_chat_id="12345",
        db_path=tmp_path / "hc.db",
        backup_dir=tmp_path / "bkp",
        healthcheck_url=healthcheck_url,
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
        outbox=TelegramOutbox(db, "12345", transport=None),
        orderbook_inspector=OrderbookInspector(an.layer_4_orderbook_live, sleep_fn=lambda _: None),
        sleep_fn=lambda _: None,
    )
    return orch, db


def test_healthcheck_url_env_empty_is_disabled_and_invalid_rejected(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env_empty = tmp_path / ".env.empty"
    env_empty.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "TELEGRAM_CHAT_ID=1\n"
        "HEALTHCHECK_URL=\n",
        encoding="utf-8",
    )
    cfg = load_config(
        env_file=env_empty,
        strategies_path=repo_root / "config" / "strategies.yaml",
        analyst_path=repo_root / "config" / "analyst.yaml",
    )
    assert cfg.runtime.healthcheck_url is None

    env_bad = tmp_path / ".env.bad"
    env_bad.write_text(
        "MODE=ANALYST_ONLY\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "TELEGRAM_CHAT_ID=1\n"
        "HEALTHCHECK_URL=ftp://invalid.local/ping\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(
            env_file=env_bad,
            strategies_path=repo_root / "config" / "strategies.yaml",
            analyst_path=repo_root / "config" / "analyst.yaml",
        )


def test_run_once_sends_heartbeat_without_secrets_outside_tx_and_critical_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, db = _make_orchestrator(
        tmp_path,
        healthcheck_url="https://hc-ping.com/uuid-secret-token-1234",
    )
    captured: list[dict[str, Any]] = []

    def _fake_get(url: str, **kwargs: Any) -> MagicMock:
        captured.append(
            {
                "url": url,
                "kwargs": kwargs,
                "in_write_tx": db._in_write_tx,
                "critical_depth": orch.public_client.governor._critical_depth,
            }
        )
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr("src.core.healthcheck.requests.get", _fake_get)
    rep = orch.run_once(now_ms=1_700_500_003_000, skip_collect=True)
    db.close()

    assert rep.unhandled_errors == 0
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == "https://hc-ping.com/uuid-secret-token-1234"
    assert call["kwargs"]["timeout"] <= 5.0
    assert call["in_write_tx"] is False
    assert call["critical_depth"] == 0
    # Zero secrets leaked in headers or kwargs
    serialized_kwargs = repr(call["kwargs"])
    assert "SUPER_SECRET_TG_TOKEN" not in serialized_kwargs
    assert "Authorization" not in serialized_kwargs


def test_run_once_disabled_when_healthcheck_url_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, db = _make_orchestrator(tmp_path, healthcheck_url="")
    called = False

    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("requests.get must not be called when HEALTHCHECK_URL is empty")

    monkeypatch.setattr("src.core.healthcheck.requests.get", _fail_if_called)
    rep = orch.run_once(now_ms=1_700_500_003_000, skip_collect=True)
    db.close()
    assert rep.unhandled_errors == 0
    assert called is False


def test_run_once_heartbeat_timeout_and_http_error_are_swallowed_and_logged_without_token_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    orch, db = _make_orchestrator(
        tmp_path,
        healthcheck_url="https://hc-ping.com/super-private-uuid-999",
    )

    def _raise_timeout(url: str, **kwargs: Any) -> Any:
        raise requests.Timeout("simulated heartbeat timeout")

    monkeypatch.setattr("src.core.healthcheck.requests.get", _raise_timeout)
    with caplog.at_level(logging.WARNING, logger="healthcheck"):
        rep = orch.run_once(now_ms=1_700_500_003_000, skip_collect=True)
    db.close()

    assert rep.unhandled_errors == 0
    assert any("Timeout" in rec.getMessage() for rec in caplog.records)
    # Path secret must NOT leak into warning logs
    assert all("super-private-uuid-999" not in rec.getMessage() for rec in caplog.records)

    # Direct unit test of HTTP 500 error in ping_healthcheck
    def _raise_http_500(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock(spec=requests.Response)
        resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        return resp

    monkeypatch.setattr("src.core.healthcheck.requests.get", _raise_http_500)
    assert ping_healthcheck("https://hc-ping.com/uuid-abc", timeout_s=1.0) is False


def test_run_once_skips_heartbeat_when_cycle_has_unhandled_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, db = _make_orchestrator(
        tmp_path,
        healthcheck_url="https://hc-ping.com/uuid-deadman",
        clock_sync_fails=True,
    )
    calls: list[str] = []
    monkeypatch.setattr("src.core.healthcheck.requests.get", lambda url, **kw: calls.append(url))
    rep = orch.run_once(now_ms=1_700_500_003_000, skip_collect=True)
    db.close()
    assert rep.unhandled_errors >= 1
    assert calls == []


def test_docker_container_healthcheck_probe(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    ok, _ = check_container_health(missing)
    assert ok is False

    db_path = tmp_path / "probe.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    # Boot window before first cycle -> healthy
    ok_boot, _ = check_container_health(db_path, max_stale_seconds=9000, now_ms=1_700_500_000_000)
    assert ok_boot is True

    # Recent cycle -> healthy
    db.log_cycle_health(
        CycleHealth(
            cycle_close_time_ms=1_700_500_000_000,
            mode="ANALYST_ONLY",
            clock_offset_ms=5.0,
            clock_rtt_ms=10.0,
            used_weight_1m_peak=20,
            stale_symbols=(),
            gaps_detected=0,
            duration_ms=500,
            unhandled_errors=0,
        )
    )
    ok_fresh, _ = check_container_health(
        db_path,
        max_stale_seconds=9000,
        now_ms=1_700_500_000_000 + 3_600_000,
    )
    assert ok_fresh is True

    # Stale cycle (> 9000s) -> unhealthy
    ok_stale, reason = check_container_health(
        db_path,
        max_stale_seconds=9000,
        now_ms=1_700_500_000_000 + 10_000_000,
    )
    db.close()
    assert ok_stale is False
    assert "stale" in reason
