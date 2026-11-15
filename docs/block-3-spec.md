# Especificação de partida — Bloco 3 Unificado

Este documento congela as assinaturas de classes, métodos (`type hints` do Python), equações estatísticas e o **Gate 3** para a construção do **Bloco 3** (Estratégias Canônicas, Simulador Causal Single-Slot, Estudo de Ablação A–E, Governança DSR e Notebooks do Google Colab).

!!! info "Pré-requisito de Partida"
    O Bloco 3 depende diretamente dos artefatos validados no [Bloco 1](block-1-spec.md) (`CandleCollector.align_causal_1h_4h`) e no [Bloco 2](block-2-spec.md) (`FeatureStore.compute_unified_features` e `RegimeDetector.detect_market_regime`). Para garantir **zero divergência entre backtest e produção**, os notebooks do Google Colab importam diretamente os módulos `src/strategies/` e `src/backtest/`.

---

## 1. Estratégias Canônicas (`src/strategies/`)

### 1.1. Contrato Base (`src/strategies/base_strategy.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from src.regimes.regime_detector import MarketRegime


@dataclass(frozen=True)
class SignalCandidate:
    symbol: str
    close_time_ms: int
    regime: MarketRegime
    strategy_name: str
    entry_price: float
    stop_price: float
    target_price: float
    atr_14: float
    stop_distance_pct: float        # (entry_price - stop_price) / entry_price
    gross_rr_ratio: float           # (target_price - entry_price) / (entry_price - stop_price)
    time_barrier_hours: int
    rs_vol_adjusted_12h: float      # (Close[t] - Close[t-12]) / ATR_14[t]
    order_flow_delta: float
    taker_buy_ratio: float
    has_liquidity_sweep: bool


class BaseStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def required_regime(self) -> MarketRegime: ...

    @abstractmethod
    def evaluate_row(
        self,
        symbol: str,
        row: pd.Series,
        regime: MarketRegime,
        require_order_flow_filter: bool = False,
    ) -> Optional[SignalCandidate]:
        """Avalia causalmente a última linha fechada (1h alinhada com 4h e BTC)."""
```

### 1.2. Estratégia 1 — `bull_trend_pullback.py` (Ativa na Fase 1)
Opera exclusivamente quando `regime == MarketRegime.BULL` e `enabled: true`:
1. **Estrutura de Tendência 1h:** $\text{EMA}_{20, 1h} > \text{EMA}_{100, 1h}$ e $\text{Close}_{1h} > \text{EMA}_{100, 1h}$.
2. **Recuo (*Pullback*) na Média Rápida com Rejeição Compradora:** A mínima da vela toca ou penetra a zona da $\text{EMA}_{20, 1h}$ ($\text{Low}_{1h} \le \text{EMA}_{20, 1h} \times 1,003$) e o fechamento recupera acima da $\text{EMA}_{20, 1h}$ ($\text{Close}_{1h} \ge \text{EMA}_{20, 1h}$) com vela de alta ($\text{Close}_{1h} > \text{Open}_{1h}$) ou gatilho de Price Action (`is_bullish_pinbar`, `is_bullish_engulfing` ou `is_liquidity_sweep`).
3. **Filtro Opcional de Ablação (*Order Flow* no Degrau C):** Quando `require_order_flow_filter=True`, exige adicionalmente $\text{Delta}_{\text{quote}, t} > 0$ ($\text{TBR}_t > 0,50$).
4. **Barreiras OCO:**
   - $P_{\text{stop}} = P_{\text{entry}} - 2,0 \cdot \text{ATR}_{14, 1h}$ ($-1,0R$ bruto)
   - $P_{\text{target}} = P_{\text{entry}} + 5,0 \cdot \text{ATR}_{14, 1h}$ ($+2,5R$ bruto)
   - Barreira de tempo: `time_barrier_candles_1h = 24`.

### 1.3. Estratégia 2 — `sideways_pingpong.py` (Desligada na Fase 1)
Opera exclusivamente quando `regime == MarketRegime.SIDEWAYS`:
- Entrada se $\text{Close}_{1h} \le \text{BB}_{\text{lower}}(20, 2,0)$ e $\text{RSI}_{14, 1h} < 32,0$.
- $P_{\text{stop}} = P_{\text{entry}} - 1,5 \cdot \text{ATR}_{14, 1h}$, $P_{\text{target}} = \text{SMA}_{20, 1h}$.
- **Trava Geométrica Obrigatória v6.0:** Bloqueia a entrada (`return None`) se:
  $$\text{RR}_{\text{initial}} = \frac{\text{SMA}_{20, 1h} - P_{\text{entry}}}{1,5 \cdot \text{ATR}_{14, 1h}} < 1,50$$

### 1.4. Estratégia 3 — `bear_capitulation_snap.py` (Em Quarentena)
Opera apenas em estudos de estresse quando explicitamente habilitada em `regime == MarketRegime.BEAR`:
- Exige $\text{RSI}_{2, 1h} < 5,0$, $\text{RVOL}_{20, 1h} \ge 3,0$ e $\text{lower\_wick\_ratio} \ge 0,40$.

---

## 2. Simulador Causal Single-Slot (`src/backtest/causal_simulator.py`)

```python
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence
import pandas as pd
from src.strategies.base_strategy import SignalCandidate


