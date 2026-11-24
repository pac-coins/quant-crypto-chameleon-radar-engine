# Próximos Passos: Backlog Priorizado e Pré-mortem da Fase 1

Este documento consolida o planejamento técnico pós-Bloco 6 e a análise cética de riscos antes da virada para capital real na **Fase 1** (`MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=PRODUCTION`).

!!! warning "Restrição inegociável de governança"
    Nenhum item deste documento propõe alterar parâmetros da estratégia congelada **`K9.3`** (`bull_trend_pullback`), editar `config/strategies.yaml` ou reabrir o Holdout do Bloco 3 ([Metodologia e Roadmap §2](methodology-and-roadmap.md)). Toda alteração de flags de fase (`profit_splitter`, `ml_judge`, `conviction_sizing`, `breakeven`, `sideways_pingpong`) permanece travada em `src/core/config.py:504-535` até que o respectivo Gate seja aprovado formalmente e receba novo `APPROVED_STRATEGIES_SHA256`.

---

## 1. Backlog Priorizado

A ordenação abaixo segue estritamente **o que desbloqueia o próximo gate mais cedo**, respeitando o estado atual do projeto (**Gate 6A** em curso na Binance Spot Testnet → **Gate 6B** em `SHADOW_CHAMELEON` → **Fase 1** → **Gate F1→F2** → **Gate F2→F3**).

!!! info "Regra operacional de deploy"
    Nenhum código deve ser implantado nem o container reiniciado **durante** o Gate 6A em andamento (`AGENTS.md`). Os itens ligados a `6A`/`6B` podem ser desenvolvidos e testados offline em branch separada, mas só entram em operação na transição controlada entre gates.

### Resumo Executivo

| Ordem | ID | Item | Gate / Fase | Esforço | Desbloqueio principal |
| --- | --- | --- | --- | --- | --- |
| **1** | **B1** | Telemetria de equity/BNB/regime no SQLite, alertas preventivos (`< 5,00 USDT` BNB e taxa `> 0,10%`) e rotação de backups locais | `6A` / `6B` | `P` | Elimina `UNKNOWN` em `tools/gate_report.py` e previne disco cheio / esgotamento silencioso de BNB |
| **2** | **B2** | Contabilidade líquida de `fees_usdt_equiv` (LIVE) e slippage ponderado por profundidade do livro (`SHADOW_CHAMELEON`) | `6B` (pré-`F1`) | `M` | Validação fiel do limiar de slippage `<= 0,08%` no Gate 6B e PnL/PF reais líquidos na Fase 1 |
| **3** | **B3** | Observabilidade ativa: Scorecard semanal do Radar, alerta de disco e isolamento de `-1121` no batch de `exchangeInfo` | `6B` | `P` | 30 dias limpos no Gate 6B sem um par Radar deslistado derrubar os 7 pares do Chameleon |
| **4** | **B4** | Treinamento offline (Treino/Validação) e carga de artefato calibrado do `MLJudge` em modo observador silencioso | `6B` → `F1->F2` | `M` | Evita desperdiçar os 60 trades reais da Fase 1 com `PlattCalibrator` não treinado (`a=-1, b=0`) |
| **5** | **B5** | Pipeline de auditoria dos dados do Radar (`virtual_signals` + `orderbook_snapshots`) para validar L4 e o Juiz de IA | `6B` → `F1->F2` | `M` | Testa empiricamente se L4 adiciona edge e se enriquece features do `MLJudge` sem violar ADR-004 |
| **6** | **B6** | Watchdog sub-horário (`<= 30 s`) para `STOP_LOSS_LIMIT` pendurado durante posição `PROTECTED` | `6B` / `F1->F2` | `M` | Reduz de ~59 min para `< 30 s` a exposição a flash-crash que pule o buffer de `2,5%` |
| **7** | **B7** | Integração runtime da Fase 2 (`RunMode` Fase 2, `ProfitSplitter`, filtro ativo Nota C, escada `1,5% -> 2,0%` + `CVaR 99%`, Auto-Downgrade 30 trades) | `F1->F2` | `G` | Habilita a virada segura para Fase 2A/2B assim que os 5 requisitos de `F1->F2` derem `PASS` |
| **8** | **B8** | Avaliação do Degrau E (Breakeven `+1,2R` em velas `1m` sobre `K9.3`) e módulo WebSocket assíncrono `bookTicker` (soak test 14 dias) | `F2->F3` | `G` | Cumpre o duplo cadeado (estatístico + 14 dias de estabilidade) antes de cogitar `breakeven_enabled` |
| **9** | **B9** | Homologação de `sideways_pingpong` (Gate 3 + 30 shadow trades) e roteamento multi-estratégia por `net_expected_r` | `F2->F3` | `G` | Desbloqueia operação em clima `SIDEWAYS` após `>= 100` trades reais na Fase 2 |
| **10** | **B10** | Estudo ponto-no-tempo de `bear_capitulation_snap` incluindo ativos deslistados/colapsados | `F2->F3` | `G` | Comprova ou arquiva definitivamente via ADR a quarentena do regime `BEAR` |

---

### Detalhamento dos Itens

#### B1 — Persistência de telemetria operacional (`system_state` / `regime_history`), alertas preventivos de BNB/taxa e rotação de backups locais

* **Estado atual verificado:**
  * `src/main.py:303-338` (`_get_live_balances`) e `src/main.py:518-554` consultam `free_usdt`, `eq_usdt` e `bnb_usdt` apenas em memória; nunca persistem `current_equity_usdt`, `initial_tactical_equity_usdt` nem `bnb_free_usdt_equiv` em `system_state`. Por isso, `tools/gate_report.py:835-841,1080-1135` retorna `UNKNOWN` em KS-1, KS-2 e Req. 3 (MaxDD).
  * `src/main.py:534` lê `system_state["shadow_virtual_equity_usdt"]`, mas `src/execution/paper_executor.py:173-198` nunca atualiza essa chave ao fechar um `shadow_trade`.
  * `ShadowManager.evaluate_defense()` calcula `bnb_below_recommended_reserve` (`< 5,00 USDT`) e `commission_rate_valid` (`<= 0,10%`) em `src/risk/shadow_manager.py:263-264,328-329`, mas `src/main.py:539-554` ignora ambos os campos e não emite alerta Telegram.
  * A tabela `regime_history` é criada em `src/core/database.py:74-85`, porém nunca recebe `INSERT` em `src/main.py`.
  * `Database.backup_to()` (`src/core/database.py:713-724`, chamado em `src/main.py:913`) cria backups diários em `BACKUP_DIR` sem política de retenção/limpeza.
