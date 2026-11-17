from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import pandas as pd

from src.auditing.virtual_auditor import VirtualAuditor
from src.core.binance_client import (
    BinanceAPIError,
    ExchangeInfoCache,
    MonotonicClock,
    PublicBinanceClient,
    SignedBinanceClient,
    build_clients,
)
from src.core.config import (
    AnalystConfig,
    BinanceEnv,
    FORBIDDEN_EXECUTION_SYMBOLS,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_config,
)
from src.core.database import CandleRow, CycleHealth, Database
from src.core.state_reconciler import ReconcileAction, StateReconciler
from src.data.candle_collector import CandleCollector, align_4h_to_1h, align_btc_context, load_frame
from src.execution.executor_protocol import ExecutionReport, ExecutionRequest, NoOpExecutor
from src.execution.oco_order_manager import OcoOrderManager
from src.execution.paper_executor import PaperExecutor
from src.features.feature_store import FeatureRequest, FeatureStore
from src.features.ml_judge import MLJudge
from src.notifications.telegram import (
    HttpTelegramTransport,
    TelegramOutbox,
    format_kill_switch_alert,
    format_radar_signal_alert,
    format_trade_entry_alert,
)
from src.radar.confluence_scorer import RadarSignal, score_and_build_signal
from src.radar.orderbook_inspector import OrderbookInspector
from src.radar.structure_ema import Layer123Score, score_layers_1_to_3
from src.regimes.regime_detector import MarketRegime, RegimeDetector
from src.risk.position_sizer import PositionSizer
from src.risk.shadow_manager import ShadowManager
from src.strategies.bull_trend_pullback import BullTrendPullbackStrategy

logger = logging.getLogger("quant_orchestrator")
ONE_HOUR_MS = 3_600_000
ONE_DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class CycleReport:
    cycle_close_time_ms: int
    mode: RunMode
    maintenance_blocked: bool
    emergency_halted: bool
    chameleon_executed: bool
    chameleon_symbol: Optional[str]
    radar_signals_emitted: int
    radar_signals_resolved: int
    backup_created: Optional[Path]
    unhandled_errors: int


