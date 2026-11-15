# Especificação do Bloco 2 unificado

Esta página congela as **assinaturas (`type hints`) e contratos matemáticos dos 5 arquivos do Bloco 2** (`indicators.py`, `order_flow.py`, `price_action.py`, `feature_store.py` e `regime_detector.py`) e os critérios binários do **Gate 2 Unificado**.

Ela incorpora:

* as fórmulas canônicas de Wilder (`ATR`, `RSI`, `ADX`) com equivalência à implementação de referência do `TA-Lib` (erro máximo $< 10^{-10}$ após *warm-up*);
* o `CHOP` na base $\log_{10}$ com proteção contra divisão por zero ($\max H - \min L == 0 \rightarrow \text{NaN}$);
* as guardas de zero do `RVOL` e do `TBR` (**C2-6**: volume zero ou mediana zero $\rightarrow \text{NaN}$, nunca $\pm\infty$);
* o cálculo de `order_flow_delta_quote` usando `quote_volume` (coluna 7) e `taker_buy_quote_volume` (coluna 10) (**C2-5**);
* o compartilhamento de `bullish_pinbar`, `bullish_engulfing` e `liquidity_sweep_bull` entre o Chameleon e a Camada 2 do Radar (**ADR-007**);
* o `FeatureStore` declarativo que calcula a união de `FeatureRequest` uma única vez por ciclo (**ADR-007**);
* a partição MECE de 4 estados (`BEAR` $\rightarrow$ `BULL` $\rightarrow$ `SIDEWAYS` $\rightarrow$ `TRANSITION`) como **fonte única de clima** para o Chameleon e para o Radar (**ADR-008**), com fallback imediato para `TRANSITION` em caso de `NaN`, infinito ou histórico menor que 120 velas de 4h.

!!! warning "Regra de avanço"
    Nenhuma linha do Bloco 3 ou dos módulos seguintes é executada antes de `pytest tests/test_block2_talib_and_mece.py` retornar `100% PASS`.

---

## 1. `src/features/indicators.py`

Funções puras, vetorizadas sobre `pandas.Series` / `pandas.DataFrame` (`float64`), sem efeitos colaterais e sem mutação dos argumentos de entrada.

### Regras matemáticas exatas (compatibilidade TA-Lib / Wilder)

1. **`ema(close, period)`:**
   * Semente inicial no índice `period - 1`: média simples dos primeiros `period` valores ($\text{SMA}_{\text{period}}$); índices `0 .. period - 2` recebem `NaN`.
   * Recorrência para $t \ge \text{period}$: $\text{EMA}_t = \alpha \cdot C_t + (1 - \alpha) \cdot \text{EMA}_{t-1}$, onde $\alpha = \frac{2}{\text{period} + 1}$.
2. **`true_range(df)`:**
   * No índice $0$: `NaN` (compatível com o `TRANGE` do `TA-Lib`, que exige $C_{t-1}$).
   * Para $t \ge 1$: $\text{TR}_t = \max\left(H_t - L_t, \; \lvert H_t - C_{t-1} \rvert, \; \lvert L_t - C_{t-1} \rvert\right)$.
3. **`atr_wilder(df, period=14)`:**
   * Semente no índice `period`: média aritmética simples de $\text{TR}_{1 \dots \text{period}}$; índices `0 .. period - 1` recebem `NaN`.
   * Recorrência de Wilder para $t > \text{period}$: $\text{ATR}_t = \frac{(\text{period} - 1)\cdot \text{ATR}_{t-1} + \text{TR}_t}{\text{period}}$.
4. **`rsi_wilder(close, period=14)`:**
   * $\Delta C_t = C_t - C_{t-1}$, $U_t = \max(\Delta C_t, 0)$, $D_t = \max(-\Delta C_t, 0)$.
   * Semente no índice `period`: $\bar{U}_{\text{period}} = \frac{1}{\text{period}}\sum_{i=1}^{\text{period}} U_i$, $\bar{D}_{\text{period}} = \frac{1}{\text{period}}\sum_{i=1}^{\text{period}} D_i$.
   * Para $t > \text{period}$: suavização RMA de Wilder. Se $\bar{U}_t == 0$ e $\bar{D}_t == 0$ (série constante), $\text{RSI}_t = 50{,}0$ ou `NaN` tratado a jusante; se apenas $\bar{D}_t == 0$, $\text{RSI}_t = 100{,}0$.
