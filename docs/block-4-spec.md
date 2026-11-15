# Especificação de partida — Bloco 4 Unificado

Este documento congela as assinaturas de classes, métodos (`type hints` do Python), equações de dimensionamento em `Decimal`, regras de controle de drawdown e o **Gate 4** para a construção do **Bloco 4** (Dimensionador de Teto Duplo `position_sizer.py`, Controle de Drawdown e Kill-Switches `shadow_manager.py`, Cofre de Lucros com High-Water Mark `profit_splitter.py`, Juiz de IA com Platt Scaling `ml_judge.py` e Notebook 03 de Estresse de Cauda).

!!! info "Pré-requisito de Partida"
    O Bloco 4 apoia-se nos artefatos validados no [Bloco 1](block-1-spec.md) (`Database`, `SymbolFilters`, `to_decimal`, `round_price_to_tick`, `floor_qty_to_step`, `SignedBinanceClient.sapi_transfer_main_to_funding`), [Bloco 2](block-2-spec.md) (`FeatureStore`, `RegimeDetector`) e [Bloco 3](block-3-spec.md) (`SignalCandidate` e configuração homologada `K9.3`).

---

## 1. Dimensionador Fracionário com Teto Duplo (`src/risk/position_sizer.py`)

### 1.1. Equações Canônicas de Dimensionamento

1. **Desconto do Cofre Pendente (`pending_hodl_bucket_usdt`):**
   O saldo em USDT reservado para transferência futura à carteira `FUNDING` nunca pode ser usado como margem de compra:
   $$C_{\text{usdt, operável}} = \max\left(0, \; C_{\text{usdt, free}} - B_{\text{pending\_hodl}}\right)$$

2. **Custo Friccional em Unidades de Risco e Limiar de Indiferença ($\underline{p}_i$):**
   Dada a distância percentual do stop $d_{\text{stop}, i} = \frac{P_{\text{entry}} - P_{\text{stop}}}{P_{\text{entry}}} > 0$ e o múltiplo R:R bruto $R_{\text{eff}, i} = \frac{P_{\text{target}} - P_{\text{entry}}}{P_{\text{entry}} - P_{\text{stop}}}$:
   $$C_{R, i} = \frac{f_{\text{ida+volta}} + s_{\text{stress}}}{d_{\text{stop}, i}}, \quad \underline{p}_i = \frac{1 + C_{R, i}}{R_{\text{eff}, i} + 1}$$
   E o valor esperado líquido previsto para uma probabilidade calibrada $\hat{p}_{\text{cal}, i}$ é:
   $$\widehat{\mathbb{E}}[R_i] = \hat{p}_{\text{cal}, i} \cdot R_{\text{eff}, i} - \left(1 - \hat{p}_{\text{cal}, i}\right) - C_{R, i}$$
   Quando o filtro de IA está ativo (`use_ml_filter = True`), qualquer operação com $\hat{p}_{\text{cal}, i} \le \underline{p}_i + m_{\text{safety}}$ ($m_{\text{safety}} = 0,03$) ou classificada como **Nota C** recebe **lote zero** (`approved = False`).

3. **Equação de Teto Duplo + Disjuntor Proporcional de Ordem Individual:**
   Seja $r_{\text{eff}} = r_{\text{grade}} \cdot m_{\text{drawdown}}$ a fração de risco efetiva (modulada pelo `ShadowManager`, ex.: $1,0\% \times 0,50 = 0,5\%$ quando em drawdown $\ge 4\%$):
   - **Teto 1 (Caixa Operável):** $V_{\text{cap\_cash}} = w_{\text{max}} \cdot C_{\text{usdt, operável}}$ ($w_{\text{max}} = 50\%$ na Nota B / Fase 1; $85\%$ na Nota A+ na Fase 2B).
   - **Teto 2 (Risco no Stop com Fricção):**
     $$V_{\text{cap\_risk}} = \frac{r_{\text{eff}} \cdot C_{\text{usdt, total}}}{d_{\text{stop}, i} + f_{\text{ida+volta}} + s_{\text{stress}}}$$
   - **Teto 3 (Disjuntor Absoluto e Proporcional, Item Cat. 2 do TODO):**
     $$V_{\text{cap\_order}} = \min\left(V_{\text{max\_single\_order\_usdt}}, \; \alpha_{\text{max\_equity}} \cdot C_{\text{usdt, total}}\right) = \min\left(500\text{ USDT}, \; 0,55 \cdot C_{\text{usdt, total}}\right)$$
   - **Volume Financeiro Alvo:**
     $$V_{\text{pos}} = \min\left(V_{\text{cap\_cash}}, \; V_{\text{cap\_risk}}, \; V_{\text{cap\_order}}, \; C_{\text{usdt, operável}}\right)$$

