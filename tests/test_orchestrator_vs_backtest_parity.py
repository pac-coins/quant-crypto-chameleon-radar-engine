"""Automated parity test: QuantOrchestrator.run_once() vs CausalSingleSlotSimulator.

Feeds the EXACT same multi-asset historical 1h/4h OHLCV dataset (stored in a temporary
SQLite database) into:
  1. QuantOrchestrator.run_once() stepped hour-by-hour in SHADOW_CHAMELEON mode using
     the frozen config/strategies.yaml (K9.3) and config/analyst.yaml; and
  2. CausalSingleSlotSimulator.run_multi_asset_simulation() over the exact same candles
     and evaluation window.

Compares on which hourly timestamps (close_time_ms) and in which symbols each engine
decides to enter a trade, WITHOUT modifying src/ or config/strategies.yaml.

Defects discovered and reproduced deterministically by this test suite:
  - Defect #1 (CRITICAL — 100% silent signal loss in live/testnet/shadow execution):
    src/strategies/bull_trend_pullback.py:20 uses `isinstance(val, (int, float))`.
    Because QuantOrchestrator._execute_chameleon_step (src/main.py:883) extracts rows
    via `df_al.iloc[-1]` (preserving `numpy.int64` for `close_time_ms`) whereas
    CausalSingleSlotSimulator (src/backtest/causal_simulator.py:315) extracts rows via
    `df.iterrows()` (unboxing `close_time_ms` to Python `int`), and
    `isinstance(np.int64(...), (int, float))` is False in Python, `_first_finite`
    returns `NaN` for `close_time_ms` on EVERY hourly cycle in QuantOrchestrator —
    causing evaluate_row() to return None 100% of the time in production/testnet/shadow!
    (The exact same bug affects tools/train_ml_judge.py:168, which also uses `df_al.iloc[idx]`.)
  - Defect #2 (MEDIUM — same-bar re-entry on exit candle t_exit):
    src/main.py:782-796 closes an open shadow trade at t_exit and immediately allows a
    new entry on the SAME candle t_exit, whereas src/backtest/causal_simulator.py:325,371
    sets `slot_busy_until_ms = sim_trade.exit_time_ms` and skips `t_ms <= slot_busy_until_ms`.
  - Defect #3 (LOW/MEDIUM — asymmetric 1h/4h warmup window after AUD-01):
    src/main.py:1205-1207 evaluates 4h regime directly on `df_4h_causal`, whereas
    src/backtest/causal_simulator.py:263-264 still calls `detect_series_from_aligned_frame`,
    which only counts unique 4h bars present inside the 1h-truncated aligned frame.
"""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import pytest
from pydantic import SecretStr

import src.strategies.bull_trend_pullback as bull_mod
from src.backtest.causal_simulator import (
    CausalSingleSlotSimulator,
    CostScenario,
    SimulatedTrade,
)
from src.core.binance_client import (
    BookTicker,
    DepthSnapshot,
    ExchangeInfoCache,
    MonotonicClock,
    WeightGovernor,
)
from src.core.config import (
    FORBIDDEN_EXECUTION_SYMBOLS,
    AnalystConfig,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_yaml_strict,
)
from src.core.database import CandleRow, Database, SymbolFilters
from src.features.feature_store import FeatureStore
from src.main import ONE_HOUR_MS, QuantOrchestrator
from src.notifications.telegram import TelegramOutbox
from src.regimes.regime_detector import RegimeDetector
from src.strategies.bull_trend_pullback import BullTrendPullbackStrategy

PARITY_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "BNBUSDT",  # Forbidden for execution (C1-C); included to prove both paths ignore it
)


def _proposed_first_finite_fix(
    row: pd.Series,
    *candidates: str,
    default: float = float("nan"),
) -> float:
    """Test-only reference of the proposed fix for src/strategies/bull_trend_pullback.py:16-22."""
    for col in candidates:
        if col in row:
            val = row[col]
            if (
                val is not None
                and isinstance(val, numbers.Real)
                and not isinstance(val, bool)
                and math.isfinite(float(val))
            ):
                return float(val)
    return default


