# Auditoria do Backtest e da Vantagem Estatística (`K9.3`)

* **Data da auditoria:** 2026-11-24
* **Escopo:** revisão estática e analítica (somente-relatório, zero alterações em código ou em `config/strategies.yaml`, sem reabrir o Holdout) dos módulos que sustentam a homologação da estratégia congelada **`K9.3`** (`bull_trend_pullback`, `n=282`, `E[R]_net=+0,230R`, `PF_base=1,43`, `PF_pess=1,29`, `DSR=0,96`, `docs/block-3-spec.md:350`).
* **Arquivos auditados:**
  * `src/backtest/causal_simulator.py`
  * `src/backtest/ablation_and_dsr.py`
  * `src/regimes/regime_detector.py`
  * `src/strategies/bull_trend_pullback.py`
  * `src/data/candle_collector.py`
  * `src/features/feature_store.py`, `src/features/indicators.py`, `src/features/order_flow.py`, `src/features/price_action.py`
  * `src/main.py` e `src/risk/position_sizer.py` (paridade backtest × produção)
  * `tools/train_ml_judge.py`
  * `notebooks/01_download_causal_1h_4h_1m.ipynb`, `notebooks/02_ablation_and_dsr_sim.ipynb`, `notebooks/03_tail_stress_cppi_cusum.ipynb`
  * `docs/block-3-spec.md` e `config/strategies.yaml`

---

## 1. Sumário Executivo e Veredicto

A revisão encontrou **zero vazamento clássico de futuro nos indicadores técnicos** (`EMA`, `ATR`, `ADX`, `CHOP`, `RVOL`, `TBR`, `Donchian 12h`) nem no `merge_asof` de `4h -> 1h`. Contudo, revelou **3 achados críticos (`P0`)** que impactam diretamente (a) a execução real em produção e (b) a confiança estatística no `DSR = 0,96` publicado para `K9.3`:

| ID | Severidade | Achado | Impacto prático |
| --- | --- | --- | --- |
| **AUD-01** | **P0 (Bloqueador)** | **`src/main.py:1186-1203` paralisa o detector de regime em `TRANSITION` (`WARMUP_LT_120`) em 100% dos ciclos ao vivo** | O backtest classifica `BULL` normalmente (`limit=100_000`), mas em produção `limit=350` velas `1h` geram no máximo `89` barras `4h` únicas (`< 120`). **O bot ao vivo nunca consegue entrar em `BULL`, nunca dispara `bull_trend_pullback` e nunca ativa o veto `BTC == BEAR`.** |
| **AUD-02** | **P0 (Estatístico)** | **Gate 3 avaliado sobre 100% do histórico (60% Treino in-sample + 25% Validação + 15% Holdout misturados), sem aplicar Purging/Embargo** | `notebooks/02_ablation_and_dsr_sim.ipynb:124-151` calcula `bounds` mas ignora `train_end_ms`, `val_start_ms` e `val_end_ms`. Os `282` trades incluem a amostra de treino; o OOS real (`Val+Holdout`) tem `~113` trades (`< 150`). |
| **AUD-03** | **P0 (Estatístico)** | **`DSR = 0,96` está estruturalmente inflado por 4 distorções simultâneas (`tpy=150`, `K=9`, `var=0,25` e Holdout observado em `K5`→`K9`)** | Corrigir apenas `trades_per_year` de `150` para a taxa real (`~66,4/ano`) já derruba o DSR de `0,960` para **`0,886`** (`< 0,95`). Com `K=30..40` sub-variantes reais e variância realista (`0,50`), o DSR cai para **`0,32–0,38`**. |
| **AUD-04** | **P1 (Paridade)** | **Vazamento de `72h` além de `end_time_ms` em `causal_simulator.py:358`** | Se a simulação for fatiada por `end_time_ms`, trades abertos perto da fronteira leem barras futuras de `aligned_frames[winner.symbol]` (não truncado). |
| **AUD-05** | **P1 (Paridade)** | **Deriva da `EMA_100_4h` em produção (`limit=250` → `4,98%` de peso residual da semente) e `tools/train_ml_judge.py:136,162` chamando `detect_series()` em frame `1h`** | Divergência silenciosa de regime nas fronteiras de tendência entre backtest e produção, e regime `1h` em vez de `4h` no treino do `MLJudge`. |
| **AUD-06** | **P1 (Paridade)** | **Entrada no backtest a `close_p` vs. `MARKET BUY` em `XX:00:03+ UTC` mantendo stop/alvo ancorados em `close_p` + bypass silencioso de `BTC` stale** | Slippage positivo de rompimento comprime `RR < 2,0` ao vivo; se `BTCUSDT` ficar stale, `btc_rs_12h` assume `default=0,0` e `btc_regime_4h` cai em `TRANSITION`. |
| **AUD-07** | **P2 (Custos)** | **Velas `1m` nunca baixadas/usadas em `K9.3`, slippage `BASE` (`0,06%` ida+volta) abaixo do próprio Gate 6B (`0,08%` só na entrada), e subperíodos divididos por contagem de trades** | Subestima impacto de cauda (`gap_shock = 0,15%` vs. flash-crash `> 2,5%`) e permite que um trimestre de bull market ocupe 2 dos 4 subperíodos. |
| **AUD-08** | **P2 (Amostral)** | **Viés de sobrevivência dos 7 pares (fixados em 2026 para simular 2022–2026) e `logs/backtest_trials_registry.csv` ausente do repositório** | Impede reconstruir exatamente a trilha completa de tentativas e superestima o desempenho histórico frente a uma cesta escolhida ponto-no-tempo em 2022. |

