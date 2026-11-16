from __future__ import annotations

from dataclasses import dataclass

from src.core.config import AnalystConfig, StrategiesConfig
from src.radar.orderbook_inspector import OrderbookInspection
from src.radar.structure_ema import Layer123Score


@dataclass(frozen=True, slots=True)
class RadarSignal:
    signal_id: str
    symbol: str
    signal_close_time_ms: int
    emitted_at_ms: int
    setup: str
    regime_4h: str
    score_total: int
    score_l1: int
    score_l2: int
    score_l3: int
    score_l4: int
    grade: str  # "B" | "A" | "A+"
    entry_ref_price: float
    candle_close_price: float
    stop_price: float
    target1_price: float
    target2_price: float
    atr14: float
    horizon_end_ms: int
    cost_roundtrip_pct: float
    features_snapshot: dict[str, object]
    orderbook: OrderbookInspection | None
    ob_ask_wall_before_t2: bool


def score_and_build_signal(
    l123: Layer123Score,
    ob: OrderbookInspection | None,
    *,
    entry_ref_price: float,
    emitted_at_ms: int,
    analyst_cfg: AnalystConfig,
    strategies_cfg: StrategiesConfig,
) -> RadarSignal | None:
    """Layer 5 Confluence Scorer: computes grade, targets, stop, and single cost model (C2-4)."""
    l5 = analyst_cfg.layer_5_scoring_and_targets
    score_l4 = ob.score_l4 if (ob is not None and ob.ob_valid) else 0
    score_total = min(100, l123.subtotal_l1_l3 + score_l4)

    if score_total < l5.min_score_to_alert_telegram:
        return None

    if score_total >= l5.grade_a_plus_min_score:
        grade = "A+"
    elif score_total >= l5.grade_a_min_score:
        grade = "A"
    else:
        grade = "B"

    atr = l123.atr14
    raw_stop = l123.low_price - l5.stop_buffer_below_wick_atr * atr
    min_dist = l5.min_stop_distance_atr * atr
    if (entry_ref_price - raw_stop) < min_dist:
        stop_price = entry_ref_price - min_dist
    else:
        stop_price = raw_stop

    if stop_price <= 0.0 or stop_price >= entry_ref_price:
        return None

    risk_dist = entry_ref_price - stop_price
    target1_price = entry_ref_price + l5.target_1_rr * risk_dist
    target2_price = entry_ref_price + l5.target_2_rr * risk_dist

    ob_ask_wall_before_t2 = False
    if ob is not None and ob.ob_valid and ob.ask_wall_price is not None:
        if entry_ref_price < ob.ask_wall_price < target2_price:
            ob_ask_wall_before_t2 = True
            if l5.adjust_target_before_ask_wall:
                adjusted_t2 = ob.ask_wall_price * 0.999
                if adjusted_t2 > target1_price:
                    target2_price = adjusted_t2

    # C2-4: Single cost model read from strategies.position_sizing
    ps = strategies_cfg.position_sizing
    cost_roundtrip_pct = float(ps.roundtrip_fee_assumption_pct + ps.stress_slippage_assumption_pct)

    # C2-7: Timestamp-based horizon
    horizon_end_ms = int(l123.signal_close_time_ms) + int(l5.time_horizon_hours) * 3_600_000

    signal_id = f"RAD_{l123.symbol}_{l123.signal_close_time_ms}"

    return RadarSignal(
        signal_id=signal_id,
        symbol=l123.symbol,
        signal_close_time_ms=l123.signal_close_time_ms,
        emitted_at_ms=emitted_at_ms,
        setup=l123.setup,
        regime_4h=str(l123.regime_4h.value),
        score_total=score_total,
        score_l1=l123.score_l1,
        score_l2=l123.score_l2,
        score_l3=l123.score_l3,
        score_l4=score_l4,
        grade=grade,
        entry_ref_price=float(entry_ref_price),
        candle_close_price=float(l123.close_price),
        stop_price=float(stop_price),
        target1_price=float(target1_price),
        target2_price=float(target2_price),
        atr14=float(atr),
        horizon_end_ms=horizon_end_ms,
        cost_roundtrip_pct=cost_roundtrip_pct,
        features_snapshot=dict(l123.features_snapshot),
        orderbook=ob,
        ob_ask_wall_before_t2=ob_ask_wall_before_t2,
    )
