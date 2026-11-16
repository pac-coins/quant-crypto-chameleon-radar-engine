# Especificação de partida — Bloco 6 Unificado

Este documento congela as assinaturas, contratos matemáticos e critérios de aprovação do **Bloco 6** (Radar & Analyst Engine L1–L5, Auditor Virtual, Outbox Telegram, Orquestrador de Loop Único `src/main.py`, empacotamento Docker/Systemd e **Gates 6A / 6B**).

!!! info "Pré-requisito de Partida"
    O Bloco 6 integra todos os componentes homologados nos Blocos [1](block-1-spec.md), [2](block-2-spec.md), [3](block-3-spec.md), [4](block-4-spec.md) e [5](block-5-spec.md): `Database`, `PublicBinanceClient`, `SignedBinanceClient`, `CandleCollector`, `FeatureStore`, `RegimeDetector`, `BullTrendPullbackStrategy`, `PositionSizer`, `ShadowManager`, `MlJudge`, `PaperExecutor`, `OcoOrderManager`, `StateReconciler` e `outcome_resolver.py`.

---

## 1. Radar & Analyst Engine (Camadas L1–L5 — `src/radar/`)

O Radar analisa todos os símbolos de `analyst.yaml -> scanner_universe.symbols` (incluindo `BNBUSDT` e altcoins extras), mas **nunca** dispara ordens reais ou simuladas no Chameleon (ADR-004: `affects_execution: Literal[False]`, ADR-008: clima lido exclusivamente de `RegimeDetector`).

### 1.1. Camadas L1, L2 e L3 (`src/radar/structure_ema.py`)
- **Camada 1 — Estrutura & EMA (0–25 pts):** Consome `ema_9`, `ema_50`, `ema_200`, `chop_14`, `atr_14` do `FeatureStore` e `MarketRegime` do `RegimeDetector`. Pontua alinhamento de tendência (`close > ema_50 > ema_200`), proximidade de pullback (`|low - ema_9| <= tol * atr` ou `|low - ema_50| <= tol * atr`) e `chop_14 <= max_chop_for_trend`.
- **Camada 2 — Price Action (0–25 pts):** Consome `PriceAction` compartilhado (Bloco 2): amplitude mínima (`min_candle_range_atr`), *Bullish Pinbar*, *Bullish Engulfing* e *Liquidity Sweep*.
- **Camada 3 — Volume & Order Flow (0–25 pts):** Consome `OrderFlow` compartilhado (Bloco 2): `rvol_20` (`min_rvol_for_signal`, `high_conviction_rvol`) e `taker_buy_ratio` (`min_taker_buy_ratio`, `strong_taker_buy_ratio`).

### 1.2. Camada 4 — Order Book Live Anti-Spoofing (`src/radar/orderbook_inspector.py`)
Implementa as três proteções estruturais da auditoria de integração:
1. **Banda Efetiva Simétrica (`C1-A`):**
   $$\text{bid\_cov} = \frac{\text{mid} - \min(\text{bids})}{\text{mid}}, \quad \text{ask\_cov} = \frac{\max(\text{asks}) - \text{mid}}{\text{mid}}$$
   $$\text{band\_eff} = \min(\text{imbalance\_band\_pct}, \; \text{bid\_cov}, \; \text{ask\_cov})$$
   Se $\text{band\_eff} < \text{min\_valid\_band\_pct}$ ($0{,}30\%$), marca `obi_valid = False`, `obi = None` e `score_l4 = 0`, impedindo falsos OBI e falsas Whale Walls em livros profundos/truncados (`BTCUSDT`, `ETHUSDT`).
2. **Grade Ancorada em `mid_0` (`C2-1`):**
   A largura da faixa é fixada uma única vez na foto 0 (`seq = 0`): $\Delta b = \text{mid}_0 \times \text{wall\_bucket\_pct}$ ($0{,}1\%$). Todas as 3 fotos (`seq = 0, 1, 2`) projetam os níveis de preço no mesmo índice `floor((p - mid_0) / Δb)`. Uma parede é classificada como *Whale Wall* quando `notional_mean >= whale_wall_multiplier * median_notional` com `persistence >= 2` de 3 fotos.
3. **Coleta Intercalada (`C2-9`) e Agregados Compactos (`C2-8`):**
   `inspect_batch()` tira a foto 0 de todos os $k \le k_{\max}$ candidatos antes de aguardar `snapshot_interval_seconds`, depois foto 1 e foto 2, gravando apenas resumos agregados (~1 KB em `orderbook_snapshots` e `orderbook_walls`).

