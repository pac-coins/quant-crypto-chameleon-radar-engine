# Metodologia de engenharia, governança científica e roadmap

Este documento define o protocolo determinístico de construção, validação estatística, transição entre fases operacionais e resposta a incidentes do **QuantCrypto Chameleon Engine (v6.0)**. Nenhuma linha de código avança de bloco e nenhum centavo adicional é arriscado sem o cumprimento binário (`PASS` / `FAIL`) dos critérios aqui estabelecidos.

---

## 1. Visão Geral: A Diferença entre "Blocos de Construção" e "Fases de Operação"

Para que nunca haja confusão sobre onde o projeto está, separamos o ciclo de vida em duas réguas independentes:

* **Explicação Intuitiva (Como funciona na prática):**
Imagine que você está construindo um avião.
1. Os **6 Blocos de Construção (Estágios 1 a 6)** são a **fábrica no chão**: você monta o motor (Bloco 1), coloca as asas (Bloco 2), testa no túnel de vento (Bloco 3), instala o paraquedas (Bloco 4), liga na tomada de teste da oficina (Bloco 5) e faz um voo de teste sem passageiros com controle remoto (Bloco 6). Você ainda não colocou dinheiro real em risco.
2. As **3 Fases de Operação (Fases 1, 2 e 3)** começam **depois que o avião já está pronto e aprovado para voar**: na **Fase 1**, você só voa em dias de céu azul (`BULL`) e baixa altitude (risco fixo de 1%). Só depois de completar 60 voos reais com lucro comprovado é que você recebe a carteira de habilitação para a **Fase 2** (ligar o piloto automático de IA e o Cofre de Lucros). E só depois de 100 voos lucrativos você libera a **Fase 3** (voar em dias nublados / mercado lateral).



```text
================================================================================
ETAPA A: FÁBRICA E VALIDAÇÃO (ZERO DINHEIRO REAL EM RISCO)
================================================================================
[Bloco 1: Fundação] ➔ [Bloco 2: Clima MECE] ➔ [Bloco 3: Túnel de Vento (Colab)]
                                                       │ (Se PASS no Gate 3)
[Bloco 6: Shadow Live] ◄── [Bloco 5: Testnet OCO] ◄── [Bloco 4: Risco e Freios]
       │
       ▼ (Se PASS no Gate 6A e 6B)
================================================================================
ETAPA B: OPERAÇÃO COM CAPITAL REAL (EVOLUÇÃO POR MÉRITO COMPROVADO)
================================================================================
[FASE 1: MVP Tendência Pura] ──(Gate F1->F2)──► [FASE 2: +IA Silenciosa Validada + Cofre] ──(Gate F2->F3)──► [FASE 3: Multi-Clima]

```

---

## 2. Governança Científica Anti-Ilusão (Regras do Bloco 3 contra *Overfitting*)

O maior risco em trading algorítmico é testar dezenas de combinações no simulador até achar uma que deu lucro por pura sorte no passado (*p-hacking* / *Data Snooping*) e perder dinheiro no futuro. Para impedir isso, o Bloco 3 obedece a **4 Leis Inegociáveis**:

### 2.1. Partição Rígida do Histórico (`2022-01-01` até o presente)

Os dados históricos das 7 moedas (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `ADAUSDT`, `LINKUSDT`, `AVAXUSDT`) são divididos cronologicamente em três cofres:

1. **In-Sample / Treino (Primeiros 60% do tempo):** Único período onde é permitido explorar dados, treinar modelos e ajustar parâmetros.
2. **Validation / Walk-Forward (25% seguintes do tempo):** Usado para validação cruzada com expurgo (*Purging* de 24h e *Embargo* de 2%) e calibração de probabilidades (*Platt Scaling*).
3. **Holdout Intocável (Últimos 15% do tempo — mínimo de 6 meses finais):** Este período fica trancado. Ele só pode ser executado **uma única vez** quando o código e os parâmetros estiverem 100% congelados.

### 2.2. Orçamento Máximo de Tentativas (*Trial Budget*)

* Toda vez que o notebook do Bloco 3 rodar uma simulação para testar uma ideia ou ajuste de parâmetro, o código grava automaticamente uma linha no arquivo `logs/backtest_trials_registry.csv` contendo: timestamp, hash dos parâmetros, Sharpe Ratio e retorno em $R$.
* **Limite de Tentativas ($K_{\max} = 20$):** São permitidas no máximo 20 execuções exploratórias no conjunto de Validação. O número total $K$ registrado nesse arquivo é inserido obrigatoriamente na fórmula do **Deflated Sharpe Ratio ($\text{DSR}$)** de Bailey & López de Prado (2014).

### 2.3. Critério de Abandono (O que fazer se a estratégia reprovar no Bloco 3?)

