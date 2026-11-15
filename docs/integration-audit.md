# Auditoria de integração Radar × Chameleon

Esta página registra a caça final a falhas **inéditas**, isto é, ausentes do
[TODO](todo.md) original, que surgem **exclusivamente** da interação entre o
Radar (livro de ofertas, OBI, Whale Walls, Auditor Virtual) e o Chameleon.

Classificação usada:

* **Categoria 1 (Catastrófico):** quebra o sistema ou perde dinheiro.
* **Categoria 2 (Refinamento útil):** melhora a precisão, a robustez ou a operação.

!!! success "Veredito"
    Foram encontradas **5 falhas inéditas de Categoria 1**, todas de
    **integração**. Nenhuma invalida a matemática de nenhum dos dois sistemas
    isoladamente. Três delas (C1-B, C1-C, C1-E) são resolvidas **dentro do
    próprio Bloco 1**. A C1-A é resolvida na Camada 4 do Radar, e a C1-D no
    reconciliador (Bloco 5) e nas regras operacionais.

    **A arquitetura está APROVADA para codificação do Bloco 1**, com as 5
    emendas incorporadas em [Contrato de Dados](data-contract.md) e
    [Especificação do Bloco 1](block-1-spec.md).

---

## Categoria 1 — Catastróficos

### C1-A · O `depth?limit=500` não cobre a banda de ±1,5% { #c1-a }

**[FATO] Medição ao vivo** (`data-api.binance.vision`, 14/11/2026):

| Símbolo | `limit` | Cobertura dos bids | Cobertura dos asks |
| --- | --- | --- | --- |
| BTCUSDT | 500 | **0,18%** | **0,32%** |
| BTCUSDT | 1000 | 0,36% | 0,63% |
| BTCUSDT | 5000 (peso 250) | 1,74% | 2,79% |
| ETHUSDT | 500 | **0,62%** | **0,90%** |
| ETHUSDT | 1000 | 1,45% | 1,94% |
| SOL / XRP / DOGE | 500 | 6,6% a 9,8% | 6,9% a 9,6% |

**Efeito:** em BTC e ETH, o $\text{OBI}_{1,5\%}$ original soma bids até 0,18% e
asks até 0,32%. São janelas de larguras diferentes, então o número mede
densidade de *tick*, não pressão. Já a "mediana das faixas de 0,1%" usada nas
Whale Walls é calculada sobre 2 ou 3 faixas, uma delas parcial, e quase tudo
vira "parede".

**Por que é Categoria 1:**

* 25 dos 100 pontos do score (e, com eles, toda a fronteira A/A+) ficam corrompidos em silêncio.
* O `virtual_signals` grava features erradas que **não podem ser recalculadas depois**, porque não existe histórico gratuito de depth.
* Ou seja, o ativo de dados que justifica o Radar nasceria inutilizável.

**Correção (Radar L4):**

$$\text{cov}_{\text{bid}} = \frac{P_{\text{best bid}} - P_{\min \text{ bid}}}{\text{mid}}, \qquad \text{cov}_{\text{ask}} = \frac{P_{\max \text{ ask}} - P_{\text{best ask}}}{\text{mid}}$$

$$\text{band}_{\text{eff}} = \min\left(1{,}5\%,\; \text{cov}_{\text{bid}},\; \text{cov}_{\text{ask}}\right)$$

1. O OBI passa a ser calculado sobre a banda **simétrica** $\text{band}_{\text{eff}}$, gravada em cada snapshot.
2. `obi_valid = band_eff ≥ 0,30%`. Quando inválido, a Camada 4 dá **0 pontos e não penaliza**, e a nota máxima possível passa a ser **A**.
3. A faixa de borda parcialmente coberta é descartada antes de calcular a mediana das Whale Walls.
4. `depth_limit` passa a ser por símbolo (BTC/ETH = 1000), respeitando `k_max = 5`.

### C1-B · Peso de IP compartilhado sem prioridade { #c1-b }

**[RISCO] Sequência fatal:**

1. A compra `MARKET` é executada.
2. No mesmo minuto, um bug ou loop de retry do Radar no `depth` (peso de 25 a 250 por chamada) leva o IP ao 429.
3. O `POST /api/v3/orderList/oco` volta 429. Se houver insistência, vem o 418 (ban de minutos a dias).
4. O reconciliador também depende do mesmo IP, e a posição fica **sem stop** até o ban expirar.

**Correção (Bloco 1, `binance_client.py`):** `WeightGovernor` com 3 prioridades.

| Prioridade | Chamadas | Regra |
| --- | --- | --- |
| `CRITICAL` | ordens, OCO, cancelamento, reconciliação | Sempre passa, salvo durante um 418 ativo |
| `HIGH` | klines, `exchangeInfo`, `time`, `bookTicker` | Recusada acima de 75% do limite |
| `LOW` | depth (Radar L4) | Recusada acima de 50% do limite, **sem retry automático** |

Regras complementares:

* `critical_section()` entre o fill e a OCO confirmada: enquanto ela está ativa, nenhuma chamada `HIGH` ou `LOW` sai.
* Em um 429, respeitar o `Retry-After` e suspender as chamadas não críticas pelo dobro desse tempo.
* O uso de peso vem do header `X-MBX-USED-WEIGHT-1M`.

### C1-C · Vazamento do universo do Radar para o executor { #c1-c }

**[RISCO]** O universo do Radar contém `BNBUSDT`, `DOGEUSDT`, `SUIUSDT` e outras moedas fora da cesta do Chameleon.

