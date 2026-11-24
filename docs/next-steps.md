# Próximos Passos: Backlog Priorizado e Pré-mortem da Fase 1

Este documento consolida o planejamento técnico pós-Bloco 6 e a análise cética de riscos antes da virada para capital real na **Fase 1** (`MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=PRODUCTION`).

!!! warning "Restrição inegociável de governança"
    Nenhum item deste documento propõe alterar parâmetros da estratégia congelada **`K9.3`** (`bull_trend_pullback`), editar `config/strategies.yaml` ou reabrir o Holdout do Bloco 3 ([Metodologia e Roadmap §2](methodology-and-roadmap.md)). Toda alteração de flags de fase (`profit_splitter`, `ml_judge`, `conviction_sizing`, `breakeven`, `sideways_pingpong`) permanece travada em `src/core/config.py:495-595` até que o respectivo Gate seja aprovado formalmente e receba novo `APPROVED_STRATEGIES_SHA256`.

---

## 1. Backlog Priorizado

A ordenação abaixo segue estritamente **o que desbloqueia o próximo gate mais cedo**, respeitando o estado atual do projeto (**Gate 6A** em curso na Binance Spot Testnet → **Gate 6B** em `SHADOW_CHAMELEON` → **Fase 1** → **Gate F1→F2** → **Gate F2→F3**).

!!! info "Regra operacional de deploy"
    Nenhum código deve ser implantado nem o container reiniciado **durante** o Gate 6A em andamento (`AGENTS.md`). Os itens **B1–B7** já foram implementados e validados em suíte unitária (`pytest`: 110 passed) sem alterar `config/strategies.yaml`, prontos para entrar na transição controlada de gate; **B8–B10** permanecem pendentes de estudos empíricos offline.

### Resumo Executivo

| Ordem | ID | Status | Item | Gate / Fase | Esforço | Desbloqueio principal |
| --- | --- | --- | --- | --- | --- | --- |
| **1** | **B1** | **Implementado** | Telemetria de equity/BNB/regime no SQLite, alertas preventivos (`< 5,00 USDT` BNB e taxa `> 0,10%`) e rotação de backups locais | `6A` / `6B` | `P` | Elimina `UNKNOWN` em `tools/gate_report.py` e previne disco cheio / esgotamento silencioso de BNB |
| **2** | **B2** | **Implementado** | Contabilidade líquida de `fees_usdt_equiv` (LIVE) e slippage ponderado por profundidade do livro (`SHADOW_CHAMELEON`) | `6B` (pré-`F1`) | `M` | Validação fiel do limiar de slippage `<= 0,08%` no Gate 6B e PnL/PF reais líquidos na Fase 1 |
| **3** | **B3** | **Implementado** | Observabilidade ativa: Scorecard semanal do Radar, alerta de disco e isolamento de `-1121` no batch de `exchangeInfo` | `6B` | `P` | 30 dias limpos no Gate 6B sem um par Radar deslistado derrubar os 7 pares do Chameleon |
| **4** | **B4** | **Implementado** (CLI + loader) | Treinamento offline (Treino/Validação) e carga de artefato calibrado do `MLJudge` em modo observador silencioso | `6B` → `F1->F2` | `M` | Evita desperdiçar os 60 trades reais da Fase 1 com `PlattCalibrator` não treinado (`a=-1, b=0`); falta apenas rodar o CLI sobre candles históricos |
| **5** | **B5** | **Implementado** | Pipeline de auditoria dos dados do Radar (`virtual_signals` + `orderbook_snapshots`) para validar L4 e o Juiz de IA | `6B` → `F1->F2` | `M` | Testa empiricamente se L4 adiciona edge e se enriquece features do `MLJudge` sem violar ADR-004 |
| **6** | **B6** | **Implementado** | Watchdog sub-horário (`<= 30 s`) para `STOP_LOSS_LIMIT` pendurado durante posição `PROTECTED` | `6B` / `F1->F2` | `M` | Reduz de ~59 min para `<= 20 s` (padrão) a exposição a flash-crash que pule o buffer de `2,5%` |
| **7** | **B7** | **Implementado** | Integração runtime da Fase 2 (`RunMode` Fase 2, `ProfitSplitter`, filtro ativo Nota C, escada `1,5% -> 2,0%` + `CVaR 99%`, Auto-Downgrade 30 trades) | `F1->F2` | `G` | Habilita a virada segura para Fase 2A/2B assim que os 5 requisitos de `F1->F2` derem `PASS` |
| **8** | **B8** | Pendente (pesquisa) | Avaliação do Degrau E (Breakeven `+1,2R` em velas `1m` sobre `K9.3`) e módulo WebSocket assíncrono `bookTicker` (soak test 14 dias) | `F2->F3` | `G` | Cumpre o duplo cadeado (estatístico + 14 dias de estabilidade) antes de cogitar `breakeven_enabled` |
| **9** | **B9** | Pendente (pesquisa) | Homologação de `sideways_pingpong` (Gate 3 + 30 shadow trades) e roteamento multi-estratégia por `net_expected_r` | `F2->F3` | `G` | Desbloqueia operação em clima `SIDEWAYS` após `>= 100` trades reais na Fase 2 |
| **10** | **B10** | Pendente (pesquisa) | Estudo ponto-no-tempo de `bear_capitulation_snap` incluindo ativos deslistados/colapsados | `F2->F3` | `G` | Comprova ou arquiva definitivamente via ADR a quarentena do regime `BEAR` |

---

### Detalhamento dos Itens

#### B1 — Persistência de telemetria operacional (`system_state` / `regime_history`), alertas preventivos de BNB/taxa e rotação de backups locais *(Implementado)*

* **Implementação entregue:**
  * `src/main.py:803-806,844-872,1205-1219` persiste `current_equity_usdt`, `initial_tactical_equity_usdt`, `bnb_free_usdt_equiv` e grava snapshots em `regime_history` via `Database.upsert_regime_history()` (`src/core/database.py:725-754`), além de enfileirar `BNB_LOW_WARNING` (`< 5,00 USDT`) e `COMMISSION_WARNING` (`taker > 0,10%`) com rate-limit diário.
  * `src/execution/paper_executor.py` atualiza `shadow_virtual_equity_usdt` e `initial_shadow_equity_usdt` ao fechar cada `shadow_trade`.
  * `Database.prune_old_backups()` (`src/core/database.py:782-812`, controlado por `BACKUP_RETENTION_DAYS=14` em `src/core/config.py`) rotaciona backups locais em `BACKUP_DIR`.
  * Coberto por `tests/test_b1_telemetry_and_retention.py` (incluindo `tools/gate_report.py` resolvendo KS-1 e KS-2 como `PASS`).