!!! danger "Conclusão prática para decisão de capital"
    1. **Bloqueador imediato antes do próximo Gate 6A / Gate 6B:** o bug **AUD-01** (`src/main.py:1186-1203`) impede qualquer operação do Chameleon em runtime e precisa de correção explícita (sem alterar `config/strategies.yaml`) antes de iniciar novo Gate 6A ou 6B.
    2. **Postura cética sobre o edge de `K9.3`:** `K9.3` **não deve ser tratado como vantagem provada com 96% de confiança**, e sim como uma **hipótese promissora ainda não validada fora da amostra**. O **Gate 6B (`SHADOW_CHAMELEON`)** é, na prática, o **primeiro Holdout verdadeiramente virgem** do projeto.

---

## 2. O Que Passou Limpo na Auditoria (Causalidade Verificada)

Para manter rigor científico e evitar alarmismo, abaixo estão os mecanismos verificados no código que **estão matematicamente corretos e livres de look-ahead bias**:

1. **Normalização de timestamps e alinhamento `4h -> 1h` (`src/data/candle_collector.py:33-53,398-422`):**
   * `normalize_close_time()` soma `+1 ms` ao `raw_close_time_ms` da Binance (`L46`) e exige alinhamento exato às fronteiras `1h` (`3_600_000 ms`) e `4h` (`14_400_000 ms`) (`L42-52`).
   * `align_4h_to_1h()` executa `pd.merge_asof(..., on="close_time_ms", direction="backward", allow_exact_matches=True)` (`L415-421`). Às `04:00:00 UTC`, a vela `4h` `00:00–04:00` já fechou com `close_time_ms = 04:00:00`, portanto `allow_exact_matches=True` é estritamente causal; às `01:00`, `02:00` e `03:00`, usa-se exclusivamente a vela `4h` fechada às `00:00`.
   * O mesmo padrão causal se aplica a `align_btc_context()` (`L425-447`).
2. **Indicadores e features (`src/features/indicators.py`, `src/features/order_flow.py`, `src/features/price_action.py`):**
   * `ema()` (`indicators.py:9-47`), `true_range()` (`L50-67`), `atr_wilder()` (`L70-96`), `rsi_wilder()` (`L99-138`), `adx_wilder()` (`L141-232`), `chop_log10()` (`L235-262`) e `rvol_median()` (`L292-304`) usam exclusivamente dados `<= t` (sem janelas centradas, sem `shift` negativo).
   * `taker_buy_ratio()` (`order_flow.py:9-16`) e `order_flow_delta_quote()` (`L29-39`) operam ponto a ponto na vela fechada `t`.
   * O canal Donchian `12h` usa `high.shift(1).rolling(12, min_periods=12).max()` tanto no simulador (`src/backtest/causal_simulator.py:301`) quanto na produção (`src/main.py:1199-1200`), comparando `close_t > max(high_{t-12..t-1})` sem vazar `high_t`.