### 1.3. Camada 5 — Score & Alvos (`src/radar/confluence_scorer.py`)
- Soma `score_total = score_l1 + score_l2 + score_l3 + score_l4` (0–100) e classifica `B` ($\ge 65$), `A` ($\ge 75$) ou `A+` ($\ge 90$).
- Calcula `stop_price` (abaixo da mínima com folga `stop_buffer_below_wick_atr`, respeitando `min_stop_distance_atr`), `target1_price` ($1{,}5R$) e `target2_price` ($2{,}5R$, com recuo opcional antes de *Ask Wall* persistente se `adjust_target_before_ask_wall = true`).
- Lê `cost_roundtrip_pct` exclusivamente de `strategies.position_sizing` (**C2-4**) e fixa `horizon_end_ms = signal_close_time_ms + time_horizon_hours * 3_600_000` (**C2-7**).

---

## 2. Auditor Virtual & Relatório Semanal (`src/auditing/virtual_auditor.py`)

- **Registro em `t+1` (`C2-2`):** Grava o alerta em `virtual_signals` com `entry_ref_price = best_ask` da foto 0 do livro (ou `bookTicker.ask` quando L4 estiver desabilitada).
- **Resolução por Tempo e Pior Caso (`C2-3`, `C2-7`):** Em cada ciclo, resolve todos os registros `OPEN` cujo `last_1h_close_time_ms >= signal_close_time_ms` usando `resolve_dual_target_horizon` (`src/auditing/outcome_resolver.py`). Se houver buraco de velas (`gap > 3_600_000 ms`), marca `status = 'INVALID_GAP'`.
- **Relatório Semanal:** `generate_weekly_scorecard_markdown()` sumariza taxa de acerto, $\mathbb{E}[R]$ líquido por grade (`B`, `A`, `A+`) e contagem de `INVALID_GAP`.

---

## 3. Notificações Persistentes Telegram (`src/notifications/telegram.py`)

- Utiliza a tabela `telegram_outbox` (`PENDING -> SENT / FAILED`) para garantir que nenhum alerta crítico (`EMERGENCY_HALT`, `HALT_REVIEW`, execução ou fechamento de trade) seja perdido por queda momentânea de rede.
- Suporta `reply_to_signal_id` para responder na mesma thread quando um sinal do Radar é resolvido.

---

## 4. Orquestrador de Loop Único (`src/main.py`)

Ordem estrita de execução por ciclo horário (`QuantOrchestrator.run_once()`):
1. **Relógio & Manutenção:** Sincroniza `MonotonicClock` se necessário e consulta `GET /sapi/v1/system/status` (se `status != 0`, bloqueia novas entradas e notifica Telegram).
2. **Coleta & Features:** Coleta velas `1h`/`4h` via `CandleCollector`, atualiza `FeatureStore` e `RegimeDetector` (persistindo `regime_history`).
3. **Caminho do Chameleon PRIMEIRO (`C2-9`):**
   - Verifica `EMERGENCY_HALT` (`ShadowManager`, `StateReconciler` `HALT_REVIEW`, BNB `< $2.00`, `max_daily_trades_cap`).
   - Gerencia posição aberta (Time-Stop / `STOP_WATCHDOG` no modo Live; resolução no `PaperExecutor` no modo Shadow).
   - Se livre e sem bloqueio, avalia `BullTrendPullbackStrategy` em `execution_universe` (`BNBUSDT` proibido — `C1-C`), calcula lote via `PositionSizer`, anota `MlJudge.score_silent()` e executa via `ExecutorProtocol` (`NoOpExecutor`, `PaperExecutor` ou `OcoOrderManager`).
4. **Caminho do Radar DEPOIS (`C2-9`):**
   - Roda L1–L3 nos símbolos de `scanner_universe`, seleciona os top `max_symbols_per_cycle = 5`, executa L4 intercalada, calcula L5, grava/resolve no `VirtualAuditor` e enfileira alertas no Telegram.
5. **Saúde & Backup:** Grava `cycle_health`, envia fila `telegram_outbox` e executa `db.backup_to()` + `verify_backup()` diariamente.

---

## 5. Gate 6 (Unitário, Testnet 6A e Shadow Live 6B)

1. **Gate 6 Unitário (`pytest tests/test_radar_orderbook_math.py`):**
   - Prova `C1-A` (`band_eff < 0.30%` invalida OBI), `C2-1` (grade ancorada em `mid_0` e Whale Wall persistente), `C2-2`/`C2-3`/`C2-7` (`VirtualAuditor` com `ask` da foto 0, pior caso intra-vela e `INVALID_GAP`), `TelegramOutbox` e `QuantOrchestrator.run_once()` nos 3 `RunMode`s (incluindo `C2-9`, bloqueio de manutenção e `EMERGENCY_HALT`).
2. **Gate 6A (Estabilidade de Engenharia — 7 dias na Binance Spot Testnet):** `MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=TESTNET`, 0 unhandled exceptions em 168 ciclos horários, clock dentro de $\pm 500\text{ ms}$ e teste real de restauração do backup SQLite.
3. **Gate 6B (Shadow Live — 30 dias ou 25 trades em Produção):** `MODE=SHADOW_CHAMELEON` lendo dados públicos reais (`data-api.binance.vision`), validando slippage médio $\le 0{,}08\%$ e frequência compatível com o Bloco 3 antes de liberar capital real na Fase 1.