* **Gate / Fase:** `6A` / `6B`.
* **Esforço:** `P`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/main.py`, `src/execution/paper_executor.py`, `src/core/database.py`, `src/risk/shadow_manager.py`, `tools/gate_report.py`.

---

#### B2 — Contabilidade líquida de taxas (`fees_usdt_equiv`) e medição real de slippage por profundidade no Gate 6B *(Implementado)*

* **Implementação entregue:**
  * `OcoOrderManager` (`src/execution/oco_order_manager.py:91,431,823,1395,1991`) e `StateReconciler` (`src/core/state_reconciler.py:1007-1020`) convertem taxas em `BNB`/`USDT`/ativo base para `fees_usdt_equiv` (com resolução zero-rede entre compra e OCO para preservar C1-B/C1-E) e gravam `realized_r` líquido de taxas (mantendo `realized_pnl_usdt` bruto conforme convenção de `tools/gate_report.py:724-725`).
  * `PaperExecutor` (`src/execution/paper_executor.py:17,111-183`) consulta `get_depth(symbol, limit=20)` via `compute_depth_vwap_ask()`, grava `depth_vwap_ask` e `book_impact_bps` nas colunas aditivas de `shadow_trades` (`src/core/database.py:170-171,454-456`, mantendo `SCHEMA_VERSION = 1`), com fallback seguro para top-of-book.
  * Coberto por `tests/test_b2_fees_and_depth_slippage.py`.
* **Gate / Fase:** `6B` (critério `slippage <= 0,08%`) e pré-requisito para `F1->F2` (Req. 2 e Req. 3).
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/execution/oco_order_manager.py`, `src/core/state_reconciler.py`, `src/execution/paper_executor.py`, `src/core/binance_client.py`, `src/core/database.py`, `tools/gate_report.py`.

---

#### B3 — Observabilidade ativa: Scorecard semanal do Radar, alerta de disco da VPS e resiliência do batch `exchangeInfo` *(Implementado)*

* **Implementação entregue:**
  * `QuantOrchestrator._maybe_emit_weekly_radar_scorecard()` (`src/main.py:510-535`) agenda envio semanal idempotente via `last_weekly_scorecard_ms` respeitando `analyst.virtual_auditor.send_weekly_scorecard_telegram`.
  * `QuantOrchestrator._check_disk_usage()` (`src/main.py:537-566`) monitora o disco em `db_path.parent`, persiste `disk_usage_pct` e emite `DISK_USAGE_WARNING` (`>= 70%`).
  * `ExchangeInfoCache` (`src/core/binance_client.py:647-705`, `src/main.py:1134-1145,1500-1505`) prioriza `execution_universe`, isola símbolos inválidos `-1121` do Radar em `last_invalid_symbols` com alerta `EXCHANGE_INFO_WARNING` e falha fechado apenas se um símbolo de execução for inválido.
  * Coberto por `tests/test_b3_observability_and_exchange_info.py`.
* **Gate / Fase:** `6B` (30 dias ininterruptos com os 15 símbolos do Radar em produção pública).
* **Esforço:** `P`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/main.py`, `src/auditing/virtual_auditor.py`, `src/core/binance_client.py`, `src/notifications/telegram.py`, `config/analyst.yaml`.

---

#### B4 — Treinamento offline (Treino/Validação) e carga de artefato calibrado do `MLJudge` para o observador silencioso da Fase 1 *(Implementado — CLI + loader prontos)*

* **Implementação entregue:**
  * `src/features/ml_judge.py:55,321,463-500` implementa `MLJudgeArtifact` (verificação SHA-256 canônica), treinador determinístico puro `numpy` `fit_l2_logistic_and_platt()` (L2 logística ponderada + calibração Platt na validação + quantis `q33`/`q70`, sem novas dependências e compatível com `mem_limit: 512m`), `MLJudge.from_artifact()` e `MLJudge.score_features()`.
  * `tools/train_ml_judge.py:71-265` treina exclusivamente em `URI mode=ro` sobre Treino/Validação (excluindo estritamente os 15% finais do Holdout via `split_temporal_vaults`) e exporta artefato JSON pronto para `ML_JUDGE_ARTIFACT_PATH` (`.env.example`, `src/core/config.py`).
  * `src/main.py:150-167,899-908` carrega o artefato se configurado, extrai features via `extract_stationary_features()`, grava `ml_p_cal_silent` também em `shadow_trades` (`src/core/database.py:185,456`, `src/execution/paper_executor.py:162,182`), mantendo `use_ml_filter = False` na Fase 1. **Ação operacional pendente antes do dia 1 da Fase 1:** rodar `tools/train_ml_judge.py` sobre banco com histórico suficiente de candles e apontar `ML_JUDGE_ARTIFACT_PATH` no `.env`.
  * Coberto por `tests/test_b4_ml_judge_artifact.py`.
* **Gate / Fase:** Preparar durante o `6B` (antes do dia 1 da Fase 1) para desbloquear `F1->F2` (Requisito 5).
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/features/ml_judge.py`, `tools/train_ml_judge.py`, `src/main.py`, `src/risk/position_sizer.py`, `src/core/database.py`, `tools/gate_report.py`.

---

#### B5 — Uso dos dados do Radar (`virtual_signals`, `orderbook_snapshots`, `orderbook_walls`) para auditar L4 e validar o Juiz de IA *(Implementado)*

* **Implementação entregue:**
  * `tools/radar_ai_audit.py` (`_open_readonly`, `generate_radar_ai_audit`, `_bootstrap_diff_ci95`, `_offline_ml_ablation` em `L31,89,110,234`) implementa auditoria estritamente somente-leitura (`URI mode=ro` + `PRAGMA query_only=ON`, ADR-002): ablação de Camada 4 (`L1–L3` vs. `L1–L4`, `ob_valid`, `ob_ask_wall_before_t2`, quartis de OBI) com IC 95% via bootstrap, convergência Chameleon × Radar (`shadow_trades`/`trades` × `virtual_signals`), ablação offline OHLCV vs. OHLCV+L4 para o `MLJudge` (somente quando `n >= 150` com `ob_valid = 1`), e preservação estrita de `affects_execution: False` (ADR-004).
  * Coberto por `tests/test_b5_radar_ai_audit.py` (incluindo teste em diretório `chmod 0555`).
* **Gate / Fase:** Coleta contínua durante `6B` e Fase 1; decisão científica antes de `F1->F2` (e eventual ADR na Fase 2B/3).
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `tools/radar_ai_audit.py`, `src/core/database.py`, `src/auditing/virtual_auditor.py`, `src/radar/orderbook_inspector.py`, `src/features/ml_judge.py`, `docs/decisions.md`.

---

#### B6 — Watchdog sub-horário para Stop Pendurado (`STOP_LOSS_LIMIT`) *(Implementado)*