3. **Precedência conservadora intrabar `1h` e bloqueio de reentrada (`src/backtest/causal_simulator.py:197-214,325,371`):**
   * Quando `1m` não é fornecido, `resolve_trade_1h_or_1m()` testa `h_low <= current_stop_p` (`L202`) **antes** de `h_high >= target_p` (`L209`) — critério conservador que atribui prejuízo em caso de ambiguidade na mesma vela `1h`.
   * `slot_busy_until_ms = sim_trade.exit_time_ms` (`L371`) combinado com `if t_ms <= slot_busy_until_ms: continue` (`L325`) impede abrir novo trade na mesma vela em que o anterior fechou.
4. **Desempate Single-Slot idêntico (`causal_simulator.py:85-88` vs. `src/main.py:892`):**
   * Ambos ordenam por `(-rs_vol_adjusted_12h, symbol)`.
5. **Denominador de momentos superiores do DSR (`src/backtest/ablation_and_dsr.py:217-219,234`):**
   * Usa curtose não-excedente `gamma_4 = mean(z**4)` (`3,0` na Normal), mantendo a expressão `1 - gamma_3 * SR + ((gamma_4 - 1)/4) * SR^2` fiel a Bailey & López de Prado (2014).

---

## 3. Achados Críticos (`P0`)

### AUD-01 — Bloqueador de Produção: `src/main.py` trava o `RegimeDetector` em `TRANSITION` (`WARMUP_LT_120`) em 100% dos ciclos

* **Evidência verificada no código:**
  * `src/main.py:1186-1187` carrega `df_1h = load_frame(self.db, sym, "1h", limit=350)` e `df_4h = load_frame(self.db, sym, "4h", limit=250)`.
  * `src/main.py:1193` executa `df_al = align_4h_to_1h(df_1h_f, df_4h_f)`. Como `merge_asof` preserva apenas o índice esquerdo (`df_1h`, de `350` horas = `87,5` velas de `4h`), `df_al["close_time_ms_4h"]` contém **no máximo `89` timestamps únicos de `4h`**, descartando as outras `~161` barras de `df_4h_f`.
  * `src/main.py:1203` chama `self.regime_detector.detect_from_aligned_frame(df_al)`.
  * `src/regimes/regime_detector.py:14,235-242` define `MIN_WARMUP_CANDLES_4H = 120` e avalia `has_hist = len(unique_4h) >= self.min_warmup_candles`. Como `89 < 120` **sempre**, `classify_regime_values(..., has_min_history=False)` (`L64-73`) devolve **`MarketRegime.TRANSITION` com `fallback_reason="WARMUP_LT_120"`** para todos os 16 símbolos em todo ciclo horário!
* **Prova reproduzida em Python (leitura pura, série sintética de alta):**
  * Entrada: `250` velas `4h` + `350` velas `1h` em tendência de alta limpa (`ADX=100`, `CHOP=36,57`, `close > ema_100`, `slope > 0`).
  * `df_al['close_time_ms_4h'].nunique()` = **`89`**.
  * `detect_from_aligned_frame(df_al)` (o que `src/main.py:1203` chama): `MarketRegime.TRANSITION`, `fallback_reason='WARMUP_LT_120'`.
  * `detect_on_4h_frame(df_4h_f)` (o estado real das `250` velas `4h`): `MarketRegime.BULL`, `fallback_reason=None`.
