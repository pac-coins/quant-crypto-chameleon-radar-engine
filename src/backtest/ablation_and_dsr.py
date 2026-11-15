"""Governança Anti-Overfitting, AFML Sample Uniqueness, DSR e Gate 3 (Bloco 3)."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

from src.backtest.causal_simulator import SimulatedTrade


@dataclass(frozen=True)
class PartitionBounds:
    """Fronteiras temporais 60% Treino / 25% Validação / 15% Holdout com Purging e Embargo."""

    train_end_ms: int
    val_start_ms: int
    val_end_ms: int
    holdout_start_ms: int
    holdout_end_ms: int


@dataclass(frozen=True)
class Gate3Report:
    """Relatório binário de aprovação do Gate 3 (Bloco 3)."""

    n_trades: int
    mean_net_r: float
    ci95_lower_r: float
    ci95_upper_r: float
    profit_factor_base: float
    profit_factor_pessimistic: float
    sharpe_annualized: float
    deflated_sharpe_ratio: float
    cvar_99_r: float
    positive_subperiods: int
    total_subperiods: int
    holdout_positive: bool
    passed_gate3: bool


def split_temporal_vaults(
    timestamps_ms: Sequence[int],
    train_pct: float = 0.60,
    val_pct: float = 0.25,
    purge_hours: int = 24,
    embargo_pct: float = 0.02,
) -> PartitionBounds:
    """
    Divide cronologicamente o histórico em 3 cofres (Treino / Validação / Holdout)
    aplicando Purging de 24h e Embargo de 2% nas fronteiras.
    """
    sorted_ts = sorted(set(int(t) for t in timestamps_ms))
    if len(sorted_ts) < 10:
        raise ValueError("Histórico insuficiente para particionamento em 3 cofres.")

    n = len(sorted_ts)
    train_idx = int(n * train_pct) - 1
    val_idx = int(n * (train_pct + val_pct)) - 1
    embargo_bars = max(1, int(n * embargo_pct))
    purge_ms = purge_hours * 3_600_000

    train_end_ms = sorted_ts[max(0, train_idx)]
    val_start_ms = max(
        train_end_ms + purge_ms,
        sorted_ts[min(n - 1, train_idx + embargo_bars)],
    )
    val_end_ms = sorted_ts[max(train_idx + 1, val_idx)]
    holdout_start_ms = max(
        val_end_ms + purge_ms,
        sorted_ts[min(n - 1, val_idx + embargo_bars)],
    )
    holdout_end_ms = sorted_ts[-1]

    return PartitionBounds(
        train_end_ms=train_end_ms,
        val_start_ms=val_start_ms,
        val_end_ms=val_end_ms,
        holdout_start_ms=holdout_start_ms,
        holdout_end_ms=holdout_end_ms,
    )


def compute_afml_sample_uniqueness(
    events: Sequence[Tuple[int, int]],
) -> np.ndarray:
    """
    AFML Snippet 4.2 (López de Prado): calcula u_bar_i = média de (1 / c_t)
    ao longo das barras discretas [t_entry, t_exit] de cada evento i.
    """
    if not events:
        return np.array([], dtype=np.float64)

    # Discretiza em passos horários se os timestamps estiverem em ms, ou em inteiros diretos
    min_span = min(max(1, end - start) for start, end in events)
    step = 3_600_000 if min_span >= 3_600_000 else 1

    concurrency: Dict[int, int] = {}
    discretized_events: List[List[int]] = []

    for start, end in events:
        s_idx = int(start // step)
        e_idx = max(s_idx, int(end // step))
        bars = list(range(s_idx, e_idx + 1))
        discretized_events.append(bars)
        for b in bars:
            concurrency[b] = concurrency.get(b, 0) + 1

    uniqueness = np.zeros(len(events), dtype=np.float64)
    for i, bars in enumerate(discretized_events):
        uniqueness[i] = float(np.mean([1.0 / concurrency[b] for b in bars]))

    return uniqueness


def compute_profit_factor(net_r_series: Sequence[float]) -> float:
    """Calcula o Profit Factor (soma dos ganhos brutos / soma das perdas absolutas)."""
    arr = np.asarray(net_r_series, dtype=np.float64)
    gains = float(np.sum(arr[arr > 0.0]))
    losses = float(np.abs(np.sum(arr[arr < 0.0])))
    if losses <= 1e-12:
        return 99.0 if gains > 0.0 else 0.0
    return gains / losses


def compute_cvar_99(net_r_series: Sequence[float]) -> float:
    """Calcula o Conditional Value at Risk (Expected Shortfall) a 99% em unidades de R."""
    arr = np.asarray(net_r_series, dtype=np.float64)
    if len(arr) == 0:
        return 0.0
    cutoff = float(np.percentile(arr, 1.0))
    tail = arr[arr <= cutoff]
    return float(np.mean(tail)) if len(tail) > 0 else cutoff


def compute_block_bootstrap_ci95(
    net_r_series: Sequence[float],
    block_size: int = 5,
    n_bootstraps: int = 1000,
    seed: int = 42,
) -> Tuple[float, float]:
    """
    Calcula o Intervalo de Confiança de 95% para a média de R via Circular Block Bootstrap.
    """
    arr = np.asarray(net_r_series, dtype=np.float64)
    n = len(arr)
    if n == 0:
        return (0.0, 0.0)
    if n == 1:
        return (float(arr[0]), float(arr[0]))

    rng = np.random.default_rng(seed)
    b_len = max(1, min(block_size, n))
    n_blocks = int(math.ceil(n / b_len))
    boot_means = np.empty(n_bootstraps, dtype=np.float64)

    doubled = np.concatenate([arr, arr])
    for b in range(n_bootstraps):
        starts = rng.integers(0, n, size=n_blocks)
        sample = np.concatenate([doubled[s : s + b_len] for s in starts])[:n]
        boot_means[b] = np.mean(sample)

    low, high = np.percentile(boot_means, [2.5, 97.5])
    return (float(low), float(high))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Aproximação racional de Acklam/Beasley-Springer-Moro para a inversa da normal padrão."""
    p_clamped = min(max(p, 1e-12), 1.0 - 1e-12)
    # Usa aproximação de alta precisão via binary search sobre _norm_cdf (rápido e exato < 1e-12)
    lo, hi = -10.0, 10.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _norm_cdf(mid) < p_clamped:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def compute_deflated_sharpe_ratio(
    returns_r: Sequence[float],
    n_trials: int,
    trials_sharpe_variance: float = 0.25,
    trades_per_year: float = 150.0,
) -> float:
    """
    Calcula o Deflated Sharpe Ratio (Bailey & López de Prado, 2014):
    DSR = Phi( ((SR_hat - SR_0) * sqrt(T - 1)) / sqrt(1 - gamma_3 * SR_hat + ((gamma_4 - 1)/4) * SR_hat^2) )
    onde SR_0 é o Sharpe máximo esperado sob a hipótese nula para K = n_trials tentativas.
    """
    arr = np.asarray(returns_r, dtype=np.float64)
    n = len(arr)
    if n < 3:
        return 0.0

    mean_r = float(np.mean(arr))
    std_r = float(np.std(arr, ddof=1))
    if std_r <= 1e-12:
        return 1.0 if mean_r > 0 else 0.0

    # Sharpe por operação (não-anualizado para consistência com T = número de trades na equação de PSR/DSR)
    sr_per_trade = mean_r / std_r

    # Assimetria (gamma_3) e Curtose (gamma_4, onde normal = 3.0)
    z = (arr - mean_r) / std_r
    gamma_3 = float(np.mean(z**3))
    gamma_4 = float(np.mean(z**4))

    # Variância dos Sharpes por operação entre as K tentativas
    var_sr_per_trade = trials_sharpe_variance / max(trades_per_year, 1.0)
    std_sr_trials = math.sqrt(max(var_sr_per_trade, 1e-12))

    euler_mascheroni = 0.5772156649015329
    k_eff = max(1, int(n_trials))
    if k_eff == 1:
        sr_0 = 0.0
    else:
        z1 = _norm_ppf(1.0 - 1.0 / k_eff)
        z2 = _norm_ppf(1.0 - 1.0 / (k_eff * math.e))
        sr_0 = std_sr_trials * ((1.0 - euler_mascheroni) * z1 + euler_mascheroni * z2)

    denom_sq = 1.0 - gamma_3 * sr_per_trade + ((gamma_4 - 1.0) / 4.0) * (sr_per_trade**2)
    denom = math.sqrt(max(denom_sq, 1e-9))
    z_score = ((sr_per_trade - sr_0) * math.sqrt(n - 1.0)) / denom
    return _norm_cdf(z_score)