* **Implementação entregue:**
  * `QuantOrchestrator.run_subhourly_watchdog_tick()` (`src/main.py:1363-1444`) e `wait_for_next_cycle_with_watchdog()` (`src/main.py:1446-1483`, ligado em `main()` em `L1533-1536` com `WATCHDOG_POLL_INTERVAL_SECONDS=20.0` em `src/core/config.py` / `.env.example`) substituem o `sleep` monolítico de até 1h por polling na thread única (respeitando ADR-002 e resposta rápida a `SIGTERM`). Faz zero chamadas de rede quando flat e liquida stop pendurado ou Time-Stop vencido em `<= 20 s` (padrão) quando há posição `PROTECTED`.
  * Coberto por `tests/test_b6_subhourly_watchdog.py`.
* **Gate / Fase:** Pronto para `6A`/`6B` e proteção ativa na Fase 1.
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/main.py`, `src/data/candle_collector.py`, `src/execution/oco_order_manager.py`, `src/core/binance_client.py`, `src/core/database.py`.

---

#### B7 — Integração runtime da Fase 2 (`RunMode` Fase 2, `ProfitSplitter`, filtro ativo Nota C, escada `1,5% -> 2,0%` + `CVaR 99%` e Auto-Downgrade de 30 trades) *(Implementado — ativação travada até Gate `F1->F2`)*

* **Implementação entregue:**
  * `RunMode.LIVE_CHAMELEON_PHASE_2` e `PHASE_2_LIVE_CONFIRMATION = "PHASE_2_F1_TO_F2_PASSED"` em `src/core/config.py:23,39,547-595`: exige `ML_JUDGE_ARTIFACT_PATH` quando `use_ml_filter=true`, proíbe flags de Fase 3 (`breakeven_enabled`, `sideways_pingpong`, `bear_capitulation_snap`), exige confirmação dedicada + `APPROVED_STRATEGIES_SHA256` em `PRODUCTION` e mantém `LIVE_CHAMELEON_PHASE_1` 100% intocado.
  * `QuantOrchestrator` (`src/main.py:169-178,223-227,237-397,911-935`) conecta: (1) filtro ativo Nota `C` apenas em `LIVE_CHAMELEON_PHASE_2` quando `use_ml_filter=true` e `ece_out_of_sample <= max_allowed_ece`; (2) `_process_newly_closed_live_trades()` idempotente por `last_post_close_processed_trade_id` chamando `ShadowManager.evaluate_phase_downgrade()` (alerta `PHASE_DOWNGRADE` em `mean_R_30 < 0` ou `ECE_30 > 0,08`) e `ProfitSplitter.on_trade_closed()` (alerta `PROFIT_SPLIT` sem duplicar transferências em restart); (3) `_compute_phase2_sizing_guards()` calculando `CVaR 99%` empírico e mantendo Estágio 1 (`1,5%`) até `>= 30` trades fechados na Fase 2 com `mean(R_30) > +0,15R` sem downgrade (antes de liberar Estágio 2 `2,0%`).
  * `config/strategies.yaml` permanece 100% congelado para Fase 1; coberto por `tests/test_b7_phase2_runtime.py`.
* **Gate / Fase:** `F1->F2` (ativação somente após `tools/gate_report.py` retornar `PASS` nos 5 requisitos de `phase_1_to_2`).
* **Esforço:** `G`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/core/config.py`, `src/main.py`, `src/risk/profit_splitter.py`, `src/risk/position_sizer.py`, `src/risk/shadow_manager.py`, `docs/methodology-and-roadmap.md`.

---

#### B8 — Estudo do Degrau E (Breakeven `+1,2R` em `1m`) e Módulo WebSocket assíncrono `bookTicker`

* **Estado atual verificado:**
  * `config/strategies.yaml:45-47` mantém `breakeven_enabled: false`, `breakeven_trigger_r: 1.2` e `require_realtime_websocket_for_be: true`.
  * Não existe cliente WebSocket no código atual (`src/core/binance_client.py` é 100% REST síncrono).
  * Nos estudos preliminares do Bloco 3, o Breakeven piorou ou empatou mal em variantes anteriores (`K1` em `docs/block-3-spec.md:291`; `K5.5` em `docs/block-3-spec.md:329`), e ainda não há ablação publicada em `1m` especificamente sobre `K9.3`.
  * `docs/methodology-and-roadmap.md:139-140` exige **duas provas**: (1) ganho matemático em Calmar Ratio e retorno líquido sobre `K9.3` em velas `1m`, e (2) WebSocket `bookTicker` rodando **14 dias sem desconexões não tratadas**.
* **Objetivo (em duas etapas estritamente sequenciais — só construir etapa 2 se etapa 1 passar!):**
  1. **Etapa 1 (Pesquisa offline em `1m`, zero código de produção):** Avaliar `CausalSingleSlotSimulator(..., breakeven_enabled=True, breakeven_trigger_r=1.2)` (`src/backtest/causal_simulator.py:63-64,129,193`) sobre `K9.3` com dados `1m` apenas em Treino+Validação (sem reabrir o Holdout!). Se Calmar e `E[R]_net` **não** melhorarem frente à `K9.3` sem BE, **encerrar e arquivar o Breakeven via ADR** (poupando complexidade operacional).
  2. **Etapa 2 (Somente se Etapa 1 = PASS):** Implementar consumidor WebSocket assíncrono `wss://stream.binance.com:9443/ws/<symbol>@bookTicker` com reconexão automática (`ping/pong`, rotação preventiva `< 24h` da Binance), isolamento de thread compatível com `ADR-002` (fila thread-safe enviando eventos ao writer único da thread principal — nunca chamando `write_tx()` na thread do socket, `src/core/database.py:388-392`), protocolo atômico de cancelamento + recriação da OCO em `critical_section()` com fallback a mercado em `-2010`, e desarme automático do BE quando o socket estiver desconectado (`require_realtime_websocket_for_be`). Rodar soak test de **14 dias** em modo observador antes de habilitar.