* **Por que os testes unitários não pegaram:** `tests/test_block2_talib_and_mece.py:296-300` testou `WARMUP_LT_120` isoladamente; nenhum teste de integração rodou `QuantOrchestrator.run_once()` esperando `BULL` a partir de `350` velas `1h` + `250` velas `4h` no SQLite.
* **Por que o backtest não teve o problema:** `notebooks/02_ablation_and_dsr_sim.ipynb:96` carrega `df_1h` com `limit=100_000` (`~9.250` barras `4h` únicas `>= 120`).
* **Impacto real:** em produção/testnet/shadow, o Chameleon fica **100% impedido de classificar `BULL` (nunca abre trade)** e **100% impedido de classificar `BEAR` no `BTCUSDT`** (desabilitando o veto macro).
* **Direção de correção recomendada (para aprovação separada, sem tocar em `strategies.yaml`):** em `src/main.py:1203`, classificar o regime diretamente sobre `df_4h_f` (`self.regime_detector.detect_on_4h_frame(df_4h_f)`) **e** aumentar `limit` de `df_4h` para `>= 500` (reduzindo a deriva de EMA descrita em AUD-05), adicionando teste de regressão que valide `BULL` em `run_once()`.

---

### AUD-02 — Homologação do Gate 3 (`K9.3`) avaliou 100% da amostra (In-Sample + OOS misturados) sem aplicar Purging/Embargo

* **Evidência verificada no código:**
  * `notebooks/02_ablation_and_dsr_sim.ipynb:124` particiona o histórico via `bounds = split_temporal_vaults(all_ts, train_pct=0.60, val_pct=0.25)`.
  * Porém, nas linhas `129-144`, todas as chamadas a `run_multi_asset_simulation(...)` **omitem `start_time_ms` e `end_time_ms`**, simulando sobre **100% do período (`2022-01` a `2026-04`)**.
  * Os campos `bounds.train_end_ms`, `bounds.val_start_ms` e `bounds.val_end_ms` **nunca são referenciados** no restante de `run_ablation_and_gate3()` (`L121-169`).
  * `evaluate_gate3_criteria()` (`src/backtest/ablation_and_dsr.py:303-380`) declara na docstring (`L311`): *"Avalia os 5 critérios binários do Gate 3 sobre os trades fora da amostra"*, mas recebeu `trades_step_c_pure` contendo **os 60% de Treino in-sample + 25% Validação + 15% Holdout (`n = 282`)**.
* **Consequências matemáticas:**
  1. Os `282` trades reportados em `docs/block-3-spec.md:350` correspondem a ~`4,25` anos inteiros (`~66,4 trades/ano`). Os `40%` fora do Treino (`Validação + Holdout`) possuem aproximadamente **`~113` trades** — abaixo do piso de `150` trades OOS exigido em `docs/methodology-and-roadmap.md:62` (e o Holdout isolado de 15% tem apenas `~42` trades, conforme `docs/block-3-spec.md:332`).
  2. Como não houve corte nem *purging/embargo* nas fronteiras, operações abertas nos 60% iniciais de Treino influenciam a fila Single-Slot na entrada da Validação, e operações abertas antes de `holdout_start_ms` ocupam o slot por até `72h` dentro do Holdout.
  3. `E[R]_net = +0,230R`, `CI95_lo = +0,068R`, `PF_base = 1,43` e `DSR = 0,96` são métricas **mistas (60% in-sample)**, não métricas puramente fora da amostra.

---

### AUD-03 — Inflação Estrutural do Deflated Sharpe Ratio (`DSR = 0,96`) e Violação de Fato do Cofre Holdout

Quatro distorções independentes operam na mesma direção (inflar o DSR de `K9.3`):

1. **Frequência anualizada hardcoded em `150.0` trades/ano (`src/backtest/ablation_and_dsr.py:196,222,341`):**
   * `compute_deflated_sharpe_ratio()` usa `var_sr_per_trade = trials_sharpe_variance / trades_per_year` com default `trades_per_year = 150.0`.
   * A frequência real de `K9.3` é `282 / 4,25 ≈ 66,4 trades/ano` (`2,26x` menor). Dividir por `150` em vez de `66,4` reduz artificialmente `std_sr_trials` por um fator de $\sqrt{150/66,4} = 1,50\times$, encolhendo o limiar nulo $\text{SR}_0$ em `33%` e inflando o DSR (além de inflar `sharpe_annualized` em `L341` pelo mesmo `1,50x`).