* **Se a estratégia reprovar no conjunto de Validação:** Você pode alterar a lógica da estratégia (ex.: exigir confirmação de volume ou mudar a regra de entrada) apenas dentro dos dados de Treino/Validação, desde que $K \le 20$.
* **Se a estratégia reprovar no cofre final de Holdout (últimos 6 meses):** É **estritamente proibido** voltar atrás, mudar um número no `strategies.yaml` (ex.: trocar a EMA de 20 para 25) e rodar o Holdout de novo! Se reprovar no Holdout, a estratégia atual está **reprovada e descartada**. Para testar uma nova estratégia do zero, será obrigatório aguardar novos dados ou mudar estruturalmente a família da estratégia, zerando o processo sob nova hipótese.
* **Holdout Prospectivo Verdadeiro (Blindagem Pós-Bloco 3 no Gate 6B):** Como nas rodadas empíricas `K1`–`K9` a métrica `HoldSum` foi observada na saída consolidada do relatório (sendo o diagnóstico estrutural de rompimento Donchian 12h + Força Relativa derivado exclusivamente do conjunto de Treino/Validação), o **Gate 6B (30 dias ou 25 operações em `SHADOW_CHAMELEON` ao vivo na Binance Produção)** atua obrigatoriamente como o **Holdout Prospectivo Verdadeiro (*Forward Out-of-Sample*)** antes da liberação do capital real na Fase 1.

---

## 3. Cronograma Sequencial de Construção (Blocos 1 a 6 e Seus Gates)

Você (e a IA programadora) só podem escrever a primeira linha de código do Bloco $N+1$ quando o comando de teste do **Gate $N$** retornar `100% PASS`.