* **Gate / Fase:** `F2->F3`.
* **Teste ou métrica que prova que funcionou:** (1) Relatório `1m` provando `Calmar(K9.3 + BE) > Calmar(K9.3)` e `E[R]_net(K9.3 + BE) > E[R]_net(K9.3)` nos cenários Base e Pessimista (ou ADR formal de descarte); (2) se aprovado, 14 dias consecutivos de soak test com 0 desconexões não tratadas, RSS `< 350 MB` (`mem_limit: 512m`), e testes de race condition (preço revertendo durante cancelamento/recriação da OCO sem jamais deixar posição sem stop).
* **Esforço:** `G`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/backtest/causal_simulator.py`, `src/execution/oco_order_manager.py`, `src/core/binance_client.py`, `src/core/database.py`, `src/main.py`, `docs/methodology-and-roadmap.md`, `docs/block-3-spec.md`, `docs/decisions.md`.

---

#### B9 — Homologação de `sideways_pingpong` (Gate 3 + 30 shadow trades) e roteamento multi-estratégia por `net_expected_r`

* **Estado atual verificado:**
  * `SidewaysPingPongStrategy` está codificada (`src/strategies/sideways_pingpong.py:16-118`, trava `RR >= 1,50` em `L90-91`), mas `src/main.py:117-121` carrega apenas `BullTrendPullbackStrategy` e `src/main.py:571` desempata candidatos apenas por `rs_vol_adjusted_12h`.
  * Não há homologação empírica de `sideways_pingpong` no Gate 3 (`docs/block-3-spec.md:280-375` cobriu apenas `bull_trend_pullback` / `K9.3`) nem coleta de 30 operações virtuais *Shadow Live* ([Metodologia §4.4](methodology-and-roadmap.md)).
* **Objetivo:** (1) Executar validação Gate 3 isolada de `sideways_pingpong` em Treino/Validação/Holdout próprio (nova hipótese estrutural separada, `n >= 150`, `RR >= 1,5` líquido, `PF_base >= 1,20`, `PF_pess > 1,05`, `DSR >= 0,95`); (2) se aprovar, habilitar registro *Shadow Live* somente-observação em `shadow_trades` durante períodos `SIDEWAYS` reais até somar `>= 30` trades com `mean(R_shadow) >= +0,10R`; (3) implementar roteamento multi-estratégia atrás de `RunMode` Fase 3 usando `phase_2_tie_breaker: "net_expected_r"` (`config/strategies.yaml:68`, `docs/architecture.md:103`) e o desarme isolado de 20 trades (`<= -3,0R`) via `ShadowManager.evaluate_phase_downgrade()` (`src/risk/shadow_manager.py:217-223`).
* **Gate / Fase:** `F2->F3` (exige também `>= 100` trades reais acumulados na Fase 2, `docs/methodology-and-roadmap.md:134`).
* **Teste ou métrica que prova que funcionou:** Gate 3 `PASS` para `sideways_pingpong` + 30 operações shadow com `mean(R_shadow) >= +0,10R` + testes unitários validando desempate por `net_expected_r` entre regimes distintos e desarme isolado de `SIDEWAYS` em `-3,0R` / 20 trades sem afetar `BULL`.
* **Esforço:** `G`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/strategies/sideways_pingpong.py`, `src/backtest/causal_simulator.py`, `src/backtest/ablation_and_dsr.py`, `src/main.py`, `src/risk/shadow_manager.py`, `docs/methodology-and-roadmap.md`, `docs/architecture.md`.

---

#### B10 — Estudo ponto-no-tempo de `bear_capitulation_snap` com ativos colapsados/deslistados

* **Estado atual verificado:**
  * `bear_capitulation_snap` está em quarentena permanente (`config/strategies.yaml:118-126`, `docs/methodology-and-roadmap.md:143-144`).
  * `src/strategies/bear_capitulation_snap.py:47-98` não implementa `required_features()` (não solicita `rsi_2` nem `lower_wick_ratio` ao `FeatureStore`), usa nomes de coluna fixos incompatíveis com `FeatureStore` (`rsi_2_1h`, `rvol_20_1h`, `atr_14_1h`, `close_time` em vez de `close_time_ms` em `L57,84`) e nunca passou por simulação sem viés de sobrevivência.