2. **Subcontagem severa de tentativas (`n_trials = 9` hardcoded em `notebooks/02_ablation_and_dsr_sim.ipynb:150`):**
   * `evaluate_gate3_criteria(..., n_trials=9)` (`L150`) recebe o literal `9` **antes** mesmo de `register_backtest_trial()` (`L153`) ser invocado — o contador `K` retornado pelo registrador é descartado.
   * `docs/block-3-spec.md:286-353` documenta `9` *baterias* (`K1`–`K9`), mas cada bateria continha múltiplas sub-variantes explicitamente numeradas: `K1` (4 etapas), `K2` (até letra `G` = ~7), `K3.1`–`K3.4` (4), `G1`–`G6`/`K5.1`–`K5.6` (~12), `K6.1`–`K6.5` (5), `K7.1`–`K7.4` (4), `K8.1`–`K8.4` (4) e `K9.1`–`K9.6` (6) — **no mínimo ~35–46 variantes testadas**, excedendo o teto $K_{\max} = 20$ de `docs/methodology-and-roadmap.md:50`.
   * Além disso, `logs/backtest_trials_registry.csv` não está versionado no repositório (`logs/` inexistente), impossibilitando auditoria do histórico exato de todas as execuções no Colab.
3. **Variância de Sharpe entre tentativas hardcoded em `0.25` (`src/backtest/ablation_and_dsr.py:195`):**
   * `trials_sharpe_variance = 0.25` implica $\sigma_{\text{SR,anual}} = 0,50$ entre as tentativas. Porém, apenas olhando `docs/block-3-spec.md:288-353`, o retorno esperado das variantes variou de `-0,1874R` (`K1 step_c_pure`) a `+0,230R` (`K9.3`) — faixa superior a `2,5–3,5` pontos de Sharpe anualizado, o que implica variância empírica $\sigma^2_{\text{SR,anual}} \ge 0,50\text{–}1,00$.
4. **Inspeção iterativa do Holdout (`docs/block-3-spec.md:309-354`):**
   * O Holdout de 15% foi aberto após `K5` (`-6,58R`), dissecado ativo por ativo (`ETH -4,50R`, `ADA -3,15R`, `LINK -3,04R`, `L332-335`), e usado como critério de aprovação durante `K6`, `K7`, `K8` e `K9` até atingir `+5,80R` em `K9.3` (onde `K9.1` e `K9.2` tiveram `DSR=0,94` e `K9.6` teve `0,948`, sendo `K9.3` a única variante da bateria a cruzar `0,95`). Isso transforma o Holdout histórico em conjunto de validação secundário.

#### Matriz de Sensibilidade Analítica do DSR de `K9.3`

Reconstruindo o Sharpe por trade de `K9.3` diretamente dos números publicados em `docs/block-3-spec.md:350` (sem tocar no Holdout):
* A partir de `E[R] = +0,230R` e `CI95_lo = +0,068R` (`n=282`): $\text{SE} \approx 0,0827 \implies \sigma_R \approx 1,388 \implies \widehat{\text{SR}}_{\text{trade}} \approx 0,1657$.
* Resolvendo exatamente `compute_deflated_sharpe_ratio` (`ablation_and_dsr.py:192-237`) para reproduzir `DSR = 0,960` em `(n=282, K=9, var=0,25, tpy=150, gamma_3=0,5, gamma_4=2,2)`: $\widehat{\text{SR}}_{\text{trade}} = 0,1626$ ($\sigma_R \approx 1,414$, consistência de `1,8%` com o IC 95% publicado).

Mantendo $\widehat{\text{SR}}_{\text{trade}} = 0,1626$ fixo e variando apenas os parâmetros distorcidos:

