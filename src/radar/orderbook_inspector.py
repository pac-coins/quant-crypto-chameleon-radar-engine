from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from src.core.binance_client import DepthSnapshot
from src.core.config import OrderbookLayerCfg


class DepthClientProtocol(Protocol):
    def get_depth(self, symbol: str, limit: int = 500) -> DepthSnapshot: ...


@dataclass(frozen=True, slots=True)
class SnapshotMetrics:
    seq: int
    symbol: str
    requested_limit: int
    taken_at_ms: int
    last_update_id: int
    best_bid: float
    best_ask: float
    mid: float
    spread_bps: float
    bid_coverage_pct: float
    ask_coverage_pct: float
    band_eff_pct: float
    bid_notional_band: float
    ask_notional_band: float
    obi: float | None
    obi_valid: bool
    impact_bps_ref: float | None
    buckets_json: str
    bid_buckets: dict[int, float]
    ask_buckets: dict[int, float]


@dataclass(frozen=True, slots=True)
class DetectedWall:
    side: str  # "BID" | "ASK"
    bucket_idx: int
    bucket_low: float
    bucket_high: float
    notional_mean: float
    median_multiple: float
    persistence: int


@dataclass(frozen=True, slots=True)
class OrderbookInspection:
    symbol: str
    ob_valid: bool
    band_eff_pct: float
    obi_mean: float | None
    score_l4: int
    best_ask_snapshot0: float
    bid_wall_price: float | None
    bid_wall_notional: float | None
    ask_wall_price: float | None
    ask_wall_notional: float | None
    snapshots: tuple[SnapshotMetrics, ...]
    walls: tuple[DetectedWall, ...]


