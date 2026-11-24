"""Juiz de IA Meta-Labeling com Calibração Platt Scaling, ECE e Modo Observador Silencioso (Blocos 3/4/6)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional, Sequence

import numpy as np
import pandas as pd

from src.core.config import MlJudgeValidationCfg, PositionSizingCfg
from src.risk.position_sizer import SignalGrade

ML_ARTIFACT_SCHEMA_VERSION: Final[int] = 1
ML_FEATURE_NAMES: Final[tuple[str, ...]] = (
    "atr_pct",
    "dist_ema100_atr",
    "rvol_20",
    "chop_14",
    "btc_regime_ret",
    "rs_vol_adjusted_12h",
)


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


@dataclass(frozen=True, slots=True)
class MLJudgeArtifact:
    """Artefato serializável e assinado por SHA-256 do classificador linear + Platt Scaling (B4)."""

    schema_version: int
    regime: str
    feature_names: tuple[str, ...]
    feature_means: tuple[float, ...]
    feature_stds: tuple[float, ...]
    weights: tuple[float, ...]
    intercept: float
    platt_a: float
    platt_b: float
    ece_out_of_sample: float
    net_r_q33_threshold: float
    net_r_q70_threshold: float
    trained_samples: int
    validation_samples: int
    created_at_utc: str
    sha256: str = ""

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "regime": str(self.regime),
            "feature_names": list(self.feature_names),
            "feature_means": [float(x) for x in self.feature_means],
            "feature_stds": [float(x) for x in self.feature_stds],
            "weights": [float(x) for x in self.weights],
            "intercept": float(self.intercept),
            "platt_a": float(self.platt_a),
            "platt_b": float(self.platt_b),
            "ece_out_of_sample": float(self.ece_out_of_sample),
            "net_r_q33_threshold": float(self.net_r_q33_threshold),
            "net_r_q70_threshold": float(self.net_r_q70_threshold),
            "trained_samples": int(self.trained_samples),
            "validation_samples": int(self.validation_samples),
            "created_at_utc": str(self.created_at_utc),
        }

    def compute_sha256(self) -> str:
        raw = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def with_computed_sha256(self) -> MLJudgeArtifact:
        return MLJudgeArtifact(
            schema_version=self.schema_version,
            regime=self.regime,
            feature_names=self.feature_names,
            feature_means=self.feature_means,
            feature_stds=self.feature_stds,
            weights=self.weights,
            intercept=self.intercept,
            platt_a=self.platt_a,
            platt_b=self.platt_b,
            ece_out_of_sample=self.ece_out_of_sample,
            net_r_q33_threshold=self.net_r_q33_threshold,
            net_r_q70_threshold=self.net_r_q70_threshold,
            trained_samples=self.trained_samples,
            validation_samples=self.validation_samples,
            created_at_utc=self.created_at_utc,
            sha256=self.compute_sha256(),
        )

    def score_vector(self, vec: Sequence[float]) -> float:
        if len(vec) != len(self.weights):
            raise ValueError(f"Expected {len(self.weights)} features, got {len(vec)}")
        total = float(self.intercept)
        for x, mu, sd, w in zip(vec, self.feature_means, self.feature_stds, self.weights):
            denom = sd if sd > 1e-12 else 1.0
            z = (float(x) - mu) / denom
            total += w * z
        return float(total)

    def save(self, path: Path) -> Path:
        signed = self.with_computed_sha256()
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = signed.canonical_payload()
        payload["sha256"] = signed.sha256
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: Path, *, verify_sha256: bool = True) -> MLJudgeArtifact:
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"MLJudge artifact not found: {src}")
        data = json.loads(src.read_text(encoding="utf-8"))
        art = cls(
            schema_version=int(data["schema_version"]),
            regime=str(data["regime"]),
            feature_names=tuple(str(x) for x in data["feature_names"]),
            feature_means=tuple(float(x) for x in data["feature_means"]),
            feature_stds=tuple(float(x) for x in data["feature_stds"]),
            weights=tuple(float(x) for x in data["weights"]),
            intercept=float(data["intercept"]),
            platt_a=float(data["platt_a"]),
            platt_b=float(data["platt_b"]),
            ece_out_of_sample=float(data["ece_out_of_sample"]),
            net_r_q33_threshold=float(data["net_r_q33_threshold"]),
            net_r_q70_threshold=float(data["net_r_q70_threshold"]),
            trained_samples=int(data["trained_samples"]),
            validation_samples=int(data["validation_samples"]),
            created_at_utc=str(data["created_at_utc"]),
            sha256=str(data.get("sha256", "")),
        )
        if art.schema_version != ML_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported MLJudgeArtifact schema_version={art.schema_version}")
        if not (
            len(art.feature_names)
            == len(art.feature_means)
            == len(art.feature_stds)
            == len(art.weights)
        ):
            raise ValueError("Inconsistent feature dimensions in MLJudgeArtifact.")
        if verify_sha256:
            expected = art.compute_sha256()
            if not art.sha256 or art.sha256.lower() != expected.lower():
                raise ValueError(
                    f"MLJudgeArtifact SHA-256 mismatch for {src.name}: "
                    f"stored={art.sha256!r}, computed={expected!r}"
                )
        return art


def features_to_vector(
    features: StationaryFeatures,
    rs_vol_adjusted_12h: float = 0.0,
) -> tuple[float, ...]:
    rs_val = float(rs_vol_adjusted_12h) if math.isfinite(float(rs_vol_adjusted_12h)) else 0.0
    return (
        float(features.atr_pct),
        float(features.dist_ema100_atr),
        float(features.rvol_20),
        float(features.chop_14),
        float(features.btc_regime_ret),
        rs_val,
    )


def extract_stationary_features(row: pd.Series) -> StationaryFeatures:
    """Extrai as 5 features estacionárias causais de uma linha alinhada 1h/4h/BTC."""
    close_p = float(row.get("close", float("nan")))
    atr_14 = float(row.get("atr_14", row.get("atr_14_1h", float("nan"))))
    ema_100 = float(row.get("ema_100", row.get("ema_100_1h", float("nan"))))
    rvol_20 = float(row.get("rvol_20", row.get("rvol_20_1h", 1.0)))
    chop_14 = float(row.get("chop_14_4h", row.get("chop_14", 50.0)))
    btc_ret = float(row.get("btc_ret_12h", row.get("btc_regime_ret", row.get("btc_rs_12h", 0.0))))

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


def fit_l2_logistic_and_platt(
    *,
    train_x: np.ndarray,
    train_y: Sequence[int],
    val_x: np.ndarray,
    val_y: Sequence[int],
    val_stop_distances_pct: Sequence[float],
    val_gross_rr_ratios: Sequence[float],
    sizing_cfg: PositionSizingCfg,
    ml_cfg: MlJudgeValidationCfg,
    train_weights: Optional[Sequence[float]] = None,
    val_weights: Optional[Sequence[float]] = None,
    regime: str = "BULL",
    l2_reg: float = 1.0,
    max_iter: int = 100,
) -> MLJudgeArtifact:
    """
    Treina regressão logística L2 ponderada por AFML uniqueness no conjunto de Treino,
    ajusta PlattCalibrator exclusivamente na Validação (OOS) e deriva quantis q33/q70 (B4).
    """
    x_tr = np.asarray(train_x, dtype=np.float64)
    y_tr = np.asarray(train_y, dtype=np.float64)
    x_va = np.asarray(val_x, dtype=np.float64)
    y_va = np.asarray(val_y, dtype=np.float64)
    if x_tr.ndim != 2 or x_va.ndim != 2 or x_tr.shape[1] != x_va.shape[1]:
        raise ValueError("train_x and val_x must be 2D arrays with equal feature count.")
    if len(x_tr) < 5 or len(x_va) < 5:
        raise ValueError("Insufficient train/validation samples to fit and calibrate MLJudge.")

    n_feat = x_tr.shape[1]
    means = np.mean(x_tr, axis=0)
    stds = np.std(x_tr, axis=0)
    safe_stds = np.where(stds > 1e-12, stds, 1.0)

    z_tr = (x_tr - means) / safe_stds
    z_va = (x_va - means) / safe_stds

    w_tr = (
        np.asarray(train_weights, dtype=np.float64)
        if train_weights is not None
        else np.ones(len(y_tr), dtype=np.float64)
    )
    w_tr = w_tr / np.maximum(np.sum(w_tr), 1e-12) * float(len(y_tr))

    # Augmented design matrix [1, z_tr] for Newton-Raphson / IRLS with L2 penalty on weights only
    x_aug = np.column_stack([np.ones(len(z_tr), dtype=np.float64), z_tr])
    theta = np.zeros(n_feat + 1, dtype=np.float64)
    reg_mat = np.diag([0.0] + [float(l2_reg)] * n_feat)

    for _ in range(max_iter):
        logits = np.clip(x_aug @ theta, -30.0, 30.0)
        probs = 1.0 / (1.0 + np.exp(-logits))
        grad = x_aug.T @ (w_tr * (probs - y_tr)) + reg_mat @ theta
        pq = w_tr * probs * (1.0 - probs)
        hess = (x_aug.T * pq) @ x_aug + reg_mat + 1e-6 * np.eye(n_feat + 1)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        theta -= step
        if float(np.linalg.norm(step)) < 1e-7:
            break

    intercept = float(theta[0])
    weights = tuple(float(v) for v in theta[1:])

    val_raw_scores = z_va @ theta[1:] + intercept
    calibrator = PlattCalibrator().fit(
        val_raw_scores.tolist(),
        [int(v > 0.5) for v in y_va],
        sample_weights=val_weights,
    )
    val_p_cal = [calibrator.predict_proba(float(s)) for s in val_raw_scores]
    ece_oos = compute_expected_calibration_error(val_p_cal, [int(v > 0.5) for v in y_va], n_bins=10)

    friction_pct = (
        sizing_cfg.roundtrip_fee_assumption_pct + sizing_cfg.stress_slippage_assumption_pct
    )
    val_net_rs: list[float] = []
    for p_c, d_s, r_g in zip(val_p_cal, val_stop_distances_pct, val_gross_rr_ratios):
        cost_r = friction_pct / max(float(d_s), 1e-9)
        val_net_rs.append(float(p_c) * max(float(r_g), 1e-9) - (1.0 - float(p_c)) - cost_r)

    q33 = float(np.quantile(val_net_rs, float(ml_cfg.reject_bottom_quantile)))
    q70 = float(np.quantile(val_net_rs, float(ml_cfg.grade_a_plus_top_quantile)))
    if q70 <= q33:
        q70 = q33 + 1e-4

    feat_names = (
        ML_FEATURE_NAMES
        if n_feat == len(ML_FEATURE_NAMES)
        else tuple(f"f_{i}" for i in range(n_feat))
    )
    return MLJudgeArtifact(
        schema_version=ML_ARTIFACT_SCHEMA_VERSION,
        regime=str(regime),
        feature_names=feat_names,
        feature_means=tuple(float(v) for v in means),
        feature_stds=tuple(float(v) for v in safe_stds),
        weights=weights,
        intercept=intercept,
        platt_a=float(calibrator.a),
        platt_b=float(calibrator.b),
        ece_out_of_sample=float(ece_oos),
        net_r_q33_threshold=q33,
        net_r_q70_threshold=q70,
        trained_samples=int(len(y_tr)),
        validation_samples=int(len(y_va)),
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    ).with_computed_sha256()


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
        artifact: Optional[MLJudgeArtifact] = None,
    ) -> None:
        self.ml_cfg = ml_cfg
        self.sizing_cfg = sizing_cfg
        self.artifact = artifact
        self.calibrator = calibrator if calibrator is not None else PlattCalibrator()
        self.ece_out_of_sample = float(ece_out_of_sample)
        self.net_r_q33_threshold = float(net_r_q33_threshold)
        self.net_r_q70_threshold = float(net_r_q70_threshold)

    @classmethod
    def from_artifact(
        cls,
        artifact_path: Path,
        ml_cfg: MlJudgeValidationCfg,
        sizing_cfg: PositionSizingCfg,
        *,
        verify_sha256: bool = True,
    ) -> MLJudge:
        art = MLJudgeArtifact.load(artifact_path, verify_sha256=verify_sha256)
        return cls(
            ml_cfg=ml_cfg,
            sizing_cfg=sizing_cfg,
            calibrator=PlattCalibrator(a=art.platt_a, b=art.platt_b),
            ece_out_of_sample=art.ece_out_of_sample,
            net_r_q33_threshold=art.net_r_q33_threshold,
            net_r_q70_threshold=art.net_r_q70_threshold,
            artifact=art,
        )

    def score_features(
        self,
        features: StationaryFeatures,
        *,
        rs_vol_adjusted_12h: float = 0.0,
    ) -> float:
        """Compute linear model raw_score when a calibrated artifact is loaded, else fallback to RS."""
        if self.artifact is not None:
            vec = features_to_vector(features, rs_vol_adjusted_12h=rs_vol_adjusted_12h)
            return self.artifact.score_vector(vec)
        return float(rs_vol_adjusted_12h)

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
