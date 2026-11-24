"""Tests for B7: LIVE_CHAMELEON_PHASE_2 invariants, idempotent ProfitSplitter, active Grade C filter, CVaR99/stage1->2, and 30-trade auto-downgrade."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import SecretStr

from src.core.binance_client import (
    BookTicker,
    ExchangeInfoCache,
    MonotonicClock,
    SystemStatus,
    WeightGovernor,
)
from src.core.config import (
    PHASE_2_LIVE_CONFIRMATION,
    AnalystConfig,
    AppConfig,
    BinanceEnv,
    ConfigError,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_yaml_strict,
    sha256_file,
    validate_mode_invariants,
)
from src.core.database import Database, SymbolFilters
from src.features.ml_judge import MLJudgeArtifact
from src.main import ONE_HOUR_MS, QuantOrchestrator
from src.notifications.telegram import TelegramOutbox
from src.regimes.regime_detector import MarketRegime
from src.risk.position_sizer import SignalGrade
from src.strategies.base_strategy import SignalCandidate


def _make_phase2_strategies_cfg(
    *,
    profit_splitter_enabled: bool = True,
    use_ml_filter: bool = True,
    conviction_sizing_enabled: bool = False,
    breakeven_enabled: bool = False,
    sideways_enabled: bool = False,
) -> StrategiesConfig:
    raw = deepcopy(load_yaml_strict(Path("config/strategies.yaml")))
    raw["profit_splitter"]["enabled"] = profit_splitter_enabled  # type: ignore[index]
    raw["ml_judge_and_validation"]["use_ml_filter"] = use_ml_filter  # type: ignore[index]
    raw["position_sizing"]["conviction_sizing_enabled"] = conviction_sizing_enabled  # type: ignore[index]
    raw["mid_trade_management"]["breakeven_enabled"] = breakeven_enabled  # type: ignore[index]
    raw["regimes_and_strategies"]["sideways_pingpong"]["enabled"] = sideways_enabled  # type: ignore[index]
    return StrategiesConfig.model_validate(raw)


def _make_calibrated_artifact(path: Path, *, weight_chop: float = -3.0, intercept: float = 1.5) -> Path:
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
        weights=(0.0, 0.0, 0.0, weight_chop, 0.0, 0.0),
        intercept=intercept,
        platt_a=-1.0,
        platt_b=0.0,
        ece_out_of_sample=0.02,
        net_r_q33_threshold=0.05,
        net_r_q70_threshold=0.30,
        trained_samples=120,
        validation_samples=60,
        created_at_utc="2026-11-24T00:00:00+00:00",
    ).with_computed_sha256()
    return art.save(path)


def test_b7_phase2_mode_invariants_and_phase1_protection(tmp_path: Path) -> None:
    an_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    key_file = tmp_path / "k.pem"
    key_file.write_text("ed25519-key", encoding="utf-8")
    art_path = _make_calibrated_artifact(tmp_path / "ml.json")
    st_sha = sha256_file(Path("config/strategies.yaml"))

    # 1. Phase 2 with use_ml_filter=True REQUIRES calibrated ML_JUDGE_ARTIFACT_PATH
    st_p2 = _make_phase2_strategies_cfg(profit_splitter_enabled=True, use_ml_filter=True)
    rt_no_art = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_2,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key=SecretStr("k"),
        binance_ed25519_private_key_path=key_file,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
    )
    with pytest.raises(ConfigError, match="ML_JUDGE_ARTIFACT_PATH"):
        validate_mode_invariants(AppConfig(runtime=rt_no_art, strategies=st_p2, analyst=an_cfg, strategies_sha256=st_sha))

    # 2. Phase 2 forbids Phase 3 flags (breakeven, sideways_pingpong)
    rt_p2_test = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_2,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key=SecretStr("k"),
        binance_ed25519_private_key_path=key_file,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        ml_judge_artifact_path=art_path,
    )
    st_be = _make_phase2_strategies_cfg(breakeven_enabled=True)
    with pytest.raises(ConfigError, match="breakeven_enabled"):
        validate_mode_invariants(AppConfig(runtime=rt_p2_test, strategies=st_be, analyst=an_cfg, strategies_sha256=st_sha))
    st_sw = _make_phase2_strategies_cfg(sideways_enabled=True)
    with pytest.raises(ConfigError, match="sideways_pingpong"):
        validate_mode_invariants(AppConfig(runtime=rt_p2_test, strategies=st_sw, analyst=an_cfg, strategies_sha256=st_sha))

    # 3. Phase 2 PRODUCTION requires PHASE_2_F1_TO_F2_PASSED + exact SHA-256
    rt_p2_prod_bad = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_2,
        binance_env=BinanceEnv.PRODUCTION,
        binance_api_key=SecretStr("k"),
        binance_ed25519_private_key_path=key_file,
        live_confirmation="PHASE_1_GATES_6A_6B_PASSED",  # wrong phase confirmation!
        approved_strategies_sha256=st_sha,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        ml_judge_artifact_path=art_path,
    )
    with pytest.raises(ConfigError, match=PHASE_2_LIVE_CONFIRMATION):
        validate_mode_invariants(AppConfig(runtime=rt_p2_prod_bad, strategies=st_p2, analyst=an_cfg, strategies_sha256=st_sha))

    rt_p2_prod_ok = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_2,
        binance_env=BinanceEnv.PRODUCTION,
        binance_api_key=SecretStr("k"),
        binance_ed25519_private_key_path=key_file,
        live_confirmation=PHASE_2_LIVE_CONFIRMATION,
        approved_strategies_sha256=st_sha,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        ml_judge_artifact_path=art_path,
    )
    validate_mode_invariants(AppConfig(runtime=rt_p2_prod_ok, strategies=st_p2, analyst=an_cfg, strategies_sha256=st_sha))

    # 4. Phase 1 still strictly rejects Phase 2 flags
    rt_p1 = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_1,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key=SecretStr("k"),
        binance_ed25519_private_key_path=key_file,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
    )
    with pytest.raises(ConfigError, match="profit_splitter"):
        validate_mode_invariants(AppConfig(runtime=rt_p1, strategies=st_p2, analyst=an_cfg, strategies_sha256=st_sha))


class _Phase2FakeSigned:
    def __init__(self, clock: MonotonicClock, free_usdt: Decimal = Decimal("500.00")) -> None:
        self.governor = WeightGovernor()
        self.clock = clock
        self.free_usdt = free_usdt
        self.free_sol = Decimal("0")
        self.transfers: list[tuple[str, Decimal]] = []
        self.buy_quote_qtys: list[Decimal] = []

    def get_server_time_ms(self) -> int:
        return self.clock.server_now_ms()

    def sapi_system_status(self) -> SystemStatus:
        return SystemStatus(normal=True, msg="normal", mocked=True)

    def get_account(self, omit_zero_balances: bool = True) -> dict[str, Any]:
        bals = [
            {"asset": "USDT", "free": format(self.free_usdt, "f"), "locked": "0"},
            {"asset": "BNB", "free": "0.02", "locked": "0"},  # 0.02 * 500 = $10
        ]
        if self.free_sol > Decimal("0"):
            bals.append({"asset": "SOL", "free": format(self.free_sol, "f"), "locked": "0"})
        return {"balances": bals}

    def get_account_commission(self, symbol: str) -> dict[str, Any]:
        return {"standardCommission": {"maker": "0.00075", "taker": "0.00075"}}

    def get_open_order_lists(self) -> list[dict[str, Any]]:
        return []

    def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
        px = Decimal("500.00") if symbol == "BNBUSDT" else Decimal("100.00")
        return BookTicker(symbol=symbol, bid=px, bid_qty=Decimal("10"), ask=px, ask_qty=Decimal("10"))

    def get_exchange_info(self, symbols: Any) -> dict[str, SymbolFilters]:
        return {
            s: SymbolFilters(
                symbol=s,
                status="TRADING",
                base_asset=s.replace("USDT", ""),
                quote_asset="USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.01"),
                min_qty=Decimal("0.01"),
                min_notional=Decimal("10"),
                oco_allowed=True,
                fetched_at_ms=self.clock.server_now_ms(),
            )
            for s in symbols
        }

    def sapi_transfer_main_to_funding(self, asset: str, amount: Decimal) -> dict[str, Any]:
        self.transfers.append((asset, amount))
        self.free_usdt -= amount
        return {"tranId": 777000 + len(self.transfers)}

    def new_market_buy_quote(self, symbol: str, quote_qty: Decimal, client_order_id: str) -> dict[str, Any]:
        self.buy_quote_qtys.append(quote_qty)
        qty = (quote_qty / Decimal("100.00")).quantize(Decimal("0.01"))
        self.free_sol += qty
        return {
            "symbol": symbol,
            "orderId": 4001 + len(self.buy_quote_qtys),
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": format(qty, "f"),
            "cummulativeQuoteQty": format(quote_qty, "f"),
            "fills": [{"price": "100.00", "qty": format(qty, "f"), "commission": "0", "commissionAsset": "USDT"}],
        }

    def new_oco_sell(self, **kwargs: Any) -> dict[str, Any]:
        return {"orderListId": 8001, "listClientOrderId": kwargs["list_client_order_id"]}


def test_b7_phase2_grade_c_filter_idempotent_profit_splitter_stage1_and_auto_downgrade(
    tmp_path: Path,
) -> None:
    an_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))
    st_p2 = _make_phase2_strategies_cfg(
        profit_splitter_enabled=True,
        use_ml_filter=True,
        conviction_sizing_enabled=True,
    )
    art_path = _make_calibrated_artifact(tmp_path / "ml_p2.json", weight_chop=-3.0, intercept=0.8)

    db_path = tmp_path / "b7.db"
    db = Database(db_path)
    db.connect()
    db.migrate()

    now_ms = 1_700_000_000_000
    clock = MonotonicClock()
    clock.sync(lambda: now_ms, samples=1)
    signed = _Phase2FakeSigned(clock, free_usdt=Decimal("550.00"))
    ex_cache = ExchangeInfoCache(signed, db, ["SOLUSDT", "BNBUSDT"])  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(now_ms)
    outbox = TelegramOutbox(db, "chat1", transport=None)
    key_file = tmp_path / "k.pem"
    key_file.write_text("dummy", encoding="utf-8")

    settings = RuntimeSettings(
        mode=RunMode.LIVE_CHAMELEON_PHASE_2,
        binance_env=BinanceEnv.TESTNET,
        binance_api_key=SecretStr("k"),
        binance_ed25519_private_key_path=key_file,
        telegram_bot_token=SecretStr("tg"),
        telegram_chat_id="chat1",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        ml_judge_artifact_path=art_path,
    )

    # Seed previous equity & HWM = 500.00, and 1 newly CLOSED profitable trade (equity now 550.00 -> +50 profit -> 30% = 15.00 >= 10 -> MAIN_FUNDING transfer!)
    db.set_state("current_equity_usdt", "500.00")
    db.set_state("profit_hwm_usdt", "500.00")
    with db.write_tx() as tx:
        tx.execute(
            """
            INSERT INTO trades (
                client_order_id, environment, symbol, strategy, regime,
                signal_close_time_ms, status,
                planned_entry_price, planned_stop_price, planned_target_price,
                atr_at_signal, risk_pct, risk_usdt, time_barrier_end_ms,
                realized_pnl_usdt, realized_r, ml_p_cal_silent, created_at_ms, updated_at_ms, closed_at_ms
            ) VALUES (
                'CHM_SOL_PREV', 'TESTNET', 'SOLUSDT', 'bull_trend_pullback', 'BULL',
                ?, 'CLOSED',
                '100', '94', '112',
                2.0, 0.01, '5.00', ?,
                '50.00', 2.0, 0.65, ?, ?, ?
            )
            """,
            (now_ms - 10 * ONE_HOUR_MS, now_ms, now_ms - 10 * ONE_HOUR_MS, now_ms, now_ms),
        )

    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st_p2,
        analyst_cfg=an_cfg,
        db=db,
        public_client=signed,  # type: ignore[arg-type]
        signed_client=signed,  # type: ignore[arg-type]
        clock=clock,
        exchange_cache=ex_cache,
        outbox=outbox,
        sleep_fn=lambda _: None,
    )
    assert orch.position_sizer.use_ml_filter is True

    # 1. Idempotent ProfitSplitter: first call transfers 15.00 USDT to FUNDING; second call is a no-op!
    processed_1 = orch._process_newly_closed_live_trades(now_ms)
    assert processed_1 == 1
    assert signed.transfers == [("USDT", Decimal("15.00"))]
    processed_2 = orch._process_newly_closed_live_trades(now_ms + 1000)
    assert processed_2 == 0
    assert len(signed.transfers) == 1  # ZERO duplicate transfers on restart!

    # 2. Active Grade C filter blocks high-chop signal in Phase 2
    def _stub_eval(sym: str, row: pd.Series, reg: MarketRegime) -> SignalCandidate | None:
        if sym != "SOLUSDT":
            return None
        return SignalCandidate(
            symbol="SOLUSDT",
            close_time_ms=now_ms,
            regime=MarketRegime.BULL,
            strategy_name="bull_trend_pullback",
            entry_price=100.0,
            stop_price=94.0,  # 6% stop -> risk_cap binds cleanly!
            target_price=112.0,
            atr_14=2.0,
            stop_distance_pct=0.06,
            gross_rr_ratio=2.0,
            time_barrier_hours=72,
            rs_vol_adjusted_12h=2.0,
            order_flow_delta=100.0,
            taker_buy_ratio=0.58,
            has_liquidity_sweep=False,
        )

    orch.strategy.evaluate_row = _stub_eval  # type: ignore[method-assign]
    bad_aligned = {
        "SOLUSDT": pd.DataFrame(
            [{"close_time_ms": now_ms, "close": 100.0, "atr_14": 2.0, "ema_100": 98.0, "rvol_20": 1.5, "chop_14_4h": 50.0, "btc_rs_12h": 0.5}]
        )
    }
    exec_c, _ = orch._execute_chameleon_step(
        now_ms=now_ms,
        cycle_close_ms=now_ms,
        aligned_frames=bad_aligned,
        regimes={"SOLUSDT": MarketRegime.BULL},
        entries_blocked=False,
        live_already_managed=True,
    )
    assert exec_c is False
    assert len(signed.buy_quote_qtys) == 0

    # 3. Grade A+ in Stage 1 (phase2_closed_trades_count=1 < 30) MUST cap risk at 1.5% (NOT jump to 2.0%!)
    # With equity = 535.00, stop_dist = 0.06 + 0.0021 = 0.0621:
    # Stage 1 (1.5% risk) -> quote ~= (0.015 * 535) / 0.0621 ~= 129.22 USDT (vs 2.0% ~= 172.30 USDT)
    good_aligned = {
        "SOLUSDT": pd.DataFrame(
            [{"close_time_ms": now_ms, "close": 100.0, "atr_14": 2.0, "ema_100": 98.0, "rvol_20": 1.5, "chop_14_4h": 28.0, "btc_rs_12h": 0.5}]
        )
    }
    exec_a, _ = orch._execute_chameleon_step(
        now_ms=now_ms,
        cycle_close_ms=now_ms,
        aligned_frames=good_aligned,
        regimes={"SOLUSDT": MarketRegime.BULL},
        entries_blocked=False,
        live_already_managed=True,
    )
    assert exec_a is True
    assert len(signed.buy_quote_qtys) == 1
    assert Decimal("115.00") <= signed.buy_quote_qtys[0] <= Decimal("140.00")

    # 4. Auto-downgrade on 30 losing trades (mean_R_30 < 0): triggers conviction_downgraded=1 and PHASE_DOWNGRADE alert
    with db.write_tx() as tx:
        # Close the active trade first so we can insert 30 losing closed trades
        tx.execute("UPDATE trades SET status = 'CLOSED', realized_r = -1.0 WHERE status != 'CLOSED'")
        for i in range(30):
            tx.execute(
                """
                INSERT INTO trades (
                    client_order_id, environment, symbol, strategy, regime,
                    signal_close_time_ms, status,
                    planned_entry_price, planned_stop_price, planned_target_price,
                    atr_at_signal, risk_pct, risk_usdt, time_barrier_end_ms,
                    realized_pnl_usdt, realized_r, ml_p_cal_silent, created_at_ms, updated_at_ms, closed_at_ms
                ) VALUES (
                    ?, 'TESTNET', 'SOLUSDT', 'bull_trend_pullback', 'BULL',
                    ?, 'CLOSED',
                    '100', '94', '112',
                    2.0, 0.01, '5.00', ?,
                    '-5.00', -0.5, 0.80, ?, ?, ?
                )
                """,
                (f"CHM_LOSS_{i}", now_ms + i * 1000, now_ms + 72 * ONE_HOUR_MS, now_ms + i * 1000, now_ms + i * 1000, now_ms + i * 1000),
            )
    orch._process_newly_closed_live_trades(now_ms + 50_000)
    assert db.get_state("conviction_downgraded") == "1"
    with db.read() as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM telegram_outbox").fetchall()]
    assert "PROFIT_SPLIT" in kinds
    assert "PHASE_DOWNGRADE" in kinds

    # Verify CVaR99 guard > 5% returns cvar99_loss_pct > 0.05 when heavy tail losses exist
    with db.write_tx() as tx:
        tx.execute("UPDATE trades SET realized_r = -6.0, risk_pct = 0.015 WHERE client_order_id LIKE 'CHM_LOSS_%'")
    cvar99, stage = orch._compute_phase2_sizing_guards(conviction_downgraded=False)
    assert cvar99 is not None and cvar99 > 0.05
    assert stage == 1
    db.close()
