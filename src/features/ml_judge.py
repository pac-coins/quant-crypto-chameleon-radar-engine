"""Juiz de IA Meta-Labeling com Calibração Platt Scaling, ECE e Modo Observador Silencioso (Blocos 3/4/6)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from src.core.config import MlJudgeValidationCfg, PositionSizingCfg
from src.risk.position_sizer import SignalGrade


@dataclass(frozen=True, slots=True)
class StationaryFeatures:
    """As 5 features estacionárias causais da Arquitetura §4.2."""

    atr_pct: float
    dist_ema100_atr: float
    rvol_20: float
    chop_14: float
    btc_regime_ret: float


@dataclass(frozen=True, slots=True)
class MLVerdict:
    """Veredito auditável do Juiz de IA (ativo ou observador silencioso)."""

    raw_score: float
    calibrated_prob: float
    indifference_prob: float
    net_expected_r: float
    grade: SignalGrade
    use_ml_filter_active: bool
    ece_out_of_sample: float


def extract_stationary_features(row: pd.Series) -> StationaryFeatures:
    """Extrai as 5 features estacionárias causais de uma linha alinhada 1h/4h/BTC."""
    close_p = float(row.get("close", float("nan")))
    atr_14 = float(row.get("atr_14", row.get("atr_14_1h", float("nan"))))
    ema_100 = float(row.get("ema_100", row.get("ema_100_1h", float("nan"))))
    rvol_20 = float(row.get("rvol_20", row.get("rvol_20_1h", 1.0)))
    chop_14 = float(row.get("chop_14_4h", row.get("chop_14", 50.0)))
    btc_ret = float(row.get("btc_ret_12h", row.get("btc_regime_ret", 0.0)))

    if math.isfinite(close_p) and close_p > 0.0 and math.isfinite(atr_14) and atr_14 > 0.0:
        atr_pct = atr_14 / close_p
        dist_ema100 = (close_p - ema_100) / atr_14 if math.isfinite(ema_100) else 0.0
    else:
        atr_pct = 0.0
        dist_ema100 = 0.0

    return StationaryFeatures(
        atr_pct=atr_pct if math.isfinite(atr_pct) else 0.0,
        dist_ema100_atr=dist_ema100 if math.isfinite(dist_ema100) else 0.0,
        rvol_20=rvol_20 if math.isfinite(rvol_20) else 1.0,
        chop_14=chop_14 if math.isfinite(chop_14) else 50.0,
        btc_regime_ret=btc_ret if math.isfinite(btc_ret) else 0.0,
    )


def compute_expected_calibration_error(
    calibrated_probs: Sequence[float],
    binary_outcomes: Sequence[int],
    n_bins: int = 10,
) -> float:
    """
    Calcula o Expected Calibration Error (ECE) em n_bins uniformes em [0, 1].
    Exige ECE <= 0.05 fora da amostra para habilitar uso ativo dos quantis.
    """
    probs = np.asarray(calibrated_probs, dtype=float)
    labels = np.asarray(binary_outcomes, dtype=float)
    n = len(probs)
    if n == 0 or len(labels) != n:
        return 0.0

    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        if i == n_bins - 1:
            mask = (probs >= lo) & (probs <= hi)
        else:
            mask = (probs >= lo) & (probs < hi)
        count = int(np.sum(mask))
        if count > 0:
            avg_p = float(np.mean(probs[mask]))
            avg_y = float(np.mean(labels[mask]))
            ece += (count / n) * abs(avg_p - avg_y)
    return float(ece)


class PlattCalibrator:
    """
    Calibração sigmoidal de Platt (1999) com suavização bayesiana de alvos
    (evita overfitting em amostras < 2.000 trades, substituindo Isotonic Regression).
      P(y=1 | f) = 1 / (1 + exp(A * f + B))
    """

    def __init__(self, a: float = -1.0, b: float = 0.0) -> None:
        self.a = float(a)
        self.b = float(b)

    def fit(
        self,
        raw_scores: Sequence[float],
        binary_outcomes: Sequence[int],
        sample_weights: Optional[Sequence[float]] = None,
        max_iter: int = 200,
        lr: float = 0.1,
    ) -> PlattCalibrator:
        f = np.asarray(raw_scores, dtype=float)
        y = np.asarray(binary_outcomes, dtype=float)
        if len(f) == 0:
            return self

        w = (
            np.asarray(sample_weights, dtype=float)
            if sample_weights is not None
            else np.ones_like(f)
        )
        w = w / np.maximum(np.sum(w), 1e-12)

        n_pos = float(np.sum(y > 0.5))
        n_neg = float(len(y) - n_pos)
        t_pos = (n_pos + 1.0) / (n_pos + 2.0)
        t_neg = 1.0 / (n_neg + 2.0)
        targets = np.where(y > 0.5, t_pos, t_neg)

        a = -1.0
        b = math.log((n_neg + 1.0) / (n_pos + 1.0))

        for _ in range(max_iter):
            z = np.clip(a * f + b, -30.0, 30.0)
            p = 1.0 / (1.0 + np.exp(z))
            # Como p = 1 / (1 + exp(z)), d(-LL)/dz = targets - p
            diff = targets - p
            pq = w * p * (1.0 - p)
            g_a = float(np.sum(w * diff * f))
            g_b = float(np.sum(w * diff))
            h_aa = float(np.sum(pq * f * f)) + 1e-8
            h_bb = float(np.sum(pq)) + 1e-8
            h_ab = float(np.sum(pq * f))
            det = h_aa * h_bb - h_ab * h_ab
            if det <= 1e-12:
                break
            da = (h_bb * g_a - h_ab * g_b) / det
            db = (h_aa * g_b - h_ab * g_a) / det
            a -= lr * da
            b -= lr * db
            if abs(da) + abs(db) < 1e-8:
                break

        self.a = float(a)
        self.b = float(b)
        return self

    def predict_proba(self, raw_score: float) -> float:
        z = max(-30.0, min(30.0, self.a * float(raw_score) + self.b))
        return 1.0 / (1.0 + math.exp(z))


class MLJudge:
    """
    Avaliador secundário segregado por regime:
    - Na Fase 1 (`use_ml_filter = False`), roda em Modo Observador Silencioso:
      calcula `calibrated_prob`, `net_expected_r` e `grade` (`C`, `B`, `A+`) para
      registro em `trades.ml_grade_silent` / `trades.ml_p_cal_silent`, mas nunca
      bloqueia nem altera o tamanho do lote.
    - Na Fase 2 (`use_ml_filter = True` e `ece <= max_allowed_ece`), bloqueia sinais
      com `calibrated_prob <= indifference_prob + margin` ou no quantil inferior (`Nota C`).
    """

    def __init__(
        self,
        ml_cfg: MlJudgeValidationCfg,
        sizing_cfg: PositionSizingCfg,
        calibrator: Optional[PlattCalibrator] = None,
        ece_out_of_sample: float = 0.02,
        net_r_q33_threshold: float = 0.05,
        net_r_q70_threshold: float = 0.25,
    ) -> None:
        self.ml_cfg = ml_cfg
        self.sizing_cfg = sizing_cfg
        self.calibrator = calibrator if calibrator is not None else PlattCalibrator()
        self.ece_out_of_sample = float(ece_out_of_sample)
        self.net_r_q33_threshold = float(net_r_q33_threshold)
        self.net_r_q70_threshold = float(net_r_q70_threshold)

    def evaluate_signal(
        self,
        *,
        raw_score: float,
        stop_distance_pct: float,
        gross_rr_ratio: float,
    ) -> MLVerdict:
        p_cal = self.calibrator.predict_proba(raw_score)
        d_stop = max(float(stop_distance_pct), 1e-9)
        r_eff = max(float(gross_rr_ratio), 1e-9)
        friction_pct = (
            self.sizing_cfg.roundtrip_fee_assumption_pct
            + self.sizing_cfg.stress_slippage_assumption_pct
        )
        cost_r = friction_pct / d_stop
        p_indiff = (1.0 + cost_r) / (r_eff + 1.0)
        net_r = p_cal * r_eff - (1.0 - p_cal) - cost_r
        margin = float(self.sizing_cfg.min_expectancy_margin_over_breakeven)

        if p_cal <= p_indiff + margin or net_r <= 0.0 or net_r < self.net_r_q33_threshold:
            grade = SignalGrade.C
        elif net_r >= self.net_r_q70_threshold:
            grade = SignalGrade.A_PLUS
        else:
            grade = SignalGrade.B

        filter_active = bool(
            self.ml_cfg.use_ml_filter and self.ece_out_of_sample <= self.ml_cfg.max_allowed_ece
        )
        return MLVerdict(
            raw_score=float(raw_score),
            calibrated_prob=p_cal,
            indifference_prob=p_indiff,
            net_expected_r=net_r,
            grade=grade,
            use_ml_filter_active=filter_active,
            ece_out_of_sample=self.ece_out_of_sample,
        )