def register_backtest_trial(
    registry_path: Path,
    step_name: str,
    params_dict: Dict[str, object],
    n_trades: int,
    mean_net_r: float,
    sharpe_annualized: float,
    max_trials_budget: int = 20,
) -> int:
    """
    Grava automaticamente uma linha em logs/backtest_trials_registry.csv e retorna K.
    Impede p-hacking levantando RuntimeError se K ultrapassar max_trials_budget.
    """
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    existing_rows = 0
    if registry_path.exists():
        with open(registry_path, "r", encoding="utf-8", newline="") as f:
            reader = list(csv.reader(f))
            existing_rows = max(0, len(reader) - 1)

    new_k = existing_rows + 1
    if new_k > max_trials_budget:
        raise RuntimeError(
            f"Orçamento máximo de tentativas de Validação excedido (K={new_k} > K_max={max_trials_budget}). "
            "Proibido continuar ajustando parâmetros para evitar p-hacking."
        )

    params_json = json.dumps(params_dict, sort_keys=True)
    params_hash = hashlib.sha256(params_json.encode("utf-8")).hexdigest()[:12]
    now_iso = datetime.now(timezone.utc).isoformat()

    write_header = not registry_path.exists() or registry_path.stat().st_size == 0
    with open(registry_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(
                [
                    "trial_k",
                    "timestamp_utc",
                    "step_name",
                    "params_hash",
                    "n_trades",
                    "mean_net_r",
                    "sharpe_annualized",
                    "params_json",
                ]
            )
        writer.writerow(
            [
                new_k,
                now_iso,
                step_name,
                params_hash,
                n_trades,
                f"{mean_net_r:.6f}",
                f"{sharpe_annualized:.6f}",
                params_json,
            ]
        )

    return new_k


def evaluate_gate3_criteria(
    trades_base: Sequence[SimulatedTrade],
    trades_pessimistic: Sequence[SimulatedTrade],
    holdout_start_ms: int,
    n_trials: int,
    min_oos_trades: int = 150,
) -> Gate3Report:
    """
    Avalia os 5 critérios binários do Gate 3 sobre os trades fora da amostra:
    (a) N >= 150 trades;
    (b) E[R] > +0.10R e IC 95% inferior (Block Bootstrap) > 0.0R;
    (c) Profit Factor >= 1.20 no custo Base e > 1.05 no custo Pessimista;
    (d) Lucro positivo em pelo menos 3 de 4 subperíodos e no Holdout;
    (e) DSR >= 0.95.
    """
    r_base = [t.net_r for t in trades_base]
    r_pess = [t.net_r for t in trades_pessimistic]
    n_trades = len(r_base)

    if n_trades == 0:
        return Gate3Report(
            n_trades=0,
            mean_net_r=0.0,
            ci95_lower_r=0.0,
            ci95_upper_r=0.0,
            profit_factor_base=0.0,
            profit_factor_pessimistic=0.0,
            sharpe_annualized=0.0,
            deflated_sharpe_ratio=0.0,
            cvar_99_r=0.0,
            positive_subperiods=0,
            total_subperiods=4,
            holdout_positive=False,
            passed_gate3=False,
        )

    mean_r = float(np.mean(r_base))
    std_r = float(np.std(r_base, ddof=1)) if n_trades > 1 else 1.0
    sharpe_ann = (mean_r / max(std_r, 1e-12)) * math.sqrt(150.0)
    ci_low, ci_high = compute_block_bootstrap_ci95(r_base)
    pf_base = compute_profit_factor(r_base)
    pf_pess = compute_profit_factor(r_pess)
    dsr = compute_deflated_sharpe_ratio(r_base, n_trials=n_trials)
    cvar_99 = compute_cvar_99(r_base)

    # Divide em 4 subperíodos iguais
    chunks = np.array_split(np.asarray(r_base, dtype=np.float64), 4)
    pos_sub = sum(1 for c in chunks if len(c) > 0 and float(np.sum(c)) > 0.0)

    holdout_r = [t.net_r for t in trades_base if t.entry_time_ms >= holdout_start_ms]
    holdout_pos = len(holdout_r) > 0 and float(np.sum(holdout_r)) > 0.0

    passed = (
        n_trades >= min_oos_trades
        and mean_r > 0.10
        and ci_low > 0.0
        and pf_base >= 1.20
        and pf_pess > 1.05
        and pos_sub >= 3
        and holdout_pos
        and dsr >= 0.95
    )

    return Gate3Report(
        n_trades=n_trades,
        mean_net_r=mean_r,
        ci95_lower_r=ci_low,
        ci95_upper_r=ci_high,
        profit_factor_base=pf_base,
        profit_factor_pessimistic=pf_pess,
        sharpe_annualized=sharpe_ann,
        deflated_sharpe_ratio=dsr,
        cvar_99_r=cvar_99,
        positive_subperiods=pos_sub,
        total_subperiods=4,
        holdout_positive=holdout_pos,
        passed_gate3=passed,
    )