* **Objetivo:** Persistir `current_equity_usdt`, `initial_tactical_equity_usdt` (uma única vez no primeiro ciclo flat do ambiente), `bnb_free_usdt_equiv`, `shadow_virtual_equity_usdt` e snapshots de `regime_history`; enfileirar alertas Telegram com *rate-limit* (ex.: 1x/dia) quando `bnb_below_recommended_reserve` ou `not commission_rate_valid`; e aplicar rotação de backups locais em `BACKUP_DIR` (mantendo os últimos N arquivos verificados). Sem tocar em `config/strategies.yaml`.
* **Gate / Fase:** `6A` (preparação sem reiniciar o teste atual) e `6B`.
* **Teste ou métrica que prova que funcionou:** `pytest` validando (a) gravação das chaves em `system_state` e linhas em `regime_history`, (b) atualização de `shadow_virtual_equity_usdt` após fechamento de `shadow_trade`, (c) alerta Telegram em `BNB = 4,50 USDT` e em taxa taker `0,15%`, (d) poda determinística mantendo os N backups mais recentes, e (e) `tools/gate_report.py` reportando `PASS` (zero `UNKNOWN`) para KS-1 e KS-2 após 1 ciclo.
* **Esforço:** `P`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/main.py`, `src/execution/paper_executor.py`, `src/core/database.py`, `src/risk/shadow_manager.py`, `tools/gate_report.py`.

---

#### B2 — Contabilidade líquida de taxas (`fees_usdt_equiv`) e medição real de slippage por profundidade no Gate 6B

* **Estado atual verificado:**
  * A coluna `trades.fees_usdt_equiv` existe no schema (`src/core/database.py:223`), mas nunca é preenchida ao fechar trades em `src/execution/oco_order_manager.py:1211-1235,1775-1797` nem em `src/core/state_reconciler.py:977-1002`. Como taxas pagas em BNB não saem de `exit_quote_received`, `realized_pnl_usdt` e `realized_r` ficam **brutos de taxas BNB**, e `tools/gate_report.py:724-725` deduz `Decimal("0")`.
  * No modo `SHADOW_CHAMELEON`, `src/execution/paper_executor.py:77-80` grava apenas o topo de livro (`get_book_ticker().ask`) e adiciona meia fração fixa (`stress_slippage_assumption_pct / 2`), sem medir o impacto real no livro de ofertas para a quantidade `base_qty` nem registrar uma métrica direta de slippage de entrada.
* **Objetivo:** (1) Ao fechar trades `LIVE`, apurar a comissão total de entrada + saída a partir de `fills`/`myTrades` (`src/core/binance_client.py:801-820`), converter comissões em BNB/ativo base para USDT no momento do fill, persistir `fees_usdt_equiv` e computar `realized_pnl_usdt` e `realized_r` líquidos de taxas; (2) no `PaperExecutor` (`SHADOW_CHAMELEON`), consultar `get_depth(symbol, limit=20)` (ou `bookTicker` com fallback seguro) para calcular o preço VWAP real para `base_qty` e registrar o slippage efetivo em bps frente ao `candles(1h).close` sem alterar a lógica de decisão nem `config/strategies.yaml`.
* **Gate / Fase:** `6B` (critério `slippage <= 0,08%`) e pré-requisito para evitar contabilidade inflada em `F1->F2` (Req. 2 e Req. 3).
* **Teste ou métrica que prova que funcionou:** `pytest` com fills mockados pagando comissão em `BNB`, `USDT` e ativo base validando `fees_usdt_equiv` e `realized_pnl_usdt` líquido com erro `<= 0,01 USDT`, somado a teste no `PaperExecutor` com livro sintético de 3 níveis validando cálculo de VWAP/slippage e leitura limpa pelo `tools/gate_report.py`.
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/execution/oco_order_manager.py`, `src/core/state_reconciler.py`, `src/execution/paper_executor.py`, `src/core/binance_client.py`, `src/core/database.py`, `tools/gate_report.py`.

---

#### B3 — Observabilidade ativa: Scorecard semanal do Radar, alerta de disco da VPS e resiliência do batch `exchangeInfo`

* **Estado atual verificado:**
  * `VirtualAuditor.generate_weekly_scorecard_markdown()` (`src/auditing/virtual_auditor.py:284-318`) está implementado e `config/analyst.yaml:82` define `send_weekly_scorecard_telegram: true`, mas o método nunca é chamado em `src/main.py`.
  * Não há monitoramento de espaço livre em disco da VPS no processo (o rito de `< 70%` em `docs/methodology-and-roadmap.md:178` depende só de inspeção manual).
  * `PublicBinanceClient.get_exchange_info()` (`src/core/binance_client.py:584-627`) solicita todos os 16 símbolos juntos em `?symbols=[...]`. Se qualquer altcoin exclusiva do Radar (ex.: `APTUSDT`, `ARBUSDT`, `OPUSDT`, `SUIUSDT` em `config/analyst.yaml:22-26`) for deslistada, a Binance devolve `-1121 Invalid symbol` para o lote inteiro, gerando `unhandled += 1` em `src/main.py:794-804` e congelando a atualização de `tickSize`/`stepSize`/`status` dos 7 pares do Chameleon (ou derrubando `boot_reconcile()` em `src/main.py:207`).
