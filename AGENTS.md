# AGENTS.md — Guia Rápido (QuantCrypto Chameleon & Radar Engine)

Bot Spot na Binance (1h/4h, sem alavancagem) em **1 processo Python** e **1 SQLite (WAL)**: **Chameleon v6.0** (executor, 7 pares) + **Radar v1.1** (scanner analítico, 15 pares + Auditor Virtual).

## 1. Estado Atual (Fase e Gates)

- **Blocos 1–6:** implementados e aprovados nos gates unitários (`pytest`: 94 passed; `docs/methodology-and-roadmap.md:225-230`). Estratégia congelada no Bloco 3: **`K9.3`** (`bull_trend_pullback`, `n=282`, `DSR=0,96`, `PF_base=1,43`; `docs/block-3-spec.md:350`).
- **Gate atual:** **Gate 6A** — 7 dias na Binance Spot Testnet (`MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=TESTNET`: 168 ciclos horários consecutivos sem exceções não tratadas, `|clock_offset_ms| <= 500 ms` e restauração validada; `docs/methodology-and-roadmap.md:84-87`).
- **Próximos gates:** **Gate 6B** (`MODE=SHADOW_CHAMELEON`, `>= 30 dias` ou `>= 25 trades`, slippage médio `<= 0,08%`; `methodology-and-roadmap.md:90-92`) → **Fase 1** (`PRODUCTION`, só `bull_trend_pullback` em `BULL`, risco 1%, IA/Radar silenciosos) → **Fase 2** (5 requisitos simultâneos em `methodology-and-roadmap.md:114-121`).
- **Runtime:** container único `mem_limit: 512m` (`docker-compose.yml:6`, `deploy/quant-chameleon.service:13`, ADR-015) + `HEALTHCHECK` read-only (`Dockerfile:21-22`).

## 2. Invariantes Inegociáveis (Verificadas no Código)

1. **Single-Slot (ADR-011):** máx. 1 posição aberta, imposto no banco por `ux_trades_single_slot` (`src/core/database.py:234-235`) e `ux_shadow_single_slot` (`database.py:188`), e no boot por `max_concurrent_positions == 1` (`src/core/config.py:504-505`).
2. **Nunca posição sem stop (C1-B / C1-E):** `PENDING_ENTRY` obrigatório antes do POST (`src/execution/oco_order_manager.py:386`); `governor.critical_section()` entre compra e OCO (`oco_order_manager.py:424`, `src/core/binance_client.py:155-159,225`); escritas pós-fill *best-effort* (`_best_effort_db_write` em `oco_order_manager.py:185` e `src/core/state_reconciler.py:106,445`); recuperação idempotente `-1007`/5XX (`oco_order_manager.py:448,855,1129`); fallback `MARKET` em `-2010`/stop violado (`oco_order_manager.py:870-1060`); resync+retry único em `-1021` pós-fill (`oco_order_manager.py:238,996,1189`); halt+alerta em HTTP `418` (`oco_order_manager.py:258`, `state_reconciler.py:139`).
3. **`Decimal` em preço/lote:** `to_decimal()` rejeita `float`/`bool` (`binance_client.py:325-329`); `round_price_to_tick()` e `floor_qty_to_step()` aplicam filtros (`binance_client.py:345-369`); ordens gravadas como `TEXT` (`docs/data-contract.md:13`).
4. **`BNBUSDT` fora da execução (C1-C):** `FORBIDDEN_EXECUTION_SYMBOLS = {"BNBUSDT"}` (`config.py:21`), barrado no boot (`config.py:474-479`), no reconciliador (`state_reconciler.py:91-93`) e no loop (`src/main.py:557`). Só existe no Radar (`config/analyst.yaml:15`).
5. **Escritor único no SQLite (ADR-002):** `write_tx()` usa `BEGIN IMMEDIATE` e lança `WrongThreadError` fora da thread dona (`database.py:385-397`). Ferramentas externas (`src/core/healthcheck.py:75`, `tools/gate_report.py:95`) abrem só em `URI mode=ro` + `PRAGMA query_only=ON`.
6. **`config/strategies.yaml` congelado por SHA-256 (ADR-009):** `LIVE_CHAMELEON_PHASE_1` + `PRODUCTION` exige `LIVE_CONFIRMATION="PHASE_1_GATES_6A_6B_PASSED"` e `APPROVED_STRATEGIES_SHA256` exato (`config.py:22,524-535`). Em `config/analyst.yaml`, `affects_execution: Literal[False]` é fixo no schema (`config.py:318,482-483`, ADR-004).
7. **Holdout só uma vez (`methodology-and-roadmap.md:40-57`):** últimos 15% (mín. 6 meses) trancados; teto $K_{\max}=20$ em `logs/backtest_trials_registry.csv` (`src/backtest/ablation_and_dsr.py:250`). Gate 6B é o Holdout Prospectivo Verdadeiro.
8. **Menor privilégio & C1-D (ADR-010 / ADR-012):** `ANALYST_ONLY`/`SHADOW_CHAMELEON` falham no boot se houver chaves Binance (`config.py:466-471`); `OFFSITE_BACKUP_ENABLED=true` falha sem `BACKUP_ENCRYPTION_KEY` (`config.py:111-119`). Saldo não rastreado entra em `HALT_REVIEW` e nunca é vendido (`state_reconciler.py:572-615`).

## 3. Mapa de Arquivos