* **Objetivo:** Realizar pesquisa offline dedicada com série histórica **ponto-no-tempo** (*point-in-time*) que inclua explicitamente grandes ativos de 2021–2024 que colapsaram ou foram deslistados (ex.: `LUNAUSDT`, `FTTUSDT`, `USTUSDT`, entre outros da Binance) além dos 7 pares atuais, corrigindo o alinhamento de features apenas no escopo do estudo (sem ativar em produção nem editar `config/strategies.yaml`). Submeter a estratégia a estresse severo de slippage/gap (`>= 3%–5%` no stop em eventos de capitulação) para decidir objetivamente entre promover a estudo Shadow ou arquivar permanentemente via ADR.
* **Gate / Fase:** `F2->F3` (menor prioridade; permanecer 100% em USDT no regime `BEAR` é o comportamento desejado nas Fases 1 e 2).
* **Teste ou métrica que prova que funcionou:** Notebook reproduzível sobre dataset ponto-no-tempo com ativos colapsados atingindo `PASS` em todos os critérios do Gate 3 (`n >= 150`, `E[R]_net > +0,10R`, `CI95_lo > 0`, `PF_pess > 1,05`, `DSR >= 0,95`) **sob estresse extremo de gap** — ou registro formal de rejeição/arquivamento em `docs/decisions.md`.
* **Esforço:** `G`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/strategies/bear_capitulation_snap.py`, `src/backtest/causal_simulator.py`, `src/backtest/ablation_and_dsr.py`, `src/features/feature_store.py`, `docs/methodology-and-roadmap.md`, `docs/architecture.md`.

---

## 2. Pré-mortem da Fase 1: As 10 Maneiras Mais Prováveis de Perder Dinheiro ou Capital

**Premissa do exercício:** imagine que estamos 6 meses após a ativação da **Fase 1** (`150 XRP` na Carteira Core + `~145,5 XRP` em USDT na Carteira Tática + `~4,5 XRP` em BNB) e houve perda relevante de patrimônio ou erosão severa do capital tático. Abaixo estão os **10 modos de falha mais prováveis**, cobrindo riscos de **mercado**, **técnicos/execução** e **operacionais/externos**, ranqueados por **Probabilidade (`P`, 1–5) × Impacto (`I`, 1–5)** e avaliados com ceticismo frente às premissas não provadas da documentação.

* **Escala de Probabilidade (`P`, horizonte 12 meses):** `1` = raro (`< 5%`), `2` = baixo (`5%–15%`), `3` = moderado (`15%–35%`), `4` = alto (`35%–60%`), `5` = quase certo (`> 60%`).
* **Escala de Impacto (`I`, sobre capital tático ou patrimônio total):** `1` = `< 2%`, `2` = `2%–5%`, `3` = `5%–12%`, `4` = `12%–25%`, `5` = `> 25%` ou perda/bloqueio severo do capital.

### Tabela-Resumo do Ranking

| Rank | Categoria | Modo de Perda | P | I | P × I | Mitigação no código? |
| --- | --- | --- | --- | --- | --- | --- |
| **#1** | Mercado / Portfólio | Queda de 70%–90% da Carteira Core (`150 XRP`) + concentração dobrada quando o bot compra `XRPUSDT` | 4 | 5 | **20** | **Nenhuma** (para risco de mercado do Core; `CVaR 99%` só tático na Fase 2 via B7) |
| **#2** | Modelo / Mercado | Overfitting residual (`HoldSum` observado em `K1`–`K9`) e viés de sobrevivência dos 7 pares | 4 | 4 | **16** | Parcial (CPPI `-4%`, Hard Stop `-12%`, Gate 6B, `evaluate_phase_downgrade` via B7) |
| **#3** | Mercado | Mudança estrutural de regime (*bear* prolongado / *whipsaws* de falsos rompimentos em 4h) | 4 | 4 | **16** | Parcial (MECE `BEAR` 1º, veto BTC, CPPI, `-12%`, `regime_history` via B1) |
| **#4** | Execução / Técnica | Flash-crash saltando buffer `2,5%` do `STOP_LOSS_LIMIT` | 3 | 5 | **15** | Melhorada via **B6** (buffer `2,5%`, teto caixa `50%`, watchdog sub-horário `<= 20 s`) |
| **#5** | Execução / Microestrutura | Slippage, spread e taxas reais na virada de hora (`XX:00:03 UTC`) acima do backtest | 3 | 4 | **12** | Melhorada via **B1/B2** (`fees_usdt_equiv`, L2 VWAP shadow, `COMMISSION_WARNING`; falta slippage de saída dedicado) |
| **#6** | Externa / Contraparte | Dependência única da Binance (contraparte/regulatório, manutenção sem aviso de 2h, API/delisting, depeg `USDT`) | 2 | 5 | **10** | Parcial (fail-closed manutenção, isolamento `-1121` via **B3**; **nenhuma** para contraparte/USDT/2h) |
| **#7** | Execução / Técnica | HTTP `418` (IP ban) ou queda de rede/VPS entre fill e OCO (ou após cancelar OCO), deixando posição nua | 2 | 5 | **10** | Parcial (`PENDING_ENTRY`, `critical_section`, reconciler; **nenhuma** sob ban 418 ativo) |
| **#8** | Operacional / Humana | Intervenção humana por tédio/pânico, trade manual na conta API (C1-D), offsite/heartbeat desligados ou BNB esgotado | 3 | 3 | **9** | Melhorada via **B1** (`HALT_REVIEW` C1-D, SHA-256, `BNB_LOW_WARNING` `< 5,00`; falta exigir `HEALTHCHECK_URL` em prod) |
| **#9** | Técnica / Governança | Cegueira de degradação e contabilidade distorcida (`ML_JUDGE_ARTIFACT_PATH` ainda não gerado em prod, aportes manuais no HWM) | 3 | 3 | **9** | Melhorada via **B2/B4/B7** (`fees_usdt_equiv`, loader `MLJudge` + CLI `train_ml_judge.py`, `evaluate_phase_downgrade`; exige rodar treino antes da Fase 1) |
| **#10** | Infraestrutura / Técnica | Falha de VPS: disco cheio, OOM (`> 512m`), drift de relógio ou perda de disco sem offsite | 2 | 4 | **8** | Melhorada via **B1/B3** (WAL, `HEALTHCHECK`, `prune_old_backups`, `DISK_USAGE_WARNING >= 70%`; sem monitor interno de RAM) |

---

### Análise Cética Detalhada

#### #1 — Queda severa (70%–90%) da Carteira Core (`150 XRP`) e concentração dobrada em `XRPUSDT` (`P=4 × I=5 = 20`)

* **Como o dinheiro é perdido:** Metade do capital total do projeto (`150 XRP`, `config/strategies.yaml:9-10`) fica em exposição direcional pura, sem stop, em uma única altcoin. Mesmo se o Chameleon tático tiver desempenho exemplar, um ciclo de baixa severo de `-75%` no XRP apaga `-37,5%` do patrimônio total. Além disso, `XRPUSDT` faz parte da cesta de execução tática (`config/strategies.yaml:73`): durante um topo local em `BULL`, o bot pode alocar até `50%` da carteira tática em `XRPUSDT` (`config/strategies.yaml:36`), concentrando temporariamente ~75% do patrimônio total no mesmo ativo.
* **Premissa não provada na documentação:** `docs/architecture.md:25` afirma que *"o risco global é monitorado pelo Value at Risk (VaR) e Conditional VaR (CVaR a 99%) do portfólio combinado"*. Mesmo após **B7**, `QuantOrchestrator._compute_phase2_sizing_guards()` (`src/main.py:360-397`) calcula `CVaR 99%` apenas sobre os trades táticos na Fase 2 — **não** inclui a marcação a mercado da Carteira Core (`150 XRP`).
* **Mitigação existente no código:** **`nenhuma`** contra risco de mercado do Core (o isolamento físico citado em `config/strategies.yaml:10` e `docs/architecture.md:24` protege apenas contra erro operacional do bot, não contra queda do preço do XRP).
* **O que mediria o risco:** Drawdown marcado a mercado do patrimônio consolidado (`150 * preço_XRP + USDT_tático + BNB`), percentual de trades táticos alocados em `XRPUSDT` e CVaR 99% empírico combinado (Core + Tático).

---

#### #2 — Overfitting residual (`HoldSum` observado em `K1`–`K9`) e viés de sobrevivência da cesta (`P=4 × I=4 = 16`)

* **Como o dinheiro é perdido:** `K9.3` exibe `E[R]_net = +0,230R`, `PF_base = 1,43` e `DSR = 0,96` (`docs/block-3-spec.md:350`), porém o próprio `docs/methodology-and-roadmap.md:57` reconhece que `HoldSum` foi observado durante as rodadas `K1`–`K9`. Na prática, `docs/block-3-spec.md:309-354` documenta explicitamente ajustes iterativos (`K5` → `K6` → `K7` → `K8` → `K9`) após o raio-X do Holdout negativo (`-6,58R`) até chegar a `+5,80R`, contabilizando múltiplas sub-variantes (`K3.1..K9.6`) como apenas `K = 9` baterias no cálculo do `DSR` (`src/backtest/ablation_and_dsr.py:226-237`). Soma-se o viés de sobrevivência retrospectivo de fixar em 2026 os 7 sobreviventes líquidos de 2022–2026 (`config/strategies.yaml:70-76`). Se a vantagem real fora da amostra for próxima de zero ou negativa após custos, a carteira tática sangra gradualmente até o hard stop de `-12%`.
* **Premissa não provada na documentação:** Que `K = 9` representa o número real de graus de liberdade explorados e que o Holdout (`+5,80R`) é livre de viés de seleção.
* **Mitigação existente no código:** CPPI reduz risco em 50% a partir de `-4,0%` abaixo do HWM (`src/risk/shadow_manager.py:305-315`); Hard Stop Patrimonial dispara `EMERGENCY_HALT` em `-12,0%` (`src/risk/shadow_manager.py:26,288-291`); `ShadowManager.evaluate_phase_downgrade()` conectado no pós-fechamento em `src/main.py:237-315` (**B7**); e o Gate 6B funciona como Holdout Prospectivo (`tools/gate_report.py:426-630`).
* **O que mediria o risco:** `mean(realized_r)` líquido acumulado e estatística CUSUM no Gate 6B e nos primeiros 30–60 trades da Fase 1 comparados à banda inferior do IC 95% do Bloco 3 (`+0,068R`, `docs/block-3-spec.md:350`), mais `Profit Factor` líquido em janela móvel.

---

#### #3 — Mudança estrutural de regime (*bear market* prolongado ou *whipsaws* de rompimento falso) (`P=4 × I=4 = 16`)

* **Como o dinheiro é perdido:** A Fase 1 executa exclusivamente `bull_trend_pullback` em modo `K9.3` (rompimento comprador Donchian `12h` + `RS >= 1,40` com stop `3,0 * ATR_14`, `src/strategies/bull_trend_pullback.py:27-33`). Em um regime prolongado de baixa pontuado por *bear-market rallies* violentos, a `EMA_100` de `4h` (~16,7 dias) vira positivamente tarde demais: o detector classifica `BULL` próximo ao topo do repique (`src/regimes/regime_detector.py:97-98`), o bot compra rompimentos esticados e sofre stops consecutivos na reversão. Alternativamente, em um ano inteiro de lateralização/queda sem tendências sustentadas de `72h`, o custo de oportunidade + falsos rompimentos corroem o caixa até `-4%`/`-12%`.
* **Premissa não provada na documentação:** Que a frequência de tendências limpas de `72h` observada em 2022–2026 (`~1,3 trade/semana`) se manterá e que `veto_if_btc_bear` impede *bull traps* quando o próprio BTC ainda está acima da `EMA_100_4h`.
* **Mitigação existente no código:** Precedência `BEAR` antes de `BULL` no MECE 4h (`src/regimes/regime_detector.py:93-98`), filtro `CHOP_14_4h < 50` (`L97`), veto `btc_regime_4h == BEAR` (`src/strategies/bull_trend_pullback.py:155-156`), filtro `ADX_14_4h >= 17,0` (`L158-160`), CPPI em `-4%` (`src/risk/shadow_manager.py:305-315`), Hard Stop em `-12%` (`L288-291`) e telemetria persistida em `regime_history` (`src/main.py:1205-1219`, **B1**).
* **O que mediria o risco:** Taxa de *false breakouts* (`exit_reason == 'STOP'` ocorridos em `< 24h` da entrada), distribuição de tempo gasto em cada regime 4h (agora persistida em `regime_history`), e soma móvel de `realized_r` nos últimos 10–20 trades.

---

#### #4 — Flash-crash saltando o buffer de `2,5%` da perna `STOP_LOSS_LIMIT` (`P=3 × I=5 = 15`)

* **Como o dinheiro é perdido:** A proteção OCO usa `STOP_LOSS_LIMIT` com limite `2,5%` abaixo do `stopPrice` (`config/strategies.yaml:27-28`, `src/execution/oco_order_manager.py:134-139`). Em quedas bruscas por cascata de liquidações em futuros (evento recorrente em `SOL`, `XRP`, `ADA`, `LINK` e `AVAX`), o preço aciona o `stopPrice` e atravessa o `stopLimitPrice` em segundos sem preencher a ordem limite. Com teto de alocação de `50%` do caixa (`config/strategies.yaml:36`), mesmo poucos segundos de gap extremo podem gerar perda muito acima de `1%` do capital tático.
* **Premissa não provada na documentação:** O simulador do Bloco 3 (`src/backtest/causal_simulator.py:108-112`) aplica no cenário `PESSIMISTIC` um `gap_shock` fixo de apenas `0,15%` (`0.0015`), nunca modelando saltos de liquidez `> 2,5%`.
* **Mitigação existente no código:** Buffer de `2,5%` em `compute_oco_leg_prices()` (`src/execution/oco_order_manager.py:134-139`), teto de caixa de `50%` (`src/risk/position_sizer.py:276,286`), e **watchdog sub-horário implementado em B6** (`src/main.py:1363-1483,1533-1536`, polling padrão a cada `20 s` via `WATCHDOG_POLL_INTERVAL_SECONDS`), reduzindo a janela exposta de ~59 minutos para `<= 20 s` (sem eliminar gaps instantâneos ocorridos dentro dessa janela).
* **O que mediria o risco:** Qualquer ocorrência de `exit_reason == 'STOP_WATCHDOG'` em `trades`, o desvio percentual `(oco_stop_price - exit_avg_price) / oco_stop_price` em todos os stops fechados (slippage de saída — hoje sem coluna própria), e o atraso entre o candle `1m` que cruzou `oco_stop_limit_price` e `trades.closed_at_ms`.

---

#### #5 — Divergência de slippage, spread e taxas reais frente às premissas do backtest (`P=3 × I=4 = 12`)

* **Como o dinheiro é perdido:** A entrada em `LIVE` envia `MARKET BUY` (`src/execution/oco_order_manager.py:492-496`) após o fechamento da vela `1h` (`XX:00:03 UTC`, `config/strategies.yaml:24`, mais latência de coleta de 16 ativos em `src/main.py:1163`), comprando exatamente **rompimentos com volume explosivo** (`RVOL >= 1,30`, `Donchian 12h`, `corpo >= 0,65 * ATR`) no segundo em que bots de rompimento disputam a mesma liquidez e spreads abrem. O backtest pressupõe `0,06%` de slippage base (`0,12%` pessimista) e `0,15%` de taxa com desconto BNB (`config/strategies.yaml:41-42`, `src/backtest/causal_simulator.py:104-110`). Se o desconto BNB falhar ou spreads abrirem na virada de hora, a expectativa líquida de `+0,230R` pode ser erodida.
* **Premissa não provada na documentação:** Que o slippage real somado (entrada + saída em stop/time-stop) em altcoins às `XX:00:03+ UTC` fica dentro de `0,06%–0,12%`.
* **Mitigação existente no código:** Ordem de execução C2-9 rodando Chameleon antes da L4 (`src/main.py:1246-1259`), registro de `trades.slippage_entry_bps` (`src/execution/oco_order_manager.py:655-680`), auditoria de slippage em `tools/gate_report.py:530-593,865-912`, alerta `COMMISSION_WARNING` em `src/main.py:859-872` (**B1**), apuração real de `fees_usdt_equiv` e `realized_r` líquido em `src/execution/oco_order_manager.py:91,431,823,1395,1991` / `src/core/state_reconciler.py:1007-1020` (**B2**), e impacto VWAP L2 (`depth_vwap_ask`, `book_impact_bps`) no `PaperExecutor` (`src/execution/paper_executor.py:17,111-183`, **B2**). **Ainda não existe coluna dedicada para slippage de saída em `trades`.**
* **O que mediria o risco:** Mediana, p90 e média de `trades.slippage_entry_bps` (alerta se `> 8 bps`) e `shadow_trades.book_impact_bps`, slippage de saída nos stops/time-stops (`(oco_stop_price - exit_avg_price) / oco_stop_price`), alertas `COMMISSION_WARNING` e reconciliação periódica do saldo real `USDT + BNB` vs. soma de `realized_pnl_usdt - fees_usdt_equiv`.

---

#### #6 — Dependência concentrada na Binance: contraparte, manutenção sem aviso de 2h, mudança de API/delisting e depeg de `USDT` (`P=2 × I=5 = 10`)

* **Como o dinheiro é perdido:** Todo o capital tático + reserva BNB reside em uma única corretora e um único endpoint hardcoded (`https://api.binance.com`, sem failover para `api1..4.binance.com`, `src/core/config.py:26,401,410`). Quatro cenários críticos:
  1. **Contraparte / regulatório / bloqueio de conta:** insolvência, restrição jurisdicional ou congelamento de conta Spot indisponibiliza 100% da carteira tática.
  2. **Manutenção sem aviso prévio de 2h:** `docs/methodology-and-roadmap.md:72` afirma *"bloqueando entradas 2h antes"*, porém `SignedBinanceClient.sapi_system_status()` (`src/core/binance_client.py:833-845`) e `src/main.py:480-491` leem apenas o binário `status == 0` vs. `1` (e a Testnet retorna `mocked=True`). O bot pode abrir uma posição minutos antes de uma parada de manutenção e sofrer gap severo na reabertura.
  3. **Mudança de contrato API ou deslistagem:** `oco_allowed` assume default `True` se o campo sumir (`src/core/binance_client.py:624`), e filtros `MARKET_LOT_SIZE` / `PERCENT_PRICE_BY_SIDE` não são verificados (`src/core/binance_client.py:604-615`). *(Nota: o isolamento de `-1121` para símbolos exclusivos do Radar foi resolvido em **B3**.)*
  4. **Depeg de `USDT`:** Em `BEAR` e `TRANSITION`, 100% da defesa fica estacionada em `USDT` (`config/strategies.yaml:11`). Uma desancoragem aguda da stablecoin atinge o caixa inteiro de uma vez.