* **Objetivo:** (a) Agendar em `src/main.py` o envio semanal idempotente (chave `last_weekly_scorecard_ms` em `system_state`) de `generate_weekly_scorecard_markdown()` via `TelegramOutbox` respeitando `analyst.virtual_auditor.send_weekly_scorecard_telegram`; (b) verificar `shutil.disk_usage(db_path.parent)` no backup diário e alertar no Telegram se uso `>= 70%` (e reportar status de offsite backup + heartbeat); (c) fazer fallback em `ExchangeInfoCache.refresh_if_stale()` para consulta isolada por símbolo (priorizando `execution_universe` antes do Radar) em caso de `-1121`, alertando o símbolo inválido sem contaminar os 7 pares executáveis.
* **Gate / Fase:** `6B` (30 dias ininterruptos com os 15 símbolos do Radar em produção pública).
* **Teste ou métrica que prova que funcionou:** `pytest` simulando (a) virada de 7 dias com envio exato de 1 scorecard (sem duplicar em restart), (b) mock de disco a `75%` gerando alerta `DISK_USAGE`, e (c) resposta `-1121` no batch de `exchangeInfo` recuperando os 7 símbolos de execução com `unhandled_errors == 0`.
* **Esforço:** `P`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/main.py`, `src/auditing/virtual_auditor.py`, `src/core/binance_client.py`, `src/notifications/telegram.py`, `config/analyst.yaml`.

---

#### B4 — Treinamento offline (Treino/Validação) e carga de artefato calibrado do `MLJudge` para o observador silencioso da Fase 1

* **Estado atual verificado (achado crítico):**
  * `src/main.py:141-144` instancia `MLJudge` com `calibrator=None`, ativando `PlattCalibrator(a=-1.0, b=0.0)` não treinado (`src/features/ml_judge.py:103,188`).
  * `extract_stationary_features()` (`src/features/ml_judge.py:40-62`) nunca é chamado em `src/main.py`.
  * `src/main.py:578-582` passa `raw_score=float(best_sig.rs_vol_adjusted_12h)` diretamente ao calibrador. Como toda entrada `K9.3` já exige `rs_12h >= 1.40` (`config/strategies.yaml:103`), `PlattCalibrator(a=-1.0, b=0.0).predict_proba(rs)` devolve `p_cal >= 0,802` para **100% dos trades**, gravando `ml_grade_silent = 'A+'` quase sempre e `n_C = 0` — o que torna **matematicamente impossível** aprovar o Requisito 5 de `F1->F2` (`tools/gate_report.py:933-956`: exige grupos `B/A+` e `C` com `ECE <= 0,05`)!
* **Objetivo:** Sem modificar `config/strategies.yaml` nem reabrir o Holdout: (1) construir script/notebook offline que treina o meta-classificador do `MLJudge` exclusivamente sobre o conjunto de Treino/Validação (`purge=24h`, `embargo=2%`, pesos `afml_mean_uniqueness`, `config/strategies.yaml:131-135`) usando `extract_stationary_features()` (e features causais de `K9.3`), ajusta `PlattCalibrator` fora da amostra e deriva os cortes reais `net_r_q33_threshold` e `net_r_q70_threshold`; (2) serializar um artefato determinístico e auditável (ex.: `models/ml_judge_bull_v1.json` com SHA-256, coeficientes do modelo linear/árvore leve, `a`, `b`, `ece_oos`, `q33`, `q70` — compatível com `mem_limit: 512m` da ADR-015); (3) carregar esse artefato em `src/main.py` **mantendo `use_ml_filter: false`** (observador 100% silencioso na Fase 1) e gravar também `ml_p_cal_silent` em `shadow_trades` (hoje só `trades` tem `ml_p_cal_silent`, `src/core/database.py:182,227-228`).
* **Gate / Fase:** Preparar durante o `6B` (antes do dia 1 da Fase 1) para desbloquear `F1->F2` (Requisito 5).
* **Teste ou métrica que prova que funcionou:** (a) Na validação Walk-Forward (sem Holdout), `ECE_val <= 0,05` e separação positiva `mean(R | B/A+) > mean(R | C)` com fração não-nula de Nota `C` (`~25%–40%`); (b) teste unitário confirmando que `QuantOrchestrator` carrega o artefato, chama `extract_stationary_features()`, popula `ml_grade_silent` e `ml_p_cal_silent` com variedade de notas `C`/`B`/`A+` e continua sem bloquear nenhuma ordem na Fase 1 (`use_ml_filter == False`).
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/features/ml_judge.py`, `src/main.py`, `src/risk/position_sizer.py`, `src/backtest/ablation_and_dsr.py`, `src/core/database.py`, `tools/gate_report.py`, `notebooks/02_ablation_and_dsr_sim.ipynb`.

---

#### B5 — Uso dos dados do Radar (`virtual_signals`, `orderbook_snapshots`, `orderbook_walls`) para auditar L4 e validar o Juiz de IA

* **Estado atual verificado:**
  * O sistema já coleta e resolve automaticamente `virtual_signals`, `orderbook_snapshots` (3 fotos intercaladas com `spread_bps`, `band_eff_pct`, `obi`, `impact_bps_ref`) e `orderbook_walls` (`src/core/database.py:88-159`, `src/auditing/virtual_auditor.py:35-282`), além de vincular silenciosamente `trades.radar_signal_id` e `shadow_trades.radar_signal_id` após cada entrada (`src/main.py:723-757,895-902`).
  * Porém não existe nenhuma ferramenta analítica que consuma essas tabelas para responder: (i) o Radar L4 (`ob_valid=1`, `ob_obi_mean`, `ob_ask_wall_before_t2`) tem valor preditivo real fora da amostra?; (ii) sinais em que Chameleon e Radar concordam (`radar_signal_id IS NOT NULL`) performam melhor?; (iii) o dataset de `virtual_signals` pode servir como laboratório de calibração/validação do `MLJudge`?
* **Objetivo:** Criar uma ferramenta de análise somente-leitura (`URI mode=ro`, ADR-002), ex.: `tools/radar_ai_audit.py` + relatório/notebook, que:
  1. **Audita a Camada 4 isoladamente** em `virtual_signals` (excluindo `OPEN` e `INVALID_GAP`): compara `outcome_r_net`, `mfe_r`, `mae_r` e taxa de `T1`/`T2`/`STOP` estratificados por `grade` (`B`/`A`/`A+`), `score_l1+l2+l3` vs. `score_l4`, `ob_valid`, quartis de `ob_obi_mean`, presença de `ob_ask_wall_before_t2` e `spread_bps`/`impact_bps_ref` (com IC 95% via *Block Bootstrap*).
  2. **Avalia convergência Chameleon × Radar** via `LEFT JOIN` de `shadow_trades`/`trades` com `virtual_signals` (via `radar_signal_id` e também por `(symbol, signal_close_time_ms)`), medindo se a ausência ou presença de sinal Radar (ou *Ask Wall* próxima) separa ganhadores de perdedores.
  3. **Valida a hipótese de uso de features L4 no `MLJudge` (somente pesquisa offline):** quando houver amostra mínima (`>= 150` sinais resolvidos no universo de 7 pares com `ob_valid = 1`), testa *Purged Walk-Forward* comparando `MLJudge` **somente com as 5 features OHLCV estacionárias** vs. **5 features + agregados L4 (`ob_obi_mean`, `spread_bps`, `impact_bps_ref`, `ob_ask_wall_before_t2`)**. Mantém rigorosamente `affects_execution: Literal[False]` (ADR-004): qualquer promoção de feature L4 exigirá novo ADR explícito após `F1->F2`.
  4. **Regra metodológica anti-vazamento:** jamais usar `virtual_signals` (que têm setups/stops/horizontes distintos de `K9.3` — `24h`, alvos `1,5R/2,5R`, `config/analyst.yaml:74-77`) como rótulo direto de treino para substituir trades de `K9.3`; usá-los apenas para (a) validar utilidade de L4, (b) estudo de microestrutura/slippage por par, e (c) *feature screening* auxiliar.