| Amostra (`n`) | `trades_per_year` | `trials_sharpe_var` | `K = 9` | `K = 20` | `K = 30` | `K = 40` | `K = 50` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Full 100% (`282`, IS+OOS) | `150,0` *(hardcoded)* | `0,25` *(hardcoded)* | **`0,960`** *(publicado)* | `0,931` | `0,913` | `0,899` | `0,888` |
| Full 100% (`282`, IS+OOS) | **`66,4`** *(real)* | `0,25` *(otimista)* | **`0,886`** | `0,788` | `0,731` | `0,689` | `0,655` |
| Full 100% (`282`, IS+OOS) | **`66,4`** *(real)* | **`0,50`** *(realista)* | `0,703` | `0,484` | **`0,382`** | **`0,317`** | `0,272` |
| Full 100% (`282`, IS+OOS) | **`66,4`** *(real)* | `1,00` *(conservador)* | `0,338` | `0,109` | `0,055` | `0,032` | `0,021` |
| Est. OOS 40% (`~113`) | **`66,4`** *(real)* | `0,25` *(otimista)* | `0,777` | `0,693` | `0,651` | `0,622` | `0,600` |
| Est. OOS 40% (`~113`) | **`66,4`** *(real)* | **`0,50`** *(realista)* | `0,632` | `0,490` | `0,425` | `0,382` | `0,351` |

**Leitura objetiva:** o selo `DSR = 0,96 >= 0,95` **só sobrevive sob a combinação simultânea** de (1) incluir os 60% de treino in-sample (`n=282`), (2) inflar `trades_per_year` em `2,26x` (`150` em vez de `66,4`), (3) contar `K=9` em vez de `~35–46` variantes, e (4) fixar `var=0,25`. Mudando **qualquer um** desses 4 pontos isoladamente, o DSR cai abaixo de `0,95`.

---

## 4. Achados de Paridade Backtest × Produção (`P1`)

### AUD-04 — Vazamento de horizonte futuro (`72h`) quando `end_time_ms` é passado ao simulador

* **Evidência:** `src/backtest/causal_simulator.py:308-310` filtra `indexed_frames[sym]` até `end_time_ms`, mas na linha **`358`** a resolução do trade usa `future_1h = aligned_frames[winner.symbol]` (o DataFrame **completo não truncado**!).
* **Efeito:** qualquer simulação que recorte `end_time_ms = bounds.train_end_ms` ou `bounds.val_end_ms` sem fatiar `aligned_frames` antes permite que trades abertos nas últimas `72h` leiam velas futuras do cofre seguinte. *(Nota: `tools/train_ml_judge.py:127-129` se protegeu fatiando `aligned_by_sym` em `<= bounds.val_end_ms` antes da simulação.)*

### AUD-05 — Deriva numérica de `EMA_100_4h` em produção (`limit=250`) e erro de timeframe no treinador offline do `MLJudge`

* **Deriva de `EMA_100_4h`:** `src/main.py:1187` carrega `250` velas `4h`, deixando apenas `150` passos de recorrência após a semente SMA de `100` velas (`src/features/indicators.py:24-45`). O peso residual da semente é $(1 - 2/101)^{150} = \mathbf{4,98\%}$, contra `0,00%` no backtest (`limit=30_000` em `02_ablation_and_dsr_sim.ipynb:97`). Em viradas onde `close_4h ≈ ema_100_4h` ou `slope ≈ 0`, o regime pode divergir entre produção e backtest. Aumentar `limit` de `4h` para `600` derruba esse resíduo para `0,0045%`.
* **Timeframe incorreto em `tools/train_ml_judge.py:136,162`:** `regime_det.detect_series(btc_df)` e `regime_det.detect_series(df_al)` invocam `detect_series()` — que espera um frame **`4h`** (`src/regimes/regime_detector.py:193-215`) e calcula `ema_100` sobre `df["close"]` (`1h`) — em vez de `detect_series_from_aligned_frame(df_al)` (`L273-330`). Deve ser corrigido antes de treinar o artefato `MLJudge` para a Fase 1.

### AUD-06 — Divergências na execução de entrada e fallback permissivo com `BTCUSDT` ausente/stale