| Bloco | Arquivos Construídos | Regras Obrigatórias Embutidas (Categorias 1 e 2) | Gate de Aprovação (Critério Binário) |
| --- | --- | --- | --- |
| **Bloco 1: Dados, Relógio, Governador e Banco** | `config.py`, `database.py`, `binance_client.py`, `candle_collector.py`, `test_block1_*.py` | 1. SQLite WAL (`BEGIN IMMEDIATE`, single-writer), `synchronous=FULL`, schema v1 completo e `sqlite3.backup()`. 2. `Decimal` em preços (`tickSize` direcional) e lotes (`stepSize` `ROUND_DOWN`). 3. `WeightGovernor` (`CRITICAL > HIGH > LOW`), `critical_section()` e sincronização monotônica (`RTT/2`). 4. Normalização `close_time` (`+1ms`), cols 7–10 e `merge_asof`. 5. `RunMode` (`ANALYST_ONLY`, `SHADOW_CHAMELEON`, `LIVE_CHAMELEON_PHASE_1`) e mock `/sapi/*` na Testnet. | **Gate 1 Unificado ([spec](block-1-spec.md#5-gate-1-unificado-criterio-binario)):** `pytest tests/test_block1_causal_and_decimal.py tests/test_block1_db_governor_modes.py` aprova: (a) truncamento às `02:00 UTC` sem vazamento; (b) 10.000 preços/lotes via `Decimal`; (c) restauração real de `sqlite3.backup()`; (d) `WeightGovernor` + `critical_section()`; (e) invariantes de `RunMode` e `BNBUSDT`; (f) Single-Slot no banco + `WrongThreadError`; (g) cols 7–10 e descarte da vela aberta. |
| **Bloco 2: Indicadores, FeatureStore e Clima MECE** | `indicators.py`, `order_flow.py`, `price_action.py`, `feature_store.py`, `regime_detector.py`, `test_block2.py` | 1. `ATR`, `RSI` e `ADX` com suavização RMA de Wilder. 2. `CHOP` na base $\log_{10}$ com proteção contra divisão por zero ($\text{High} == \text{Low} \rightarrow \text{NaN}$) e guardas de zero em `TBR`/`RVOL`. 3. `FeatureStore` calculando a união de EMAs (`9/20/50/100/200`) uma única vez. 4. Qualquer `NaN`, infinito ou histórico menor que 120 velas força retorno imediato para `TRANSITION` (100% em USDT). 5. Ordem MECE (fonte única de clima): `BEAR` $\rightarrow$ `BULL` $\rightarrow$ `SIDEWAYS` $\rightarrow$ `TRANSITION`. | **Gate 2 Unificado ([spec](block-2-spec.md#6-gate-2-unificado-criterio-binario)):** `pytest tests/test_block2_talib_and_mece.py` aprova: (a) erro máximo $< 10^{-10}$ (correlação $> 0,9999$) com `TA-Lib`/Wilder após aquecimento; (b) série constante/volume zero retorna `NaN` e `TRANSITION` sem exceção; (c) guarda `< 120` velas de 4h; (d) 100% de cobertura disjunta nos testes de fronteira MECE; (e) `FeatureStore` + `PriceAction` + `OrderFlow` compartilhados. |
| **Bloco 3: Simulador Causal e Ablação** | `strategies/*`, `causal_simulator.py`, `ablation_and_dsr.py`, `01_download_causal_1h_4h_1m.ipynb`, `02_ablation_and_dsr_sim.ipynb`, `04_radar_layers_123_backtest.ipynb`, `test_block3_*.py` | 1. Simulação da fila Single-Slot descontando taxas assimétricas (*maker* no alvo, *taker* no stop/tempo). 2. Cálculo empírico de $\mathbb{E}[R]$ incluindo o resultado real das saídas por tempo (`24h` / `72h` em `K9.3`). 3. Resolução de 1 minuto (`1m`) para testar se o Breakeven (Degrau E) ajuda ou atrapalha. 4. Registro automático no `trials_registry.csv` (incluindo estudo das Camadas 1–3 do Radar). | **Gate 3 ([spec](block-3-spec.md#5-gate-3-unificado-pytest-teststest_block3_causal_sim_and_dsrpy)):** `pytest tests/test_block3_causal_sim_and_dsr.py` + Colab aprovam: (a) Mínimo de **150 trades** fora da amostra; (b) Expectância líquida $\bar{R} \gt +0,10R$ com limite inferior do Intervalo de Confiança de 95% (*Block Bootstrap*) acima de $0,0R$; (c) *Profit Factor* maior ou igual a $1,20$ no cenário de custo Base e maior que $1,05$ no cenário Pessimista (slippage dobrado); (d) Lucro positivo em pelo menos 3 de 4 subperíodos e no Holdout de 6 meses; (e) $\text{DSR} \ge 0,95$. |
| **Bloco 4: Cérebro de Risco e Lote** | `position_sizer.py`, `shadow_manager.py`, `profit_splitter.py`, `ml_judge.py`, `03_tail_stress_cppi_cusum.ipynb`, `test_block4_*.py` | 1. Teto duplo: risco de $1,0\%$ no stop e máximo de $50\%$ do caixa operável (com guarda de CVaR 99% $\le 5,0\%$ e escada $1,5\% \rightarrow 2,0\%$ na Nota A+). 2. Disjuntor de ordem máxima proporcional: $\min(500 \text{ USDT}, 55\% \text{ do caixa})$. 3. Reserva mínima preventiva de `$5,00 USDT` em BNB (`$2,00` hard stop) e checagem de `/api/v3/account/commission`. 4. Desconto de `pending_hodl_bucket_usdt` do caixa livre. 5. Regra de *High-Water Mark* no Cofre (só separa 30% do lucro *acima* do topo histórico da carteira tática). | **Gate 4 ([spec](block-4-spec.md#5-gate-4-unificado-pytest-teststest_block4_dual_cap_and_cppipy)):** `pytest tests/test_block4_dual_cap_and_cppi.py` aprova: (a) stop largo de $6\%$ limita a compra a $\sim 16\%$ do caixa (jamais furando $1\%$ de perda); (b) lote menor que `1,05 * minNotional` retorna `0.0` (abortado); (c) lucro após um drawdown que ainda não recuperou o topo histórico gera transferência zero para o Cofre; (d) queda de $4\%$ reduz o lote seguinte exatamente pela metade ($0,5\%$). |
| **Bloco 5: Execução OCO, Paper Executor e Cura no Boot** | `outcome_resolver.py`, `executor_protocol.py`, `paper_executor.py`, `oco_order_manager.py`, `state_reconciler.py`, `test_block5_*.py` | 1. Gravação `PENDING_ENTRY` antes do envio e consulta obrigatória por `origClientOrderId` em caso de Timeout (`-1007`); escritas pós-fill *best-effort* nunca bloqueiam a OCO (C1-E). 2. Quantidade da OCO descontando comissão no ativo base. 3. Tratamento cirúrgico do `-2010`: vende a `MARKET` imediatamente se $P_{\text{bid}} \le P_{\text{stop}} \times 1,0005$ ou mensagem `"trigger immediately"`. 4. *Watchdog* de Stop Pendurado: se a perna de stop ativou mas o preço pulou o `stopLimitPrice`, cancela e vende a `MARKET`. 5. Fechamento serializado de 4 passos no Time-Stop, cura de OCO parcial e `HALT_REVIEW` para saldo excedente não rastreado (C1-D). | **Gate 5 ([spec](block-5-spec.md#5-gate-5-unificado-pytest-teststest_block5_reconciler_and_ocopy)):** `pytest tests/test_block5_reconciler_and_oco.py` + Teste na Binance Spot Testnet aprovam os **4 Testes de Tortura**: (1) Timeout simulado na compra recupera a ordem via `origClientOrderId` sem comprar duplicado; (2) Compra sem BNB desconta a taxa do ativo e cria a OCO sem erro `-2010`; (3) Queda de energia simulada com preço abaixo do `stopPrice` dispara venda a `MARKET` no 1º segundo do boot; (4) Execução parcial de 40% da OCO faz o reconciliador proteger os 60% restantes. |
| **Bloco 6: Radar, Auditor, Observabilidade e Voo Assistido** | `radar/*`, `auditing/*`, `telegram.py`, `main.py`, `Dockerfile`, `docker-compose.yml` | 1. Radar L1–L5 com `band_eff` simétrica (C1-A), grade ancorada (C2-1) e Auditor Virtual (`outcome_resolver.py`, C2-2/C2-3). 2. Alertas claros no Telegram via outbox (clima atual, distância da EMA, tamanho do lote, status do relógio e reserva de BNB). 3. Verificação de manutenção da Binance (`/sapi/v1/system/status`) bloqueando entradas 2h antes. 4. Container único (`mem_limit: 512m`) com reinício automático e backup diário consistente. | **Gate 6A (Engenharia — 7 dias na Testnet em `LIVE_CHAMELEON_PHASE_1`)** e **Gate 6B (Shadow Live — 30 dias/trades em `SHADOW_CHAMELEON` com dados reais)** descritos na Seção 4 abaixo. |

---

## 4. Como Saber Exatamente Quando Mudar de Fase (Critérios de Graduação)

Esta seção responde de forma matemática e sem subjetividade à pergunta: **"Quando eu viro a chave de uma fase para a outra?"**

### 4.1. Transição 0 $\rightarrow$ Fase 1: Da Testnet para o Dinheiro Real (Os 145,5 XRP em USDT)

Você **não** coloca dinheiro real logo após terminar de programar o Bloco 6. A passagem para a Fase 1 exige dois carimbos sequenciais:

1. **Gate 6A — Estabilidade de Engenharia (7 Dias Contínuos na Binance Spot Testnet):**
* Zero exceções não tratadas (`Unhandled Exceptions`) nos logs do Docker em 168 ciclos horários consecutivos.
* Offset do relógio monotônico mantido sempre dentro de $\pm 500\text{ ms}$.
* **Teste de Restauração de Desastre:** Ao final do 7º dia, você apaga o container, restaura o arquivo de backup do SQLite em uma pasta limpa e inicia o robô — ele deve reconhecer todo o histórico e estado sem erros.


2. **Gate 6B — Estabilidade de Microestrutura em Produção (*Paper Trading / Shadow Live* por 30 Dias ou 25 Trades):**
* O robô é ligado na VPS em modo `MODE=SHADOW_CHAMELEON` apontando para os **dados reais da Binance Produção** (`data-api.binance.vision`, zero chaves privadas), ou operando lote mínimo em conta isolada apenas para medir execução.
* **Critério de Aprovação:** A diferença entre o preço de execução esperado pelo simulador e o preço real do livro de ofertas da Binance (*slippage* realizado) deve ser, na média, menor ou igual a $0,08\%$, e a taxa de trades gerados por semana deve ser compatível com a distribuição histórica do Bloco 3.



---

### 4.2. O Que Roda na Fase 1 (Configuração Congelada do MVP Real)

Quando o Gate 6B é aprovado, você vira a chave para a **Fase 1** com os seus ~145,5 XRP em USDT e ~4,5 XRP em BNB:

* **Ativo:** Apenas o sensor de 4h (`BEAR`, `BULL`, `SIDEWAYS`, `TRANSITION`) e **apenas 1 estratégia** (`bull_trend_pullback` quando o clima é `BULL`). Nos outros 3 climas, o robô fica 100% protegido em USDT.
* **Lote:** Risco fixo de **1,0% da banca tática no Stop Loss** (ou 0,5% se o drawdown passar de 4%), limitado a no máximo 50% do caixa.
* **Desligados para proteção:** Breakeven ($+1,2R$) desligado, Cofre de Lucros (`profit_splitter`) desligado, e Juiz de IA (`ml_judge`) rodando **apenas em modo observador silencioso** (ele calcula a probabilidade e grava a nota C, B ou A+ numa coluna do SQLite, mas **não bloqueia nem altera o tamanho do lote**).

---

### 4.3. Transição Fase 1 $\rightarrow$ Fase 2: Quando Ligar o Cofre de Lucros e o Juiz de IA?

*Explicação simples:* Enquanto o robô opera na Fase 1, o Juiz de IA fica sentado no banco do passageiro anotando num caderno: *"Nesse trade eu teria dado Nota C (bloqueado); naquele eu teria dado Nota A+"*. Quando você completar 60 trades reais, você abre o caderno do Juiz: se os trades que ele chamou de "Nota C" realmente deram prejuízo na vida real e os "Nota A+" deram lucro, o Juiz provou que sabe o que está fazendo no mercado ao vivo e ganha permissão para dirigir na Fase 2!

Você **só** muda o `strategies.yaml` da Fase 1 para a Fase 2 quando **TODOS os 5 requisitos abaixo** forem verdadeiros ao mesmo tempo:

| # | Requisito para Subir para a Fase 2 | Métrica Exata Exigida (Lida do Banco SQLite) |
| --- | --- | --- |
| **1** | **Amostra Mínima Ao Vivo** | Pelo menos **60 operações reais fechadas** na Fase 1 (e mínimo de 60 dias corridos de operação). |
| **2** | **Crescimento Patrimonial Real** | A Carteira Tática em USDT está acima do seu valor inicial (lucro líquido acumulado positivo após todas as taxas de BNB) e com *Profit Factor* real $\ge 1,15$. |
| **3** | **Expectância Real Positiva** | Média realizada por operação $\bar{R}_{\text{live}} \ge +0,10R$, com *Max Drawdown* real registrado menor que $10\%$. |
| **4** | **Zero Falhas Graves de Execução** | Zero casos de posições órfãs não recuperadas pelo reconciliador e slippage médio real dentro do limite de estresse ($s_{\text{real}} \le 0,10\%$). |
| **5** | **Prova Ao Vivo da IA Silenciosa** | Nos 60+ trades reais gravados no SQLite, o grupo que a IA marcou silenciosamente como **Nota B/A+** teve média $\bar{R}$ superior ao grupo marcado como **Nota C**, com erro de calibração $\text{ECE} \le 0,05$ (*Platt Scaling*). |

**O que muda na Fase 2 (Em duas subetapas de segurança):**

* **Fase 2A (Primeiros 30 trades da Fase 2):** Liga o **Cofre de Lucros (`profit_splitter.enabled: true`)** com regra de *High-Water Mark* (transferindo 30% dos novos lucros acima do topo histórico para a carteira `FUNDING` via `MAIN_FUNDING` a cada 10 USDT) e liga o **Filtro Defensivo da IA (`ml_judge.use_ml_filter: true`)** apenas para **bloquear os sinais Nota C** (0% de lote), mantendo os sinais Nota B e Nota A+ ainda com risco conservador de **1,0%**.
* **Fase 2B (Após +30 trades mantendo $\bar{R} \gt +0,15R$ na Fase 2A):** Libera o escalonamento de lote da **Nota A+** para arriscar **1,5% da banca** (e até **2,0%** após mais 30 trades, desde que o CVaR 99% empírico da carteira não ultrapasse 5,0% de perda em um único evento de cauda).

---

### 4.4. Transição Fase 2 $\rightarrow$ Fase 3: Quando Ligar o Clima Lateral (`sideways_pingpong`) ou o Breakeven?

A Fase 3 adiciona novas armas ao robô. Cada peça extra tem seu próprio cadeado individual e **só** pode ser ligada se cumprir seu requisito:

1. **Para ligar `sideways_pingpong` (Operar Mercado Lateral):**
* O robô já deve estar na Fase 2 com pelo menos **100 trades reais acumulados** no histórico total.
* A estratégia `sideways_pingpong` isolada deve ter passado por todos os critérios do **Gate 3** no Colab (mínimo de 150 trades OOS, filtro $\text{RR} \ge 1,5$ líquido de custos, *Profit Factor* $\ge 1,20$ e $\text{DSR} \ge 0,95$).
* A estratégia deve ter rodado por pelo menos **30 operações virtuais em modo *Shadow Live* ao vivo** durante períodos reais de lateralização, apresentando expectância líquida simulada $\bar{R}_{\text{shadow}} \ge +0,10R$.


2. **Para ligar o Breakeven em $+1,2R$ (`breakeven_enabled: true`):**
* Só pode ser ligado se o **Degrau E** do Bloco 3 (simulado com velas de 1 minuto) tiver provado matematicamente que ligar o breakeven aumenta o *Calmar Ratio* e o retorno líquido em comparação ao Degrau C/D sem breakeven, **e** o módulo WebSocket assíncrono (`bookTicker`) tiver rodado por 14 dias sem desconexões não tratadas.


3. **Sobre `bear_capitulation_snap` (Tempestade):**
* Permanece **permanentemente desligada por padrão** (defesa 100% em USDT no regime `BEAR`), a menos que um estudo dedicado futuro com dados ponto-no-tempo (incluindo moedas que sofreram colapso) comprove vantagem estatística líquida.



---

## 5. Protocolo de Regressão Automática (Downgrade) e Disjuntores de Emergência (Kill-Switches)

Um sistema profissional não precisa apenas saber quando acelerar; ele precisa saber quando **reduzir a marcha sozinho** se o mercado mudar de comportamento, e quando **puxar o freio de mão** e chamar você no Telegram.

### 5.1. Tabela de Rebaixamento Automático de Fase (*Auto-Downgrade*)

O módulo `shadow_manager.py` monitora a saúde estatística da operação ao final de cada trade fechado:

| Condição Detectada em Produção | Ação Automática do Robô | Como Recuperar o Status Anterior |
| --- | --- | --- |
| **Drawdown Tático atinge $-4,0\%$** abaixo do Topo Histórico (*High-Water Mark*) | **Ativa `DRAWDOWN_RISK_SCALING`:** Corta o risco de todas as novas operações pela metade (ex.: de $1,0\%$ para $0,5\%$) e pausa transferências do Cofre. | Automático assim que o saldo da Carteira Tática voltar a superar o Topo Histórico. |
| **Na Fase 2:** A média móvel dos últimos **30 trades reais** fica negativa ($\bar{R}_{30} \lt 0,0R$) ou a IA apresenta descalibração ($\text{ECE}_{30} \gt 0,08$) | **Downgrade Automático para Fase 1:** Desliga a alocação extra da Nota A+ (volta tudo para risco base de $0,5\%$ a $1,0\%$) e notifica no Telegram. | Exige nova sequência de 30 trades com $\bar{R}_{30} \ge +0,10R$ e auditoria do modelo no Colab. |
| **Na Fase 3:** A estratégia `sideways_pingpong` acumula **$-3,0R$ líquidos** nos últimos 20 trades laterais | **Desarme Isolado de Clima:** Desliga `sideways_pingpong` (`enabled: false`), voltando a ficar 100% em USDT no clima `SIDEWAYS`, sem interromper o clima `BULL`. | Reavaliação completa no Bloco 3. |

### 5.2. Disjuntores de Parada Total (*Hard Kill-Switches* — Estado `EMERGENCY_HALT`)

O robô interrompe imediatamente **qualquer nova compra** (mantendo a vigilância e proteção das posições que já estiverem abertas) e envia alerta sonoro prioritário no Telegram se ocorrer qualquer um dos 4 eventos abaixo:

1. **Limite Máximo de perda da Carteira Tática (*Hard Stop Patrimonial* de $-12,0\%$):** Se a Carteira Tática cair $12\%$ abaixo do topo histórico (ou abaixo do capital inicial), o robô entra em `HALT` para revisão humana, impedindo qualquer risco de destruição da banca.
2. **Esgotamento da Reserva de Taxas em BNB:** Se o saldo livre de `BNB` cair abaixo do equivalente a **$2,00 USDT**, o robô bloqueia novas entradas até que você recarregue o BNB, garantindo que nenhuma ordem OCO falhe por comissão descontada no ativo base.
3. **Falha Repetida de Reconciliação ou API:** Se `state_reconciler.py` encontrar uma divergência de saldo na corretora que não consiga curar em 3 tentativas consecutivas, ou se a Binance retornar erro de permissão/assinatura (`-1002`, `-1022`, `-2015`).
4. **Anomalia de Frequência (*Runaway Loop Protection*):** Se o sistema tentar disparar mais do que `max_daily_trades_cap` (6 ordens de entrada em 24 horas).

### 5.3. Regra de Ouro da Supervisão Humana (O Que Você Deve e Não Deve Fazer)

* **Proibido intervir por tédio:** Em meses de queda ou mercado travado (`BEAR` ou `TRANSITION`), é normal e esperado que o robô passe **10 a 20 dias seguidos sem fazer nenhuma compra**, mantendo 100% do seu dinheiro protegido em dólar (USDT). **Não diminua os limiares do `strategies.yaml` para "forçar o robô a operar"** — ficar fora de mercados ruins é exatamente a fonte da preservação de capital do Chameleon.
* **Rito de Manutenção Mensal (15 minutos no dia 1º de cada mês):**
1. Verificar na mensagem mensal do Telegram se a reserva de `BNB` está acima de $5,00 USDT.
2. Verificar se o backup automático do SQLite foi salvo corretamente e se o espaço em disco da VPS está abaixo de 70%.
3. Se houver saldo de lucro acumulado na carteira `FUNDING` (na Fase 2), decidir se deseja mantê-lo em dólar ou converter manualmente para `XRP` para somar à sua Carteira Core.

---

## 6. Registro Histórico de Triagem Homologada (Blocos 1 a 6) e Itens Cosméticos Descartados

Todos os itens abaixo foram auditados, implementados e validados por testes automatizados (`pytest`) e/ou execução empírica no Google Colab durante a conclusão dos **Blocos 1 a 6**, tendo sido migrados do [`docs/todo.md`](todo.md) para esta documentação permanente.

### 6.1. Itens de Categoria 1 e Categoria 2 Concluídos nos Blocos 1 a 6

| Categoria | Item Validado e Homologado | Quem Apontou | Bloco / Módulo Implementado | Impacto Prático Comprovado |
| --- | --- | --- | --- | --- |
| **1 — Catastrófico** | Arredondamento `close_time` (+1ms) no `merge_asof` | DeepSeek | Bloco 1 (`src/data/candle_collector.py`) | Evita perder a vela de 4h recém-fechada e trata truncamento de manutenção (`2023-03-24`) |
| **1 — Catastrófico** | Tratamento de `NaN` e divisão por zero no `CHOP` | ChatGPT | Bloco 2 (`src/features/indicators.py`) | Impede que o robô trave em moeda parada ($\text{High} == \text{Low} \rightarrow \text{NaN} \rightarrow \text{TRANSITION}$) |
| **1 — Catastrófico** | Backup SQLite via `sqlite3.backup()` nativo | ChatGPT | Bloco 1 (`src/core/database.py`) | Evita arquivo de backup corrompido durante escrita WAL |
| **1 — Catastrófico** | Cálculo empírico de $\mathbb{E}[R]$ com Time-Stop no Gate 3 | ChatGPT e DeepSeek | Bloco 3 (`src/backtest/causal_simulator.py` e Colab) | Mede o lucro líquido real de quem sai por tempo (`24h`/`72h`) |
| **1 — Catastrófico** | Trava de 20 trials (`trials_registry.csv`) e Holdout 6m | Gemini e DeepSeek | Bloco 3 (`src/backtest/ablation_and_dsr.py` e Colab) | Impede falso lucro por tentativa e erro ($\text{DSR} = 0,96 \ge 0,95$ em $K=9/20$) |
| **1 — Catastrófico** | `WeightGovernor` (`CRITICAL > HIGH > LOW`) e `critical_section()` (**C1-B**) | Auditoria de Integração | Bloco 1 (`src/core/binance_client.py`) | Impede que `429`/`418` do Radar deixe posição sem OCO |
| **1 — Catastrófico** | Segregação `execution_universe` vs. Radar e fronteira `StrategySignal` (**C1-C**) | Auditoria de Integração | Bloco 1 (`src/core/config.py`) | Impede liquidar reserva `BNBUSDT` ou criar órfãos fora da cesta |
| **1 — Catastrófico** | Filtro exato do erro `-2010` e Stop Pendurado | ChatGPT e DeepSeek | Bloco 5 (`src/execution/oco_order_manager.py`) | Evita vender errado ou ficar sem stop (`FALLBACK_MARKET_2010` e `STOP_WATCHDOG`) |
| **1 — Catastrófico** | Saldo excedente não rastreado entra em `HALT_REVIEW` (nunca vende posição manual) (**C1-D**) | Auditoria de Integração | Bloco 5 (`src/core/state_reconciler.py`) | Evita liquidar posição manual do operador na mesma conta |
| **1 — Catastrófico** | Escritas pós-fill *best-effort* nunca interrompem criação da OCO (**C1-E**) | Auditoria de Integração | Blocos 1 e 5 (`database.py` e `oco_order_manager.py`) | Garante que falha de SQLite pós-fill não deixe posição nua |
| **1 — Catastrófico** | Orquestração de Kill-Switches e Manutenção API (`/sapi/v1/system/status`) | Gemini e DeepSeek | Bloco 6 (`src/main.py`) | Bloqueia novas entradas em manutenção da corretora ou `EMERGENCY_HALT` |
| **1 — Catastrófico** | Banda efetiva simétrica no `/api/v3/depth` (`band_eff` e `obi_valid`) (**C1-A**) | Auditoria de Integração | Bloco 6 — Radar L4 (`src/radar/orderbook_inspector.py`) | Evita OBI e Whale Walls inválidos em livros profundos/truncados (`BTC`/`ETH`) |
| **2 — Refinamento** | Mock de `/sapi/*` na Testnet e Permissão `Universal Transfer` | ChatGPT e DeepSeek | Blocos 1 e 4 (`binance_client.py` / `profit_splitter.py`) | Testnet não aceita `/sapi/*` (evita erro 404 na transferência `MAIN_FUNDING`) |
| **2 — Refinamento** | *High-Water Mark* (Topo Histórico) no Cofre de Lucros | ChatGPT | Bloco 4 (`src/risk/profit_splitter.py`) | Só separa 30% quando superar prejuízos anteriores acima do HWM |
| **2 — Refinamento** | Teto de ordem em % da banca (`55%`) e BNB em Dólar (`$5`) | ChatGPT e DeepSeek | Bloco 4 (`strategies.yaml`, `position_sizer.py`, `shadow_manager.py`) | Protege bancas pequenas desde o dia 1 ($\min(500\text{ USDT}, 55\%)$ e alerta BNB $< \$5$) |
| **2 — Refinamento** | Direção de arredondamento do `tickSize` na OCO | ChatGPT | Blocos 1, 4 e 5 (`binance_client.py` / `oco_order_manager.py`) | Evita que `stopPrice` (`UP`) cole no `stopLimit` (`DOWN`) |
| **2 — Refinamento** | Trocar *Isotonic* por *Platt Scaling* para menos de 2.000 trades | DeepSeek | Bloco 4 (`src/features/ml_judge.py`) | Evita que a IA decore dados pequenos ($\text{ECE} \le 0,05$) |
| **2 — Refinamento** | Mínimo de 150 trades, Profit Factor maior que 1,20 e Holdout | ChatGPT | Bloco 3 (`ablation_and_dsr.py` e Colab `K9.3`) | Homologado com `n=282`, $\text{PF}_{\text{base}}=1,43$, $\text{PF}_{\text{pess}}=1,29$ e Holdout `+5,80R` |
| **2 — Refinamento** | Salvar Colunas 8 (`trades`) e 9 (`taker_buy_vol`) da vela | Gemini (Trader Ref) | Bloco 1 (`src/data/candle_collector.py`) | Captura Order Flow grátis da Binance |
| **2 — Refinamento** | Calcular *Order Flow Delta* e *Liquidity Sweep* em 1h | Gemini (Trader Ref) | Bloco 2 (`src/features/order_flow.py` e `price_action.py`) | Mede agressão compradora (`TBR`, `of_delta`) e absorção |
| **2 — Refinamento** | Testar ablação Com vs. Sem *Order Flow* no Colab | Gemini (Trader Ref) | Bloco 3 (`notebooks/02_ablation_and_dsr_sim.ipynb`) | Validou a combinação Donchian 12h + `RVOL >= 1.30` + `TBR >= 0.52` |
| **2 — Refinamento** | IA em *Modo Observador Silencioso* na Fase 1 (60 trades) | Gemini | Bloco 4 (`src/features/ml_judge.py` e `position_sizer.py`) | Grava `ml_grade_silent` e `ml_p_cal_silent` sem bloquear ordens na Fase 1 |
| **2 — Refinamento** | `outcome_resolver.py` compartilhado (pior caso intra-vela) (**C2-3**) | Auditoria de Integração | Bloco 5 (`src/auditing/outcome_resolver.py` e `paper_executor.py`) | Evita divergência entre Auditor Virtual e Shadow Executor (`SAME_CANDLE_STOP`) |
| **2 — Refinamento** | Modelo de custo único lido de `strategies.yaml` (**C2-4**) | Auditoria de Integração | Blocos 1, 3, 4, 5 e 6 (`config.py` / `confluence_scorer.py`) | Estatísticas do Radar e Chameleon comparáveis ($0,15\%$ + $0,06\%$) |
| **2 — Refinamento** | Persistir `quote_volume` (col 7) e `taker_buy_quote_volume` (col 10) (**C2-5**) | Auditoria de Integração | Bloco 1 (`candles` em `database.py` / `candle_collector.py`) | Delta financeiro em USDT exato |
| **2 — Refinamento** | Guardas de zero no `TBR` e `RVOL` (`V=0` ou mediana=0 $\rightarrow$ `NaN`) (**C2-6**) | Auditoria de Integração | Bloco 2 (`order_flow.py` / `indicators.py`) | Evita `inf` passar em limiares numéricos |
| **2 — Refinamento** | Gravar apenas agregados/faixas do livro (~1 KB) sem JSON bruto (**C2-8**) | Auditoria de Integração | Blocos 1 e 6 (`orderbook_snapshots` / `orderbook_inspector.py`) | Mantém backup abaixo de 50 MB (limite Telegram) |
| **2 — Refinamento** | Faixas de 0,1% do livro ancoradas em `mid_0` (**C2-1**) | Auditoria de Integração | Bloco 6 (`src/radar/orderbook_inspector.py`) | Evita deslizamento de faixas entre as 3 fotos |
| **2 — Refinamento** | Auditor inicia em `t+1` usando `ask` do snapshot 0 (**C2-2**) | Auditoria de Integração | Bloco 6 (`src/auditing/virtual_auditor.py`) | Entrada realista alinhada ao atraso do alerta |
| **2 — Refinamento** | Horizonte por tempo (`*_end_ms`) no Auditor Virtual (**C2-7**) | Auditoria de Integração | Bloco 6 (`src/auditing/virtual_auditor.py`) | Imunidade a janelas de manutenção da Binance (`INVALID_GAP`) |
| **2 — Refinamento** | Execução do Chameleon antes da L4; 3 fotos intercaladas (**C2-9**) | Auditoria de Integração | Bloco 6 (`src/main.py` / `orderbook_inspector.py`) | Não adiciona 10–15 s de latência ao slippage do Gate 6B |

### 6.2. Checklist de Construção Homologado (Blocos 1 a 6)

- **Bloco 1 ([Especificação do Bloco 1](block-1-spec.md)):** `config/strategies.yaml` (v6.0) + `config/analyst.yaml` (v1.1), `src/core/config.py`, `src/core/database.py` (SQLite WAL, `write_tx()`, schema v1, `sqlite3.backup()`), `src/core/binance_client.py` (Ed25519, `WeightGovernor`, `MonotonicClock`, `Decimal`), `src/data/candle_collector.py` e **Gate 1 Unificado (`100% PASS`)**.
- **Bloco 2 ([Especificação do Bloco 2](block-2-spec.md)):** `src/features/indicators.py`, `src/features/order_flow.py`, `src/features/price_action.py`, `src/features/feature_store.py`, `src/regimes/regime_detector.py` (MECE 4 estados) e **Gate 2 Unificado (`100% PASS`)**.
- **Bloco 3 ([Especificação do Bloco 3](block-3-spec.md)):** `src/strategies/*`, `src/backtest/causal_simulator.py`, `src/backtest/ablation_and_dsr.py`, Notebooks `01`, `02` e `04`, e **Gate 3 de Engenharia e Empírico no Colab (`K9.3`, `PASS = True` na tentativa $K = 9 / 20$)**.
- **Bloco 4 ([Especificação do Bloco 4](block-4-spec.md)):** `src/risk/position_sizer.py`, `src/risk/shadow_manager.py`, `src/risk/profit_splitter.py`, `src/features/ml_judge.py`, `notebooks/03_tail_stress_cppi_cusum.ipynb` e **Gate 4 Unificado (`100% PASS`)**.
- **Bloco 5 ([Especificação do Bloco 5](block-5-spec.md)):** `src/auditing/outcome_resolver.py` (`C2-3`), `src/execution/executor_protocol.py`, `src/execution/paper_executor.py`, `src/execution/oco_order_manager.py` (`-1007`, `-2010`, `STOP_WATCHDOG`, Time-Stop de 4 passos, `C1-E`), `src/core/state_reconciler.py` (cura de OCO parcial e `HALT_REVIEW` `C1-D`) e **Gate 5 Unificado (`100% PASS`)**.
- **Bloco 6 ([Especificação do Bloco 6](block-6-spec.md)):** `src/radar/*` (`structure_ema.py`, `orderbook_inspector.py` `C1-A`/`C2-1`/`C2-8`/`C2-9`, `confluence_scorer.py`), `src/auditing/virtual_auditor.py` (`C2-2`/`C2-7`), `src/notifications/telegram.py` (outbox persistente), `src/main.py` (`QuantOrchestrator`), `Dockerfile`, `docker-compose.yml`, `deploy/quant-chameleon.service` e **Gate 6 Unitário (`100% PASS`)**.

### 6.3. Itens de Categoria 3 (Cosméticos — Descartados Permanentemente)

| Categoria | Sugestão Descartada | Quem Apontou | Motivo Técnico do Descarte |
| --- | --- | --- | --- |
| **3 — Cosmético** | Trocar o texto "Sobrevivência Determinística" no README | ChatGPT | Muda zero linhas de código |
| **3 — Cosmético** | Renomear `CPPI_DRAWDOWN_CONTROL` para `DRAWDOWN_SCALING` | ChatGPT | Muda zero linhas de matemática |
| **3 — Cosmético** | Reclamar que a Fase 1 é "simples demais / MVP" | DeepSeek | Contradição: simplificar a Fase 1 era a meta! |
| **3 — Cosmético** | Decidir se o Cofre em `FUNDING` guarda USDT ou XRP | DeepSeek | Guardar USDT já protege o lucro sem taxa extra |
| **3 — Cosmético** | Testar se `SIDEWAYS` deve vir antes de `BEAR` | DeepSeek | Ficar em dólar na queda (`BEAR` 1º) é mais seguro |
| **3 — Cosmético** | Ler *Order Book Level 2* ao vivo como gatilho de execução | Gemini (Descartado) | Sem backtest grátis; permitido apenas no caminho analítico do Radar (`affects_execution: false`, ADR-004) |