4. **Arredondamento `Decimal` e Aborto Estrito Abaixo de $1,05 \times \text{minNotional}$:**
   - $Q_{\text{step}} = \text{floor\_qty\_to\_step}\left(V_{\text{pos}} / P_{\text{entry}}, \; \text{stepSize}\right)$
   - $V_{\text{notional}} = Q_{\text{step}} \cdot P_{\text{entry}}$
   - Se $Q_{\text{step}} < \text{minQty}$ ou $V_{\text{notional}} < 1,05 \times \text{minNotional}$, a operação é **abortada** (`approved = False`, `qty = Decimal("0")`), jamais arredondada para cima.

### 1.2. Contrato de Código (`src/risk/position_sizer.py`)

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Optional
from src.core.config import PortfolioCfg, PositionSizingCfg
from src.core.database import SymbolFilters


class SignalGrade(StrEnum):
    A_PLUS = "A+"
    B = "B"
    C = "C"


@dataclass(frozen=True, slots=True)
class SizingDecision:
    approved: bool
    symbol: str
    grade: SignalGrade
    effective_risk_pct: Decimal
    indifference_prob: float
    net_expected_r: Optional[float]
    usable_cash_usdt: Decimal
    quote_order_qty: Decimal
    base_qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    cap_binding: str            # "RISK_CAP" | "CASH_CAP" | "ORDER_CAP" | "REJECTED"
    reject_reason: Optional[str] = None
    ml_grade_silent: Optional[str] = None
    ml_p_cal_silent: Optional[float] = None


class PositionSizer:
    def __init__(
        self,
        sizing_cfg: PositionSizingCfg,
        portfolio_cfg: PortfolioCfg,
        use_ml_filter: bool = False,
    ) -> None: ...

    def compute_indifference_threshold(
        self,
        stop_distance_pct: float,
        gross_rr_ratio: float,
    ) -> tuple[float, float]:
        """Retorna (custo_em_r, p_indiferenca)."""

    def evaluate_and_size(
        self,
        *,
        symbol: str,
        entry_price: Decimal | str,
        stop_price: Decimal | str,
        target_price: Decimal | str,
        free_usdt: Decimal | str,
        tactical_equity_usdt: Decimal | str | None = None,
        pending_hodl_bucket_usdt: Decimal | str = Decimal("0"),
        symbol_filters: SymbolFilters,
        drawdown_risk_multiplier: Decimal | str = Decimal("1.0"),
        ml_calibrated_prob: Optional[float] = None,
        ml_proposed_grade: Optional[SignalGrade] = None,
        portfolio_cvar99_loss_pct: Optional[float] = None,
        phase2b_graduation_stage: int = 2,
    ) -> SizingDecision: ...
```

---

## 2. Controle de Drawdown CPPI, Modo Fantasma CUSUM e Kill-Switches (`src/risk/shadow_manager.py`)

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Optional, Sequence
from src.core.config import DrawdownShadowProtectionCfg, PortfolioCfg
from src.core.database import Database


class KillSwitchReason(StrEnum):
    NONE = "NONE"
    HARD_STOP_DRAWDOWN_12PCT = "HARD_STOP_DRAWDOWN_12PCT"
    BNB_FEE_RESERVE_DEPLETED = "BNB_FEE_RESERVE_DEPLETED"
    RECONCILIATION_OR_AUTH_FAILURE = "RECONCILIATION_OR_AUTH_FAILURE"
    DAILY_TRADES_CAP_EXCEEDED = "DAILY_TRADES_CAP_EXCEEDED"


@dataclass(frozen=True, slots=True)
class DefenseStatus:
    hwm_usdt: Decimal
    current_equity_usdt: Decimal
    drawdown_pct: Decimal
    risk_multiplier: Decimal        # 1.0 (normal), 0.5 (CPPI >= 4% DD), 0.0 (Shadow ou Halt)
    cppi_active: bool
    shadow_mode_active: bool
    emergency_halt: bool
    kill_switch_reason: KillSwitchReason
    conviction_downgraded: bool
    sideways_disabled: bool
    bnb_below_recommended_reserve: bool = False
    commission_rate_valid: bool = True


class ShadowManager:
    """
    Gerencia High-Water Mark tático, CPPI Drawdown Control (-4% -> 0.5x risco),
    CUSUM Shadow Mode (4 vitórias virtuais com E[R]>0), Auto-Downgrade de Fase,
    checagem preventiva de reserva de BNB ($5.00 USDT) e /api/v3/account/commission,
    além dos 4 Hard Kill-Switches (-12% DD, BNB < $2, 3 falhas de reconciliação/API, >6 trades/24h).
    """
```

