# Quadro geral de triagem

| Categoria | Item Validado | Quem Apontou | Onde Entra | Impacto Prático |
| --- | --- | --- | --- | --- |
| **1 — Catastrófico** | Filtro exato do erro `-2010` e Stop Pendurado | ChatGPT e DeepSeek | Bloco 5 (`oco_order_manager`) | Evita vender errado ou ficar sem stop |
| **1 — Catastrófico** | Arredondamento `close_time` (+1ms) no `merge_asof` | DeepSeek | Bloco 1 (`candle_collector`) | Evita perder a vela de 4h recém-fechada |
| **1 — Catastrófico** | Tratamento de `NaN` e divisão por zero no `CHOP` | ChatGPT | Bloco 2 (`indicators.py`) | Impede que o robô trave em moeda parada |
| **1 — Catastrófico** | Backup SQLite via `sqlite3.backup()` nativo | ChatGPT | Bloco 1 (`database.py`) | Evita arquivo de backup corrompido |
| **1 — Catastrófico** | Cálculo empírico de $\mathbb{E}[R]$ com Time-Stop no Gate 3 | ChatGPT e DeepSeek | Bloco 3 (Colab) | Mede o lucro real de quem sai por tempo |
| **1 — Catastrófico** | Trava de 20 trials (`trials_registry.csv`) e Holdout 6m | Gemini e DeepSeek | Bloco 3 (Colab) | Impede falso lucro por tentativa e erro |
| **1 — Catastrófico** | Kill-Switches (-12% DD, BNB min $2 e Manutenção API) | Gemini e DeepSeek | Bloco 6 (`main.py`) | Trava novas compras em pane ou crise |
| **1 — Catastrófico** | Banda efetiva simétrica no `/api/v3/depth` (`band_eff` e `obi_valid`) (**C1-A**) | Auditoria de Integração | Radar L4 (`orderbook_inspector.py`) | Evita OBI e Whale Walls inválidos em BTC/ETH |
| **1 — Catastrófico** | `WeightGovernor` (`CRITICAL > HIGH > LOW`) e `critical_section()` (**C1-B**) | Auditoria de Integração | Bloco 1 (`binance_client.py`) | Impede que `429`/`418` do Radar deixe posição sem OCO |
| **1 — Catastrófico** | Segregação `execution_universe` vs. Radar e fronteira `StrategySignal` (**C1-C**) | Auditoria de Integração | Bloco 1 (`config.py`) e `main.py` | Impede liquidar reserva BNB ou criar órfãos fora da cesta |
| **1 — Catastrófico** | Saldo excedente não rastreado entra em `HALT_REVIEW` (nunca vende posição manual) (**C1-D**) | Auditoria de Integração | Bloco 5 (`state_reconciler.py`) | Evita liquidar posição manual do operador na mesma conta |
| **1 — Catastrófico** | Escritas pós-fill *best-effort* nunca interrompem criação da OCO (**C1-E**) | Auditoria de Integração | Bloco 1 (`database.py`) e Bloco 5 | Garante que falha de SQLite pós-fill não deixe posição nua |
| **2 — Refinamento** | Mock de `/sapi/*` na Testnet e Permissão `Universal Transfer` | ChatGPT e DeepSeek | Bloco 5 e `.env` | Testnet não aceita `/sapi/*` (evita erro 404) |
| **2 — Refinamento** | *High-Water Mark* (Topo Histórico) no Cofre de Lucros | ChatGPT | Bloco 6 (`profit_splitter`) | Só tira 30% quando recuperar prejuízos |
| **2 — Refinamento** | Teto de ordem em % da banca (`55%`) e BNB em Dólar (`$5`) | ChatGPT e DeepSeek | Bloco 4 (`strategies.yaml`) | Protege bancas pequenas desde o dia 1 |
| **2 — Refinamento** | Direção de arredondamento do `tickSize` na OCO | ChatGPT | Bloco 5 (`binance_client`) | Evita que `stopPrice` cole no `stopLimit` |
| **2 — Refinamento** | Trocar *Isotonic* por *Platt Scaling* para menos de 2.000 trades | DeepSeek | Bloco 3 (`ml_judge.py`) | Evita que a IA decore dados pequenos |
| **2 — Refinamento** | Mínimo de 150 trades, Profit Factor maior que 1,20 e Holdout | ChatGPT | Bloco 3 (Colab) | Mais segurança estatística antes do ao vivo |
| **2 — Refinamento** | Salvar Colunas 8 (`trades`) e 9 (`taker_buy_vol`) da vela | Gemini (Trader Ref) | Bloco 1 (`candle_collector`) | Captura Order Flow grátis da Binance |
| **2 — Refinamento** | Calcular *Order Flow Delta* e *Liquidity Sweep* em 1h | Gemini (Trader Ref) | Bloco 2 (`indicators.py`) | Mede agressão compradora e absorção |
| **2 — Refinamento** | Testar ablação Com vs. Sem *Order Flow* no Colab | Gemini (Trader Ref) | Bloco 3 (Colab) | Só ativa Order Flow se subir o $\mathbb{E}[R]$ |
| **2 — Refinamento** | IA em *Modo Observador Silencioso* na Fase 1 (60 trades) | Gemini | Bloco 6 (`ml_judge.py`) | Prova que a IA acerta ao vivo antes da Fase 2 |
| **2 — Refinamento** | Faixas de 0,1% do livro ancoradas em `mid_0` (**C2-1**) | Auditoria de Integração | Radar L4 (`orderbook_inspector.py`) | Evita deslizamento de faixas entre as 3 fotos |
| **2 — Refinamento** | Auditor inicia em `t+1` usando `ask` do snapshot 0 (**C2-2**) | Auditoria de Integração | `auditing/virtual_auditor.py` | Entrada realista alinhada ao atraso do alerta |
| **2 — Refinamento** | `outcome_resolver.py` compartilhado (pior caso intra-vela) (**C2-3**) | Auditoria de Integração | `src/auditing/outcome_resolver.py` | Evita divergência entre Auditor e Shadow |
| **2 — Refinamento** | Modelo de custo único lido de `strategies.yaml` (**C2-4**) | Auditoria de Integração | `config.py` / `virtual_auditor.py` | Estatísticas do Radar e Chameleon comparáveis |
| **2 — Refinamento** | Persistir `quote_volume` (col 7) e `taker_buy_quote_volume` (col 10) (**C2-5**) | Auditoria de Integração | Bloco 1 (`candles`) | Delta financeiro em USDT exato |
| **2 — Refinamento** | Guardas de zero no `TBR` e `RVOL` (`V=0` ou mediana=0 $\rightarrow$ `NaN`) (**C2-6**) | Auditoria de Integração | Bloco 2 (`order_flow.py` / `indicators.py`) | Evita `inf` passar em limiares numéricos |
| **2 — Refinamento** | Horizonte por tempo (`*_end_ms`) e detecção de gaps de velas (**C2-7**) | Auditoria de Integração | Bloco 1 + Auditor | Imunidade a janelas de manutenção da Binance |
| **2 — Refinamento** | Gravar apenas agregados/faixas do livro (~1 KB) sem JSON bruto (**C2-8**) | Auditoria de Integração | Schema SQLite (`orderbook_snapshots`) | Mantém backup abaixo de 50 MB (limite Telegram) |
| **2 — Refinamento** | Execução do Chameleon antes da L4; 3 fotos intercaladas (**C2-9**) | Auditoria de Integração | `main.py` / `orderbook_inspector.py` | Não adiciona 10–15 s de latência ao slippage do Gate 6B |
| **3 — Cosmético** | Trocar o texto "Sobrevivência Determinística" no README | ChatGPT | Texto do README | Muda zero linhas de código |
| **3 — Cosmético** | Renomear `CPPI_DRAWDOWN_CONTROL` para `DRAWDOWN_SCALING` | ChatGPT | Nome no YAML | Muda zero linhas de matemática |
| **3 — Cosmético** | Reclamar que a Fase 1 é "simples demais / MVP" | DeepSeek | Nenhum | Contradição: simplificar a Fase 1 era a meta! |
| **3 — Cosmético** | Decidir se o Cofre em `FUNDING` guarda USDT ou XRP | DeepSeek | Operação manual | Guardar USDT já protege o lucro sem taxa extra |
| **3 — Cosmético** | Testar se `SIDEWAYS` deve vir antes de `BEAR` | DeepSeek | Nenhum | Ficar em dólar na queda (`BEAR` 1º) é mais seguro |
| **3 — Cosmético** | Ler *Order Book Level 2* (Livro de Ofertas) ao vivo como gatilho de execução | Gemini (Descartado) | Nenhum (`affects_execution: false` no Radar) | Sem backtest grátis; permitido apenas no caminho analítico do Radar (ADR-004) |