5. **`adx_wilder(df, period=14)`:**
   * Segue a recorrência exata do `TA-Lib`:
     * $+\text{DM}_t$ e $-\text{DM}_t$ acumulados nas primeiras `period` barras ($t = 1 \dots \text{period}$) por soma simples ($\text{prev\_plus\_dm}, \text{prev\_minus\_dm}, \text{prev\_tr}$), atualizados para $t \ge \text{period}$ por $\text{prev} - \frac{\text{prev}}{\text{period}} + \text{curr}$.
     * $\text{DX}_t = 100 \cdot \frac{\lvert +\text{DI}_t - -\text{DI}_t \rvert}{+\text{DI}_t + -\text{DI}_t}$ (ou $0{,}0$ se o denominador for zero).
     * O primeiro valor de $\text{ADX}$ no índice `2 * period - 1` é a média simples dos `period` valores de $\text{DX}$ ($t = \text{period} \dots 2\cdot\text{period} - 1$); para $t \ge 2\cdot\text{period}$, $\text{ADX}_t = \frac{(\text{period} - 1)\cdot \text{ADX}_{t-1} + \text{DX}_t}{\text{period}}$.
6. **`chop_log10(df, period=14)`:**
   * $\text{CHOP}_{n, t} = 100 \cdot \dfrac{\log_{10}\left(\sum_{j=t-n+1}^{t} \text{TR}_j \;/\; (\max_{j} H_j - \min_{j} L_j)\right)}{\log_{10}(n)}$.
   * **Proteção Cat. 1 (TODO):** se $\max H - \min L \le 0$ ou $\sum \text{TR}_j \le 0$, retorna `NaN` naquele índice (jamais `inf`, `-inf` ou `ZeroDivisionError`).
7. **`bollinger(close, period=20, n_std=2.0)`:**
   * Retorna `DataFrame` com colunas `mid`, `upper`, `lower`, `bandwidth` usando desvio-padrão populacional (`ddof=0`, padrão do `TA-Lib`).
8. **`rvol_median(volume, lookback=20)`:**
   * $\text{RVOL}_t = \dfrac{V_t}{\text{Mediana}(V_{t-\text{lookback}+1 \dots t})}$.
   * **Guarda C2-6:** se a mediana da janela for $\le 0$ ou $V_t < 0$, retorna `NaN` (nunca `inf`).

```python
from __future__ import annotations
import pandas as pd

def ema(close: pd.Series, period: int) -> pd.Series: ...
def true_range(df: pd.DataFrame) -> pd.Series: ...
def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series: ...
def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series: ...
def adx_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series: ...
def chop_log10(df: pd.DataFrame, period: int = 14) -> pd.Series: ...
def bollinger(close: pd.Series, period: int = 20, n_std: float = 2.0) -> pd.DataFrame: ...
def rvol_median(volume: pd.Series, lookback: int = 20) -> pd.Series: ...
```

---

## 2. `src/features/order_flow.py`

Calcula as métricas de agressão a mercado da Camada 3 do Radar e do filtro de *Order Flow* avaliado no Bloco 3.

```python
from __future__ import annotations
import pandas as pd

def taker_buy_ratio(volume: pd.Series, taker_buy_base: pd.Series) -> pd.Series:
    """TBR_t = V_buy,t / V_t em [0, 1]. Se V_t <= 0 -> NaN (C2-6)."""

def order_flow_delta_base(volume: pd.Series, taker_buy_base: pd.Series) -> pd.Series:
    """Delta em ativo base: 2 * V_buy,t - V_t. Se V_t <= 0 -> NaN."""

def order_flow_delta_quote(quote_volume: pd.Series, taker_buy_quote: pd.Series) -> pd.Series:
    """Delta financeiro em USDT (C2-5): 2 * Q_buy,t - Q_t. Se Q_t <= 0 -> NaN."""

def wyckoff_absorption_bull(
    df: pd.DataFrame,
    ema50: pd.Series,
    ema200: pd.Series,
    atr14: pd.Series,
    rvol20: pd.Series,
    tbr: pd.Series,
    *,
    touch_tol_atr: float = 0.45,
    min_rvol: float = 2.0,
    min_lower_wick_ratio: float = 0.50,
    min_tbr: float = 0.52,
) -> pd.Series:
    """Sinal booleano de absorção institucional (Effort vs Result de Wyckoff)."""
```

---

## 3. `src/features/price_action.py`