* **Com `BNBUSDT`:** o saldo comprado se mistura à reserva de taxas. O
  reconciliador, que compara `free + locked` com a quantidade coberta pela OCO,
  enxerga a reserva como resíduo descoberto acima do `minNotional` e **liquida a
  reserva de BNB**. As próximas OCOs passam a pagar comissão no ativo base, o
  que causa `-2010` e posições sem stop.
* **Com moedas fora da cesta:** o reconciliador só varre os 7 ativos. Após uma
  queda de energia, a posição fica **órfã, sem stop e invisível**.

**Correção (Bloco 1, `config.py`):**

* `execution_universe = strategies.universe.symbols − {BNBUSDT}`.
* A validação em LIVE falha no boot se houver interseção com `FORBIDDEN_EXECUTION_SYMBOLS`.
* O reconciliador lê **a mesma constante**.
* O executor consome apenas `StrategySignal` de `bull_trend_pullback` (ADR-003).

### C1-D · Trade manual do "copiloto" na mesma conta da API { #c1-d }

**[RISCO]** Se você comprar manualmente um ativo da cesta (por exemplo, SOL) na
mesma conta Spot em que o Chameleon opera:

* o reconciliador detecta saldo de SOL sem OCO correspondente e **vende a sua posição manual a mercado**;
* ou, se o Chameleon já estiver posicionado em SOL, calcula `Q_oco` com `min(..., FreeBalance)` sobre um saldo misturado e protege uma quantidade errada;
* e o USDT gasto manualmente reduz o caixa usado no dimensionamento.

**Correção:**

1. **Regra operacional:** em `LIVE_CHAMELEON_PHASE_1`, operações manuais acontecem **somente em subconta ou conta separada**.
2. **Regra de código (Bloco 5):** saldo de um ativo da cesta que **exceda** a quantidade rastreada em `trades` (mais a poeira abaixo do `minNotional`) **nunca é vendido automaticamente**. O sistema entra em `HALT_REVIEW` (bloqueia novas entradas) e alerta no Telegram. A venda automática fica restrita a resíduos de OCO **rastreados**.
3. Em modo LIVE, cards do Radar sobre ativos da cesta trazem o rodapé: *"⚠️ Ativo gerido pelo executor — não opere nesta conta."*

### C1-E · Falha de escrita no SQLite após o fill bloqueia a OCO { #c1-e }

**[RISCO]** Fluxo ingênuo: `POST order` → `db.update(ENTRY_FILLED)` → `POST oco`.
Se a atualização lançar `OperationalError` (lock, disco cheio, I/O da VPS), a
exceção aborta o fluxo **antes** da OCO. O reconciliador só roda no próximo
ciclo, até 1 hora depois, e durante esse tempo a posição fica nua.

**Correção (contrato do `database.py` + Bloco 5):**

* A escrita de `PENDING_ENTRY` **antes** do POST é obrigatória: se falhar, não compra.
* Escritas **depois** do fill são *best-effort* com retry. O estado em memória é a fonte de verdade até a OCO ser confirmada.
* A sequência fill → OCO **nunca** é interrompida por exceção de banco. A falha é registrada, e o reconciliador cura o registro em seguida.
* O single-writer (ADR-002) elimina a causa mais provável.

---

## Categoria 2 — Refinamentos úteis

| # | Item | Onde entra |
| --- | --- | --- |
| C2-1 | **Faixas de 0,1% ancoradas na grade absoluta do snapshot 0** (`floor(price / (mid₀ · 0,001))`). Sem isso, o `mid` se move entre as fotos, as faixas deslizam e a "persistência 3/3" do anti-spoofing falha ou passa por acidente. | Radar L4 |
| C2-2 | **O auditor começa na vela t+1** (`open_time ≥ signal_close_time`) e usa como referência de entrada o **ask** do snapshot 0, não o `close` (o alerta sai de 10 a 20 s depois do fechamento). | `virtual_auditor.py` |
| C2-3 | **Um `outcome_resolver.py` compartilhado** por auditor e `paper_executor`: pior caso intra-vela (stop e alvo na mesma vela = stop) e Time-Stop no `close` da última vela do horizonte. | `auditing/` |
| C2-4 | **Modelo de custo único:** o Radar usava `0,20%` e o Chameleon usa `0,15% + 0,06%`. Os dois passam a ler `strategies.yaml → position_sizing`. | `config.py` |
| C2-5 | **Persistir as colunas 7 e 10 do kline** (`quote_volume` e `taker_buy_quote_volume`) para calcular o Delta em USDT exato. | Bloco 1 (`candles`) |
| C2-6 | **Guardas de zero:** com `V = 0`, `TBR = NaN`; com mediana = 0, `RVOL = NaN` (nunca `inf`, porque `inf ≥ 1,25` é verdadeiro). | Bloco 2 |
| C2-7 | **Lacunas de velas** (manutenção da Binance): detectar gaps por `close_time` e medir o horizonte do auditor e o Time-Stop por **tempo**, não por contagem de linhas. | Bloco 1 + auditor |
| C2-8 | **Não gravar o JSON bruto do depth** (cerca de 32 KB por foto com `limit=500`, medido). Gravar só os agregados e as faixas comprimidas (cerca de 1 KB). O JSON bruto faria o banco crescer cerca de 160 MB/ano, e o backup via Telegram tem limite de **50 MB por arquivo** na Bot API. | Schema |
| C2-9 | **Ordem do ciclo:** a execução do Chameleon roda antes da Camada 4, e as fotos são intercaladas entre símbolos (janela de cerca de 10 s, seja qual for k), o que não atrasa a entrada medida no Gate 6B. | `main.py` |

Todos os itens acima foram incorporados ao [TODO](todo.md).