- `config/strategies.yaml` — Parâmetros congelados do Chameleon v6.0 / `K9.3` (SHA-256).
- `config/analyst.yaml` — Parâmetros do Radar v1.1 (15 símbolos, `affects_execution: false`).
- `src/main.py` — Orquestrador horário único (`QuantOrchestrator`: live → coleta → Chameleon → Radar → Auditor → backup → heartbeat).
- `src/core/config.py` — Pydantic estrito (`.env` + 2 YAMLs), `RunMode` e `validate_mode_invariants()`.
- `src/core/database.py` — SQLite WAL single-writer (`SCHEMA_DDL` v1, `write_tx()`, `backup_to()`, `verify_backup()`).
- `src/core/binance_client.py` — Cliente público/Ed25519, `WeightGovernor`, `MonotonicClock`, `Decimal`, `ExchangeInfoCache`, mock `/sapi/*` na Testnet.
- `src/core/state_reconciler.py` — Cura no boot/ciclo (`PENDING_ENTRY`, `CLOSING`, `HALT_REVIEW`, OCO parcial, C1-D).
- `src/core/healthcheck.py` — Ping dead-man switch (`HEALTHCHECK_URL`) e probe read-only do Docker.
- `src/core/offsite_backup.py` — Backup offsite `gzip` + Fernet via Telegram (`< 50 MB`) e CLI de restauração.
- `src/data/candle_collector.py` — Coleta `XX:00:03 UTC`, `close_time+1ms`, gaps e `merge_asof`.
- `src/features/` — `indicators.py`, `order_flow.py`, `price_action.py`, `feature_store.py`, `ml_judge.py` (observador silencioso + ECE).
- `src/regimes/regime_detector.py` — Fonte única MECE (`BEAR` → `BULL` → `SIDEWAYS` → `TRANSITION`, ADR-008).
- `src/strategies/` — `bull_trend_pullback.py` (ativa na Fase 1); `sideways_pingpong.py` e `bear_capitulation_snap.py` (desligadas).
- `src/risk/` — `position_sizer.py` (teto duplo), `shadow_manager.py` (CPPI 4%, 4 kill-switches), `profit_splitter.py` (HWM → `FUNDING`).
- `src/execution/` — `oco_order_manager.py` (OCO líquida, `-1007`/`-2010`/`-1021`/`418`, Watchdog, Time-Stop), `paper_executor.py`, `executor_protocol.py`.
- `src/radar/` & `src/auditing/` — Radar L1–L5 (`structure_ema.py`, `orderbook_inspector.py`, `confluence_scorer.py`) e Auditor (`outcome_resolver.py`, `virtual_auditor.py`).
- `src/notifications/telegram.py` — Outbox persistente `telegram_outbox` + `HttpTelegramTransport`.
- `src/backtest/` — `causal_simulator.py` e `ablation_and_dsr.py` (Bloco 3).
- `src/tools/testnet_prepare.py` — Limpeza de saldos não rastreados e reset de halt só na Testnet.
- `tools/gate_report.py` — Relatório read-only (`mode=ro`) de Gates 6A/6B, Fase 1→2 e kill-switches.
- `tools/restore_drill.sh` — Teste isolado de restauração em `/tmp` (modo seguro `ANALYST_ONLY`, sem chaves).
- `docs/` — Specs (`unified-architecture.md`, `data-contract.md`, `methodology-and-roadmap.md`, `decisions.md`, `block-1..6-spec.md`, `runbook.md`, `backup-and-restore.md`).
- `tests/` — Suíte `pytest` (Gates 1–6, regressões da auditoria, healthcheck, backup, fault injection, `gate_report`, `restore_drill`).

## 4. Comandos

```bash
.venv/bin/python -m pytest -q                                                    # Testes (antes e depois)
.venv/bin/python tools/gate_report.py --db data/quant.db --log-file logs/bot.log # Relatório read-only
tools/restore_drill.sh data/backups/quant_backup_....db --report-json /tmp/d.json # Restore drill seguro
BACKUP_ENCRYPTION_KEY='<key>' .venv/bin/python -m src.core.offsite_backup restore /tmp/b.db.gz.enc ./data/quant.db # Restore real (container parado)
.venv/bin/python -m src.tools.testnet_prepare                                    # Preview Testnet (recusa PRODUCTION)
.venv/bin/mkdocs build --strict                                                  # Validar docs (ou mkdocs serve)
docker compose up -d --build && docker compose logs -f --tail=100 quant-chameleon # Deploy (só com autorização)
```

## 5. NUNCA Faça

- **NUNCA reinicie containers ou faça deploy** durante Gate 6A/6B sem autorização explícita.
- **NUNCA altere `config/strategies.yaml`** nem parâmetros da estratégia `K9.3`.
- **NUNCA rode ou ajuste parâmetros no Holdout** (`docs/methodology-and-roadmap.md §2.3`).
- **NUNCA opere manualmente na conta Spot da API Key** (dispara `HALT_REVIEW` C1-D; use subconta isolada).
- **NUNCA abra o SQLite em escrita por fora do bot** (ADR-002) nem substitua `data/quant.db` com o container ativo.
- **NUNCA conecte Radar ou Order Book L2 ao executor na Fase 1** (ADR-003, ADR-004).
- **NUNCA commite ou imprima segredos** (`.env`, `secrets/`, `.pem`, `BACKUP_ENCRYPTION_KEY`, `.db`).
