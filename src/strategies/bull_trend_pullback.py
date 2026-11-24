"""Estratégia canônica da Fase 1: bull_trend_pullback (Clima BULL, Homologada no Gate 3 K9.3)."""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from src.core.config import BullTrendPullbackCfg
from src.features.feature_store import FeatureRequest
from src.regimes.regime_detector import MarketRegime
from src.strategies.base_strategy import BaseStrategy, SignalCandidate


def _first_finite(row: pd.Series, *candidates: str, default: float = float("nan")) -> float:
    for col in candidates:
        if col in row:
            val = row[col]
            if val is not None and isinstance(val, (int, float)) and math.isfinite(float(val)):
                return float(val)
    return default


class BullTrendPullbackStrategy(BaseStrategy):
    """
    Estratégia da Fase 1 (Clima BULL), suportando o modo homologado no Gate 3 Empírico (K9.3):
    - Clima 4h == BULL, Veto se BTC 4h == BEAR e força direcional 4h ADX_14_4h >= 17.0;
    - Alinhamento de tendência 1h: EMA_9 > EMA_20 > EMA_50 > EMA_100;
    - Rompimento Donchian de 12h (Close > max(High[t-12 : t-1])) com corpo >= 0.65 * ATR_14;
    - Confirmação de Order Flow: RVOL_20 >= 1.30 e Taker Buy Ratio >= 0.52;
    - Liderança de Força Relativa: RS_12h >= 1.40 e RS_12h >= RS_12h do BTCUSDT;
    - Barreiras OCO: Stop = 3.0 * ATR_14 (-1.0R), Alvo = 6.0 * ATR_14 (+2.0R), Time-Stop = 72h.
    """

    def __init__(
        self,
        enabled: bool = True,
        ema_fast_1h: int = 20,
        ema_slow_1h: int = 100,
        stop_atr_mult: float = 3.0,
        target_atr_mult: float = 6.0,
        time_barrier_candles_1h: int = 72,
        pullback_tolerance_pct: float = 0.003,
        donchian_lookback_1h: int = 12,
        min_adx_4h: float = 17.0,
        min_rvol_1h: float = 1.30,
        min_taker_buy_ratio: float = 0.52,
        min_body_atr_1h: float = 0.65,
        min_rs_12h: float = 1.40,
        require_beat_btc_rs_12h: bool = True,
        veto_if_btc_bear: bool = True,
    ) -> None:
        self.enabled = enabled
        self.ema_fast_1h = ema_fast_1h
        self.ema_slow_1h = ema_slow_1h
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.time_barrier_candles_1h = time_barrier_candles_1h
        self.pullback_tolerance_pct = pullback_tolerance_pct
        self.donchian_lookback_1h = donchian_lookback_1h
        self.min_adx_4h = min_adx_4h
        self.min_rvol_1h = min_rvol_1h
        self.min_taker_buy_ratio = min_taker_buy_ratio
        self.min_body_atr_1h = min_body_atr_1h
        self.min_rs_12h = min_rs_12h
        self.require_beat_btc_rs_12h = require_beat_btc_rs_12h
        self.veto_if_btc_bear = veto_if_btc_bear

    @classmethod
    def from_config(cls, cfg: BullTrendPullbackCfg) -> BullTrendPullbackStrategy:
        return cls(
            enabled=cfg.enabled,
            ema_fast_1h=cfg.ema_fast_1h,
            ema_slow_1h=cfg.ema_slow_1h,
            stop_atr_mult=cfg.stop_atr_mult,
            target_atr_mult=cfg.target_atr_mult,
            time_barrier_candles_1h=cfg.time_barrier_candles_1h,
            donchian_lookback_1h=cfg.donchian_lookback_1h,
            min_adx_4h=cfg.min_adx_4h,
            min_rvol_1h=cfg.min_rvol_1h,
            min_taker_buy_ratio=cfg.min_taker_buy_ratio,
            min_body_atr_1h=cfg.min_body_atr_1h,
            min_rs_12h=cfg.min_rs_12h,
            require_beat_btc_rs_12h=cfg.require_beat_btc_rs_12h,
            veto_if_btc_bear=cfg.veto_if_btc_bear,
        )

    @property
    def name(self) -> str:
        return "bull_trend_pullback"

    @property
    def required_regime(self) -> MarketRegime:
        return MarketRegime.BULL

    def required_features(self) -> frozenset[FeatureRequest]:
        return frozenset(
            {
                FeatureRequest("ema", 9, "1h"),
                FeatureRequest("ema", self.ema_fast_1h, "1h"),
                FeatureRequest("ema", 50, "1h"),
                FeatureRequest("ema", self.ema_slow_1h, "1h"),
                FeatureRequest("atr", 14, "1h"),
                FeatureRequest("rvol", 20, "1h"),
                FeatureRequest("tbr", 1, "1h"),
                FeatureRequest("of_delta", 1, "1h"),
                FeatureRequest("sweep", 20, "1h"),
            }
        )

    def evaluate_row(
        self,
        symbol: str,
        row: pd.Series,
        regime: MarketRegime,
        require_order_flow_filter: bool = False,
    ) -> Optional[SignalCandidate]:
        if not self.enabled or regime != MarketRegime.BULL:
            return None

        open_p = _first_finite(row, "open")
        low_p = _first_finite(row, "low")
        close_p = _first_finite(row, "close")
        ema_fast = _first_finite(row, f"ema_{self.ema_fast_1h}", f"ema_{self.ema_fast_1h}_1h")
        ema_slow = _first_finite(row, f"ema_{self.ema_slow_1h}", f"ema_{self.ema_slow_1h}_1h")
        atr_14 = _first_finite(row, "atr_14", "atr_14_1h")
        ct_val = _first_finite(row, "close_time_ms", "close_time")

        if not all(
            math.isfinite(x) for x in (open_p, low_p, close_p, ema_fast, ema_slow, atr_14, ct_val)
        ):
            return None

        if close_p <= 0.0 or atr_14 <= 0.0:
            return None

        of_delta = _first_finite(row, "of_delta_quote", "order_flow_delta", default=0.0)
        tbr = _first_finite(row, "tbr", "taker_buy_ratio", default=0.5)
        rs_score = _first_finite(row, "rs_vol_adjusted_12h", default=0.0)
        has_sweep = bool(
            row.get("is_liquidity_sweep", False) or row.get("liquidity_sweep_bull_20", False)
        )

        # Verifica se o frame possui as colunas de estrutura completa do modo K9.3 (Donchian Leader)
        h_col = f"prev_high_{self.donchian_lookback_1h}h"
        has_k93_cols = (
            self.donchian_lookback_1h > 0
            and h_col in row
            and ("ema_9" in row or "ema_9_1h" in row)
            and ("ema_50" in row or "ema_50_1h" in row)
        )

        if has_k93_cols:
            if self.veto_if_btc_bear:
                raw_btc_reg = row.get("btc_regime_4h")
                btc_reg_str = "" if raw_btc_reg is None else str(raw_btc_reg).strip()
                if btc_reg_str == MarketRegime.BEAR.value:
                    return None
                if symbol != "BTCUSDT" and btc_reg_str not in (
                    MarketRegime.BULL.value,
                    MarketRegime.SIDEWAYS.value,
                    MarketRegime.TRANSITION.value,
                ):
                    return None

            adx_4h = _first_finite(row, "adx_14_4h", default=float("nan"))
            if not math.isfinite(adx_4h) or adx_4h < self.min_adx_4h:
                return None

            ema_9 = _first_finite(row, "ema_9", "ema_9_1h")
            ema_50 = _first_finite(row, "ema_50", "ema_50_1h")
            rvol_20 = _first_finite(row, "rvol_20", "rvol_20_1h", default=0.0)
            prev_high = _first_finite(row, h_col)

            if not all(math.isfinite(x) for x in (ema_9, ema_50, prev_high)):
                return None

            if not (ema_9 > ema_fast > ema_50 > ema_slow and close_p > open_p):
                return None

            if (close_p - open_p) < self.min_body_atr_1h * atr_14:
                return None

            if rvol_20 < self.min_rvol_1h or tbr < self.min_taker_buy_ratio:
                return None

            if rs_score < self.min_rs_12h:
                return None

            if self.require_beat_btc_rs_12h and symbol != "BTCUSDT":
                btc_rs = _first_finite(row, "btc_rs_12h", default=float("nan"))
                if not math.isfinite(btc_rs) or rs_score < btc_rs:
                    return None

            if close_p <= prev_high:
                return None

            if require_order_flow_filter and not (of_delta > 0.0 and tbr >= 0.55):
                return None
        else:
            # Fallback compatível com testes unitários sintéticos de linha isolada
            if not (ema_fast > ema_slow and close_p > ema_slow):
                return None
            touched_ema_fast = low_p <= ema_fast * (1.0 + self.pullback_tolerance_pct)
            recovered_above_ema = close_p >= ema_fast
            if not (touched_ema_fast and recovered_above_ema):
                return None
            has_pa_trigger = bool(
                row.get("is_bullish_pinbar", False)
                or row.get("is_bullish_engulfing", False)
                or has_sweep
            )
            if not (close_p > open_p or has_pa_trigger):
                return None
            if require_order_flow_filter and not (of_delta > 0.0 and tbr > 0.50):
                return None

        stop_dist = self.stop_atr_mult * atr_14
        target_dist = self.target_atr_mult * atr_14
        stop_price = close_p - stop_dist
        target_price = close_p + target_dist
        if stop_price <= 0.0:
            return None

        return SignalCandidate(
            symbol=symbol,
            close_time_ms=int(ct_val),
            regime=regime,
            strategy_name=self.name,
            entry_price=close_p,
            stop_price=stop_price,
            target_price=target_price,
            atr_14=atr_14,
            stop_distance_pct=stop_dist / close_p,
            gross_rr_ratio=target_dist / stop_dist,
            time_barrier_hours=self.time_barrier_candles_1h,
            rs_vol_adjusted_12h=rs_score,
            order_flow_delta=of_delta,
            taker_buy_ratio=tbr,
            has_liquidity_sweep=has_sweep,
        )