---

## 3. Cofre de Lucros com Regra de High-Water Mark (`src/risk/profit_splitter.py`)

```python
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from src.core.binance_client import SignedBinanceClient
from src.core.config import ProfitSplitterCfg
from src.core.database import Database


@dataclass(frozen=True, slots=True)
class ProfitSplitResult:
    equity_before_usdt: Decimal
    equity_after_usdt: Decimal
    previous_hwm_usdt: Decimal
    new_hwm_usdt: Decimal
    new_high_profit_usdt: Decimal
    added_to_bucket_usdt: Decimal
    pending_bucket_usdt: Decimal
    transferred_to_funding_usdt: Decimal
    transfer_tx_id: Optional[int] = None


class ProfitSplitter:
    """
    Separa 30% apenas do lucro líquido ACIMA do High-Water Mark (profit_hwm_usdt).
    Ao acumular >= 10 USDT em pending_hodl_bucket_usdt, executa transferência
    MAIN_FUNDING e ajusta o High-Water Mark pelo valor transferido.
    """
```

---

## 4. Juiz de IA e Calibração por Platt Scaling (`src/features/ml_judge.py`)

- Calcula as 5 features estacionárias causais (`atr_pct`, `dist_ema100_atr`, `rvol_20`, `chop_14`, `btc_regime_ret`).
- Aplica calibração sigmoidal de **Platt Scaling** ($\hat{p}_{\text{cal}} = \frac{1}{1 + \exp(A \cdot z + B)}$) e mede o **Expected Calibration Error ($\text{ECE} \le 0,05$)**.
- Na **Fase 1 (`use_ml_filter = False`)**, opera estritamente em **Modo Observador Silencioso**: calcula `ml_grade_silent` (`A+`, `B`, `C`) e `ml_p_cal_silent` para gravação no SQLite, sem bloquear nem alterar o tamanho do lote.

---

## 5. Gate 4 Unificado (`pytest tests/test_block4_dual_cap_and_cppi.py`)

O comando `pytest tests/test_block4_dual_cap_and_cppi.py` valida deterministicamente:

1. **Teste 4A (Teto Duplo, Disjuntor $\min(500\text{ USDT}, 55\%\text{ da banca})$ e Desconto `pending_hodl_bucket_usdt`):**
   - Um stop largo de `6,0%` jamais aloca `50%` ou `85%/95%` do caixa, sendo travado exatamente pelo teto de risco da carteira ($V_{\text{cap\_risk}}$);
   - Um stop curto nunca ultrapassa `50%` do caixa operável (Nota B) nem o disjuntor de ordem individual;
   - O saldo acumulado em `pending_hodl_bucket_usdt` é descontado integralmente do caixa operável.
2. **Teste 4B (Limiar de Indiferença $\underline{p}_i$, Modo Observador Silencioso na Fase 1 e Bloqueio Nota C na Fase 2):**
   - Com `use_ml_filter=True`, qualquer sinal com $\hat{p}_{\text{cal}} \le \underline{p}_i + 0,03$ recebe lote zero (`approved=False`);
   - Com `use_ml_filter=False` (Fase 1), `ml_grade_silent` e `ml_p_cal_silent` são preenchidos, mas a operação Nota B de `1,0%` prossegue normalmente.
3. **Teste 4C (Aborto Estrito em $< 1,05 \times \text{minNotional}$ e Aritmética `Decimal`):**
   - Lotes abaixo de $1,05 \times \text{minNotional}$ são abortados (`approved=False`, `base_qty=0`), jamais arredondados para cima.
4. **Teste 4D (CPPI Drawdown -4%, CUSUM Shadow 4 Vitórias, Auto-Downgrade e 4 Kill-Switches):**
   - Drawdown de `4,0%` corta o risco de `1,0%` para `0,5%` e recupera `1,0%` ao superar o High-Water Mark;
   - Modo Shadow exige 4 vitórias virtuais com $\mathbb{E}[R] > 0$ (não religa com 1 única vitória de $+2,5R$);
   - Disparo exato de `EMERGENCY_HALT` nos 4 Kill-Switches (`-12%` DD, BNB `< $2,00`, 3 falhas de reconciliação/API e `>= 6` trades/24h).
5. **Teste 4E (Cofre de Lucros `ProfitSplitter` com High-Water Mark e `MAIN_FUNDING`):**
   - Ganho que apenas recupera drawdown abaixo do High-Water Mark gera `added_to_bucket_usdt == 0`;
   - Ganho acima do High-Water Mark acumula `30%` do excedente em `pending_hodl_bucket_usdt` e transfere via `MAIN_FUNDING` ao atingir `10 USDT`, ajustando o HWM pelo valor transferido.