* **Premissa não provada na documentação:** Que `/sapi/v1/system/status` antecipa manutenções em 2h, que `USDT` é livre de risco e que os contratos `/api/v3/orderList/oco` e `/api/v3/exchangeInfo` permanecem estáveis.
* **Mitigação existente no código:** Fail-closed quando `sapi_system_status()` falha (`src/main.py:486-491`), pré-validação `status == TRADING` e `oco_allowed` antes da compra (`src/execution/oco_order_manager.py:389-410`), isolamento `-1121` por símbolo priorizando `execution_universe` em `ExchangeInfoCache` (`src/core/binance_client.py:647-705`, **B3**), fallback para filtros em cache se `exchangeInfo` falhar (`src/main.py:1146-1154`), e Core fora da chave Spot (`config/strategies.yaml:10`). **Nenhuma mitigação existe para risco de contraparte, depeg de `USDT`, failover de host ou janela antecipada de 2h.**
* **O que mediria o risco:** Taxa de erros HTTP 4xx/5xx em `order_events`, alertas `EXCHANGE_INFO_WARNING` e falhas em `sapi_system_status`, monitoramento externo de *peg* `USDT/USD` (`< 0,995`) e revisão periódica do changelog da Binance API ([Runbook](runbook.md)).

---

#### #7 — HTTP `418` (IP ban) ou queda de rede/VPS entre fill da compra e criação da OCO, deixando posição nua (`P=2 × I=5 = 10`)