1. **Âncora de preço na entrada (`bull_trend_pullback.py:210-226` vs. `src/main.py:896-897,921-925`):**
   * O backtest entra no exato `close_p` da vela `1h` (`causal_simulator.py:124`).
   * Em produção, o robô consulta `bt.ask` em `XX:00:03+ UTC` (após buscar klines de 16 ativos em `src/main.py:1163`) e passa `entry_price=bt.ask` ao `PositionSizer`, **mantendo `stop_price` e `target_price` fixos no `close_p`** (`bull_trend_pullback.py:212-213`). Se o rompimento andar `+0,5%` entre o fechamento da vela e o fill, o risco real até o stop aumenta e a distância até o alvo encolhe (`RR` efetivo cai abaixo de `2,0`).
2. **Bypass silencioso de `BTCUSDT` stale ou ausente (`bull_trend_pullback.py:155,182-185` e `src/main.py:1229-1244`):**
   * Se `BTCUSDT` for marcado como `stale` no ciclo (`src/main.py:1184-1185`), `btc_df` fica `None` (`L1229`) e `btc_regime_str` assume `TRANSITION` (`L1230`). Assim, `veto_if_btc_bear` não dispara mesmo com o BTC em `BEAR`, e `btc_rs_12h` fica ausente — fazendo `_first_finite(row, "btc_rs_12h", default=0.0)` (`bull_trend_pullback.py:183`) comparar `rs_score >= 0.0` (sempre verdadeiro porque `rs_score >= 1,40`).
   * No backtest (`causal_simulator.py:302-305`), `sub[tcol].map(btc_rs_lookup)` usa correspondência exata de timestamp: em velas de manutenção onde o BTC teve kline truncada descartada (`candle_collector.py:224-226`), `btc_rs_12h` vira `NaN` e cai no mesmo `default=0.0`.
3. **Diferença no teto de caixa com fricção (`causal_simulator.py:225-227` vs. `position_sizer.py:281-286`):**
   * O simulador calcula `effective_r_unit_equity = min(r_pct, max_cash_pct * d_stop)`, enquanto `PositionSizer` inclui `fee_dec + slip_dec` no denominador de `cap_risk_usdt` e aplica arredondamento `floor_qty_to_step` (`L302`). Diferença pequena (`~1%–5%` conservadora em produção).

---

## 5. Custos, Slippage, Resolução `1m` e Viés de Sobrevivência (`P2`)

### AUD-07 — Realismo de custos, ausência de `1m` e subperíodos por contagem

1. **Velas `1m` ausentes na prática:** apesar de `config/strategies.yaml:81` declarar `intracandle_sim_timeframe: "1m"` e `notebooks/01_download_causal_1h_4h_1m.ipynb:7` mencionar `1m` no título, o código (`L82-97`) baixa apenas `'1h'` e `'4h'`, e `notebooks/02_ablation_and_dsr_sim.ipynb:132-144,182` executa `K9.3` (e até `step_e_breakeven_1m`!) com `intracandle_1m_map=None`. Para `K9.3` (sem breakeven), o viés do fallback `1h` é **conservador** (checa `STOP` antes de `TARGET`), mas para qualquer estudo futuro de `B8` (Degrau E / Breakeven), rodar sem `1m` invalida o resultado.
2. **Slippage `BASE` mais otimista que o critério de aprovação do Gate 6B:**
   * `causal_simulator.py:103-112` modela:
     * `BASE`: `0,15%` taxas + `0,03%` slippage em `TARGET` / `0,06%` ida+volta (**3 bps por perna**) em `STOP`/`TIME_BARRIER`, `gap_shock = 0%`.
     * `PESSIMISTIC`: `0,20%` taxas + `0,06%` (`TARGET`) / `0,12%` (`STOP`/`TIME_BARRIER`) + `gap_shock = 0,15%` em `STOP`.
   * O Gate 6B aprova slippage médio de entrada até **`0,08%` (`8 bps`)** (`docs/methodology-and-roadmap.md:91`). Um slippage de `8 bps` na entrada + `8 bps` na saída por stop/time-stop totaliza `16 bps` (`0,16%`) — acima inclusive do `0,12%` do cenário `PESSIMISTIC`.
