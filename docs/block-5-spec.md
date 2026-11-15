# Especificação de partida — Bloco 5 Unificado

Este documento congela as assinaturas de classes, métodos (`type hints` do Python), máquinas de estado de execução, regras de proteção contra falhas de rede/corretora (`-1007`, `-2010`, Stop Pendurado, `C1-D`, `C1-E`, `C2-3`) e o **Gate 5 (4 Testes de Tortura)** para a construção do **Bloco 5** (`outcome_resolver.py`, `executor_protocol.py`, `paper_executor.py`, `oco_order_manager.py` e `state_reconciler.py`).

!!! info "Pré-requisito de Partida"
    O Bloco 5 apoia-se nos artefatos validados nos Blocos [1](block-1-spec.md), [2](block-2-spec.md), [3](block-3-spec.md) e [4](block-4-spec.md): `Database` (tabelas `trades`, `shadow_trades`, `order_events`), `SignedBinanceClient`, `WeightGovernor.critical_section()`, `PositionSizer` (`SizingDecision`) e `ShadowManager`.

---

## 1. Resolvedor Compartilhado de Desfecho Intra-Vela (`src/auditing/outcome_resolver.py` — Item C2-3)

Para eliminar qualquer divergência entre o `PaperExecutor` (modo `SHADOW_CHAMELEON`) e o `VirtualAuditor` do Radar (Bloco 6), ambos invocam a função pura `resolve_single_target_horizon` (e `resolve_dual_target_horizon` para dois alvos):

1. **Início estrito em $t + 1$ (`open_time_ms >= signal_close_time_ms`):** Nenhuma vela anterior ou igual ao fechamento do sinal pode resolver a operação.
2. **Pior Caso Intra-Vela (*Worst-Case Intra-Candle*):** Se na mesma vela ocorrer simultaneamente `low <= stop_price` e `high >= target_price`, o resolvedor atribui obrigatoriamente derrota no stop (`SAME_CANDLE_STOP`, `same_candle_ambiguity = True`).
3. **Horizonte por Tempo (`time_barrier_end_ms`, Item C2-7):** A barreira de tempo é medida pelo timestamp `close_time_ms >= time_barrier_end_ms` (e não por contagem cega de linhas), saindo pelo `close` da vela que atingir o horizonte.

---

## 2. Protocolo de Execução e Paper Executor (`src/execution/executor_protocol.py` e `src/execution/paper_executor.py`)

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Optional, Protocol, Sequence
from src.core.database import CandleRow, SymbolFilters
from src.risk.position_sizer import SizingDecision