---

# Checklist de Execução por Bloco (Ordem Estrita de Construção)

## Bloco 1: Fundação, Banco SQLite WAL, Relógio, Governador e Coletor Causal
- [x] Criar `config/strategies.yaml` (v6.0) e `config/analyst.yaml` (v1.1) com validador estrito em `src/core/config.py` (`RunMode`, `validate_mode_invariants`, trava `affects_execution: false` e segregação `execution_universe`).
- [x] Implementar `src/core/database.py` com `PRAGMA journal_mode=WAL`, `synchronous=FULL`, `isolation_level=None`, `write_tx()` (`BEGIN IMMEDIATE` + `WrongThreadError`), schema completo (v1) de [Contrato de Dados](data-contract.md) e rotina nativa `sqlite3.backup()`.
- [x] Implementar `src/core/binance_client.py` com assinatura Ed25519, `WeightGovernor` (`CRITICAL > HIGH > LOW`, `critical_section()`), sincronização monotônica (`RTT/2`), cache dinâmico de `/api/v3/exchangeInfo`, arredondamento `Decimal` direcional (`tickSize` / `stepSize`) e mock automático para `/sapi/*` na Testnet.
- [x] Implementar `src/data/candle_collector.py` persistindo colunas 0..10 (incluindo `quote_volume` e `taker_buy_quote_volume`), normalizando `close_time` (`+1ms`), detectando gaps e fazendo junção 4h/1h e BTC via `merge_asof(..., direction='backward', allow_exact_matches=True)`.
- [x] Passar 100% no **Gate 1 Unificado** (`pytest tests/test_block1_causal_and_decimal.py tests/test_block1_db_governor_modes.py`, conforme [Especificação do Bloco 1](block-1-spec.md)).