@dataclass(frozen=True)
class BreakoutSpec:
    """Deterministic 1h breakout bar override + optional post-entry trajectory."""

    symbol: str
    hour_index: int
    body_pct: float = 0.040
    volume_mult: float = 3.0
    tbr: float = 0.72
    outcome: str = "NONE"  # "TARGET" | "STOP" | "HOLD" | "NONE"
    outcome_delay_bars: int = 3


class _ReplayPublicClient:
    """Deterministic stub client returning each symbol's exact candle close as bid/ask and L2 book."""

    def __init__(self, initial_now_ms: int) -> None:
        self.governor = WeightGovernor()
        self.now_ms = int(initial_now_ms)
        self.current_close_by_symbol: dict[str, float] = {}

    def get_server_time_ms(self) -> int:
        return self.now_ms

    def get_book_ticker(self, symbol: str, priority: Any = None) -> BookTicker:
        px = self.current_close_by_symbol.get(symbol, 100.0)
        dec = Decimal(f"{px:.4f}")
        return BookTicker(
            symbol=symbol,
            bid=dec,
            bid_qty=Decimal("1000"),
            ask=dec,
            ask_qty=Decimal("1000"),
        )

    def get_depth(self, symbol: str, limit: int = 20) -> DepthSnapshot:
        px = self.current_close_by_symbol.get(symbol, 100.0)
        return DepthSnapshot(
            symbol=symbol,
            last_update_id=1,
            taken_at_ms=self.now_ms,
            requested_limit=limit,
            bids=((px, 1_000_000.0),),
            asks=((px, 1_000_000.0),),
        )

    def get_exchange_info(self, symbols: Sequence[str]) -> dict[str, SymbolFilters]:
        return {
            s: SymbolFilters(
                symbol=s,
                status="TRADING",
                base_asset=s.replace("USDT", ""),
                quote_asset="USDT",
                tick_size=Decimal("0.0001"),
                step_size=Decimal("0.0001"),
                min_qty=Decimal("0.0001"),
                min_notional=Decimal("5"),
                oco_allowed=True,
                fetched_at_ms=self.now_ms,
            )
            for s in symbols
        }