* **Gate / Fase:** Coleta contínua durante `6B` e Fase 1; decisão científica antes de `F1->F2` (e eventual ADR na Fase 2B/3).
* **Teste ou métrica que prova que funcionou:** `pytest` com banco SQLite sintético comprovando abertura `mode=ro`, cálculo correto de ablação `L1-L3` vs. `L1-L4`, cálculo de `ECE` e `ΔE[R]` com bootstrap, e invariante `affects_execution: false` preservada. Em dados reais, exigência de `n >= 150` sinais válidos e `CI95_lo(ΔE[R]) > 0` para considerar L4 promissora.
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/core/database.py`, `src/auditing/virtual_auditor.py`, `src/radar/orderbook_inspector.py`, `src/radar/confluence_scorer.py`, `src/features/ml_judge.py`, `tools/gate_report.py`, `docs/radar.md`, `docs/decisions.md`.

---

#### B6 — Watchdog sub-horário para Stop Pendurado (`STOP_LOSS_LIMIT`)

* **Estado atual verificado:**
  * `OcoOrderManager.check_hanging_stop_watchdog()` (`src/execution/oco_order_manager.py:1354-1627`) já implementa cancelamento seguro + venda `MARKET` limitada ao rastreado (`C1-D`), mas `src/main.py:455-469,790,1034` só o chama **1 vez por hora** no fechamento da vela.
  * Entre ciclos horários, `CandleCollector.wait_for_cycle()` (`src/data/candle_collector.py:154-167`) dorme até `XX:00:03 UTC` sem vigiar `ticker.bid < oco_stop_limit_price`.
* **Objetivo:** Substituir o `sleep` monolítico de até 3600 s em `src/main.py:1034` (quando existir trade `PROTECTED` em modo `LIVE`) por um loop de espera fracionado (ex.: intervalos de 15–30 s na própria thread principal, respeitando ADR-002 single-writer e `SIGTERM` rápido!) que consulta `check_hanging_stop_watchdog()` (peso 2 em `GET /api/v3/ticker/bookTicker`, `src/core/binance_client.py:27`, ~4–8 peso/min de 6000) e aciona a liquidação a mercado em segundos caso o preço pule o `oco_stop_limit_price`. Sem alterar `config/strategies.yaml` nem a geometria da OCO.
* **Gate / Fase:** Desenvolver durante `6B` para proteger a Fase 1 (ou entre `6A` e `6B`, nunca durante um gate em curso); reduz risco de cauda até `F1->F2`.
* **Teste ou métrica que prova que funcionou:** Teste de injeção de falha simulando salto abaixo de `oco_stop_limit_price` no meio da hora provando disparo de `STOP_WATCHDOG` em `<= 30 s`, zero chamadas a `bookTicker` quando não há posição aberta, respeito ao `SIGTERM` sem travar 1h e zero violações de `WrongThreadError`.
* **Esforço:** `M`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/main.py`, `src/data/candle_collector.py`, `src/execution/oco_order_manager.py`, `src/core/binance_client.py`, `src/core/database.py`.

---

#### B7 — Integração runtime da Fase 2 (`RunMode` Fase 2, `ProfitSplitter`, filtro ativo Nota C, escada `1,5% -> 2,0%` + `CVaR 99%` e Auto-Downgrade de 30 trades)

* **Estado atual verificado:**
  * `RunMode` (`src/core/config.py:34-37`) possui apenas `ANALYST_ONLY`, `SHADOW_CHAMELEON` e `LIVE_CHAMELEON_PHASE_1`; `validate_mode_invariants()` (`src/core/config.py:506-513`) bloqueia todas as flags de Fase 2.
  * `ProfitSplitter` (`src/risk/profit_splitter.py:32-153`) existe e tem testes unitários, mas **não é importado nem chamado** em `src/main.py`, `src/core/state_reconciler.py` ou `src/execution/oco_order_manager.py`.
  * `PositionSizer` em `src/main.py:132-135` é instanciado sem `use_ml_filter`, e `src/main.py:585-597` não passa `portfolio_cvar99_loss_pct` nem `phase2b_graduation_stage` (cujo default em `src/risk/position_sizer.py:123` é `2` — o que pularia o degrau intermediário de `1,5%` da Fase 2B direto para `2,0%`!).
  * `ShadowManager.evaluate_phase_downgrade()` (`src/risk/shadow_manager.py:190-225`) nunca é chamado em `src/main.py`, deixando a regra de rebaixamento automático da Fase 2 (`mean_R_30 < 0,0R` ou `ECE_30 > 0,08`, [Metodologia §5.1](methodology-and-roadmap.md)) desconectada.
* **Objetivo:** Implementar a infraestrutura de código para Fase 2 **sem alterar `config/strategies.yaml` agora**:
  1. Novo `RunMode.LIVE_CHAMELEON_PHASE_2` em `src/core/config.py` com trava dedicada (`LIVE_CONFIRMATION` específica da Fase 2 + `APPROVED_STRATEGIES_SHA256`), mantendo `LIVE_CHAMELEON_PHASE_1` intocado.
  2. Conexão idempotente de `ProfitSplitter.on_trade_closed()` no fechamento de trades (persistindo `last_profit_split_trade_id` em `system_state` para evitar transferência `MAIN_FUNDING` duplicada em crash/restart) e notificação Telegram.
  3. Conexão de `ShadowManager.evaluate_phase_downgrade()` após cada fechamento (lendo últimos 30 trades `CLOSED` e `ECE_30`), respeitando `defense.conviction_downgraded` no `PositionSizer` e notificando Telegram.
  4. Cálculo empírico de `portfolio_cvar99_loss_pct` sobre o histórico real + controle explícito de `phase2b_graduation_stage` (`1` = até `1,5%` nos primeiros 30 trades da Fase 2B; `2` = até `2,0%` após +30 trades com `mean(R) > +0,15R`).
