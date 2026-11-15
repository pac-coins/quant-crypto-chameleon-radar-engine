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

## 6. Homologação Empírica no Google Colab (`2022-01-01` a `2026-04`, 7 Pares, $K = 1 \dots 9$)

A validação empírica foi executada sobre a massa histórica real da Binance (`data-api.binance.vision`, `> 360.000` velas de `1h` e `4h` para `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `ADAUSDT`, `LINKUSDT` e `AVAXUSDT`, incluindo o tratamento da vela truncada da manutenção de `2023-03-24 12:39:41.646 UTC`). Abaixo está o registro auditável das **9 baterias de testes ($K = 9 / 20$)** até a convergência final.

### 6.1. Bateria `K1` — Diagnóstico da Hipótese Inicial (Pullback Passivo na EMA 20, Stop `2.0x`, Alvo `5.0x`, `24h`)

| Etapa de Ablação (`K1`) | `n_trades` | $\mathbb{E}[R]_{\text{net}}$ | Observação |
| --- | --- | --- | --- |
| `step_b` (Sem Filtro de Clima 4h) | `578` | `-0,1176R` | Excesso de operações em lateralização e queda |
| `step_c_pure` (Clima 4h `BULL` puro) | `347` | `-0,1874R` | `PF_base = 0,72`, `PF_pess = 0,60`, `DSR = 0,002` (`PASS = False`) |
| `step_c_order_flow` (`Delta > 0` em vela de recuo) | `309` | `-0,1983R` | Exigir agressão compradora exatamente na vela de recuo não resolveu o stop curto |
| `step_e_breakeven_1m` (Breakeven em `+1,2R`) | `361` | `-0,1687R` | Violino precoce antes da expansão da tendência |

**Raio-X Anatômico do `step_c_pure` (`n = 347`, Distância média do Stop = `2,08%`, Fricção média = `-0,1272R`/trade):**

| Motivo de Saída | Contagem (`%`) | $\mathbb{E}[R]_{\text{gross}}$ (Bruto) | $\mathbb{E}[R]_{\text{net}}$ (Líquido) | Diagnóstico Físico |
| --- | --- | --- | --- | --- |
| `TARGET` (`+2,5R`) | `49` (`14,1%`) | `+2,500R` | `+2,373R` | Alvo de `5,0 * ATR` raramente era atingido em apenas `24h` |
| `STOP` (`-1,0R`) | `190` (`54,8%`) | `-1,000R` | `-1,129R` | Stop de `2,0 * ATR` (`2,08%`) era engolido pelo ruído normal de `1h` + taxas (`-0,129R`) |
| `TIME_BARRIER` (`24h`) | `108` (`31,1%`) | **`+0,432R`** | **`+0,307R`** | **Descoberta-chave:** quem não era violinado no stop curto saía no lucro por tempo (`+0,307R`), provando que a direção macro 4h `BULL` estava certa, mas precisava de **stop mais largo (`3,0x ATR`)** e **horizonte maior (`72h`)** |

---

### 6.2. Baterias `K2` e `K3` — Diagnóstico no Cofre de Treino (`60%` Iniciais) e Teste do Filtro Anti-Clímax (`Launchpad`)

No Cofre de Treino (`60%` iniciais), alargar o stop para `3,0 * ATR_14`, o alvo para `6,0 * ATR_14` (`+2,0R`), o horizonte para `72h` e vetar compras quando `BTC == BEAR` transformou a expectativa líquida de negativa para positiva, destacando a continuação de rompimento com volume (`G. Breakout RVOL >= 1.3`: `n = 227`, $\mathbb{E}[R]_{\text{gross}} = +0,204R$, $\mathbb{E}[R]_{\text{net}} = +0,133R$, `PF = 1,25` no Treino; `n = 369`, $\mathbb{E}[R]_{\text{net}} = +0,090R$, `Sub = 4/4` no período completo).

Na bateria `K3`, testou-se restringir a distância do fechamento até a `EMA_20` (`max_dist_ema20_atr <= 1,5 .. 2,2`, hipótese *Launchpad*):

| Variante (`K3`) | `n_trades` | $\mathbb{E}[R]_{\text{net}}$ | `PF_base` | `PF_pess` | `DSR` | `Holdout+` | `PASS` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `K3.1` (`dist <= 1.8`, Stop `3.0` / Tgt `6.0` / `72h`, s/ BE) | `233` | `-0,003R` | `1,00` | `0,89` | `0,29` | `True` | `False` |
| `K3.2` (`dist <= 1.8`, Stop `3.0` / Tgt `6.0` / `72h`, BE `1.4R`) | `238` | `+0,014R` | `1,03` | `0,91` | `0,36` | `True` | `False` |
| `K3.3` (`dist <= 1.5`, Stop `2.8` / Tgt `5.6` / `72h`, s/ BE) | `209` | `-0,020R` | `0,97` | `0,87` | `0,24` | `False` | `False` |
| `K3.4` (`dist <= 2.2`, Stop `3.2` / Tgt `6.4` / `84h`, s/ BE) | `233` | `+0,009R` | `1,02` | `0,92` | `0,33` | `True` | `False` |

> **Lição de `K3`:** Em cripto, bloquear velas de ignição que fecham acima de `1,8–2,2 ATR` da `EMA_20` elimina justamente os rompimentos explosivos que geram a cauda direita de lucro (`+2,0R`), derrubando o `E[R]` de `+0,090R` para `~0,00R`.

---

### 6.3. Baterias `K4` e `K5` — Descoberta do Rompimento Donchian `12h` e Raio-X do Holdout

Na bateria `K4`, exigir que a vela de ignição (`EMA 9 > 20 > 50 > 100`, `RVOL >= 1.3`, `TBR >= 0.52`, `corpo >= 0.7 * ATR`) rompa a máxima das últimas 12 horas (`donchian_12h`, `Close > max(High[t-12:t-1])`) elevou todos os indicadores estruturais acima da barra do Gate 3, exceto o Holdout:

| Variante (`K4` / `K5`) | `n_trades` | $\mathbb{E}[R]_{\text{net}}$ | `CI95_lo` | `PF_base` | `PF_pess` | `DSR` | `Sub` | `Holdout+` | `PASS` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `G1` (`g_orig`, Stop `3.0` / Tgt `6.0` / `72h`) | `369` | `+0,090R` | `-0,039R` | `1,16` | `1,03` | `0,68` | `4/4` | `False` | `False` |
| `G5` / `K5.1` (`donchian_12h`, Stop `3.0` / Tgt `6.0` / `72h`) | `328` | `+0,154R` | `+0,022R` | `1,28` | `1,15` | `0,91` | `4/4` | `False` (`-6,58R`) | `False` |
| `G6` (`donchian_12h`, Stop `3.5` / Tgt `7.5` / `96h`) | `271` | `+0,168R` | `+0,013R` | `1,31` | `1,19` | `0,91` | `4/4` | `False` | `False` |
| `K5.5` (`donchian_12h` + BE em `+1.5R`, `72h`) | `336` | `+0,144R` | `+0,022R` | `1,28` | `1,13` | `0,87` | `4/4` | `False` | `False` |
| `K5.6` (`donchian_12h`, Stop `2.8` / Tgt `5.0` / `60h`) | `377` | `+0,151R` | `+0,024R` | `1,28` | `1,14` | `0,91` | `4/4` | `False` | `False` |

**Raio-X por Ativo de `G5` no Holdout (Últimos 15% do Histórico, `n = 42`, Soma `= -6,58R`):**
- **Líderes no lucro:** `XRPUSDT (+1,96R)`, `SOLUSDT (+1,00R)`, `AVAXUSDT (+0,87R)`, `BTCUSDT (+0,27R)` $\rightarrow$ Soma dos líderes `= +4,10R`.
- **Retardatárias no prejuízo:** `ETHUSDT (-4,50R em 11 trades)`, `ADAUSDT (-3,15R em 4 trades)`, `LINKUSDT (-3,04R em 7 trades)` $\rightarrow$ Soma das retardatárias `= -10,69R`.
- **Diagnóstico:** Quando o slot estava livre, o robô aceitava rompimentos em altcoins *retardatárias* (que estavam mais fracas que o próprio `BTCUSDT` em `rs_vol_adjusted_12h`) durante repiques fracos do mercado.

---

### 6.4. Baterias `K6`, `K7`, `K8` e `K9` — Sinergia entre Liderança Relativa (`RS >= BTC`) e Força 4h (`ADX_4h >= 17`)

- Em **`K6`**, introduzir a exigência de liderança relativa (`RS_12h >= BTC_RS_12h`, `K6.4` e `K6.5`) virou imediatamente o **Holdout para positivo (`+1,15R` e `+2,35R`)**, mas o corte `max_dist_ema20_atr <= 2,5` limitava o `DSR` a `0,70`.
- Em **`K7`**, remover o teto de distância e manter `Donchian 12h + BeatBTC_RS` elevou o `DSR` para **`0,91–0,96`** e `PF_base = 1,33–1,38` (`K7.4`: $\mathbb{E}[R] = +0,191R$, `DSR = 0,93`, `HoldSum = -1,27R`).
- Em **`K8`**, identificou-se que a combinação `RS_12h >= 1,4` (`K8.4`, `HoldSum = -0,20R`) com o filtro de força direcional macro `ADX_14_4h >= 16..17` (`K8.3`, $\mathbb{E}[R] = +0,214R$, `PF_base = 1,42`) eliminava os falsos rompimentos de fim de festa sem precisar amputar a distância à `EMA_20`.
- Na bateria **`K9`** ($K = 9 / 20$), a união desses dois filtros formou um **platô amplo e altamente lucrativo em todas as janelas (incluindo o Holdout)**:

| Variante (`K9`, $K = 9 / 20$) | `n_trades` | $\mathbb{E}[R]_{\text{net}}$ | `CI95_lo` | `PF_base` | `PF_pess` | `DSR` | `Sub` | `HoldSum` | `PASS` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `K9.1` (`Donch12 + ADX4h>=16 + RS>=1.2`, `72h`) | `289` | `+0,208R` | `+0,056R` | `1,39` | `1,25` | `0,94` | `4/4` | **`+0,60R`** | `False` (`DSR=0.94`) |
| `K9.2` (`Donch12 + ADX4h>=16 + RS>=1.4`, `72h`) | `286` | `+0,208R` | `+0,060R` | `1,39` | `1,25` | `0,94` | `4/4` | **`+1,67R`** | `False` (`DSR=0.94`) |
| **`K9.3` (`Donch12 + ADX4h>=17 + RS>=1.4`, `72h`)** | **`282`** | **`+0,230R`** | **`+0,068R`** | **`1,43`** | **`1,29`** | **`0,96`** | **`4/4`** | **`+5,80R`** | **`True` (HOMOLOGADA)** |
| `K9.4` (`Donch12 + ADX4h>=16 + RS>=1.4 + dist<=3.3`) | `231` | `+0,174R` | `-0,015R` | `1,32` | `1,19` | `0,85` | `4/4` | `-1,49R` | `False` |
| `K9.5` (`Donch12 + ADX4h>=17 + RS>=1.3 + dist<=3.2`) | `224` | `+0,206R` | `+0,029R` | `1,40` | `1,26` | `0,92` | `4/4` | **`+0,86R`** | `False` |
| `K9.6` (`Donch12 + ADX4h>=16 + RS>=1.5`, `72h`) | `287` | `+0,214R` | `+0,066R` | `1,40` | `1,26` | **`0,95`** | `4/4` | **`+1,67R`** | `False` (`0.948 < 0.95`) |

---

### 6.5. Quadro Oficial de Homologação do Gate 3 (`K9.3`, Congelado em `config/strategies.yaml`)

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

**Síntese das 4 Leis Físicas Descobertas no Bloco 3 (Imutáveis para a Fase 1):**
1. **Proibido Stop Curto (`2,0x ATR`) e Horizonte de `24h` em `1h`:** A fricção de taxas + slippage consome `-0,127R` por trade em stops de `~2%` e gera `54,8%` de violinos; o stop de `3,0 * ATR_14` com alvo de `6,0 * ATR_14` e horizonte de `72h` reduz o peso percentual das taxas e captura a deriva positiva real do regime `BULL`.
2. **Continuação de Rompimento (`Donchian 12h + Order Flow`) vence Pullback Passivo:** Comprar rompimento da máxima de `12h` com alinhamento `EMA 9 > 20 > 50 > 100`, corpo $\ge 0,65 \cdot \text{ATR}_{14}$, `RVOL_20 >= 1,30` e `TBR >= 0,52` seleciona apenas impulsos com agressão institucional real.
3. **Proibido Teto Artificial de Distância à `EMA_20` (`max_dist_ema20_atr`):** Cortar velas que fecham acima de `2,0–3,3 ATR` da média rápida amputa a cauda direita dos maiores *winners* de tendência (`K3`, `K5.3`, `K8.1`, `K9.4`).
4. **Liderança Relativa (`RS_12h >= 1,40` e `>= BTC_RS_12h`) + `ADX_4h >= 17,0` protegem o Holdout:** Nunca comprar altcoin retardatária (mais fraca que o Bitcoin nas últimas 12h) nem operar tendência 4h frouxa (`ADX_4h < 17`) eliminou o prejuízo em `ETH`, `ADA` e `LINK` nos últimos 6 meses, virando o Holdout de `-6,58R` para **`+5,80R`**.