def analyze_snapshot(
    snap: DepthSnapshot,
    *,
    seq: int,
    mid_anchor: float,
    cfg: OrderbookLayerCfg,
) -> SnapshotMetrics:
    """Compute symmetric effective band (C1-A) and anchored 0.1% buckets (C2-1)."""
    if not snap.bids or not snap.asks:
        return SnapshotMetrics(
            seq=seq,
            symbol=snap.symbol,
            requested_limit=snap.requested_limit,
            taken_at_ms=snap.taken_at_ms,
            last_update_id=snap.last_update_id,
            best_bid=0.0,
            best_ask=0.0,
            mid=0.0,
            spread_bps=0.0,
            bid_coverage_pct=0.0,
            ask_coverage_pct=0.0,
            band_eff_pct=0.0,
            bid_notional_band=0.0,
            ask_notional_band=0.0,
            obi=None,
            obi_valid=False,
            impact_bps_ref=None,
            buckets_json="{}",
            bid_buckets={},
            ask_buckets={},
        )

    best_bid = max(p for p, _ in snap.bids)
    best_ask = min(p for p, _ in snap.asks)
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0.0:
        raise ValueError(f"Invalid mid price {mid} for {snap.symbol}")

    spread_bps = ((best_ask - best_bid) / mid) * 10_000.0
    min_bid = min(p for p, _ in snap.bids)
    max_ask = max(p for p, _ in snap.asks)

    bid_cov = max(0.0, (mid - min_bid) / mid)
    ask_cov = max(0.0, (max_ask - mid) / mid)
    band_eff = min(cfg.imbalance_band_pct, bid_cov, ask_cov)
    obi_valid = band_eff >= cfg.min_valid_band_pct

    lo_price = mid * (1.0 - band_eff)
    hi_price = mid * (1.0 + band_eff)

    anchor = mid_anchor if mid_anchor > 0.0 else mid
    bucket_width = anchor * cfg.wall_bucket_pct

    bid_notional_band = 0.0
    ask_notional_band = 0.0
    bid_buckets: dict[int, float] = {}
    ask_buckets: dict[int, float] = {}

    for p, q in snap.bids:
        if lo_price <= p <= mid and p > 0.0 and q > 0.0:
            notional = p * q
            bid_notional_band += notional
            idx = math.floor((p - anchor) / bucket_width)
            bid_buckets[idx] = bid_buckets.get(idx, 0.0) + notional

    for p, q in snap.asks:
        if mid <= p <= hi_price and p > 0.0 and q > 0.0:
            notional = p * q
            ask_notional_band += notional
            idx = math.floor((p - anchor) / bucket_width)
            ask_buckets[idx] = ask_buckets.get(idx, 0.0) + notional

    total_band = bid_notional_band + ask_notional_band
    if obi_valid and total_band > 0.0:
        obi: float | None = (bid_notional_band - ask_notional_band) / total_band
    else:
        obi = None
        obi_valid = False

    # Reference 10,000 USDT market-buy price impact in bps
    ref_usdt = 10_000.0
    rem = ref_usdt
    cost_sum = 0.0
    qty_sum = 0.0
    for p, q in sorted(snap.asks, key=lambda x: x[0]):
        if p <= 0.0 or q <= 0.0:
            continue
        lvl_notional = p * q
        take = min(rem, lvl_notional)
        cost_sum += take
        qty_sum += take / p
        rem -= take
        if rem <= 1e-9:
            break
    impact_bps_ref: float | None = None
    if qty_sum > 0.0 and rem <= 1e-6:
        avg_fill = cost_sum / qty_sum
        impact_bps_ref = ((avg_fill - mid) / mid) * 10_000.0

    # Compact ~1 KB summary JSON (C2-8: never store raw Binance depth JSON)
    top_bids = sorted(bid_buckets.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_asks = sorted(ask_buckets.items(), key=lambda kv: kv[1], reverse=True)[:5]
    buckets_json = json.dumps(
        {
            "mid_anchor": round(anchor, 8),
            "bucket_width": round(bucket_width, 8),
            "top_bid_buckets": [[int(k), round(v, 2)] for k, v in top_bids],
            "top_ask_buckets": [[int(k), round(v, 2)] for k, v in top_asks],
        },
        separators=(",", ":"),
    )

    return SnapshotMetrics(
        seq=seq,
        symbol=snap.symbol,
        requested_limit=snap.requested_limit,
        taken_at_ms=snap.taken_at_ms,
        last_update_id=snap.last_update_id,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread_bps=spread_bps,
        bid_coverage_pct=bid_cov,
        ask_coverage_pct=ask_cov,
        band_eff_pct=band_eff,
        bid_notional_band=bid_notional_band,
        ask_notional_band=ask_notional_band,
        obi=obi,
        obi_valid=obi_valid,
        impact_bps_ref=impact_bps_ref,
        buckets_json=buckets_json,
        bid_buckets=bid_buckets,
        ask_buckets=ask_buckets,
    )


def _detect_side_walls(
    side: str,
    snapshots: Sequence[SnapshotMetrics],
    *,
    mid_anchor: float,
    cfg: OrderbookLayerCfg,
) -> list[DetectedWall]:
    if not snapshots or mid_anchor <= 0.0:
        return []
    bucket_width = mid_anchor * cfg.wall_bucket_pct

    all_values: list[float] = []
    per_idx: dict[int, list[float]] = {}
    for sm in snapshots:
        bmap = sm.bid_buckets if side == "BID" else sm.ask_buckets
        for idx, val in bmap.items():
            if val > 0.0:
                all_values.append(val)
                per_idx.setdefault(idx, []).append(val)

    if not all_values:
        return []
    med = statistics.median(all_values)
    if med <= 0.0:
        return []

    walls: list[DetectedWall] = []
    for idx, vals in per_idx.items():
        persistence = len(vals)
        if persistence < 2:
            continue
        mean_val = sum(vals) / persistence
        mult = mean_val / med
        if mult >= cfg.whale_wall_multiplier:
            b_low = mid_anchor + idx * bucket_width
            b_high = b_low + bucket_width
            walls.append(
                DetectedWall(
                    side=side,
                    bucket_idx=idx,
                    bucket_low=b_low,
                    bucket_high=b_high,
                    notional_mean=mean_val,
                    median_multiple=mult,
                    persistence=persistence,
                )
            )
    walls.sort(key=lambda w: w.notional_mean, reverse=True)
    return walls


def evaluate_snapshots_for_symbol(
    raw_snapshots: Sequence[DepthSnapshot],
    cfg: OrderbookLayerCfg,
) -> OrderbookInspection:
    """Evaluate 1..3 snapshots for a single symbol using C1-A and C2-1."""
    if not raw_snapshots:
        raise ValueError("raw_snapshots must not be empty")

    sym = raw_snapshots[0].symbol
    snap0 = raw_snapshots[0]
    if snap0.bids and snap0.asks:
        mid_0 = (max(p for p, _ in snap0.bids) + min(p for p, _ in snap0.asks)) / 2.0
        best_ask_0 = min(p for p, _ in snap0.asks)
    else:
        mid_0 = 0.0
        best_ask_0 = 0.0

    metrics: list[SnapshotMetrics] = []
    for seq, raw in enumerate(raw_snapshots):
        metrics.append(analyze_snapshot(raw, seq=seq, mid_anchor=mid_0, cfg=cfg))

    all_valid = all(m.obi_valid and m.obi is not None for m in metrics)
    band_eff_min = min(m.band_eff_pct for m in metrics)

    if not all_valid:
        return OrderbookInspection(
            symbol=sym,
            ob_valid=False,
            band_eff_pct=band_eff_min,
            obi_mean=None,
            score_l4=0,
            best_ask_snapshot0=best_ask_0,
            bid_wall_price=None,
            bid_wall_notional=None,
            ask_wall_price=None,
            ask_wall_notional=None,
            snapshots=tuple(metrics),
            walls=(),
        )

    obi_vals = [float(m.obi) for m in metrics if m.obi is not None]
    obi_mean = sum(obi_vals) / len(obi_vals)

    bid_walls = _detect_side_walls("BID", metrics, mid_anchor=mid_0, cfg=cfg)
    ask_walls = _detect_side_walls("ASK", metrics, mid_anchor=mid_0, cfg=cfg)
    all_walls = tuple(bid_walls + ask_walls)

    score_l4 = 0
    if obi_mean >= cfg.bullish_obi_threshold:
        score_l4 += 15
    elif obi_mean > 0.0:
        score_l4 += 8
    elif obi_mean <= cfg.bearish_obi_warning_threshold:
        score_l4 = 0

    if bid_walls and obi_mean > cfg.bearish_obi_warning_threshold:
        score_l4 += 7
    if not ask_walls and obi_mean > 0.0:
        score_l4 += 3
    score_l4 = max(0, min(25, score_l4))

    top_bid = bid_walls[0] if bid_walls else None
    top_ask = ask_walls[0] if ask_walls else None

    return OrderbookInspection(
        symbol=sym,
        ob_valid=True,
        band_eff_pct=band_eff_min,
        obi_mean=obi_mean,
        score_l4=score_l4,
        best_ask_snapshot0=best_ask_0,
        bid_wall_price=(top_bid.bucket_low + top_bid.bucket_high) / 2.0 if top_bid else None,
        bid_wall_notional=top_bid.notional_mean if top_bid else None,
        ask_wall_price=(top_ask.bucket_low + top_ask.bucket_high) / 2.0 if top_ask else None,
        ask_wall_notional=top_ask.notional_mean if top_ask else None,
        snapshots=tuple(metrics),
        walls=all_walls,
    )


class OrderbookInspector:
    """Layer 4 live order book inspector with interleaved anti-spoofing snapshots (C2-9)."""

    def __init__(
        self,
        cfg: OrderbookLayerCfg,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = cfg
        self._sleep = sleep_fn

    def inspect_batch(
        self,
        symbols: Sequence[str],
        client: DepthClientProtocol,
    ) -> dict[str, OrderbookInspection]:
        if not self._cfg.enabled or not symbols:
            return {}

        selected = list(dict.fromkeys(symbols))[: self._cfg.max_symbols_per_cycle]
        n_snaps = max(1, int(self._cfg.anti_spoofing_snapshots))
        collected: dict[str, list[DepthSnapshot]] = {s: [] for s in selected}

        for snap_idx in range(n_snaps):
            for sym in selected:
                limit = int(self._cfg.depth_limit_overrides.get(sym, self._cfg.depth_limit_default))
                collected[sym].append(client.get_depth(sym, limit=limit))
            if snap_idx < n_snaps - 1 and self._cfg.snapshot_interval_seconds > 0:
                self._sleep(float(self._cfg.snapshot_interval_seconds))

        return {
            sym: evaluate_snapshots_for_symbol(snaps, self._cfg)
            for sym, snaps in collected.items()
        }