class QuantOrchestrator:
    """Single-loop orchestrator unifying Chameleon v6 execution and Radar v1.1 (C2-9)."""

    def __init__(
        self,
        *,
        settings: RuntimeSettings,
        strategies_cfg: StrategiesConfig,
        analyst_cfg: AnalystConfig,
        db: Database,
        public_client: PublicBinanceClient,
        signed_client: SignedBinanceClient | None,
        clock: MonotonicClock,
        exchange_cache: ExchangeInfoCache,
        outbox: TelegramOutbox,
        collector: CandleCollector | None = None,
        orderbook_inspector: OrderbookInspector | None = None,
        ml_judge: MLJudge | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.strategies_cfg = strategies_cfg
        self.analyst_cfg = analyst_cfg
        self.db = db
        self.public_client = public_client
        self.signed_client = signed_client
        self.clock = clock
        self.exchange_cache = exchange_cache
        self.outbox = outbox
        self._sleep = sleep_fn

        self.collector = collector or CandleCollector(
            client=public_client,
            db=db,
            clock=clock,
            fetch_delay_s=strategies_cfg.execution_and_resilience.candle_close_fetch_delay_seconds,
            max_retries=strategies_cfg.execution_and_resilience.candle_fetch_max_retries,
            sleep=sleep_fn,
        )
        self.regime_detector = RegimeDetector(strategies_cfg.regimes_and_strategies.regime_thresholds_4h)
        self.strategy = BullTrendPullbackStrategy.from_config(
            strategies_cfg.regimes_and_strategies.bull_trend_pullback
        )
        self.feature_store = FeatureStore(
            [self.regime_detector, self.strategy],
            extra_requests=(
                FeatureRequest("ema", analyst_cfg.layer_1_structure_ema.ema_fast, "1h"),
                FeatureRequest("ema", analyst_cfg.layer_1_structure_ema.ema_mid, "1h"),
                FeatureRequest("ema", analyst_cfg.layer_1_structure_ema.ema_slow, "1h"),
                FeatureRequest("chop", analyst_cfg.layer_1_structure_ema.chop_period, "1h"),
                FeatureRequest("rvol", analyst_cfg.layer_3_volume_and_order_flow.rvol_lookback, "1h"),
                FeatureRequest("tbr", 1, "1h"),
                FeatureRequest("sweep", analyst_cfg.layer_2_price_action.liquidity_sweep_lookback_candles, "1h"),
            ),
        )
        self.position_sizer = PositionSizer(
            strategies_cfg.position_sizing,
            strategies_cfg.portfolio,
        )
        self.shadow_manager = ShadowManager(
            strategies_cfg.drawdown_and_shadow_protection,
            strategies_cfg.portfolio,
            db=db,
        )
        self.ml_judge = ml_judge or MLJudge(
            strategies_cfg.ml_judge_and_validation,
            strategies_cfg.position_sizing,
        )
        self.virtual_auditor = VirtualAuditor(db)
        self.orderbook_inspector = orderbook_inspector or OrderbookInspector(
            analyst_cfg.layer_4_orderbook_live,
            sleep_fn=sleep_fn,
        )

        # Executors by RunMode
        self.noop_executor = NoOpExecutor()
        self.paper_executor = PaperExecutor(
            db=db,
            public_client=public_client,
            sizing_cfg=strategies_cfg.position_sizing,
            shadow_manager=self.shadow_manager,
        )
        self.oco_manager: OcoOrderManager | None = None
        self.reconciler: StateReconciler | None = None
        if signed_client is not None:
            self.oco_manager = OcoOrderManager(
                db=db,
                signed_client=signed_client,
                resilience_cfg=strategies_cfg.execution_and_resilience,
                environment=settings.binance_env,
            )
            self.reconciler = StateReconciler(
                db=db,
                signed_client=signed_client,
                oco_manager=self.oco_manager,
                execution_universe=strategies_cfg.universe.symbols,
                filters_provider=exchange_cache.get,
                shadow_manager=self.shadow_manager,
            )

        self._last_clock_sync_ms: int = 0
        self._last_backup_day_utc: int = -1

    def _all_universe_symbols(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                list(self.strategies_cfg.universe.symbols)
                + list(self.analyst_cfg.scanner_universe.symbols)
                + ["BNBUSDT"]
            )
        )

    def boot_reconcile(self, *, enqueue_startup_alert: bool = True) -> None:
        """Run startup clock sync, exchangeInfo refresh, initial backfill, and StateReconciler."""
        self.clock.sync(self.public_client.get_server_time_ms)
        now_ms = self.clock.server_now_ms()
        self._last_clock_sync_ms = now_ms
        self.exchange_cache.refresh_if_stale(now_ms)

        # Ensure minimum historical candles exist on fresh DB boot so 4h regime & EMA 200 work immediately
        all_syms = self._all_universe_symbols()
        need_1h: list[str] = []
        need_4h: list[str] = []
        with self.db.read() as conn:
            for sym in all_syms:
                c1 = conn.execute(
                    "SELECT COUNT(*) FROM candles WHERE symbol = ? AND interval = '1h'",
                    (sym,),
                ).fetchone()[0]
                c4 = conn.execute(
                    "SELECT COUNT(*) FROM candles WHERE symbol = ? AND interval = '4h'",
                    (sym,),
                ).fetchone()[0]
                if int(c1) < 240:
                    need_1h.append(sym)
                if int(c4) < 140:
                    need_4h.append(sym)
        if need_1h:
            self.collector.backfill(need_1h, "1h", lookback_candles=350)
        if need_4h:
            self.collector.backfill(need_4h, "4h", lookback_candles=250)

        if self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1 and self.reconciler is not None:
            res = self.reconciler.reconcile()
            if res.action == ReconcileAction.HALT_REVIEW_UNTRACKED_BALANCE or (
                self.db.get_state("emergency_halt") == "1"
            ):
                self.outbox.enqueue(
                    "KILL_SWITCH",
                    format_kill_switch_alert(
                        self.settings.mode.value,
                        res.details or res.action.value,
                    ),
                    now_ms=now_ms,
                )
                self.outbox.flush(now_ms=now_ms)

        if enqueue_startup_alert:
            offset_ms = self.clock.last_sample.offset_ms if self.clock.last_sample else 0.0
            self.outbox.enqueue(
                "BOT_START",
                (
                    f"🤖 QuantCrypto iniciado\n"
                    f"Modo: {self.settings.mode.value} ({self.settings.binance_env.value})\n"
                    f"Símbolos monitorados: {len(all_syms)} | Offset relógio: {offset_ms:+.1f} ms"
                ),
                now_ms=now_ms,
            )
            self.outbox.flush(now_ms=now_ms)

    def _check_maintenance(self) -> bool:
        if self.signed_client is None:
            return False
        try:
            st = self.signed_client.sapi_system_status()
            return not bool(st.normal)
        except BinanceAPIError as exc:
            logger.warning("sapi_system_status failed (%s); assuming maintenance block.", exc)
            return True
        except Exception as exc:
            logger.warning("sapi_system_status error (%s); assuming maintenance block.", exc)
            return True

    def _count_trades_last_24h(self, now_ms: int) -> int:
        cutoff = now_ms - ONE_DAY_MS
        table = "shadow_trades" if self.settings.mode == RunMode.SHADOW_CHAMELEON else "trades"
        with self.db.read() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE created_at_ms >= ?",
                (int(cutoff),),
            ).fetchone()
        return int(row[0]) if row else 0

    def _get_live_balances(self) -> tuple[Decimal, Decimal, Decimal]:
        """Return (free_usdt, tactical_equity_usdt, bnb_free_usdt_equiv)."""
        if self.signed_client is None:
            eq = Decimal(str(self.settings.shadow_initial_usdt))
            return eq, eq, Decimal("10.00")

        acc = self.signed_client.get_account(omit_zero_balances=True)
        free_usdt = Decimal("0")
        free_bnb = Decimal("0")
        for b in acc.get("balances", []):
            asset = str(b.get("asset", ""))
            if asset == "USDT":
                free_usdt = Decimal(str(b.get("free", "0")))
            elif asset == "BNB":
                free_bnb = Decimal(str(b.get("free", "0")))

        bnb_usdt = Decimal("0")
        if free_bnb > Decimal("0"):
            try:
                bt = self.public_client.get_book_ticker("BNBUSDT")
                bnb_usdt = free_bnb * bt.bid
            except Exception:
                bnb_usdt = Decimal("10.00")
        return free_usdt, free_usdt, bnb_usdt

    def _has_open_live_trade(self) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                """
                SELECT trade_id, symbol, status, oco_order_list_id, oco_qty,
                       oco_stop_price, oco_stop_limit_price, time_barrier_end_ms
                FROM trades
                WHERE status IN ('PENDING_ENTRY','ENTRY_FILLED','PROTECTED','CLOSING','HALT_REVIEW')
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def _load_recent_1h_candle_rows(self, symbol: str, since_ms: int) -> list[CandleRow]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT symbol, interval, open_time_ms, close_time_ms, open, high, low, close,
                       volume, quote_volume, number_of_trades, taker_buy_base_volume,
                       taker_buy_quote_volume, ingested_at_ms
                FROM candles
                WHERE symbol = ? AND interval = '1h' AND open_time_ms >= ?
                ORDER BY open_time_ms ASC
                """,
                (symbol, int(since_ms)),
            ).fetchall()
        return [
            CandleRow(
                symbol=str(r["symbol"]),
                interval="1h",
                open_time_ms=int(r["open_time_ms"]),
                close_time_ms=int(r["close_time_ms"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                quote_volume=float(r["quote_volume"]),
                number_of_trades=int(r["number_of_trades"]),
                taker_buy_base_volume=float(r["taker_buy_base_volume"]),
                taker_buy_quote_volume=float(r["taker_buy_quote_volume"]),
                ingested_at_ms=int(r["ingested_at_ms"]),
            )
            for r in rows
        ]

    def _manage_open_positions(self, now_ms: int) -> None:
        if self.settings.mode == RunMode.SHADOW_CHAMELEON:
            with self.db.read() as conn:
                open_sh = conn.execute(
                    "SELECT symbol, signal_close_time_ms FROM shadow_trades WHERE status = 'OPEN' LIMIT 1"
                ).fetchone()
            if open_sh is not None:
                sym = str(open_sh["symbol"])
                bars = self._load_recent_1h_candle_rows(sym, int(open_sh["signal_close_time_ms"]))
                self.paper_executor.evaluate_open_shadow_trade({sym: bars})
            return

        if self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1 and self.oco_manager is not None:
            if self.reconciler is not None:
                res = self.reconciler.reconcile()
                if res.action == ReconcileAction.HALT_REVIEW_UNTRACKED_BALANCE:
                    self.outbox.enqueue(
                        "KILL_SWITCH",
                        format_kill_switch_alert(
                            self.settings.mode.value,
                            res.details or res.action.value,
                        ),
                        now_ms=now_ms,
                    )
                    return
            open_t = self._has_open_live_trade()
            if open_t is None or open_t["status"] != "PROTECTED":
                return
            trade_id = int(open_t["trade_id"])
            sym = str(open_t["symbol"])
            filters = self.exchange_cache.get(sym)
            if now_ms >= int(open_t["time_barrier_end_ms"]):
                self.oco_manager.close_on_time_barrier(
                    trade_id=trade_id,
                    symbol=sym,
                    base_asset=filters.base_asset,
                    oco_order_list_id=int(open_t["oco_order_list_id"]),
                    oco_qty=Decimal(str(open_t["oco_qty"] or "0")),
                    symbol_filters=filters,
                )
            elif open_t["oco_stop_limit_price"] is not None:
                self.oco_manager.check_hanging_stop_watchdog(
                    trade_id=trade_id,
                    symbol=sym,
                    base_asset=filters.base_asset,
                    oco_order_list_id=int(open_t["oco_order_list_id"]),
                    oco_stop_price=Decimal(str(open_t["oco_stop_price"] or "0")),
                    oco_stop_limit_price=Decimal(str(open_t["oco_stop_limit_price"])),
                    symbol_filters=filters,
                )

    def _execute_chameleon_step(
        self,
        *,
        now_ms: int,
        cycle_close_ms: int,
        aligned_frames: dict[str, pd.DataFrame],
        regimes: dict[str, MarketRegime],
        entries_blocked: bool,
    ) -> tuple[bool, Optional[str]]:
        """Run Chameleon execution path FIRST (C2-9) before Radar L4."""
        self._manage_open_positions(now_ms)
        if entries_blocked or self.settings.mode == RunMode.ANALYST_ONLY:
            return False, None

        if self.settings.mode == RunMode.SHADOW_CHAMELEON and self.paper_executor.has_open_shadow_trade():
            return False, None
        if self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1 and self._has_open_live_trade() is not None:
            return False, None

        if self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1:
            free_usdt, eq_usdt, bnb_usdt = self._get_live_balances()
        else:
            stored_eq = self.db.get_state("shadow_virtual_equity_usdt")
            eq_usdt = Decimal(stored_eq) if stored_eq else Decimal(str(self.settings.shadow_initial_usdt))
            free_usdt = eq_usdt
            bnb_usdt = Decimal("10.00")

        defense = self.shadow_manager.evaluate_defense(
            current_equity_usdt=eq_usdt,
            bnb_free_usdt_equiv=bnb_usdt,
            trades_last_24h=self._count_trades_last_24h(now_ms),
        )
        if defense.emergency_halt:
            self.outbox.enqueue(
                "KILL_SWITCH",
                format_kill_switch_alert(self.settings.mode.value, defense.kill_switch_reason.value),
                now_ms=now_ms,
            )
            return False, None
        if defense.shadow_mode_active and self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1:
            return False, None

        candidates: list[tuple[float, str, Any]] = []
        for sym in self.strategies_cfg.universe.symbols:
            if sym in FORBIDDEN_EXECUTION_SYMBOLS:  # C1-C
                continue
            df_al = aligned_frames.get(sym)
            if df_al is None or df_al.empty:
                continue
            row = df_al.iloc[-1]
            reg = regimes.get(sym, MarketRegime.TRANSITION)
            sig = self.strategy.evaluate_row(sym, row, reg)
            if sig is not None:
                candidates.append((float(sig.rs_vol_adjusted_12h), sym, sig))

        if not candidates:
            return False, None

        candidates.sort(key=lambda item: (-item[0], item[1]))
        _, sym, best_sig = candidates[0]

        filters = self.exchange_cache.get(sym)
        bt = self.public_client.get_book_ticker(sym)
        entry_ref = bt.ask

        ml_eval = self.ml_judge.evaluate_signal(
            raw_score=float(best_sig.rs_vol_adjusted_12h),
            stop_distance_pct=float(best_sig.stop_distance_pct),
            gross_rr_ratio=float(best_sig.gross_rr_ratio),
        )
        pending_hodl = Decimal(self.db.get_state("pending_hodl_bucket_usdt") or "0")

        sizing = self.position_sizer.evaluate_and_size(
            symbol=sym,
            entry_price=entry_ref,
            stop_price=Decimal(str(best_sig.stop_price)),
            target_price=Decimal(str(best_sig.target_price)),
            free_usdt=free_usdt,
            tactical_equity_usdt=eq_usdt,
            pending_hodl_bucket_usdt=pending_hodl,
            symbol_filters=filters,
            drawdown_risk_multiplier=defense.risk_multiplier,
            ml_calibrated_prob=ml_eval.calibrated_prob,
            ml_proposed_grade=ml_eval.grade,
        )
        if not sizing.approved:
            return False, None

        time_barrier_end_ms = cycle_close_ms + int(
            self.strategies_cfg.regimes_and_strategies.bull_trend_pullback.time_barrier_candles_1h
        ) * ONE_HOUR_MS

        req = ExecutionRequest(
            symbol=sym,
            strategy=best_sig.strategy_name,
            regime=best_sig.regime.value,
            signal_close_time_ms=cycle_close_ms,
            atr_at_signal=float(best_sig.atr_14),
            time_barrier_end_ms=time_barrier_end_ms,
            sizing=sizing,
            symbol_filters=filters,
        )

        report: ExecutionReport
        if self.settings.mode == RunMode.SHADOW_CHAMELEON:
            report = self.paper_executor.execute_entry(
                req,
                virtual_equity_before=eq_usdt,
                now_ms=now_ms,
            )
        elif self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1 and self.oco_manager is not None:
            report = self.oco_manager.execute_entry(req)
        else:
            report = self.noop_executor.execute_entry(req)

        if report.executed:
            self.outbox.enqueue(
                "TRADE_ENTRY",
                format_trade_entry_alert(
                    mode=self.settings.mode.value,
                    symbol=sym,
                    regime=best_sig.regime.value,
                    quote_usdt=str(sizing.quote_order_qty),
                    entry_price=str(report.avg_entry_price),
                    stop_price=str(sizing.stop_price),
                    target_price=str(sizing.target_price),
                    ml_grade=sizing.ml_grade_silent,
                ),
                now_ms=now_ms,
            )
            return True, sym
        return False, None

    def _execute_radar_step(
        self,
        *,
        now_ms: int,
        aligned_frames: dict[str, pd.DataFrame],
        regimes: dict[str, MarketRegime],
    ) -> tuple[int, int]:
        """Run Radar L1-L3 -> L4 (top k_max interleaved) -> L5 -> VirtualAuditor (C2-9)."""
        l123_hits: list[Layer123Score] = []
        for sym in self.analyst_cfg.scanner_universe.symbols:
            df_al = aligned_frames.get(sym)
            if df_al is None or df_al.empty:
                continue
            reg = regimes.get(sym, MarketRegime.TRANSITION)
            hit = score_layers_1_to_3(sym, df_al, reg, self.analyst_cfg)
            if hit is not None and hit.subtotal_l1_l3 >= 40:
                l123_hits.append(hit)

        l123_hits.sort(key=lambda h: h.subtotal_l1_l3, reverse=True)
        k_max = int(self.analyst_cfg.layer_4_orderbook_live.max_symbols_per_cycle)
        top_hits = l123_hits[:k_max]

        ob_results = {}
        if self.analyst_cfg.layer_4_orderbook_live.enabled and top_hits:
            ob_results = self.orderbook_inspector.inspect_batch(
                [h.symbol for h in top_hits],
                self.public_client,
            )

        emitted = 0
        for hit in top_hits:
            ob = ob_results.get(hit.symbol)
            if ob is not None and ob.best_ask_snapshot0 > 0.0:
                entry_ref = ob.best_ask_snapshot0
            else:
                try:
                    entry_ref = float(self.public_client.get_book_ticker(hit.symbol).ask)
                except Exception:
                    entry_ref = hit.close_price

            sig: RadarSignal | None = score_and_build_signal(
                hit,
                ob,
                entry_ref_price=entry_ref,
                emitted_at_ms=now_ms,
                analyst_cfg=self.analyst_cfg,
                strategies_cfg=self.strategies_cfg,
            )
            if sig is None:
                continue
            if self.virtual_auditor.record_signal(sig):
                emitted += 1
                self.outbox.enqueue(
                    "RADAR_SIGNAL",
                    format_radar_signal_alert(sig),
                    reply_to_signal_id=sig.signal_id,
                    now_ms=now_ms,
                )

        resolved_list = self.virtual_auditor.resolve_open_signals(now_ms)
        for res in resolved_list:
            r_txt = f"{res.outcome_r_net:+.2f}R" if res.outcome_r_net is not None else "N/A"
            self.outbox.enqueue(
                "RADAR_OUTCOME",
                f"✅ RADAR RESOLVIDO {res.symbol} [{res.grade}]: {res.status} ({r_txt})",
                reply_to_signal_id=res.signal_id,
                now_ms=now_ms,
            )
        return emitted, len(resolved_list)

    def run_once(self, *, now_ms: int | None = None, skip_collect: bool = False) -> CycleReport:
        """Execute a single hourly cycle deterministically."""
        t0 = time.monotonic()
        unhandled = 0
        self.public_client.governor.reset_peak()

        cur_ms = int(now_ms if now_ms is not None else self.clock.server_now_ms())
        sync_interval_ms = int(self.strategies_cfg.execution_and_resilience.clock_sync_interval_minutes) * 60_000
        if cur_ms - self._last_clock_sync_ms >= sync_interval_ms:
            try:
                self.clock.sync(self.public_client.get_server_time_ms)
                self._last_clock_sync_ms = self.clock.server_now_ms()
            except Exception as exc:
                logger.warning("Clock sync failed: %s", exc)
                unhandled += 1

        self.exchange_cache.refresh_if_stale(cur_ms)
        maintenance_blocked = self._check_maintenance()
        emergency_halted = self.db.get_state("emergency_halt") == "1"

        all_symbols = self._all_universe_symbols()
        stale_symbols: list[str] = []
        gaps_detected = 0
        if not skip_collect:
            reports = self.collector.run_cycle(all_symbols)
            for rep in reports:
                stale_symbols.extend(rep.stale)
                for sym, sym_gaps in rep.gaps.items():
                    gaps_detected += len(sym_gaps)
                    try:
                        self.collector.repair_gaps(sym, rep.interval, sym_gaps)
                    except Exception as exc:
                        logger.warning("Gap repair failed for %s (%s): %s", sym, rep.interval, exc)
            stale_symbols = list(dict.fromkeys(stale_symbols))

        aligned_frames: dict[str, pd.DataFrame] = {}
        regimes: dict[str, MarketRegime] = {}
        cycle_close_ms = (cur_ms // ONE_HOUR_MS) * ONE_HOUR_MS

        donchian_lb = int(
            self.strategies_cfg.regimes_and_strategies.bull_trend_pullback.donchian_lookback_1h
        )
        for sym in all_symbols:
            df_1h = load_frame(self.db, sym, "1h", limit=350)
            df_4h = load_frame(self.db, sym, "4h", limit=250)
            if df_1h.empty or df_4h.empty:
                regimes[sym] = MarketRegime.TRANSITION
                continue
            df_1h_f = self.feature_store.compute_timeframe(df_1h, "1h")
            df_4h_f = self.feature_store.compute_timeframe(df_4h, "4h")
            df_al = align_4h_to_1h(df_1h_f, df_4h_f)
            if "atr_14" in df_al.columns:
                df_al["rs_vol_adjusted_12h"] = (df_al["close"] - df_al["close"].shift(12)) / df_al[
                    "atr_14"
                ].replace(0.0, float("nan"))
            if donchian_lb > 0 and "high" in df_al.columns:
                df_al[f"prev_high_{donchian_lb}h"] = (
                    df_al["high"].shift(1).rolling(donchian_lb, min_periods=donchian_lb).max()
                )
            aligned_frames[sym] = df_al
            snap = self.regime_detector.detect_from_aligned_frame(df_al)
            regimes[sym] = snap.regime
            last_1h_close = int(df_al["close_time_ms"].iloc[-1])
            if last_1h_close > cycle_close_ms or cycle_close_ms == (cur_ms // ONE_HOUR_MS) * ONE_HOUR_MS:
                cycle_close_ms = last_1h_close

        # Enrich all non-BTC frames with BTC 12h RS and BTC 4h regime (K9.3 context)
        btc_df = aligned_frames.get("BTCUSDT")
        btc_regime_str = regimes.get("BTCUSDT", MarketRegime.TRANSITION).value
        if btc_df is not None and not btc_df.empty:
            btc_ctx = pd.DataFrame({"close_time_ms": btc_df["close_time_ms"]})
            if "rs_vol_adjusted_12h" in btc_df.columns:
                btc_ctx["rs_12h"] = btc_df["rs_vol_adjusted_12h"]
            for sym, df_al in list(aligned_frames.items()):
                if sym == "BTCUSDT":
                    df_al["btc_rs_12h"] = df_al.get("rs_vol_adjusted_12h", 0.0)
                    df_al["btc_regime_4h"] = btc_regime_str
                    continue
                enriched = align_btc_context(df_al, btc_ctx)
                if "rs_12h_btc" in enriched.columns:
                    enriched["btc_rs_12h"] = enriched["rs_12h_btc"]
                enriched["btc_regime_4h"] = btc_regime_str
                aligned_frames[sym] = enriched

        # C2-9: Chameleon execution path FIRST, Radar L4 SECOND
        chameleon_executed, chameleon_symbol = self._execute_chameleon_step(
            now_ms=cur_ms,
            cycle_close_ms=cycle_close_ms,
            aligned_frames=aligned_frames,
            regimes=regimes,
            entries_blocked=maintenance_blocked or emergency_halted,
        )
        emitted, resolved = self._execute_radar_step(
            now_ms=cur_ms,
            aligned_frames=aligned_frames,
            regimes=regimes,
        )

        self.outbox.flush(now_ms=cur_ms)

        backup_path: Optional[Path] = None
        day_utc = cur_ms // ONE_DAY_MS
        if (
            self.strategies_cfg.execution_and_resilience.daily_sqlite_backup_enabled
            and day_utc > self._last_backup_day_utc
        ):
            try:
                backup_path = self.db.backup_to(self.settings.backup_dir)
                Database.verify_backup(backup_path)
                self._last_backup_day_utc = day_utc
            except Exception as exc:
                logger.error("Daily SQLite backup failed: %s", exc)
                unhandled += 1

        last_sample = self.clock.last_sample
        duration_ms = int((time.monotonic() - t0) * 1000)
        self.db.log_cycle_health(
            CycleHealth(
                cycle_close_time_ms=cycle_close_ms,
                mode=self.settings.mode.value,
                clock_offset_ms=last_sample.offset_ms if last_sample else 0.0,
                clock_rtt_ms=last_sample.rtt_ms if last_sample else 0.0,
                used_weight_1m_peak=self.public_client.governor.peak_weight_1m,
                stale_symbols=tuple(stale_symbols),
                gaps_detected=gaps_detected,
                duration_ms=duration_ms,
                unhandled_errors=unhandled,
            )
        )

        return CycleReport(
            cycle_close_time_ms=cycle_close_ms,
            mode=self.settings.mode,
            maintenance_blocked=maintenance_blocked,
            emergency_halted=self.db.get_state("emergency_halt") == "1",
            chameleon_executed=chameleon_executed,
            chameleon_symbol=chameleon_symbol,
            radar_signals_emitted=emitted,
            radar_signals_resolved=resolved,
            backup_created=backup_path,
            unhandled_errors=unhandled,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config()
    settings = cfg.runtime
    strategies_cfg = cfg.strategies
    analyst_cfg = cfg.analyst
    db = Database(settings.db_path)
    db.connect()
    db.migrate()

    public_client, signed_client, _governor, clock = build_clients(cfg)
    exchange_cache = ExchangeInfoCache(public_client, db, cfg.market_data_universe)
    transport = HttpTelegramTransport(settings.telegram_bot_token.get_secret_value())
    outbox = TelegramOutbox(db, settings.telegram_chat_id, transport=transport)

    orch = QuantOrchestrator(
        settings=settings,
        strategies_cfg=strategies_cfg,
        analyst_cfg=analyst_cfg,
        db=db,
        public_client=public_client,
        signed_client=signed_client,
        clock=clock,
        exchange_cache=exchange_cache,
        outbox=outbox,
    )
    orch.boot_reconcile()

    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    try:
        while not stop_requested:
            orch.collector.wait_for_cycle()
            if stop_requested:
                break
            try:
                rep = orch.run_once()
                logger.info(
                    "Cycle %d complete | mode=%s | maint=%s | halt=%s | cham=%s(%s) | radar=%d/%d",
                    rep.cycle_close_time_ms,
                    rep.mode.value,
                    rep.maintenance_blocked,
                    rep.emergency_halted,
                    rep.chameleon_executed,
                    rep.chameleon_symbol,
                    rep.radar_signals_emitted,
                    rep.radar_signals_resolved,
                )
            except Exception as exc:
                logger.exception("Unhandled error during hourly cycle: %s", exc)
                try:
                    outbox.enqueue(
                        "CYCLE_ERROR",
                        f"⚠️ Erro transitório no ciclo horário ({settings.mode.value}): {exc}",
                    )
                    outbox.flush()
                except Exception:
                    pass
    finally:
        db.close()


if __name__ == "__main__":
    main()