## Bloco 2: Indicadores Canônicos, FeatureStore, Order Flow e Clima MECE
- [x] Implementar `src/features/indicators.py`, `src/features/order_flow.py` e `src/features/price_action.py` (funções puras compartilhadas entre Chameleon e Radar, com guardas de divisão por zero / `NaN` e `CHOP` base 10).
- [x] Implementar `src/features/feature_store.py` calculando a união de `FeatureRequest` (EMA 9/20/50/100/200, ATR, ADX, RSI, CHOP, RVOL, TBR, Delta, Sweep) uma única vez por ciclo.
- [x] Implementar `src/regimes/regime_detector.py` com partição MECE de 4 estados (`BEAR` -> `BULL` -> `SIDEWAYS` -> `TRANSITION`) como fonte única de clima (ADR-008) e fallback automático para `TRANSITION` em caso de `NaN` ou histórico menor que 120 velas.
- [x] Passar 100% no **Gate 2** (`pytest tests/test_block2_talib_and_mece.py`, conforme [Especificação do Bloco 2](block-2-spec.md)).

## Bloco 3: Simulador Causal Single-Slot e Ablação no Google Colab
- [x] Criar `notebooks/01_download_causal_1h_4h_1m.ipynb` (com paginação `backfill_historical_range` em `candle_collector.py`) baixando dados desde `2022-01-01` das 7 moedas (`BTC`, `ETH`, `SOL`, `XRP`, `ADA`, `LINK`, `AVAX`) incluindo volume de agressão compradora (cols 9 e 10).
- [x] Criar `notebooks/02_ablation_and_dsr_sim.ipynb` (apoiado em `src/strategies/` e `src/backtest/`) com partição `60% Treino / 25% Validação / 15% Holdout (6 meses)`, cálculo empírico de $\mathbb{E}[R]$ (incluindo saídas por tempo de 24h), comparação com e sem filtro de *Order Flow Delta* e registro automático em `trials_registry.csv`.
- [x] Criar `notebooks/04_radar_layers_123_backtest.ipynb` para validar as Camadas 1, 2 e 3 do Radar sob a mesma governança anti-overfitting.
- [x] Passar 100% no **Gate 3 de Engenharia e Matemática** (`pytest tests/test_block3_causal_sim_and_dsr.py`, conforme [Especificação do Bloco 3](block-3-spec.md)).
- [x] **Execução Empírica no Google Colab:** rodar os notebooks `01`, `02` e `04` no Colab sobre a massa histórica real (`2022-01-01`+, `> 360.000` velas) congelando a configuração `K9.3` (Rompimento Donchian 12h + `ADX_14_4h >= 17.0` + `Order Flow RVOL >= 1.30 / TBR >= 0.52` + `RS_12h >= 1.40` e `>= BTC_RS_12h`, `Stop 3.0x / Target 6.0x / 72h`) e homologando o **Gate 3 Empírico (`PASS = True` na tentativa $K = 9 / 20$):** `n_trades = 282` ($\ge 150$), $\mathbb{E}[R]_{\text{net}} = +0,230R$ ($> +0,10R$), IC 95% inferior $= +0,068R$ ($> 0,0R$ via *Block Bootstrap*), $\text{PF}_{\text{base}} = 1,43$ ($\ge 1,20$), $\text{PF}_{\text{pess}} = 1,29$ ($> 1,05$), Subperíodos `4/4`, Holdout `+5,80R` (`True`) e $\text{DSR} = 0,96$ ($\ge 0,95$).