class CostScenario(str, Enum):
    OPTIMISTIC = "OPTIMISTIC"   # Maker no alvo (0.075%), Taker no stop/tempo (0.075%) + 0.03% slippage
    BASE = "BASE"               # Taker/Taker (0.15% ida+volta com BNB) + 0.06% slippage base
    PESSIMISTIC = "PESSIMISTIC" # Sem BNB (0.20% ida+volta) + 0.12% slippage + gap adverso no stop


@dataclass(frozen=True)
class SimulatedTrade:
    symbol: str
    strategy_name: str
    regime: str
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    exit_reason: str            # "TARGET", "STOP", "BREAKEVEN", "TIME_BARRIER"
    gross_r: float
    net_r: float                # R líquido após taxas e slippage do cenário
    pnl_pct_equity: float       # Impacto percentual na Carteira Tática
    bars_held: int
    rs_score: float


class CausalSingleSlotSimulator:
    """
    Simulador determinístico de fila única (MAX_OPEN_POSITIONS = 1).
    """

    def __init__(
        self,
        cost_scenario: CostScenario = CostScenario.BASE,
        risk_per_trade_pct: float = 0.010,
        max_cash_pct: float = 0.50,
        breakeven_enabled: bool = False,
        breakeven_trigger_r: float = 1.2,
        cppi_enabled: bool = True,
        cppi_drawdown_threshold_pct: float = 0.04,
        cppi_reduction_factor: float = 0.50,
    ) -> None: ...

    def select_single_slot_winner(
        self,
        candidates: Sequence[SignalCandidate],
    ) -> Optional[SignalCandidate]:
        """
        Desempate coerente da Fase 1: escolhe o candidato com maior rs_vol_adjusted_12h
        (e desempate secundário determinístico por ordem alfabética do symbol).
        """

    def resolve_trade_1h_or_1m(
        self,
        candidate: SignalCandidate,
        future_bars_1h: pd.DataFrame,
        intracandle_bars_1m: Optional[pd.DataFrame] = None,
    ) -> SimulatedTrade:
        """
        Resolve o desfecho da posição aberta respeitando:
        - Precedência pessimista (STOP antes de TARGET se ambos tocados na mesma barra sem 1m);
        - Resolução de 1 minuto (1m) quando fornecida (obrigatória para testar Breakeven no Degrau E);
        - Fechamento a mercado no close da 24ª barra (TIME_BARRIER), computando o R líquido real.
        """
```

---

## 3. Governança Anti-Overfitting, AFML e DSR (`src/backtest/ablation_and_dsr.py`)

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
import numpy as np
import pandas as pd
from src.backtest.causal_simulator import SimulatedTrade


@dataclass(frozen=True)
class PartitionBounds:
    train_end_ms: int       # Primeiros 60%
    val_end_ms: int         # Próximos 25% (com Purging de 24h e Embargo de 2%)
    holdout_end_ms: int     # Últimos 15% (cofre intocável)


@dataclass(frozen=True)
class Gate3Report:
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
) -> PartitionBounds: ...


def compute_afml_sample_uniqueness(
    events: Sequence[Tuple[int, int]],
) -> np.ndarray:
    """
    AFML Snippet 4.2 (López de Prado): calcula u_bar_i = média de (1 / c_t)
    ao longo de toda a janela [t_entry, t_exit] de cada evento.
    """


def compute_block_bootstrap_ci95(
    net_r_series: Sequence[float],
    block_size: int = 5,
    n_bootstraps: int = 1000,
    seed: int = 42,
) -> Tuple[float, float]: ...


def compute_deflated_sharpe_ratio(
    returns_r: Sequence[float],
    n_trials: int,
    trials_sharpe_variance: float = 0.25,
     trades_per_year: float = 150.0,
) -> float:
    """
    Calcula o Deflated Sharpe Ratio (Bailey & López de Prado, 2014) descontando
    não-normalidade (assimetria gamma_3 e curtose gamma_4) e o número total de
    tentativas K registradas em trials_registry.csv.
    """


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
    Registra a tentativa em trials_registry.csv e retorna o contador K acumulado.
    Dispara RuntimeError se K > max_trials_budget no conjunto de Validação.
    """
```

---

## 4. Notebooks do Google Colab (`notebooks/`)

1. **`notebooks/01_download_causal_1h_4h_1m.ipynb`:**
   - Baixa velas históricas públicas (`data-api.binance.vision`) desde `2022-01-01` para os 7 pares do universo (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `ADAUSDT`, `LINKUSDT`, `AVAXUSDT`), preservando colunas 0 a 10 (incluindo `quote_volume` e `taker_buy_quote_volume`).
   - Executa `CandleCollector.align_causal_1h_4h` e salva os datasets causais prontos para os estudos de ablação.