3. **Análise justa do *breakeven* de fricção em `K9.3`:**
   * Como `K9.3` usa stop largo de `3,0 * ATR_14` (estimado em `~3,12%`, extrapolando o stop médio de `2,08%` em `2,0 * ATR` de `docs/block-3-spec.md:293`), cada `+10 bps` (`+0,10%`) de fricção extra ida+volta custa apenas `~0,032R` por trade.
   * **Conclusão equilibrada:** se a expectativa real fora da amostra fosse realmente `+0,230R`, slippage normal de `10–16 bps` reduziria `E[R]` em apenas `~0,02R–0,03R` (ainda positivo; o *breakeven* exigiria `~+72 bps` extras). O risco real está na combinação de **(a) edge real bem menor que `+0,230R`** devido a AUD-02/AUD-03 (ex.: `+0,05R`) com **(b) gaps extremos `> 2,5%`** em flash-crashes (`> 0,8R` de perda extra num único evento), nunca modelados por `gap_shock = 0,15%`.
4. **Subperíodos divididos por número de trades (`src/backtest/ablation_and_dsr.py:349`):**
   * `np.array_split(r_base, 4)` reparte os `282` trades em 4 blocos de ~70 operações, e não em 4 intervalos de calendário (~1 ano cada). Um bull market ativo com muitos trades pode ocupar 2 blocos enquanto um ano inteiro parado ou adverso com poucos trades fica diluído dentro de outro bloco.

### AUD-08 — Viés de sobrevivência e rastreabilidade do registro de trials

* Os 7 símbolos de `config/strategies.yaml:70-76` (`BTC`, `ETH`, `SOL`, `XRP`, `ADA`, `LINK`, `AVAX`) foram fixados em 2026 sabendo quais grandes criptomoedas mantiveram alta liquidez e se recuperaram ao longo de 2022–2026 (ativos que colapsaram ou perderam protagonismo desde 2022 não estão na cesta).
* `logs/backtest_trials_registry.csv` não existe no repositório (`logs/` ausente), e `compute_afml_sample_uniqueness` é importado em `02_ablation_and_dsr_sim.ipynb:113` mas nunca chamado *(nota: sob Single-Slot, `u_bar_i = 1,0` para todos os trades porque não há posições simultâneas, portanto não distorce `K9.3`, apenas confirma desconexão no notebook)*.

---

## 6. Recomendações Práticas (Sem Reabrir Holdout e Sem Alterar `K9.3`)

1. **Ação imediata obrigatória antes do novo Gate 6A / Gate 6B (correção de bug P0, não altera estratégia):**
   * Corrigir **AUD-01** (`src/main.py:1186-1203`): classificar o regime usando `detect_on_4h_frame(df_4h_f)` e elevar o carregamento de `4h` para `limit=600` (fechando também a deriva de `4,98%` de **AUD-05**).
   * Corrigir **AUD-05** em `tools/train_ml_judge.py:136,162` (usar `detect_series_from_aligned_frame(df_al)` em vez de `detect_series(df_al)`) antes de gerar o artefato `MLJudge` durante o Gate 6B.
   * Tornar fail-closed o filtro `BTCUSDT` em `bull_trend_pullback.py:182-185` / `src/main.py:1229-1244` quando `BTCUSDT` estiver ausente ou stale (evitar `default=0.0` e `TRANSITION` permissivos).
2. **Disciplina de validação prospectiva:**
   * **Não** re-otimizar `K9.3` no histórico de 2022–2026 nem alterar `config/strategies.yaml`.
   * Encarar o **Gate 6B (`SHADOW_CHAMELEON`, `>= 30 dias` ou `>= 25 trades`)** como o verdadeiro teste fora da amostra virgem, monitorando estritamente `E[R]_net`, `book_impact_bps` (L2 VWAP), `slippage <= 0,08%` e distribuição real de regimes em `regime_history`.
   * Na **Fase 1 (`PRODUCTION`)**, manter o risco travado em `1,0%`, CPPI `-4%` e Hard Stop `-12%` até que os **60 trades reais** provem expectativa líquida positiva e calibração real fora da amostra (`tools/gate_report.py`).