## Bloco 4: Dimensionamento de Teto Duplo, Drawdown Scaling e Cofre
- [x] Implementar `src/risk/position_sizer.py` com teto duplo (risco de 1% e máximo de 50% do caixa operável), trava proporcional $\min(500\text{ USDT}, 55\%\text{ da banca})$, desconto de `pending_hodl_bucket_usdt` e aborto se menor que $1,05 \times \text{minNotional}$.
- [x] Implementar `src/risk/shadow_manager.py` com corte de risco pela metade em drawdown de 4% e gatilhos de *Auto-Downgrade* e *Hard Stop* (-12%).
- [x] Implementar `src/risk/profit_splitter.py` com regra de *High-Water Mark* e transferência `MAIN_FUNDING`, além de `src/features/ml_judge.py` (*Platt Scaling* e Modo Observador Silencioso) e `notebooks/03_tail_stress_cppi_cusum.ipynb`.
- [x] Passar 100% no **Gate 4** (`pytest tests/test_block4_dual_cap_and_cppi.py`, conforme [Especificação do Bloco 4](block-4-spec.md)).

## Bloco 5: Execução OCO, Paper Executor e Cura no Boot
- [ ] Implementar `src/execution/executor_protocol.py` e `src/execution/paper_executor.py` (modo `SHADOW_CHAMELEON`, usando `bookTicker` real e `outcome_resolver.py`).
- [ ] Implementar `src/execution/oco_order_manager.py` descontando comissão no ativo base, tratando erro `-2010` com verificação de preço/mensagem para fallback `MARKET`, *watchdog* de stop pendurado, fechamento de 4 passos no Time-Stop e escritas pós-fill *best-effort* (C1-E).
- [ ] Implementar `src/core/state_reconciler.py` resolvendo `PENDING_ENTRY` via `origClientOrderId` (sem duplicar compra em caso de timeout `-1007`), curando execuções parciais de OCO e aplicando `HALT_REVIEW` para saldo excedente não rastreado (C1-D).
- [ ] Passar 100% no **Gate 5** (`pytest tests/test_block5_reconciler_and_oco.py` + 4 Testes de Tortura na Testnet).

## Bloco 6: Radar (L1–L5), Auditor Virtual, Observabilidade e Graduação de Fases
- [ ] Implementar `src/radar/` (`structure_ema.py`, `orderbook_inspector.py` com `band_eff` e grade ancorada, `confluence_scorer.py`) e `src/auditing/` (`outcome_resolver.py`, `virtual_auditor.py`) + `pytest tests/test_radar_orderbook_math.py`.
- [ ] Implementar `src/notifications/telegram.py` (outbox persistente) e `src/main.py` (orquestrando os 3 `RunMode`s, checagem `/sapi/v1/system/status` e reserva de BNB).
- [ ] Aprovar **Gate 6A** (7 dias na Testnet em `LIVE_CHAMELEON_PHASE_1` + teste de restauração do backup SQLite) e **Gate 6B** (30 dias ou 25 trades em `SHADOW_CHAMELEON` com dados reais).
- [ ] Iniciar **Fase 1** com capital real (`LIVE_CHAMELEON_PHASE_1` + `PRODUCTION`, apenas `BULL`, risco fixo de 1%, IA e Radar anotando em modo silencioso) até completar 60 trades reais para avaliar subida à **Fase 2**.
