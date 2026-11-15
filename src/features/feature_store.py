from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence

import pandas as pd

from src.data.candle_collector import align_4h_to_1h
from src.features.indicators import (
    adx_wilder,
    atr_wilder,
    bollinger,
    chop_log10,
    ema,
    rsi_wilder,
    rvol_median,
)
from src.features.order_flow import order_flow_delta_quote, taker_buy_ratio
from src.features.price_action import liquidity_sweep_bull

FeatureKind = Literal[
    "ema",
    "atr",
    "adx",
    "rsi",
    "chop",
    "bb",
    "rvol",
    "tbr",
    "of_delta",
    "sweep",
]


@dataclass(frozen=True, slots=True)
class FeatureRequest:
    kind: FeatureKind
    period: int = 14
    timeframe: Literal["1h", "4h"] = "1h"
    extra: tuple[float, ...] = ()

    def _base_column(self) -> str:
        if self.kind in ("ema", "atr", "adx", "rsi", "chop", "rvol"):
            return f"{self.kind}_{self.period}"
        if self.kind == "bb":
            n_std = self.extra[0] if self.extra else 2.0
            return f"bb_{self.period}_{n_std}_mid"
        if self.kind == "tbr":
            return "tbr"
        if self.kind == "of_delta":
            return "of_delta_quote"
        if self.kind == "sweep":
            return f"liquidity_sweep_bull_{self.period}"
        raise ValueError(f"Unsupported FeatureRequest kind: {self.kind!r}")

    @property
    def column(self) -> str:
        base = self._base_column()
        return base if self.timeframe == "1h" else f"{base}_4h"


class FeatureConsumer(Protocol):
    def required_features(self) -> frozenset[FeatureRequest]: ...


class FeatureStore:
    def __init__(
        self,
        consumers: Sequence[FeatureConsumer] = (),
        *,
        extra_requests: Sequence[FeatureRequest] = (),
    ) -> None:
        combined: set[FeatureRequest] = set(extra_requests)
        for c in consumers:
            combined.update(c.required_features())
        self._requests: frozenset[FeatureRequest] = frozenset(combined)

    @property
    def requests(self) -> frozenset[FeatureRequest]:
        return self._requests

    def compute_timeframe(
        self,
        df: pd.DataFrame,
        timeframe: Literal["1h", "4h"],
    ) -> pd.DataFrame:
        out = df.copy()
        tf_reqs = sorted(
            (r for r in self._requests if r.timeframe == timeframe),
            key=lambda r: (r.kind, r.period, r.extra),
        )
        for req in tf_reqs:
            base_col = req._base_column()
            if req.kind == "ema":
                out[base_col] = ema(out["close"], req.period)
            elif req.kind == "atr":
                out[base_col] = atr_wilder(out, req.period)
            elif req.kind == "adx":
                out[base_col] = adx_wilder(out, req.period)
            elif req.kind == "rsi":
                out[base_col] = rsi_wilder(out["close"], req.period)
            elif req.kind == "chop":
                out[base_col] = chop_log10(out, req.period)
            elif req.kind == "rvol":
                out[base_col] = rvol_median(out["volume"], req.period)
            elif req.kind == "tbr":
                out[base_col] = taker_buy_ratio(
                    out["volume"], out["taker_buy_base_volume"]
                )
            elif req.kind == "of_delta":
                out[base_col] = order_flow_delta_quote(
                    out["quote_volume"], out["taker_buy_quote_volume"]
                )
            elif req.kind == "sweep":
                out[base_col] = liquidity_sweep_bull(out, req.period)
            elif req.kind == "bb":
                n_std = req.extra[0] if req.extra else 2.0
                prefix = f"bb_{req.period}_{n_std}"
                bb_df = bollinger(out["close"], req.period, n_std)
                out[f"{prefix}_mid"] = bb_df["mid"]
                out[f"{prefix}_upper"] = bb_df["upper"]
                out[f"{prefix}_lower"] = bb_df["lower"]
                out[f"{prefix}_bandwidth"] = bb_df["bandwidth"]
        return out

    def build(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> pd.DataFrame:
        frame_1h = self.compute_timeframe(df_1h, "1h")
        has_4h_reqs = any(r.timeframe == "4h" for r in self._requests)
        if not has_4h_reqs and df_4h.empty:
            return frame_1h
        frame_4h = self.compute_timeframe(df_4h, "4h")
        return align_4h_to_1h(frame_1h, frame_4h, suffix="_4h")