* **Como o dinheiro é perdido:** Se a Binance retornar HTTP `418` (possível em VPS com IP compartilhado/reutilizado ou bug de rajada) logo **após** `POST /api/v3/order` executar e **antes** de `POST /api/v3/orderList/oco`, `WeightGovernor.on_rate_limited(..., banned=True)` (`src/core/binance_client.py:212-213,466-472`) bloqueia localmente **todas** as requisições — inclusive `Priority.CRITICAL` (`src/core/binance_client.py:150-153`) — durante `Retry-After`. `OcoOrderManager._handle_ip_ban()` (`src/execution/oco_order_manager.py:258-291`) aciona `emergency_halt` e envia alerta `KILL_SWITCH`, mas **a posição permanece sem stop na corretora** até ação humana ou expiração do ban. Falha análoga ocorre se a VPS perder conectividade após cancelar a OCO no passo 1 do Time-Stop ou do Watchdog antes da venda `MARKET`.
* **Premissa não provada na documentação:** Que o operador lerá o alerta Telegram em poucos minutos (inclusive de madrugada) e terá acesso imediato à Binance para proteger ou liquidar a posição manualmente.
* **Mitigação existente no código:** `PENDING_ENTRY` gravado antes do POST (`src/execution/oco_order_manager.py:597`), `governor.critical_section()` (`src/core/binance_client.py:155-159,225`), escritas pós-fill *best-effort* (`src/execution/oco_order_manager.py:233`), recuperação automática no boot para `PENDING_ENTRY`/`ENTRY_FILLED`/`CLOSING` (`src/core/state_reconciler.py:398-520,643-710`), alerta `KILL_SWITCH` em HTTP 418 e heartbeat opcional `HEALTHCHECK_URL` (`src/main.py:1342-1348`, `src/core/healthcheck.py:21-54`). **Nenhuma mitigação de software consegue colocar a OCO enquanto o IP estiver banido (418) ou a VPS estiver sem rede.**
* **O que mediria o risco:** `cycle_health.used_weight_1m_peak` (alerta preventivo se `> 3000` de 6000), qualquer registro em `order_events` com `http_status IN (429, 418)`, qualquer trade em `ENTRY_FILLED`/`CLOSING` por `> 60 s`, e disparo do dead-man switch externo (`HEALTHCHECK_URL`, que **vem vazio por padrão** em `.env.example`).

---

#### #8 — Erro operacional humano: intervenção por tédio/pânico, trade manual na conta API, proteções desligadas por padrão ou BNB sem recarga (`P=3 × I=3 = 9`)

* **Como o dinheiro é perdido:**
  1. **Intervenção emocional:** Com taxa de acerto típica de estratégia `2:1`, sequências de 5 a 7 stops consecutivos ou 20 dias sem sinal em `BEAR`/`TRANSITION` são normais. Fechar posições no pânico ou desligar o robô no fundo do drawdown cristaliza a perda e impede capturar os grandes *winners* de `+2,0R` que pagam a expectativa.
  2. **Trade manual na mesma conta Spot da chave de API:** Qualquer saldo excedente `>= minNotional` em um dos 7 pares dispara `HALT_REVIEW` (C1-D, `src/core/state_reconciler.py:562-614`), paralisando o robô e podendo induzir erros manuais durante o destravamento.
  3. **Subir produção com defaults inseguros:** Em `.env.example`, tanto `HEALTHCHECK_URL` quanto `OFFSITE_BACKUP_ENABLED` vêm vazios/`false` por padrão; esquecer de configurá-los deixa produção sem dead-man switch e sem backup fora da VPS.
  4. **Esgotamento de BNB se o alerta preventivo for ignorado:** `BNB_LOW_WARNING` (`< 5,00 USDT`) agora alerta via Telegram (**B1**), mas se o operador não recarregar a conta Spot, o bot para em `EMERGENCY_HALT` ao atingir `< 2,00 USDT` (`src/risk/shadow_manager.py:292-295`).