Geometria estrita de velas fechadas compartilhada entre o Chameleon (`liquidity_sweep_bull`) e a Camada 2 do Radar (`bullish_pinbar`, `bullish_engulfing`, `liquidity_sweep_bull`).

```python
from __future__ import annotations
import pandas as pd

def candle_wick_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Retorna DataFrame com colunas: range, body, lower_wick_ratio, upper_wick_ratio.
    Se H == L, lower_wick_ratio e upper_wick_ratio = 0.0 (sem divisão por zero).
    """

def bullish_pinbar(
    df: pd.DataFrame,
    atr: pd.Series,
    *,
    min_lower: float = 0.55,
    max_upper: float = 0.18,
    min_range_atr: float = 0.75,
) -> pd.Series:
    """Série booleana: Martelo / Pinbar de rejeição compradora."""

def bullish_engulfing(
    df: pd.DataFrame,
    atr: pd.Series,
    ema_fast: pd.Series,
    *,
    min_atr_mult: float = 1.10,
) -> pd.Series:
    """Série booleana: Engolfo institucional de alta fechando acima de H_{t-1} e da EMA rápida."""

def liquidity_sweep_bull(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """Série booleana: L_t < min(L_{t-lookback .. t-1}) e C_t > min(L_{t-lookback .. t-1})."""
```

---

## 4. `src/features/feature_store.py`

Motor declarativo único (**ADR-007**): recebe uma coleção de `FeatureConsumer`s (ou um conjunto explícito de `FeatureRequest`s), calcula cada indicador **uma única vez** por `(símbolo, timeframe)` e alinha as colunas de `4h` ao `DataFrame` de `1h` via `align_4h_to_1h` (backward `merge_asof`).

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Protocol, Sequence
import pandas as pd

FeatureKind = Literal[
    "ema", "atr", "adx", "rsi", "chop", "bb", "rvol", "tbr", "of_delta", "sweep"
]

@dataclass(frozen=True, slots=True)
class FeatureRequest:
    kind: FeatureKind
    period: int = 14
    timeframe: Literal["1h", "4h"] = "1h"
    extra: tuple[float, ...] = ()

    @property
    def column(self) -> str: ...
    # Exemplos:
    # ("ema", 20, "1h") -> "ema_20"
    # ("ema", 100, "4h") -> "ema_100_4h"
    # ("chop", 14, "4h") -> "chop_14_4h"
    # ("adx", 14, "4h") -> "adx_14_4h"
    # ("atr", 14, "1h") -> "atr_14"
    # ("rvol", 20, "1h") -> "rvol_20"
    # ("tbr", 1, "1h") -> "tbr"
    # ("of_delta", 1, "1h") -> "of_delta_quote"
    # ("sweep", 20, "1h") -> "liquidity_sweep_bull_20"
    # ("bb", 20, "1h", (2.0,)) -> "bb_20_2.0_mid" (e cria _upper, _lower, _bandwidth)

class FeatureConsumer(Protocol):
    def required_features(self) -> frozenset[FeatureRequest]: ...

