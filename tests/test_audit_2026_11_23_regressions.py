"""Regression test suite for all findings in docs/audit-2026-11-23.md (Cat. 1, 2 and 3)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.core.binance_client import (
    BinanceAPIError,
    BookTicker,
    ExchangeInfoCache,
    IPBannedError,
    MonotonicClock,
    Priority,
    RateLimitedError,
    RequestTimeoutUnknownState,
    SignedBinanceClient,
    SystemStatus,
    WeightBudgetExceeded,
    WeightGovernor,
)
from src.core.config import (
    AnalystConfig,
    BinanceEnv,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_yaml_strict,
)
from src.core.database import CandleRow, Database, SymbolFilters


def _sol_filters(symbol: str = "SOLUSDT", *, oco_allowed: bool = True, status: str = "TRADING") -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        status=status,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("5.00"),
        oco_allowed=oco_allowed,
        fetched_at_ms=1_700_000_000_000,
    )


def _load_cfgs() -> tuple[StrategiesConfig, AnalystConfig]:
    st = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    return st, an


def _insert_trade(
    db: Database,
    *,
    status: str,
    symbol: str = "SOLUSDT",
    client_order_id: str = "CHM_SOLUSDT_1700000000000",
    entry_executed_qty: str | None = "10.00",
    oco_order_list_id: int | None = 5001,
    oco_qty: str | None = "10.00",
    oco_stop_price: str | None = "94.00",
    oco_stop_limit_price: str | None = "91.65",
    oco_target_price: str | None = "112.00",
    planned_entry_price: str = "100.00",
    planned_stop_price: str = "94.00",
    planned_target_price: str = "112.00",
    time_barrier_end_ms: int = 1_700_259_200_000,
    created_at_ms: int = 1_700_000_000_000,
) -> int:
    with db.write_tx() as tx:
        cur = tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt, quote_order_qty,
                entry_executed_qty, entry_avg_price, entry_quote_spent,
                oco_order_list_id, oco_qty, oco_stop_price, oco_stop_limit_price, oco_target_price,
                time_barrier_end_ms, created_at_ms, updated_at_ms
            ) VALUES (?, 'TESTNET', ?, 'bull_trend_pullback', 'BULL',
                1700000000000, ?,
                ?, ?, ?,
                2.0, 0.01, '10.00', '1000.00',
                ?, ?, '1000.00',
                ?, ?, ?, ?, ?,
                ?, ?, ?)
            """,
            (
                client_order_id,
                symbol,
                status,
                planned_entry_price,
                planned_stop_price,
                planned_target_price,
                entry_executed_qty,
                planned_entry_price if entry_executed_qty else None,
                oco_order_list_id,
                oco_qty,
                oco_stop_price,
                oco_stop_limit_price,
                oco_target_price,
                time_barrier_end_ms,
                created_at_ms,
                created_at_ms,
            ),
        )
        return int(cur.lastrowid)


def _write_temp_ed25519(path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


# ==============================================================================
# Commit 1: SignedBinanceClient & WeightGovernor (A1-04, A2-03, query helpers)
# ==============================================================================
def test_audit_a1_04_http_503_on_market_buy_raises_unknown_state(tmp_path: Path) -> None:
    pem_path = _write_temp_ed25519(tmp_path / "ed25519.pem")
    session = MagicMock(spec=requests.Session)
    resp_503 = MagicMock(spec=requests.Response)
    resp_503.status_code = 503
    resp_503.headers = {}
    resp_503.json.return_value = {"code": -1001, "msg": "Internal error; unable to process your request."}
    resp_503.text = "Service Unavailable"
    session.request.return_value = resp_503

    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        WeightGovernor(),
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=pem_path,
        testnet=True,
        session=session,
    )
    with pytest.raises(RequestTimeoutUnknownState):
        client.new_market_buy_quote("SOLUSDT", Decimal("1000.00"), "CHM_SOLUSDT_503")

    # Also HTML 502 with code=None on POST must raise RequestTimeoutUnknownState
    resp_502 = MagicMock(spec=requests.Response)
    resp_502.status_code = 502
    resp_502.headers = {}
    resp_502.json.side_effect = ValueError("not json")
    resp_502.text = "<html>502 Bad Gateway</html>"
    session.request.return_value = resp_502
    with pytest.raises(RequestTimeoutUnknownState):
        client.new_oco_sell("SOLUSDT", Decimal("10"), Decimal("112"), Decimal("94"), Decimal("91.65"), "OCO_1")


def test_audit_a2_03_weight_governor_window_and_retry_after(tmp_path: Path) -> None:
    now_s = [59.0]
    gov = WeightGovernor(limit_per_min=6000, high_ceiling=0.75, low_ceiling=0.50, now=lambda: now_s[0])
    gov.update_from_headers({"X-MBX-USED-WEIGHT-1M": "4400"})
    now_s[0] = 61.0  # 2s later: must NOT wipe 4400 on arbitrary monotonic // 60 rollover
    with pytest.raises(WeightBudgetExceeded):
        gov.acquire(50, Priority.LOW)

    # Authoritative lower header from Binance UTC rollover resets window immediately
    now_s[0] = 70.0
    gov.update_from_headers({"X-MBX-USED-WEIGHT-1M": "20"})
    assert gov.used_weight_1m == 20
    gov.acquire(50, Priority.LOW)

    # RateLimitedError & IPBannedError preserve retry_after_s, and governor tracks remaining cooldown
    pem_path = _write_temp_ed25519(tmp_path / "ed25519_rl.pem")
    session = MagicMock(spec=requests.Session)
    resp_429 = MagicMock(spec=requests.Response)
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "15", "X-MBX-USED-WEIGHT-1M": "6000"}
    resp_429.json.return_value = {"code": -1003, "msg": "Too many requests"}
    resp_429.text = "Too many requests"
    session.request.return_value = resp_429

    client = SignedBinanceClient(
        "https://testnet.binance.vision",
        gov,
        MonotonicClock(mono=lambda: 100.0, wall=lambda: 1_700_000_000.0),
        api_key="k",
        ed25519_private_key_path=pem_path,
        testnet=True,
        session=session,
    )
    with pytest.raises(RateLimitedError) as rl_info:
        client.get_account()
    assert rl_info.value.retry_after_s == pytest.approx(15.0)
    assert gov.retry_after_remaining_s == pytest.approx(15.0)

    # get_order(order_id=...), get_order_list(orig_client_order_id=...), and get_my_trades()
    ok_resp = MagicMock(spec=requests.Response)
    ok_resp.status_code = 200
    ok_resp.headers = {}
    ok_resp.content = b"[]"
    ok_resp.json.return_value = {"orderId": 42}
    session.request.return_value = ok_resp
    assert client.get_order("SOLUSDT", order_id=42)["orderId"] == 42
    assert client.get_order_list(orig_client_order_id="OCO_SOL_1")["orderId"] == 42
    ok_resp.json.return_value = [{"id": 1, "commission": "0.01", "commissionAsset": "SOL"}]
    assert client.get_my_trades("SOLUSDT", order_id=42)[0]["commissionAsset"] == "SOL"