def _aggregate_1h_to_4h(candles_1h: Sequence[CandleRow]) -> list[CandleRow]:
    """Aggregate exact 4-bar blocks of 1h candles into consistent 4h Binance candles."""
    out: list[CandleRow] = []
    ordered = sorted(candles_1h, key=lambda c: c.open_time_ms)
    four_h_ms = 4 * ONE_HOUR_MS
    buckets: dict[int, list[CandleRow]] = {}
    for c in ordered:
        b_open = (c.open_time_ms // four_h_ms) * four_h_ms
        buckets.setdefault(b_open, []).append(c)

    for b_open in sorted(buckets):
        grp = buckets[b_open]
        if len(grp) != 4:
            continue
        sym = grp[0].symbol
        b_close = b_open + four_h_ms
        out.append(
            CandleRow(
                symbol=sym,
                interval="4h",
                open_time_ms=b_open,
                close_time_ms=b_close,
                open=grp[0].open,
                high=max(x.high for x in grp),
                low=min(x.low for x in grp),
                close=grp[-1].close,
                volume=sum(x.volume for x in grp),
                quote_volume=sum(x.quote_volume for x in grp),
                number_of_trades=sum(x.number_of_trades for x in grp),
                taker_buy_base_volume=sum(x.taker_buy_base_volume for x in grp),
                taker_buy_quote_volume=sum(x.taker_buy_quote_volume for x in grp),
                ingested_at_ms=b_close,
            )
        )
    return out


def _build_multi_asset_history(
    *,
    base_open_ms: int,
    total_hours: int,
    breakouts: Sequence[BreakoutSpec],
    btc_bear_window: tuple[int, int] | None = None,
) -> tuple[dict[str, list[CandleRow]], dict[str, list[CandleRow]]]:
    """
    Build a deterministic multi-asset 1h + 4h history where baseline bars trend gently upward
    (4h BULL, ADX >= 17, CHOP < 50, EMA_9 > EMA_20 > EMA_50 > EMA_100) with modest volume/body
    so K9.3 triggers ONLY on scheduled BreakoutSpec hours.
    """
    bo_map: dict[tuple[str, int], BreakoutSpec] = {
        (b.symbol, b.hour_index): b for b in breakouts
    }
    candles_1h_by_sym: dict[str, list[CandleRow]] = {}
    candles_4h_by_sym: dict[str, list[CandleRow]] = {}

    base_prices: dict[str, float] = {
        "BTCUSDT": 30000.0,
        "ETHUSDT": 2000.0,
        "SOLUSDT": 100.0,
        "XRPUSDT": 1.0,
        "ADAUSDT": 0.50,
        "LINKUSDT": 15.0,
        "AVAXUSDT": 30.0,
        "BNBUSDT": 300.0,
    }

    for sym in PARITY_SYMBOLS:
        px = base_prices[sym]
        rows_1h: list[CandleRow] = []
        scheduled_moves: dict[int, tuple[str, float]] = {}

        for h in range(total_hours):
            o_ms = base_open_ms + h * ONE_HOUR_MS
            c_ms = o_ms + ONE_HOUR_MS
            base_vol = 1000.0
            in_btc_bear = (
                sym == "BTCUSDT"
                and btc_bear_window is not None
                and btc_bear_window[0] <= h <= btc_bear_window[1]
            )

            spec = bo_map.get((sym, h))
            move = scheduled_moves.get(h)

            if in_btc_bear:
                o_p = px
                c_p = px * 0.992
                h_p = o_p * 1.001
                l_p = c_p * 0.998
                vol = base_vol * 1.1
                tbr = 0.40
            elif spec is not None:
                o_p = px
                c_p = px * (1.0 + spec.body_pct)
                h_p = c_p * 1.003
                l_p = o_p * 0.999
                vol = base_vol * spec.volume_mult
                tbr = spec.tbr

                if spec.outcome == "TARGET":
                    for d in range(1, spec.outcome_delay_bars):
                        scheduled_moves[h + d] = ("HOLD", c_p)
                    scheduled_moves[h + spec.outcome_delay_bars] = ("TARGET", c_p)
                elif spec.outcome == "STOP":
                    for d in range(1, spec.outcome_delay_bars):
                        scheduled_moves[h + d] = ("HOLD", c_p)
                    scheduled_moves[h + spec.outcome_delay_bars] = ("STOP", c_p)
                elif spec.outcome == "HOLD":
                    for d in range(1, 75):
                        scheduled_moves[h + d] = ("HOLD", c_p)
            elif move is not None:
                kind, ref_px = move
                if kind == "HOLD":
                    o_p = px
                    c_p = px * 1.0004
                    h_p = c_p * 1.0015
                    l_p = o_p * 0.9990
                    vol = base_vol * 0.90
                    tbr = 0.51
                elif kind == "TARGET":
                    o_p = px
                    c_p = px * 1.002
                    h_p = ref_px * 1.35
                    l_p = o_p * 0.999
                    vol = base_vol * 0.95
                    tbr = 0.51
                elif kind == "STOP":
                    o_p = px
                    c_p = px * 1.001
                    h_p = c_p * 1.002
                    l_p = ref_px * 0.75
                    vol = base_vol * 0.95
                    tbr = 0.49
                else:
                    o_p = px
                    c_p = px * 1.0015
                    h_p = c_p * 1.0012
                    l_p = o_p * 0.9992
                    vol = base_vol
                    tbr = 0.51
            else:
                o_p = px
                c_p = px * 1.0015
                h_p = c_p * 1.0012
                l_p = o_p * 0.9992
                vol = base_vol
                tbr = 0.50

            rows_1h.append(
                CandleRow(
                    symbol=sym,
                    interval="1h",
                    open_time_ms=o_ms,
                    close_time_ms=c_ms,
                    open=o_p,
                    high=h_p,
                    low=l_p,
                    close=c_p,
                    volume=vol,
                    quote_volume=vol * c_p,
                    number_of_trades=120,
                    taker_buy_base_volume=vol * tbr,
                    taker_buy_quote_volume=vol * tbr * c_p,
                    ingested_at_ms=c_ms,
                )
            )
            px = c_p

        candles_1h_by_sym[sym] = rows_1h
        candles_4h_by_sym[sym] = _aggregate_1h_to_4h(rows_1h)

    return candles_1h_by_sym, candles_4h_by_sym


def _run_orchestrator_vs_simulator(
    *,
    tmp_path: Path,
    candles_1h_by_sym: Mapping[str, Sequence[CandleRow]],
    candles_4h_by_sym: Mapping[str, Sequence[CandleRow]],
    eval_start_ms: int,
    eval_end_ms: int,
    db_name: str = "parity.db",
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], list[dict[str, Any]], list[SimulatedTrade]]:
    """
    Execute BOTH QuantOrchestrator.run_once() (hour-by-hour causal replay in SQLite)
    and CausalSingleSlotSimulator.run_multi_asset_simulation() on the EXACT same candles.
    """
    st_cfg = StrategiesConfig.model_validate(load_yaml_strict(Path("config/strategies.yaml")))
    an_cfg = AnalystConfig.model_validate(load_yaml_strict(Path("config/analyst.yaml")))

    db_path = tmp_path / db_name
    db = Database(db_path)
    db.connect()
    db.migrate()

    warmup_rows: list[CandleRow] = []
    for sym in PARITY_SYMBOLS:
        warmup_rows.extend(c for c in candles_1h_by_sym[sym] if c.close_time_ms < eval_start_ms)
        warmup_rows.extend(c for c in candles_4h_by_sym[sym] if c.close_time_ms < eval_start_ms)
    db.upsert_candles(warmup_rows)

    eval_1h_by_ts: dict[int, list[CandleRow]] = {}
    eval_4h_by_ts: dict[int, list[CandleRow]] = {}
    for sym in PARITY_SYMBOLS:
        for c in candles_1h_by_sym[sym]:
            if eval_start_ms <= c.close_time_ms <= eval_end_ms:
                eval_1h_by_ts.setdefault(c.close_time_ms, []).append(c)
        for c in candles_4h_by_sym[sym]:
            if eval_start_ms <= c.close_time_ms <= eval_end_ms:
                eval_4h_by_ts.setdefault(c.close_time_ms, []).append(c)

    eval_timestamps = sorted(eval_1h_by_ts.keys())
    assert eval_timestamps, "Evaluation window must contain at least one 1h candle"

    pub = _ReplayPublicClient(eval_timestamps[0] + 3_000)
    clock = MonotonicClock()
    clock.sync(pub.get_server_time_ms, samples=1)

    all_syms = list(
        dict.fromkeys(
            list(st_cfg.universe.symbols) + list(an_cfg.scanner_universe.symbols) + ["BNBUSDT"]
        )
    )
    ex_cache = ExchangeInfoCache(pub, db, all_syms)  # type: ignore[arg-type]
    ex_cache.refresh_if_stale(eval_timestamps[0] + 3_000)

    outbox = TelegramOutbox(db, "parity-chat", transport=None)
    settings = RuntimeSettings(
        mode=RunMode.SHADOW_CHAMELEON,
        shadow_initial_usdt=10_000.0,
        telegram_bot_token=SecretStr("parity-dummy-token"),
        telegram_chat_id="parity-chat",
        db_path=db_path,
        backup_dir=tmp_path / f"backups_{db_name}",
    )

    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=st_cfg,
        analyst_cfg=an_cfg,
        db=db,
        public_client=pub,  # type: ignore[arg-type]
        signed_client=None,
        clock=clock,
        exchange_cache=ex_cache,
        outbox=outbox,
        sleep_fn=lambda _: None,
    )

    for t_ms in eval_timestamps:
        batch = list(eval_1h_by_ts[t_ms]) + list(eval_4h_by_ts.get(t_ms, []))
        db.upsert_candles(batch)
        pub.now_ms = t_ms + 3_000
        for c in eval_1h_by_ts[t_ms]:
            pub.current_close_by_symbol[c.symbol] = float(c.close)
        rep = orch.run_once(now_ms=t_ms + 3_000, skip_collect=True)
        assert rep.unhandled_errors == 0

    with db.read() as conn:
        orch_shadow_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT shadow_id, symbol, strategy, regime, signal_close_time_ms,
                       status, exit_reason, closed_at_ms, sim_entry_price, stop_price, target_price
                FROM shadow_trades
                ORDER BY shadow_id ASC
                """
            ).fetchall()
        ]

    exec_symbols = [
        s for s in st_cfg.universe.symbols if s not in FORBIDDEN_EXECUTION_SYMBOLS
    ]
    regime_detector = RegimeDetector(st_cfg.regimes_and_strategies.regime_thresholds_4h)
    strategy = BullTrendPullbackStrategy.from_config(
        st_cfg.regimes_and_strategies.bull_trend_pullback
    )
    feature_store = FeatureStore([regime_detector, strategy])

    aligned_frames: dict[str, pd.DataFrame] = {}
    for sym in exec_symbols:
        df_1h = db.load_candles(sym, "1h", limit=10_000, until_close_time_ms=eval_end_ms)
        df_4h = db.load_candles(sym, "4h", limit=10_000, until_close_time_ms=eval_end_ms)
        if not df_1h.empty and not df_4h.empty:
            aligned_frames[sym] = feature_store.build(df_1h, df_4h)

    sim = CausalSingleSlotSimulator(
        cost_scenario=CostScenario.BASE,
        risk_per_trade_pct=st_cfg.position_sizing.default_grade_b_risk_pct,
        max_cash_pct=st_cfg.position_sizing.default_grade_b_max_cash_pct,
        cppi_enabled=(
            st_cfg.drawdown_and_shadow_protection.protection_mode == "CPPI_DRAWDOWN_CONTROL"
        ),
        cppi_drawdown_threshold_pct=st_cfg.drawdown_and_shadow_protection.cppi_drawdown_threshold_pct,
        cppi_reduction_factor=st_cfg.drawdown_and_shadow_protection.cppi_risk_reduction_factor,
    )
    sim_trades = sim.run_multi_asset_simulation(
        aligned_frames=aligned_frames,
        strategies=[strategy],
        regime_detector=regime_detector,
        start_time_ms=eval_start_ms,
        end_time_ms=eval_end_ms,
    )

    db.close()

    orch_entries = [(int(r["signal_close_time_ms"]), str(r["symbol"])) for r in orch_shadow_rows]
    sim_entries = [(int(t.entry_time_ms), str(t.symbol)) for t in sim_trades]
    return orch_entries, sim_entries, orch_shadow_rows, sim_trades


def test_parity_unmodified_orchestrator_exposes_np_int64_total_signal_loss(tmp_path: Path) -> None:
    """
    DEFECT #1 (CRITICAL): Feeds UNMODIFIED QuantOrchestrator.run_once() and
    CausalSingleSlotSimulator with identical SQLite history (zero monkeypatching).

    Root cause:
      - src/strategies/bull_trend_pullback.py:20 checks `isinstance(val, (int, float))`.
      - src/main.py:883 extracts `row = df_al.iloc[-1]` where `row["close_time_ms"]` is
        `numpy.int64` -> `isinstance(np.int64(...), (int, float))` is FALSE -> `_first_finite`
        returns NaN -> `evaluate_row()` returns None on 100% of live/testnet/shadow cycles!
      - src/backtest/causal_simulator.py:315 iterates via `df.iterrows()`, which unboxes
        `close_time_ms` to Python `int` -> `evaluate_row()` succeeds in backtest.
    """
    base_open_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    total_hours = 530
    eval_start_h = 523

    breakouts = [
        BreakoutSpec("SOLUSDT", hour_index=524, body_pct=0.055, volume_mult=3.5, tbr=0.75),
    ]
    c1h, c4h = _build_multi_asset_history(
        base_open_ms=base_open_ms,
        total_hours=total_hours,
        breakouts=breakouts,
    )
    eval_start_ms = base_open_ms + (eval_start_h + 1) * ONE_HOUR_MS
    eval_end_ms = base_open_ms + (526 + 1) * ONE_HOUR_MS

    orch_entries, sim_entries, _, _ = _run_orchestrator_vs_simulator(
        tmp_path=tmp_path,
        candles_1h_by_sym=c1h,
        candles_4h_by_sym=c4h,
        eval_start_ms=eval_start_ms,
        eval_end_ms=eval_end_ms,
        db_name="parity_unmodified_defect1.db",
    )

    t_sol = base_open_ms + (524 + 1) * ONE_HOUR_MS

    # Backtest simulator fires SOLUSDT cleanly at h=524
    assert sim_entries == [(t_sol, "SOLUSDT")]
    # Unmodified QuantOrchestrator silently drops 100% of signals due to np.int64 in _first_finite!
    assert orch_entries == []

    # Direct proof of root cause on pandas iloc[-1] vs iterrows():
    df_probe = pd.DataFrame(
        {"close_time_ms": [np.int64(t_sol)], "btc_regime_4h": ["BULL"], "close": [231.0]}
    )
    row_iloc = df_probe.iloc[-1]
    row_iterrows = next(r for _, r in df_probe.iterrows())
    assert isinstance(row_iloc["close_time_ms"], np.int64)
    assert not isinstance(row_iloc["close_time_ms"], (int, float))
    assert math.isnan(bull_mod._first_finite(row_iloc, "close_time_ms", "close_time"))
    assert bull_mod._first_finite(row_iterrows, "close_time_ms", "close_time") == float(t_sol)


def test_parity_multi_asset_entries_tiebreak_single_slot_and_time_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Core parity verification (632h = 158x4h bars, exceeding load_frame limit=500):
    With ONLY `_first_finite` monkeypatched in-test to isolate the rest of the pipeline
    (zero edits to src/), verifies QuantOrchestrator.run_once() and CausalSingleSlotSimulator
    decide the EXACT same entry hours and symbols across:
      1. Simultaneous multi-asset K9.3 breakout with RS_12h tie-breaker (SOLUSDT > ETHUSDT)
         and C1-C BNBUSDT exclusion -> TARGET exit;
      2. Single-asset K9.3 breakout (ETHUSDT) -> STOP exit;
      3. Single-slot suppression while XRPUSDT is open (SOLUSDT breakout ignored by both)
         -> TIME_BARRIER (72h) exit;
      4. Post-TIME_BARRIER breakout (LINKUSDT).
    """
    monkeypatch.setattr(bull_mod, "_first_finite", _proposed_first_finite_fix)

    base_open_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    total_hours = 632
    eval_start_h = 522

    breakouts = [
        BreakoutSpec("BNBUSDT", hour_index=524, body_pct=0.070, volume_mult=4.0, tbr=0.80),
        BreakoutSpec("SOLUSDT", hour_index=524, body_pct=0.055, volume_mult=3.5, tbr=0.75, outcome="TARGET", outcome_delay_bars=3),
        BreakoutSpec("ETHUSDT", hour_index=524, body_pct=0.035, volume_mult=3.0, tbr=0.70),
        BreakoutSpec("ETHUSDT", hour_index=534, body_pct=0.050, volume_mult=3.5, tbr=0.74, outcome="STOP", outcome_delay_bars=3),
        BreakoutSpec("XRPUSDT", hour_index=546, body_pct=0.055, volume_mult=3.5, tbr=0.76, outcome="HOLD"),
        BreakoutSpec("SOLUSDT", hour_index=560, body_pct=0.060, volume_mult=4.0, tbr=0.78),
        BreakoutSpec("LINKUSDT", hour_index=624, body_pct=0.055, volume_mult=3.5, tbr=0.75),
    ]

    c1h, c4h = _build_multi_asset_history(
        base_open_ms=base_open_ms,
        total_hours=total_hours,
        breakouts=breakouts,
    )

    eval_start_ms = base_open_ms + (eval_start_h + 1) * ONE_HOUR_MS
    eval_end_ms = base_open_ms + total_hours * ONE_HOUR_MS

    orch_entries, sim_entries, orch_rows, sim_trades = _run_orchestrator_vs_simulator(
        tmp_path=tmp_path,
        candles_1h_by_sym=c1h,
        candles_4h_by_sym=c4h,
        eval_start_ms=eval_start_ms,
        eval_end_ms=eval_end_ms,
        db_name="parity_core.db",
    )

    expected_entries = [
        (base_open_ms + (524 + 1) * ONE_HOUR_MS, "SOLUSDT"),
        (base_open_ms + (534 + 1) * ONE_HOUR_MS, "ETHUSDT"),
        (base_open_ms + (546 + 1) * ONE_HOUR_MS, "XRPUSDT"),
        (base_open_ms + (624 + 1) * ONE_HOUR_MS, "LINKUSDT"),
    ]

    assert orch_entries == expected_entries, f"Orchestrator entries mismatch: {orch_entries}"
    assert sim_entries == expected_entries, f"Simulator entries mismatch: {sim_entries}"
    assert orch_entries == sim_entries

    assert orch_rows[0]["exit_reason"] == "TARGET"
    assert sim_trades[0].exit_reason == "TARGET"
    assert int(orch_rows[0]["closed_at_ms"]) == sim_trades[0].exit_time_ms

    assert orch_rows[1]["exit_reason"] == "STOP"
    assert sim_trades[1].exit_reason == "STOP"
    assert int(orch_rows[1]["closed_at_ms"]) == sim_trades[1].exit_time_ms

    assert orch_rows[2]["exit_reason"] == "TIME"
    assert sim_trades[2].exit_reason == "TIME_BARRIER"
    assert int(orch_rows[2]["closed_at_ms"]) == sim_trades[2].exit_time_ms