* **Premissa não provada na documentação:** Disciplina humana perfeita em períodos longos sem operação e execução pontual do rito mensal manual (`docs/methodology-and-roadmap.md:175-179`).
* **Mitigação existente no código:** Trava dupla de produção (`LIVE_CONFIRMATION` + `APPROVED_STRATEGIES_SHA256`, `src/core/config.py:532-544,583-595`), `HALT_REVIEW` C1-D nunca vende saldo manual (`src/core/state_reconciler.py:580-613`), alerta preventivo `BNB_LOW_WARNING` em `< 5,00 USDT` (`src/main.py:844-858`, **B1**), `EMERGENCY_HALT` em `BNB < 2,00 USDT` (`src/risk/shadow_manager.py:292-295`), `OFFSITE_BACKUP_ENABLED=true` falha no boot sem `BACKUP_ENCRYPTION_KEY` (`src/core/config.py:111-119`), e procedimentos passo a passo em [Runbook Operacional](runbook.md). **Nenhuma mitigação impede `HEALTHCHECK_URL` vazio em produção.**
* **O que mediria o risco:** Checklist automatizado pré-produção validando `.env` (`HEALTHCHECK_URL` + `OFFSITE_BACKUP_ENABLED=true`), auditoria mensal de divergência entre histórico `myTrades` da Binance e tabela `trades` (detecta trades manuais), contagem de `HALT_REVIEW` e saldo `BNB` em USDT (`system_state.bnb_free_usdt_equiv`).

---

#### #9 — Cegueira de degradação e contabilidade distorcida (`P=3 × I=3 = 9`)

* **Como o dinheiro é perdido:** Decisões ruins de gestão de capital (continuar operando estratégia degradada ou promover risco/Fase 2 prematuramente) caso o operador não conclua as calibrações pré-produção ou misture aportes/saques na conta tática:
  * **B2, B4 e B7** resolveram no código a apuração de `fees_usdt_equiv` líquido, o loader/treinador calibrado do `MLJudge` (`tools/train_ml_judge.py`, `src/features/ml_judge.py:55,321,463`) e a conexão de `ShadowManager.evaluate_phase_downgrade()` (`src/main.py:237-315`);
  * **Riscos residuais ativos:** (1) se a Fase 1 iniciar sem gerar e apontar `ML_JUDGE_ARTIFACT_PATH`, o fallback continuará sendo `PlattCalibrator(a=-1.0, b=0.0)` não treinado; (2) `src/main.py:619` ainda iguala `tactical_equity_usdt = free_usdt` — depósitos/saques manuais de USDT na carteira Spot distorcem silenciosamente `tactical_hwm_usdt` (`src/risk/shadow_manager.py:266-277`), podendo disparar CPPI/Hard Stop falsamente em saque ou mascarar drawdown real em aporte.
* **Premissa não provada na documentação:** Que o artefato do `MLJudge` terá `ECE <= 0,05` e separação real sobre os 60 trades ao vivo da Fase 1, e que nenhum aporte/saque manual alterará o `free_usdt` durante a execução.
* **Mitigação existente no código:** `tools/gate_report.py` adota fail-safe rigoroso de `UNKNOWN` (nunca `PASS` por omissão); `tools/train_ml_judge.py` exclui estritamente o Holdout (**B4**); `LIVE_CHAMELEON_PHASE_2` exige `ML_JUDGE_ARTIFACT_PATH` no boot quando `use_ml_filter=true` (`src/core/config.py:576-581`, **B7**); `fees_usdt_equiv` + `realized_r` líquido (**B2**) e `evaluate_phase_downgrade()` (**B7**). **Nenhuma mitigação distingue automaticamente aportes/saques manuais de USDT de lucro/prejuízo de trade.**
* **O que mediria o risco:** Reconciliação semanal `Δ(USDT + BNB real na Binance - aportes + saques) - sum(trades.realized_pnl_usdt - trades.fees_usdt_equiv)`, distribuição de `trades.ml_grade_silent` (`% C / % B / % A+`), e execução periódica de `tools/gate_report.py` e `tools/radar_ai_audit.py` (**B5**).

---

#### #10 — Falha de infraestrutura da VPS: disco cheio, OOM (`> 512m`), drift de relógio ou perda de disco (`P=2 × I=4 = 8`)

* **Como o dinheiro é perdido:** Uma parada prolongada ou falha de escrita no disco da VPS não destrói o saldo imediatamente quando a OCO já está ativa na Binance, mas impede (a) gravar `PENDING_ENTRY` ou executar `STOP_WATCHDOG` e Time-Stop de `72h` no prazo, (b) manter o estado de `trades` (`oco_order_list_id`, `time_barrier_end_ms`, `tactical_hwm_usdt`) se o disco corromper sem backup offsite — fazendo um banco recriado do zero disparar `HALT_REVIEW` (C1-D) sobre a própria posição aberta do bot! Especificamente:
  * `docker-compose.yml:6` e `deploy/quant-chameleon.service:13` impõem `mem_limit: 512m` (ADR-015; apenas `import src.main` já consome ~93,4 MB de RSS, `docs/decisions.md:23`); vazamentos ou picos de memória causam `OOMKilled` (`Exit 137`).
  * Falhas repetidas de `MonotonicClock.sync()` (`src/main.py:1115-1121`) podem acumular drift `> 500 ms` e provocar rejeições `-1021` (há apenas 1 tentativa de resync+retry em `OcoOrderManager`).
* **Premissa não provada na documentação:** Que o consumo de RAM permanece folgado abaixo de `512 MB` ao longo de meses de histórico em SQLite/pandas sem monitoramento automático interno de RSS.
* **Mitigação existente no código:** Rotação automática de backups locais `Database.prune_old_backups()` (`src/core/database.py:782-812`, `BACKUP_RETENTION_DAYS=14`, **B1**), monitoramento interno de disco `QuantOrchestrator._check_disk_usage()` com alerta `DISK_USAGE_WARNING >= 70%` (`src/main.py:537-566`, **B3**), rotação de logs Docker (`max-size: 10m`, `max-file: 3` em `docker-compose.yml:13-17`), `restart: unless-stopped` (`docker-compose.yml:5`), `mem_limit: 512m` (`docker-compose.yml:6`), SQLite WAL + `synchronous=FULL` (`src/core/database.py:19-20`), escritas pós-fill *best-effort*, Docker `HEALTHCHECK` somente-leitura (`Dockerfile:21-22`, `src/core/healthcheck.py:57-100`), backup offsite criptografado opcional (`src/core/offsite_backup.py:43-165`), resync + retry único em `-1021`, e `StateReconciler` no boot. **Nenhuma mitigação monitora consumo de RAM (RSS) internamente.**
* **O que mediria o risco:** Uso de disco (`system_state.disk_usage_pct` e `df -h`), tamanho e quantidade de arquivos em `data/backups/` e `data/quant.db`, memória RSS do container (`docker stats`), `cycle_health.clock_offset_ms`, `cycle_health.duration_ms`, `cycle_health.unhandled_errors`, `cycle_health.stale_symbols` e alertas do `HEALTHCHECK_URL`.