* **Gate / Fase:** `F1->F2` (código pronto antes do trade #60 da Fase 1; ativação somente após `tools/gate_report.py` retornar `PASS` nos 5 requisitos de `phase_1_to_2`).
* **Teste ou métrica que prova que funcionou:** Suíte `pytest` cobrindo (a) Fase 2A bloqueando Nota `C` e transferindo `MAIN_FUNDING` acima do HWM com idempotência em restart; (b) Fase 2B travando em `1,5%` no estágio 1 e reduzindo para Nota `B` se `CVaR 99% > 5,0%`; (c) auto-downgrade disparando em `mean_R_30 < 0` ou `ECE_30 > 0,08`; (d) zero regressão nos testes existentes da Fase 1.
* **Esforço:** `G`
* **Contexto mínimo para IA (arquivos a ler):** `AGENTS.md`, `src/core/config.py`, `src/main.py`, `src/risk/profit_splitter.py`, `src/risk/position_sizer.py`, `src/risk/shadow_manager.py`, `src/core/state_reconciler.py`, `src/execution/oco_order_manager.py`, `docs/methodology-and-roadmap.md`.

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
| **#1** | Mercado / Portfólio | Queda de 70%–90% da Carteira Core (`150 XRP`) + concentração dobrada quando o bot compra `XRPUSDT` | 4 | 5 | **20** | **Nenhuma** (para risco de mercado) |
| **#2** | Modelo / Mercado | Overfitting residual (`HoldSum` observado em `K1`–`K9`) e viés de sobrevivência dos 7 pares | 4 | 4 | **16** | Parcial (CPPI `-4%`, Hard Stop `-12%`, Gate 6B) |
| **#3** | Mercado | Mudança estrutural de regime (*bear* prolongado / *whipsaws* de falsos rompimentos em 4h) | 4 | 4 | **16** | Parcial (MECE `BEAR` 1º, veto BTC, CPPI, `-12%`) |
| **#4** | Execução / Técnica | Flash-crash saltando buffer `2,5%` do `STOP_LOSS_LIMIT` com Watchdog apenas horário (~59 min exposto) | 3 | 5 | **15** | Parcial (buffer `2,5%`, teto caixa `50%`, watchdog 1h) |
| **#5** | Execução / Microestrutura | Slippage, spread e taxas reais na virada de hora (`XX:00:03 UTC`) acima do backtest + `fees_usdt_equiv` nulo | 3 | 4 | **12** | Parcial (`slippage_entry_bps`, C2-9; falta taxa/saída) |
| **#6** | Externa / Contraparte | Dependência única da Binance (contraparte/regulatório, manutenção sem aviso de 2h, API/delisting, depeg `USDT`) | 2 | 5 | **10** | Parcial (fail-closed manutenção, preflight OCO; **nenhuma** para contraparte/USDT/2h) |
| **#7** | Execução / Técnica | HTTP `418` (IP ban) ou queda de rede/VPS entre fill e OCO (ou após cancelar OCO), deixando posição nua | 2 | 5 | **10** | Parcial (`PENDING_ENTRY`, `critical_section`, reconciler; **nenhuma** sob ban 418 ativo) |
| **#8** | Operacional / Humana | Intervenção humana por tédio/pânico, trade manual na conta API (C1-D), offsite/heartbeat desligados ou BNB esgotado | 3 | 3 | **9** | Parcial (`HALT_REVIEW` C1-D, SHA-256, BNB `< 2,00`; falta alerta BNB `< 5,00`) |
| **#9** | Técnica / Governança | Cegueira de degradação e contabilidade parcial (`MLJudge` não treinado, `fees_usdt_equiv` nulo, `evaluate_phase_downgrade` desconectado) | 3 | 3 | **9** | Parcial (`gate_report.py` usa `UNKNOWN`; **nenhuma** para PnL bruto ou IA não treinada) |
| **#10** | Infraestrutura / Técnica | Falha de VPS: disco cheio por backups sem rotação, OOM (`> 512m`), drift de relógio ou perda de disco sem offsite | 2 | 4 | **8** | Parcial (WAL, `HEALTHCHECK`, log rotation; **nenhuma** para rotação de `BACKUP_DIR`) |

---

### Análise Cética Detalhada

#### #1 — Queda severa (70%–90%) da Carteira Core (`150 XRP`) e concentração dobrada em `XRPUSDT` (`P=4 × I=5 = 20`)

* **Como o dinheiro é perdido:** Metade do capital total do projeto (`150 XRP`, `config/strategies.yaml:9-10`) fica em exposição direcional pura, sem stop, em uma única altcoin. Mesmo se o Chameleon tático tiver desempenho exemplar, um ciclo de baixa severo de `-75%` no XRP apaga `-37,5%` do patrimônio total. Além disso, `XRPUSDT` faz parte da cesta de execução tática (`config/strategies.yaml:73`): durante um topo local em `BULL`, o bot pode alocar até `50%` da carteira tática em `XRPUSDT` (`config/strategies.yaml:36`), concentrando temporariamente ~75% do patrimônio total no mesmo ativo.
* **Premissa não provada na documentação:** `docs/architecture.md:25` afirma que *"o risco global é monitorado pelo Value at Risk (VaR) e Conditional VaR (CVaR a 99%) do portfólio combinado"*. **No código de produção, isso não existe:** `src/main.py:585-597` não calcula VaR/CVaR combinado nem passa `portfolio_cvar99_loss_pct` ao `PositionSizer`.
* **Mitigação existente no código:** **`nenhuma`** contra risco de mercado do Core (o isolamento físico citado em `config/strategies.yaml:10` e `docs/architecture.md:24` protege apenas contra erro operacional do bot, não contra queda do preço do XRP).
* **O que mediria o risco:** Drawdown marcado a mercado do patrimônio consolidado (`150 * preço_XRP + USDT_tático + BNB`), percentual de trades táticos alocados em `XRPUSDT` e CVaR 99% empírico combinado (Core + Tático).

---

#### #2 — Overfitting residual (`HoldSum` observado em `K1`–`K9`) e viés de sobrevivência da cesta (`P=4 × I=4 = 16`)

* **Como o dinheiro é perdido:** `K9.3` exibe `E[R]_net = +0,230R`, `PF_base = 1,43` e `DSR = 0,96` (`docs/block-3-spec.md:350`), porém o próprio `docs/methodology-and-roadmap.md:57` reconhece que `HoldSum` foi observado durante as rodadas `K1`–`K9`. Na prática, `docs/block-3-spec.md:309-354` documenta explicitamente ajustes iterativos (`K5` → `K6` → `K7` → `K8` → `K9`) após o raio-X do Holdout negativo (`-6,58R`) até chegar a `+5,80R`, contabilizando múltiplas sub-variantes (`K3.1..K9.6`) como apenas `K = 9` baterias no cálculo do `DSR` (`src/backtest/ablation_and_dsr.py:226-237`). Soma-se o viés de sobrevivência retrospectivo de fixar em 2026 os 7 sobreviventes líquidos de 2022–2026 (`config/strategies.yaml:70-76`). Se a vantagem real fora da amostra for próxima de zero ou negativa após custos, a carteira tática sangra gradualmente até o hard stop de `-12%`.
* **Premissa não provada na documentação:** Que `K = 9` representa o número real de graus de liberdade explorados e que o Holdout (`+5,80R`) é livre de viés de seleção.
* **Mitigação existente no código:** CPPI reduz risco em 50% a partir de `-4,0%` abaixo do HWM (`src/risk/shadow_manager.py:305-315`); Hard Stop Patrimonial dispara `EMERGENCY_HALT` em `-12,0%` (`src/risk/shadow_manager.py:26,288-291`); e o Gate 6B funciona como Holdout Prospectivo (`tools/gate_report.py:426-630`). *(Nota: `ShadowManager.evaluate_phase_downgrade()` em `src/risk/shadow_manager.py:190-225` não é chamado em `src/main.py`.)*
* **O que mediria o risco:** `mean(realized_r)` líquido acumulado e estatística CUSUM no Gate 6B e nos primeiros 30–60 trades da Fase 1 comparados à banda inferior do IC 95% do Bloco 3 (`+0,068R`, `docs/block-3-spec.md:350`), mais `Profit Factor` líquido em janela móvel.

---

#### #3 — Mudança estrutural de regime (*bear market* prolongado ou *whipsaws* de rompimento falso) (`P=4 × I=4 = 16`)

* **Como o dinheiro é perdido:** A Fase 1 executa exclusivamente `bull_trend_pullback` em modo `K9.3` (rompimento comprador Donchian `12h` + `RS >= 1,40` com stop `3,0 * ATR_14`, `src/strategies/bull_trend_pullback.py:27-33`). Em um regime prolongado de baixa pontuado por *bear-market rallies* violentos, a `EMA_100` de `4h` (~16,7 dias) vira positivamente tarde demais: o detector classifica `BULL` próximo ao topo do repique (`src/regimes/regime_detector.py:97-98`), o bot compra rompimentos esticados e sofre stops consecutivos na reversão. Alternativamente, em um ano inteiro de lateralização/queda sem tendências sustentadas de `72h`, o custo de oportunidade + falsos rompimentos corroem o caixa até `-4%`/`-12%`.
* **Premissa não provada na documentação:** Que a frequência de tendências limpas de `72h` observada em 2022–2026 (`~1,3 trade/semana`) se manterá e que `veto_if_btc_bear` impede *bull traps* quando o próprio BTC ainda está acima da `EMA_100_4h`.
* **Mitigação existente no código:** Precedência `BEAR` antes de `BULL` no MECE 4h (`src/regimes/regime_detector.py:93-98`), filtro `CHOP_14_4h < 50` (`L97`), veto `btc_regime_4h == BEAR` (`src/strategies/bull_trend_pullback.py:155-156`), filtro `ADX_14_4h >= 17,0` (`L158-160`), CPPI em `-4%` (`src/risk/shadow_manager.py:305-315`) e Hard Stop em `-12%` (`L288-291`).
* **O que mediria o risco:** Taxa de *false breakouts* (`exit_reason == 'STOP'` ocorridos em `< 24h` da entrada), distribuição de tempo gasto em cada regime 4h (hoje não persistida pois `regime_history` em `src/core/database.py:74-85` nunca recebe escritas), e soma móvel de `realized_r` nos últimos 10–20 trades.

---

#### #4 — Flash-crash saltando o buffer de `2,5%` da perna `STOP_LOSS_LIMIT` com Watchdog apenas horário (`P=3 × I=5 = 15`)

* **Como o dinheiro é perdido:** A proteção OCO usa `STOP_LOSS_LIMIT` com limite `2,5%` abaixo do `stopPrice` (`config/strategies.yaml:27-28`, `src/execution/oco_order_manager.py:134-139`). Em quedas bruscas por cascata de liquidações em futuros (evento recorrente em `SOL`, `XRP`, `ADA`, `LINK` e `AVAX`), o preço aciona o `stopPrice` e atravessa o `stopLimitPrice` em segundos sem preencher a ordem limite. Como `check_hanging_stop_watchdog()` (`src/execution/oco_order_manager.py:1354-1627`) só executa dentro do ciclo horário `run_once()` (`src/main.py:455-469,790,1034`), a posição pode sangrar **por até ~59 minutos** antes de ser liquidada a mercado. Com teto de alocação de `50%` do caixa (`config/strategies.yaml:36`), uma queda de `-20%` nesse intervalo gera perda de **`-10%` da carteira tática em um único trade** (10x o risco planejado de `1%`).
* **Premissa não provada na documentação:** O simulador do Bloco 3 (`src/backtest/causal_simulator.py:108-112`) aplica no cenário `PESSIMISTIC` um `gap_shock` fixo de apenas `0,15%` (`0.0015`), nunca modelando salto `> 2,5%` com latência de 1 hora no watchdog.
* **Mitigação existente no código:** Buffer de `2,5%` em `compute_oco_leg_prices()` (`src/execution/oco_order_manager.py:134-139`), teto de caixa de `50%` (`src/risk/position_sizer.py:276,286`) e `check_hanging_stop_watchdog()` no ciclo horário (`src/main.py:456`, `src/execution/oco_order_manager.py:1354-1627`). **Nenhuma mitigação sub-horária existe no código atual.**
* **O que mediria o risco:** Qualquer ocorrência de `exit_reason == 'STOP_WATCHDOG'` em `trades`, o desvio percentual `(oco_stop_price - exit_avg_price) / oco_stop_price` em todos os stops fechados (slippage de saída — hoje sem coluna própria), e o atraso entre o candle `1m` que cruzou `oco_stop_limit_price` e `trades.closed_at_ms`.

---

#### #5 — Divergência de slippage, spread e taxas reais frente às premissas do backtest (`P=3 × I=4 = 12`)

* **Como o dinheiro é perdido:** A entrada em `LIVE` envia `MARKET BUY` (`src/execution/oco_order_manager.py:492-496`) após o fechamento da vela `1h` (`XX:00:03 UTC`, `config/strategies.yaml:24`, mais latência de coleta de 16 ativos em `src/main.py:813`), comprando exatamente **rompimentos com volume explosivo** (`RVOL >= 1,30`, `Donchian 12h`, `corpo >= 0,65 * ATR`) no segundo em que bots de rompimento disputam a mesma liquidez e spreads abrem. O backtest pressupõe `0,06%` de slippage base (`0,12%` pessimista) e `0,15%` de taxa com desconto BNB (`config/strategies.yaml:41-42`, `src/backtest/causal_simulator.py:104-110`). Em produção, dois agravantes amplificam o risco: (a) se a opção *"Use BNB to pay fees"* estiver desativada ou falhar, a taxa sobe para `0,20%`, e `src/main.py:539-554` **não alerta** quando `defense.commission_rate_valid == False` (`src/risk/shadow_manager.py:264,329`); (b) `trades.fees_usdt_equiv` nunca é preenchida (`src/execution/oco_order_manager.py:1211-1222`, `src/core/state_reconciler.py:979-990`), escondendo a erosão de taxas no SQLite.
* **Premissa não provada na documentação:** Que o slippage real somado (entrada + saída em stop/time-stop) em altcoins às `XX:00:03+ UTC` fica dentro de `0,06%–0,12%`, e que o `PaperExecutor` usando apenas top-of-book `ask` (`src/execution/paper_executor.py:77-80`) representa bem o impacto real da ordem.
* **Mitigação existente no código:** Ordem de execução C2-9 rodando Chameleon antes da L4 (`src/main.py:881-894`), registro de `trades.slippage_entry_bps` (`src/execution/oco_order_manager.py:655-680`), auditoria de slippage em `tools/gate_report.py:530-593,865-912` e checagem parcial `verify_commission_payload()` (`src/risk/shadow_manager.py:228-245`, porém sem alerta no orquestrador). **Nenhuma mitigação grava slippage de saída nem `fees_usdt_equiv` hoje.**
* **O que mediria o risco:** Mediana, p90 e média de `trades.slippage_entry_bps` (alerta se `> 8 bps`), slippage de saída nos stops/time-stops (`(oco_stop_price - exit_avg_price) / oco_stop_price`), monitoramento de `commission_rate_valid` e reconciliação periódica do saldo real `USDT + BNB` vs. soma de `realized_pnl_usdt`.

---

#### #6 — Dependência concentrada na Binance: contraparte, manutenção sem aviso de 2h, mudança de API/delisting e depeg de `USDT` (`P=2 × I=5 = 10`)

* **Como o dinheiro é perdido:** Todo o capital tático + reserva BNB reside em uma única corretora e um único endpoint hardcoded (`https://api.binance.com`, sem failover para `api1..4.binance.com`, `src/core/config.py:25,392,401`). Quatro cenários críticos:
  1. **Contraparte / regulatório / bloqueio de conta:** insolvência, restrição jurisdicional ou congelamento de conta Spot indisponibiliza 100% da carteira tática.
  2. **Manutenção sem aviso prévio de 2h:** `docs/methodology-and-roadmap.md:72` afirma *"bloqueando entradas 2h antes"*, porém `SignedBinanceClient.sapi_system_status()` (`src/core/binance_client.py:833-845`) e `src/main.py:274-285` leem apenas o binário `status == 0` vs. `1` (e a Testnet retorna `mocked=True`). O bot pode abrir uma posição minutos antes de uma parada de manutenção e sofrer gap severo na reabertura.
  3. **Mudança de contrato API ou deslistagem:** `get_exchange_info()` busca todos os 16 símbolos em lote (`src/core/binance_client.py:587-595`); deslistagem de um único par Radar quebra o lote com `-1121` (`src/main.py:794-804`). `oco_allowed` assume default `True` se o campo sumir (`src/core/binance_client.py:624`), e filtros `MARKET_LOT_SIZE` / `PERCENT_PRICE_BY_SIDE` não são verificados (`src/core/binance_client.py:604-615`).
  4. **Depeg de `USDT`:** Em `BEAR` e `TRANSITION`, 100% da defesa fica estacionada em `USDT` (`config/strategies.yaml:11`). Uma desancoragem aguda da stablecoin atinge o caixa inteiro de uma vez.
* **Premissa não provada na documentação:** Que `/sapi/v1/system/status` antecipa manutenções em 2h, que `USDT` é livre de risco e que os contratos `/api/v3/orderList/oco` e `/api/v3/exchangeInfo` permanecem estáveis.
* **Mitigação existente no código:** Fail-closed quando `sapi_system_status()` falha (`src/main.py:280-285`), pré-validação `status == TRADING` e `oco_allowed` antes da compra (`src/execution/oco_order_manager.py:389-410`), fallback para filtros em cache se `exchangeInfo` falhar (`src/main.py:797-802`), e Core fora da chave Spot (`config/strategies.yaml:10`). **Nenhuma mitigação existe para risco de contraparte, depeg de `USDT`, failover de host ou janela antecipada de 2h.**
* **O que mediria o risco:** Taxa de erros HTTP 4xx/5xx em `order_events`, alertas de falha em `sapi_system_status` / `exchangeInfo`, monitoramento externo de *peg* `USDT/USD` (`< 0,995`) e revisão periódica do changelog da Binance API ([Runbook](runbook.md)).

---

#### #7 — HTTP `418` (IP ban) ou queda de rede/VPS entre fill da compra e criação da OCO, deixando posição nua (`P=2 × I=5 = 10`)

* **Como o dinheiro é perdido:** Se a Binance retornar HTTP `418` (possível em VPS com IP compartilhado/reutilizado ou bug de rajada) logo **após** `POST /api/v3/order` executar e **antes** de `POST /api/v3/orderList/oco`, `WeightGovernor.on_rate_limited(..., banned=True)` (`src/core/binance_client.py:212-213,466-472`) bloqueia localmente **todas** as requisições — inclusive `Priority.CRITICAL` (`src/core/binance_client.py:150-153`) — durante `Retry-After`. `OcoOrderManager._handle_ip_ban()` (`src/execution/oco_order_manager.py:258-291,985-987`) aciona `emergency_halt` e envia alerta `KILL_SWITCH`, mas **a posição permanece sem stop na corretora** até ação humana ou expiração do ban. Falha análoga ocorre se a VPS perder conectividade após cancelar a OCO no passo 1 do Time-Stop (`src/execution/oco_order_manager.py:1657`) ou do Watchdog (`src/execution/oco_order_manager.py:1426`) antes da venda `MARKET`.
* **Premissa não provada na documentação:** Que o operador lerá o alerta Telegram em poucos minutos (inclusive de madrugada) e terá acesso imediato à Binance para proteger ou liquidar a posição manualmente.
* **Mitigação existente no código:** `PENDING_ENTRY` gravado antes do POST (`src/execution/oco_order_manager.py:451-486`), `governor.critical_section()` (`src/core/binance_client.py:155-159,225`, `src/execution/oco_order_manager.py:489`), escritas pós-fill *best-effort* (`src/execution/oco_order_manager.py:185-199`), recuperação automática no boot para `PENDING_ENTRY`/`ENTRY_FILLED`/`CLOSING` (`src/core/state_reconciler.py:398-520,643-710`), alerta `KILL_SWITCH` em HTTP 418 (`src/execution/oco_order_manager.py:258-291`) e heartbeat opcional `HEALTHCHECK_URL` (`src/main.py:970-976`, `src/core/healthcheck.py:21-54`). **Nenhuma mitigação de software consegue colocar a OCO enquanto o IP estiver banido (418) ou a VPS estiver sem rede.**
* **O que mediria o risco:** `cycle_health.used_weight_1m_peak` (alerta preventivo se `> 3000` de 6000), qualquer registro em `order_events` com `http_status IN (429, 418)`, qualquer trade em `ENTRY_FILLED`/`CLOSING` por `> 60 s`, e disparo do dead-man switch externo (`HEALTHCHECK_URL`, que **vem vazio por padrão** em `.env.example:15`).

---

#### #8 — Erro operacional humano: intervenção por tédio/pânico, trade manual na conta API, proteções desligadas por padrão ou BNB sem recarga (`P=3 × I=3 = 9`)

* **Como o dinheiro é perdido:**
  1. **Intervenção emocional:** Com taxa de acerto típica de estratégia `2:1`, sequências de 5 a 7 stops consecutivos ou 20 dias sem sinal em `BEAR`/`TRANSITION` são normais. Fechar posições no pânico ou desligar o robô no fundo do drawdown cristaliza a perda e impede capturar os grandes *winners* de `+2,0R` que pagam a expectativa.
  2. **Trade manual na mesma conta Spot da chave de API:** Qualquer saldo excedente `>= minNotional` em um dos 7 pares dispara `HALT_REVIEW` (C1-D, `src/core/state_reconciler.py:562-614`), paralisando o robô e podendo induzir erros manuais durante o destravamento.
  3. **Subir produção com defaults inseguros:** Em `.env.example:15-22`, tanto `HEALTHCHECK_URL` quanto `OFFSITE_BACKUP_ENABLED` vêm vazios/`false` por padrão; esquecer de configurá-los deixa produção sem dead-man switch e sem backup fora da VPS.
  4. **Esgotamento silencioso de BNB:** Como `src/main.py:539-554` não alerta quando `bnb_below_recommended_reserve` (`< 5,00 USDT`) ativa, o operador só é avisado quando o BNB cai abaixo de `2,00 USDT` (`EMERGENCY_HALT`, `src/risk/shadow_manager.py:292-295`) — ou depois de um trade já ter pago taxa no ativo base e acionado o fallback de quantidade estimada `-0,1%` (`src/execution/oco_order_manager.py:821-879`).
* **Premissa não provada na documentação:** Disciplina humana perfeita em períodos longos sem operação e execução pontual do rito mensal manual (`docs/methodology-and-roadmap.md:175-179`).
* **Mitigação existente no código:** Trava dupla de produção (`LIVE_CONFIRMATION` + `APPROVED_STRATEGIES_SHA256`, `src/core/config.py:523-535`), `HALT_REVIEW` C1-D nunca vende saldo manual (`src/core/state_reconciler.py:580-613`), `EMERGENCY_HALT` em `BNB < 2,00 USDT` (`src/risk/shadow_manager.py:292-295`), `OFFSITE_BACKUP_ENABLED=true` falha no boot sem `BACKUP_ENCRYPTION_KEY` (`src/core/config.py:111-119`), e procedimentos passo a passo em [Runbook Operacional](runbook.md). **Nenhuma mitigação impede `HEALTHCHECK_URL` vazio em produção nem envia alerta preventivo em `BNB < 5,00 USDT` no `src/main.py`.**
* **O que mediria o risco:** Checklist automatizado pré-produção validando `.env` (`HEALTHCHECK_URL` + `OFFSITE_BACKUP_ENABLED=true`), auditoria mensal de divergência entre histórico `myTrades` da Binance e tabela `trades` (detecta trades manuais), contagem de `HALT_REVIEW` e saldo `BNB` em USDT.

---

#### #9 — Cegueira de degradação e contabilidade distorcida por módulos parcialmente desconectados (`P=3 × I=3 = 9`)

* **Como o dinheiro é perdido:** Decisões ruins de gestão de capital (continuar operando estratégia degradada ou promover risco/Fase 2 prematuramente) causadas por painéis e defesas que a documentação descreve como ativas, mas que estão **incompletas no código atual**:
  * `MLJudge` roda com `PlattCalibrator(a=-1.0, b=0.0)` não treinado (`src/main.py:141-144,578-582`, `src/features/ml_judge.py:103,188`), gravando `A+` quase sempre e invalidando o termômetro silencioso da Fase 1;
  * `trades.fees_usdt_equiv` permanece `NULL` (`src/execution/oco_order_manager.py:1211-1222`, `src/core/state_reconciler.py:979-990`), fazendo `realized_pnl_usdt`, `realized_r` e `Profit Factor` parecerem melhores que o caixa líquido real;
  * `src/main.py:338` iguala `tactical_equity_usdt = free_usdt`: depósitos/saques manuais de USDT na carteira Spot distorcem silenciosamente o `tactical_hwm_usdt` (`src/risk/shadow_manager.py:266-277`) — um saque dispara CPPI/Hard Stop falsamente, enquanto um aporte mascara drawdown real;
  * `ShadowManager.evaluate_phase_downgrade()` (`src/risk/shadow_manager.py:190-225`) nunca é chamado em `src/main.py`.
* **Premissa não provada na documentação:** Que o observador silencioso da Fase 1 já está calibrado e pronto para avaliar `F1->F2` ([Metodologia §4.2–4.3](methodology-and-roadmap.md)) e que `shadow_manager.py` já executa regressão automática ao fim de cada trade (`docs/methodology-and-roadmap.md:156`).
* **Mitigação existente no código:** `tools/gate_report.py` adota fail-safe rigoroso de `UNKNOWN` (nunca `PASS` por omissão) quando faltam dados (`tools/gate_report.py:9-10,835-841,933-947,1080-1135`). **Nenhuma mitigação impede hoje a superestimação bruta de `realized_pnl_usdt`/`realized_r` nem a saída degenerada do `MLJudge` não treinado.**
* **O que mediria o risco:** Reconciliação semanal `Δ(USDT + BNB real na Binance - aportes + saques) - sum(trades.realized_pnl_usdt)`, distribuição de `trades.ml_grade_silent` (`% C / % B / % A+`), e execução periódica de `tools/gate_report.py` tratando qualquer `UNKNOWN` inesperado como pendência de engenharia.

---

#### #10 — Falha de infraestrutura da VPS: disco cheio por backups locais sem rotação, OOM (`> 512m`), drift de relógio ou perda de disco (`P=2 × I=4 = 8`)

* **Como o dinheiro é perdido:** Uma parada prolongada ou falha de escrita no disco da VPS não destrói o saldo imediatamente quando a OCO já está ativa na Binance, mas impede (a) gravar `PENDING_ENTRY` ou executar `STOP_WATCHDOG` e Time-Stop de `72h` no prazo, (b) manter o estado de `trades` (`oco_order_list_id`, `time_barrier_end_ms`, `tactical_hwm_usdt`) se o disco corromper sem backup offsite — fazendo um banco recriado do zero disparar `HALT_REVIEW` (C1-D) sobre a própria posição aberta do bot! Especificamente:
  * `Database.backup_to()` (`src/core/database.py:713-724`, chamado diariamente em `src/main.py:908-917`) **nunca apaga backups antigos** em `BACKUP_DIR`, garantindo eventual lotação de disco em VPS com armazenamento pequeno.
  * `docker-compose.yml:6` e `deploy/quant-chameleon.service:13` impõem `mem_limit: 512m` (ADR-015; apenas `import src.main` já consome ~93,4 MB de RSS, `docs/decisions.md:23`); vazamentos ou picos de memória causam `OOMKilled` (`Exit 137`).
  * Falhas repetidas de `MonotonicClock.sync()` (`src/main.py:777-783`) podem acumular drift `> 500 ms` e provocar rejeições `-1021` (há apenas 1 tentativa de resync+retry em `src/execution/oco_order_manager.py:996-1024,1189-1195`).
* **Premissa não provada na documentação:** Que o monitoramento manual mensal de disco `< 70%` (`docs/methodology-and-roadmap.md:178`) será suficiente sem rotação automática de `BACKUP_DIR`, e que o consumo de RAM permanece folgado abaixo de `512 MB` ao longo de meses de histórico em SQLite/pandas.
* **Mitigação existente no código:** Rotação de logs Docker (`max-size: 10m`, `max-file: 3` em `docker-compose.yml:13-17`), `restart: unless-stopped` (`docker-compose.yml:5`), `mem_limit: 512m` (`docker-compose.yml:6`), SQLite WAL + `synchronous=FULL` (`src/core/database.py:19-20`), escritas pós-fill *best-effort* (`src/execution/oco_order_manager.py:185-199`), Docker `HEALTHCHECK` somente-leitura (`Dockerfile:21-22`, `src/core/healthcheck.py:57-100`), backup offsite criptografado opcional (`src/core/offsite_backup.py:43-165`), resync + retry único em `-1021` (`src/execution/oco_order_manager.py:238-256,996-1024`), e `StateReconciler` no boot (`src/main.py:232-260`). **Nenhuma mitigação limpa backups locais antigos em `BACKUP_DIR` nem monitora uso de disco/RAM internamente.**
* **O que mediria o risco:** Uso de disco (`df -h`), tamanho e quantidade de arquivos em `data/backups/` e `data/quant.db`, memória RSS do container (`docker stats`), `cycle_health.clock_offset_ms`, `cycle_health.duration_ms`, `cycle_health.unhandled_errors`, `cycle_health.stale_symbols` e alertas do `HEALTHCHECK_URL`.