def test_parity_btc_bear_veto_blocks_altcoin_entries_in_both_engines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verifies K9.3 BTC 4h BEAR veto parity (with _first_finite isolated via monkeypatch so
    the veto is genuinely tested rather than masked by Defect #1).
    """
    monkeypatch.setattr(bull_mod, "_first_finite", _proposed_first_finite_fix)

    base_open_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    total_hours = 552
    eval_start_h = 536

    breakouts = [
        BreakoutSpec("SOLUSDT", hour_index=540, body_pct=0.055, volume_mult=3.5, tbr=0.76),
    ]

    c1h, c4h = _build_multi_asset_history(
        base_open_ms=base_open_ms,
        total_hours=total_hours,
        breakouts=breakouts,
        btc_bear_window=(480, 551),
    )

    eval_start_ms = base_open_ms + (eval_start_h + 1) * ONE_HOUR_MS
    eval_end_ms = base_open_ms + total_hours * ONE_HOUR_MS

    orch_entries, sim_entries, _, _ = _run_orchestrator_vs_simulator(
        tmp_path=tmp_path,
        candles_1h_by_sym=c1h,
        candles_4h_by_sym=c4h,
        eval_start_ms=eval_start_ms,
        eval_end_ms=eval_end_ms,
        db_name="parity_btc_bear.db",
    )

    assert orch_entries == []
    assert sim_entries == []
    assert orch_entries == sim_entries


def test_parity_exposes_same_bar_reentry_divergence_on_exit_candle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    DEFECT #2 (MEDIUM): Reproduces same-bar re-entry divergence when a new breakout fires on
    the EXACT same candle t_exit where the prior trade exits (with Defect #1 isolated in-test):
      - QuantOrchestrator._execute_chameleon_step (src/main.py:782-796) closes the shadow
        trade at t_exit FIRST and immediately allows a new entry on the SAME candle t_exit.
      - CausalSingleSlotSimulator (src/backtest/causal_simulator.py:325,371) sets
        slot_busy_until_ms = sim_trade.exit_time_ms and skips t_ms <= slot_busy_until_ms.
    """
    monkeypatch.setattr(bull_mod, "_first_finite", _proposed_first_finite_fix)

    base_open_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    total_hours = 536
    eval_start_h = 522

    breakouts = [
        BreakoutSpec("SOLUSDT", hour_index=524, body_pct=0.055, volume_mult=3.5, tbr=0.75, outcome="TARGET", outcome_delay_bars=3),
        BreakoutSpec("ETHUSDT", hour_index=527, body_pct=0.055, volume_mult=3.5, tbr=0.76),
    ]

    c1h, c4h = _build_multi_asset_history(
        base_open_ms=base_open_ms,
        total_hours=total_hours,
        breakouts=breakouts,
    )

    eval_start_ms = base_open_ms + (eval_start_h + 1) * ONE_HOUR_MS
    eval_end_ms = base_open_ms + total_hours * ONE_HOUR_MS

    orch_entries, sim_entries, _, _ = _run_orchestrator_vs_simulator(
        tmp_path=tmp_path,
        candles_1h_by_sym=c1h,
        candles_4h_by_sym=c4h,
        eval_start_ms=eval_start_ms,
        eval_end_ms=eval_end_ms,
        db_name="parity_same_bar.db",
    )

    t_sol = base_open_ms + (524 + 1) * ONE_HOUR_MS
    t_eth_same_exit_bar = base_open_ms + (527 + 1) * ONE_HOUR_MS

    assert orch_entries == [(t_sol, "SOLUSDT"), (t_eth_same_exit_bar, "ETHUSDT")]
    assert sim_entries == [(t_sol, "SOLUSDT")]
    assert orch_entries != sim_entries


def test_parity_exposes_asymmetric_1h_4h_warmup_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    DEFECT #3 (LOW/MEDIUM): Reproduces asymmetric 1h/4h warmup divergence when the DB has
    >= 120 4h candles (150x4h) but < 480 1h candles (160x1h -> only 40 4h bars in df_aligned):
      - QuantOrchestrator.run_once() (src/main.py:1205-1207, AUD-01 fix) classifies BULL on
        df_4h_causal and enters SOLUSDT.
      - CausalSingleSlotSimulator (src/backtest/causal_simulator.py:263-264) still uses
        detect_series_from_aligned_frame(df_aligned) and stays stuck in WARMUP_LT_120 ([]).
    """
    monkeypatch.setattr(bull_mod, "_first_finite", _proposed_first_finite_fix)

    base_open_ms = 1_700_000_000_000 - (1_700_000_000_000 % (4 * ONE_HOUR_MS))
    total_hours = 600
    eval_start_h = 576

    breakouts = [
        BreakoutSpec("SOLUSDT", hour_index=580, body_pct=0.055, volume_mult=3.5, tbr=0.76),
    ]

    full_1h, full_4h = _build_multi_asset_history(
        base_open_ms=base_open_ms,
        total_hours=total_hours,
        breakouts=breakouts,
    )

    cutoff_1h_open_ms = base_open_ms + 440 * ONE_HOUR_MS
    trimmed_1h = {
        sym: [c for c in rows if c.open_time_ms >= cutoff_1h_open_ms]
        for sym, rows in full_1h.items()
    }

    eval_start_ms = base_open_ms + (eval_start_h + 1) * ONE_HOUR_MS
    eval_end_ms = base_open_ms + total_hours * ONE_HOUR_MS

    orch_entries, sim_entries, _, _ = _run_orchestrator_vs_simulator(
        tmp_path=tmp_path,
        candles_1h_by_sym=trimmed_1h,
        candles_4h_by_sym=full_4h,
        eval_start_ms=eval_start_ms,
        eval_end_ms=eval_end_ms,
        db_name="parity_asym_warmup.db",
    )

    t_sol = base_open_ms + (580 + 1) * ONE_HOUR_MS

    assert orch_entries == [(t_sol, "SOLUSDT")]
    assert sim_entries == []
    assert orch_entries != sim_entries