2. **`notebooks/02_ablation_and_dsr_sim.ipynb`:**
   - Executa os **Degraus A, B, C (com e sem filtro de Order Flow Delta), D e E (com velas de 1m para Breakeven)** sobre a partição `60% Treino / 25% Validação / 15% Holdout`.
   - Registra cada execução em `logs/backtest_trials_registry.csv`, calcula $\bar{u}_i$ (AFML), $\text{DSR}$, $\text{CVaR}_{99\%}$ e valida o **Gate 3**.
3. **`notebooks/04_radar_layers_123_backtest.ipynb`:**
   - Audita as Camadas 1 (Estrutura EMA 9/50/200), 2 (Price Action: Pinbar, Engolfo, Liquidity Sweep) e 3 (Order Flow: RVOL e Taker Buy Ratio) do Radar usando `FeatureStore` e o mesmo protocolo anti-overfitting.

---

## 5. Gate 3 Unificado (`pytest tests/test_block3_causal_sim_and_dsr.py`)

O comando `pytest tests/test_block3_causal_sim_and_dsr.py` valida deterministicamente:

1. **Teste 3A (Fila Single-Slot e Desempate `rs_vol_adjusted_12h`):** Quando 2 ou mais moedas geram sinal no mesmo fechamento de 1h, o simulador seleciona exclusivamente a moeda de maior `rs_vol_adjusted_12h` e bloqueia novas entradas enquanto a posição estiver aberta.
2. **Teste 3B (Três Cenários de Custos, Time-Barrier 24h e Resolução Intra-Vela de 1m para Breakeven):**
   - Prova que $R_{\text{net}}(\text{OPTIMISTIC}) > R_{\text{net}}(\text{BASE}) > R_{\text{net}}(\text{PESSIMISTIC})$ para um mesmo trade;
   - Prova que saídas na 24ª barra (`TIME_BARRIER`) entram no cálculo empírico de $\mathbb{E}[R]$ com seu P&L real líquido;
   - Prova que a resolução de 1 minuto (`1m`) aciona corretamente o Breakeven em $+1,2R$ quando o preço toca $+1,2R$ e depois reverte para o preço de entrada.
3. **Teste 3C (AFML Sample Uniqueness $\bar{u}_i$, Partição Temporal com Purging/Embargo e Orçamento $K_{\max} = 20$):**
   - Dois eventos com 100% de sobreposição temporal têm unicidade média $\bar{u}_i = 0,5$, e eventos disjuntos têm $\bar{u}_i = 1,0$;
   - O registrador `register_backtest_trial` grava em `trials_registry.csv` e levanta `RuntimeError` na tentativa $K = 21$.
4. **Teste 3D (Ablação de Order Flow no Degrau C, Modo K9.3, Block Bootstrap CI 95%, DSR e Notebooks Válidos):**
   - Verifica a matemática do $\text{DSR}$ (decrescente com o número de tentativas $K$ e crescente com o Sharpe e tamanho amostral $N$), o modo `K9.3` de `bull_trend_pullback`, a trava geométrica $\text{RR} \ge 1,50$ de `sideways_pingpong`, e valida a estrutura JSON e execução ponta-a-ponta dos 3 notebooks em `notebooks/`.

---

## 6. Homologação Empírica no Google Colab (`2022-01-01` a `2026-04`, 7 Pares)

A validação empírica sobre mais de `360.000` velas históricas reais da Binance (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `ADAUSDT`, `LINKUSDT`, `AVAXUSDT`) foi concluída na tentativa $K = 9$ de $K_{\max} = 20$ (`K9.3`), aprovando **todos os 8 critérios do Gate 3 (`passed_gate3 = True`)**:

| Métrica do Gate 3 | Requisito Mínimo | Resultado Empírico (`K9.3`) | Status |
| --- | --- | --- | --- |
| **Amostra (`n_trades`)** | $\ge 150$ trades | `282` trades | **PASS** |
| **Expectância Líquida ($\mathbb{E}[R]_{\text{net}}$)** | $> +0,10R$ | **`+0,230R`** | **PASS** |
| **IC 95% Inferior (*Block Bootstrap*)** | $> 0,00R$ | **`+0,068R`** | **PASS** |
| **Profit Factor (Cenário Base)** | $\ge 1,20$ | **`1,43`** | **PASS** |
| **Profit Factor (Cenário Pessimista)** | $> 1,05$ | **`1,29`** | **PASS** |
| **Consistência em Subperíodos** | $\ge 3 / 4$ positivos | **`4 / 4` positivos** | **PASS** |
| **Holdout Intocável (Últimos 15%)** | Soma $> 0,0R$ (`holdout_positive = True`) | **`+5,80R` (`True`)** | **PASS** |
| **Deflated Sharpe Ratio ($\text{DSR}$, $K=9$)** | $\ge 0,95$ | **`0,96`** | **PASS** |

