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
from src.core.database import CycleHealth, Database
from src.core.state_reconciler import StateReconciler
from src.data.candle_collector import CandleCollector, align_4h_to_1h, load_frame
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
            db,
            strategies_cfg.portfolio,
            strategies_cfg.drawdown_and_shadow_protection,
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

    def boot_reconcile(self) -> None:
        """Run startup clock sync, exchangeInfo refresh, and StateReconciler (LIVE only)."""
        self.clock.sync(self.public_client.get_server_time_ms)
        now_ms = self.clock.server_now_ms()
        self._last_clock_sync_ms = now_ms
        self.exchange_cache.refresh_if_stale(now_ms)
        if self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1 and self.reconciler is not None:
            res = self.reconciler.reconcile()
            if res.halted:
                self.outbox.enqueue(
                    "KILL_SWITCH",
                    format_kill_switch_alert(self.settings.mode.value, res.details),
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
                SELECT trade_id, symbol, status, oco_order_list_id, oco_stop_price,
                       oco_stop_limit_price, time_barrier_end_ms
                FROM trades
                WHERE status IN ('PENDING_ENTRY','ENTRY_FILLED','PROTECTED','CLOSING','HALT_REVIEW')
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def _manage_open_positions(self, now_ms: int) -> None:
        if self.settings.mode == RunMode.SHADOW_CHAMELEON:
            if self.paper_executor.has_open_shadow_trade():
                self.paper_executor.evaluate_open_shadow_trade()
            return

        if self.settings.mode == RunMode.LIVE_CHAMELEON_PHASE_1 and self.oco_manager is not None:
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
                    oco_order_list_id=int(open_t["oco_order_list_id"]),
                    filters=filters,
                )
            elif open_t["oco_stop_limit_price"] is not None:
                self.oco_manager.check_hanging_stop_watchdog(
                    trade_id=trade_id,
                    symbol=sym,
                    oco_order_list_id=int(open_t["oco_order_list_id"]),
                    stop_limit_price=Decimal(str(open_t["oco_stop_limit_price"])),
                    filters=filters,
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
        if defense.emergency_halted or defense.bnb_hard_stop:
            self.outbox.enqueue(
                "KILL_SWITCH",
                format_kill_switch_alert(self.settings.mode.value, defense.kill_switch_reason.value),
                now_ms=now_ms,
            )
            return False, None

        btc_regime = regimes.get("BTCUSDT", MarketRegime.TRANSITION)
        candidates: list[tuple[float, Any, pd.Series]] = []
        for sym in self.strategies_cfg.universe.symbols:
            if sym in FORBIDDEN_EXECUTION_SYMBOLS:  # C1-C
                continue
            df_al = aligned_frames.get(sym)
            if df_al is None or df_al.empty:
                continue
            row = df_al.iloc[-1]
            reg = regimes.get(sym, MarketRegime.TRANSITION)
            sig = self.strategy.evaluate_row(
                sym,
                row,
                reg,
                btc_regime=btc_regime,
            )
            if sig is not None:
                candidates.append((float(sig.rs_score), sig, row))

        if not candidates:
            return False, None

        candidates.sort(key=lambda item: item[0], reverse=True)
        _, best_sig, best_row = candidates[0]
        sym = best_sig.symbol

        filters = self.exchange_cache.get(sym)
        bt = self.public_client.get_book_ticker(sym)
        entry_ref = bt.ask

        ml_eval = self.ml_judge.evaluate_signal(best_row, regime=best_sig.regime)
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
            drawdown_risk_multiplier=defense.drawdown_risk_multiplier,
            ml_calibrated_prob=ml_eval.p_calibrated,
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
                ml_grade_silent=ml_eval.grade.value,
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
                    ml_grade=ml_eval.grade.value,
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

        all_symbols = tuple(
            dict.fromkeys(
                list(self.strategies_cfg.universe.symbols)
                + list(self.analyst_cfg.scanner_universe.symbols)
                + ["BNBUSDT"]
            )
        )
        stale_symbols: list[str] = []
        gaps_detected = 0
        if not skip_collect:
            reports = self.collector.run_cycle(all_symbols)
            for rep in reports:
                gaps_detected += rep.gaps_found
                if not rep.up_to_date:
                    stale_symbols.append(rep.symbol)

        aligned_frames: dict[str, pd.DataFrame] = {}
        regimes: dict[str, MarketRegime] = {}
        cycle_close_ms = (cur_ms // ONE_HOUR_MS) * ONE_HOUR_MS

        for sym in all_symbols:
            df_1h = load_frame(self.db, sym, "1h", limit=350)
            df_4h = load_frame(self.db, sym, "4h", limit=250)
            if df_1h.empty or df_4h.empty:
                regimes[sym] = MarketRegime.TRANSITION
                continue
            df_1h_f = self.feature_store.compute_timeframe(df_1h, "1h")
            df_4h_f = self.feature_store.compute_timeframe(df_4h, "4h")
            df_al = align_4h_to_1h(df_1h_f, df_4h_f)
            aligned_frames[sym] = df_al
            snap = self.regime_detector.detect_from_aligned_frame(df_al)
            regimes[sym] = snap.regime
            last_1h_close = int(df_al["close_time_ms"].iloc[-1])
            if last_1h_close > cycle_close_ms or cycle_close_ms == (cur_ms // ONE_HOUR_MS) * ONE_HOUR_MS:
                cycle_close_ms = last_1h_close

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

    public_client, signed_client, clock, exchange_cache = build_clients(cfg, db)
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
            orch.run_once()
    finally:
        db.close()


if __name__ == "__main__":
    main()