class FeatureStore:
    def __init__(
        self,
        consumers: Sequence[FeatureConsumer] = (),
        *,
        extra_requests: Sequence[FeatureRequest] = (),
    ) -> None: ...

    @property
    def requests(self) -> frozenset[FeatureRequest]: ...

    def compute_timeframe(
        self,
        df: pd.DataFrame,
        timeframe: Literal["1h", "4h"],
    ) -> pd.DataFrame: ...

    def build(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> pd.DataFrame: ...
```

---

## 5. `src/regimes/regime_detector.py`

Implementa a partição **Mutuamente Exclusiva e Coletivamente Exaustiva (MECE)** dos 4 climas de mercado sobre a última vela fechada de 4h (**ADR-008**):

| Precedência | Estado (`MarketRegime`) | Condição Matemática Exata (4h) |
| --- | --- | --- |
| **0 (Guarda)** | `TRANSITION` | Histórico $< 120$ velas de 4h (`WARMUP_LT_120`) **ou** qualquer valor `NaN`/`±inf` em `close_4h`, `ema_100_4h`, `ema_100_slope_4h`, `chop_14_4h` ou `adx_14_4h` (`NAN`) |
| **1º** | `BEAR` | $\text{Close}_{4h} < \text{EMA}_{100, 4h}$ **e** $\Delta\text{EMA}_{100, 4h} < 0$ |
| **2º** | `BULL` | $\text{Close}_{4h} > \text{EMA}_{100, 4h}$ **e** $\Delta\text{EMA}_{100, 4h} > 0$ **e** $\text{CHOP}_{14, 4h} < 50{,}0$ |
| **3º** | `SIDEWAYS` | Não é `BEAR`, $\text{CHOP}_{14, 4h} \ge 55{,}0$ **e** $\text{ADX}_{14, 4h} < 22{,}0$ |
| **4º** | `TRANSITION` | Qualquer estado restante |

Onde $\Delta\text{EMA}_{100, 4h, t} = \text{EMA}_{100, 4h, t} - \text{EMA}_{100, 4h, t-1}$.

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal
import pandas as pd
from src.core.config import RegimeThresholds4hCfg
from src.features.feature_store import FeatureRequest

MIN_WARMUP_CANDLES_4H: Final[int] = 120

class MarketRegime(StrEnum):
    BEAR = "BEAR"
    BULL = "BULL"
    SIDEWAYS = "SIDEWAYS"
    TRANSITION = "TRANSITION"

FallbackReason = Literal["WARMUP_LT_120", "NAN"] | None

@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    regime: MarketRegime
    close_4h: float | None
    ema100_4h: float | None
    ema100_slope_4h: float | None
    chop14_4h: float | None
    adx14_4h: float | None
    fallback_reason: FallbackReason = None

def classify_regime_values(
    *,
    close_4h: float,
    ema100_4h: float,
    ema100_slope_4h: float,
    chop14_4h: float,
    adx14_4h: float,
    thresholds: RegimeThresholds4hCfg | None = None,
    has_min_history: bool = True,
) -> RegimeSnapshot: ...

class RegimeDetector:
    def __init__(
        self,
        thresholds: RegimeThresholds4hCfg | None = None,
        *,
        min_warmup_candles: int = MIN_WARMUP_CANDLES_4H,
    ) -> None: ...

    def required_features(self) -> frozenset[FeatureRequest]: ...
    def detect_on_4h_frame(self, df_4h: pd.DataFrame) -> RegimeSnapshot: ...
    def detect_series(self, df_4h: pd.DataFrame) -> pd.Series: ...
```

---

## 6. Gate 2 Unificado (critério binário)

`pytest tests/test_block2_talib_and_mece.py` precisa passar em **100%**:

| # | Teste | Critério Exato |
| --- | --- | --- |
| **a** | Equivalência numérica com `TA-Lib` / Wilder (`EMA`, `ATR_14`, `RSI_14`, `ADX_14`) | Erro absoluto máximo $< 10^{-10}$ (e correlação $> 0{,}999999$) contra a implementação canônica de Wilder/TA-Lib após o período de aquecimento |
| **b** | Série artificial de preços constantes ($H_t == L_t == C_t$) e volume zero | `chop_log10` retorna `NaN` sem `ZeroDivisionError`; `rvol_median` e `taker_buy_ratio` retornam `NaN` (nunca `inf`, **C2-6**); `RegimeDetector` retorna `TRANSITION` com `fallback_reason="NAN"` |
| **c** | Guarda de histórico curto (`< 120` velas de 4h) | `RegimeDetector.detect_on_4h_frame` retorna `TRANSITION` com `fallback_reason="WARMUP_LT_120"`, mesmo que os números pontuais pareçam `BULL` |
| **d** | Cobertura MECE exaustiva e disjunta em grade de estados e fronteiras | Varredura cartesiana de $(\text{Close}-\text{EMA}_{100}, \Delta\text{EMA}_{100}, \text{CHOP}_{14}, \text{ADX}_{14}, \text{NaN/Inf})$ incluindo fronteiras exatas ($\text{CHOP} \in \{49{,}999, 50{,}0, 52{,}0, 55{,}0\}$, $\text{ADX} \in \{21{,}999, 22{,}0\}$) prova que **100%** dos pontos caem em **exatamente 1** estado válido e que `BEAR` tem precedência sobre `SIDEWAYS` |
| **e** | `FeatureStore` + `PriceAction` + `OrderFlow` compartilhados | Consumidores pedindo `EMA 20/100` (Chameleon) e `EMA 9/50/200` (Radar) + `atr_14` geram um único DataFrame alinhado sem duplicar cálculos; `bullish_pinbar`, `bullish_engulfing`, `liquidity_sweep_bull` e `order_flow_delta_quote` batem com casos sintéticos exatos |