class TradeStatus(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    ENTRY_FILLED = "ENTRY_FILLED"
    PROTECTED = "PROTECTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    HALT_REVIEW = "HALT_REVIEW"


class ExitReason(StrEnum):
    TARGET = "TARGET"
    STOP = "STOP"
    TIME = "TIME"
    FALLBACK_MARKET_2010 = "FALLBACK_MARKET_2010"
    STOP_WATCHDOG = "STOP_WATCHDOG"
    RECONCILER = "RECONCILER"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    symbol: str
    strategy: str
    regime: str
    signal_close_time_ms: int
    atr_at_signal: float
    time_barrier_end_ms: int
    sizing: SizingDecision
    symbol_filters: SymbolFilters
    radar_signal_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    executed: bool
    mode: str
    trade_id: Optional[int]
    client_order_id: str
    status: str
    filled_qty: Decimal
    avg_entry_price: Decimal
    oco_order_list_id: Optional[int] = None
    exit_reason: Optional[str] = None
    error_msg: Optional[str] = None


class ExecutorProtocol(Protocol):
    def execute_entry(self, req: ExecutionRequest) -> ExecutionReport: ...
```

---

## 3. Gerenciador de Ordens OCO e Resiliência (`src/execution/oco_order_manager.py`)

Implementa as 6 blindagens obrigatórias de execução na Binance Spot:

1. **Idempotência contra Timeout (`-1007`):**
   - Grava `PENDING_ENTRY` no SQLite **antes** do `POST /api/v3/order` com `client_order_id = f"CHM_{symbol}_{epoch_ms}"`. Se a gravação falhar, a compra não é enviada.
   - Se `new_market_buy_quote` lançar `RequestTimeoutUnknownState` (`-1007` ou queda de conexão), **jamais** reenvia o `POST`: consulta obrigatoriamente `GET /api/v3/order?symbol=...&origClientOrderId=...`. Se a ordem executou (`executedQty > 0`), prossegue direto para a criação da OCO; se não existiu (`-2013`), marca `ENTRY_REJECTED`.
2. **Escritas Pós-Fill *Best-Effort* (`C1-E`) e `critical_section()` (`C1-B`):**
   - Entre o envio da compra a mercado e a confirmação da OCO (ou fallback a mercado), o gerenciador mantém `governor.critical_section()` ativa (bloqueando chamadas `HIGH` e `LOW`) e executa atualizações no SQLite em modo *best-effort*: uma falha de I/O ou lock no banco após o fill **nunca** interrompe o envio imediato da ordem OCO.
3. **Quantidade Líquida da OCO (Desconto de Comissão no Ativo Base):**
   $$Q_{\text{oco}} = \text{floor\_qty\_to\_step}\left(\min\left(Q_{\text{executed}} - \sum_{k \text{ onde asset}=A} \text{commission}_k, \; \text{FreeBalance}(A)\right), \; \text{stepSize}_A\right)$$
4. **Arredondamento Direcional do `tickSize` na OCO:**
   - `abovePrice` (Take Profit): `round_price_to_tick(target_price, tick_size, "DOWN")`
   - `belowStopPrice` (Gatilho de Stop): `round_price_to_tick(stop_price, tick_size, "UP")`
   - `belowPrice` (Limite do Stop): `round_price_to_tick(stop_price * (1 - buffer_pct), tick_size, "DOWN")`, garantindo `belowPrice <= belowStopPrice - tick_size`.
5. **Tratamento Cirúrgico do Erro `-2010` ("Stop price would trigger immediately"):**
   - Se antes do envio da OCO o `bookTicker.bid` já estiver em $P_{\text{bid}} \le P_{\text{stop}} \times 1,0005$, ou se a Binance rejeitar a OCO com código `-2010` / mensagem `"trigger immediately"`, dispara imediatamente uma venda total `MARKET` (`exit_reason = "FALLBACK_MARKET_2010"`).
6. **Watchdog de Stop Pendurado e Fechamento Atômico de 4 Passos no Time-Stop:**
   - **Watchdog:** Se a perna de stop da OCO foi acionada mas a ordem limite ficou pendurada porque o preço pulou abaixo de `belowPrice`, cancela a OCO e vende o saldo residual a `MARKET` (`STOP_WATCHDOG`).
   - **Time-Stop (4 passos):** (1) Cancela a `orderList` OCO; (2) Lê as quantidades já executadas nas pernas da OCO; (3) Consulta o saldo livre atualizado em `/api/v3/account`; (4) Vende a `MARKET` apenas a quantidade residual não executada (`TIME`).

---

## 4. Reconciliador de Estado e Cura no Boot (`src/core/state_reconciler.py`)

Varre exclusivamente `cfg.execution_universe` (7 moedas da cesta, nunca tocando em `BNBUSDT` — `C1-C`):

1. **Resolução de `PENDING_ENTRY`:** Consulta `origClientOrderId`. Se `FILLED`, recupera os dados do fill e chama `oco_manager.protect_or_fallback_market()` (criando a OCO ou vendendo a `MARKET` se o preço já violou o `stopPrice`). Se inexistente (`-2013`), marca `ENTRY_REJECTED`.
2. **Cura de Posição Desprotegida (`ENTRY_FILLED`) ou OCO Parcial (`PROTECTED`):**
   - Se a OCO executou parcialmente (ex.: 40% executados e 60% desprotegidos acima de `minNotional`), recria a proteção OCO exatamente para os 60% restantes (ou vende a `MARKET` se o preço estiver abaixo do `stopPrice`).
3. **Proteção contra Liquidação de Posição Manual (`C1-D` — `HALT_REVIEW`):**
   - Se o saldo `free + locked` de qualquer moeda da cesta exceder a quantidade rastreada na tabela `trades` em mais do que `minNotional` (ex.: compra manual feita pelo operador na mesma conta), o reconciliador **jamais vende o saldo excedente**. Ele transiciona o estado para `HALT_REVIEW`, aciona o disjuntor no `ShadowManager` e emite alerta para revisão humana.

---

## 5. Gate 5 Unificado (`pytest tests/test_block5_reconciler_and_oco.py`)

O comando `pytest tests/test_block5_reconciler_and_oco.py` valida os **4 Testes de Tortura** mais as garantias de integração `C1-D`, `C1-E` e `C2-3`:

1. **Tortura 1 (Timeout `-1007` na Compra a Mercado):** Simula queda de conexão / `-1007` durante `new_market_buy_quote`. Prova que o robô **não** envia uma segunda compra duplicada, consulta `GET /api/v3/order?origClientOrderId=...`, descobre que a ordem executou e cria a OCO normalmente.
2. **Tortura 2 (Compra sem BNB com Comissão Descontada no Ativo Base + Direção do `tickSize`):** Simula uma compra onde a corretora desconta `0,10%` de taxa no próprio ativo comprado (ex.: comprou `10.00 SOL`, recebeu `9.99 SOL` livres). Prova que `OcoOrderManager` calcula `Q_oco = 9.99` via `floor_qty_to_step` e envia a OCO sem sofrer erro `-2010` por saldo insuficiente, garantindo `belowPrice < belowStopPrice < abovePrice`.
3. **Tortura 3 (Queda de Energia com Preço Abaixo do `stopPrice` no Boot — Fallback `-2010`):** Simula o desligamento do bot após a compra, com o preço despencando abaixo do `stopPrice` (ou a Binance retornando `-2010: Stop price would trigger immediately`). Prova que no 1º segundo do boot o reconciliador + `OcoOrderManager` disparam venda `MARKET` imediata (`FALLBACK_MARKET_2010`), zerando a posição desprotegida.
4. **Tortura 4 (Execução Parcial de 40% da OCO, Watchdog de Stop Pendurado e Time-Stop de 4 Passos):** Simula uma OCO onde 40% foram executados e cancelados, provando que o `StateReconciler` protege exatamente os 60% restantes; testa também o *Watchdog* de Stop Pendurado (`STOP_WATCHDOG`) e o fechamento atômico de 4 passos no Time-Stop (`TIME`).
5. **Tortura 5 (Blindagens de Integração `C1-D`, `C1-E` e `C2-3`):**
   - `C1-D`: Saldo manual não rastreado em ativo da cesta aciona `HALT_REVIEW` sem vender um único centavo da posição manual;
   - `C1-E`: Falha simulada de escrita no SQLite (`sqlite3.OperationalError`) imediatamente após o fill da compra **não** impede a criação da OCO na Binance;
   - `C2-3`: `PaperExecutor` e `outcome_resolver.py` resolvem ambiguidade intra-vela pelo pior caso (`SAME_CANDLE_STOP`) e respeitam Single-Slot em `shadow_trades`.
