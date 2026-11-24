# AGENTS.md — Guia Rápido (QuantCrypto Chameleon & Radar Engine)

Bot Spot na Binance (1h/4h, sem alavancagem) em **1 processo Python** e **1 SQLite (WAL)**: **Chameleon v6.0** (executor, 7 pares) + **Radar v1.1** (scanner analítico, 15 pares + Auditor Virtual).

## 1. Estado Atual (Fase e Gates)

- **Blocos 1–6 + Backlog B1–B7 + CI/Preflight + AUD-01/04/05/06:** implementados e aprovados nos testes (`pytest`: 119 passed; `docs/methodology-and-roadmap.md:225-230`, `docs/next-steps.md`). Estratégia congelada no Bloco 3: **`K9.3`** (`bull_trend_pullback`, ver auditoria estatística `AUD-02`/`AUD-03` em `docs/backtest-edge-audit.md`).
- **Gate atual:** **Gate 6A** — 7 dias na Binance Spot Testnet (`MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=TESTNET`: 168 ciclos horários consecutivos sem exceções não tratadas, `|clock_offset_ms| <= 500 ms` e restauração validada; `docs/methodology-and-roadmap.md:84-87`).
- **Próximos gates:** **Gate 6B** (`MODE=SHADOW_CHAMELEON`, `>= 30 dias` ou `>= 25 trades`, slippage médio `<= 0,08%`; `methodology-and-roadmap.md:90-92`) → **Fase 1** (`PRODUCTION`, só `bull_trend_pullback` em `BULL`, risco 1%, IA/Radar silenciosos) → **Fase 2** (`LIVE_CHAMELEON_PHASE_2`, 5 requisitos simultâneos em `methodology-and-roadmap.md:114-121`).
- **Runtime:** container único `mem_limit: 512m` (`docker-compose.yml:6`, `deploy/quant-chameleon.service:13`, ADR-015) + `HEALTHCHECK` read-only (`Dockerfile:21-22`) + mount `./models:/app/models:ro` (`docker-compose.yml:13`).

## 2. Invariantes Inegociáveis (Verificadas no Código)

1. **Single-Slot (ADR-011):** máx. 1 posição aberta, imposto no banco por `ux_trades_single_slot` (`src/core/database.py:237-238`) e `ux_shadow_single_slot` (`database.py:191`), e no boot por `max_concurrent_positions == 1` (`src/core/config.py:513-514,565-566`).
2. **Nunca posição sem stop (C1-B / C1-E):** `PENDING_ENTRY` obrigatório antes do POST (`src/execution/oco_order_manager.py:596-616`); `governor.critical_section()` entre compra e OCO (`src/core/binance_client.py:155-159,225`); escritas pós-fill *best-effort* (`_best_effort_db_write` em `oco_order_manager.py:226` e `src/core/state_reconciler.py:106`); recuperação idempotente `-1007`/5XX; fallback `MARKET` em `-2010`/stop violado; resync+retry único em `-1021` pós-fill; halt+alerta em HTTP `418` (`oco_order_manager.py:299`, `state_reconciler.py:139`); watchdog sub-horário single-thread (`src/main.py:1363-1483`).
3. **`Decimal` em preço/lote:** `to_decimal()` rejeita `float`/`bool` (`binance_client.py:325-329`); `round_price_to_tick()` e `floor_qty_to_step()` aplicam filtros (`binance_client.py:345-369`); ordens gravadas como `TEXT` (`docs/data-contract.md:13`).
4. **`BNBUSDT` fora da execução (C1-C):** `FORBIDDEN_EXECUTION_SYMBOLS = {"BNBUSDT"}` (`config.py:21`), barrado no boot (`config.py:483-488`), no reconciliador (`state_reconciler.py:93`) e no loop (`src/main.py:878`). Só existe no Radar (`config/analyst.yaml:15`).
5. **Escritor único no SQLite (ADR-002):** `write_tx()` usa `BEGIN IMMEDIATE` e lança `WrongThreadError` fora da thread dona (`database.py:388-397`). Ferramentas externas (`src/core/healthcheck.py:75`, `tools/gate_report.py:95`, `tools/train_ml_judge.py:44`, `tools/radar_ai_audit.py:31`) abrem só em `URI mode=ro` + `PRAGMA query_only=ON`.
6. **`config/strategies.yaml` congelado por SHA-256 (ADR-009):** `LIVE_CHAMELEON_PHASE_1` + `PRODUCTION` exige `LIVE_CONFIRMATION="PHASE_1_GATES_6A_6B_PASSED"` e `LIVE_CHAMELEON_PHASE_2` exige `"PHASE_2_F1_TO_F2_PASSED"`, ambos com `APPROVED_STRATEGIES_SHA256` exato (`config.py:22-23,532-544,583-595`); protegido também no CI (`.github/workflows/ci.yml`). Em `config/analyst.yaml`, `affects_execution: Literal[False]` é fixo no schema (`config.py:324,491-492`, ADR-004).
7. **Holdout só uma vez (`methodology-and-roadmap.md:40-57`):** últimos 15% (mín. 6 meses) trancados; teto $K_{\max}=20$ em `logs/backtest_trials_registry.csv` (`src/backtest/ablation_and_dsr.py:250`). Gate 6B é o Holdout Prospectivo Verdadeiro.
8. **Menor privilégio & C1-D (ADR-010 / ADR-012):** `ANALYST_ONLY`/`SHADOW_CHAMELEON` falham no boot se houver chaves Binance (`config.py:475-480`); `OFFSITE_BACKUP_ENABLED=true` falha sem `BACKUP_ENCRYPTION_KEY` (`config.py:119-125`). Saldo não rastreado entra em `HALT_REVIEW` e nunca é vendido (`state_reconciler.py:572-615`).

## 3. Mapa de Arquivos

- `config/strategies.yaml` — Parâmetros congelados do Chameleon v6.0 / `K9.3` (SHA-256).
- `config/analyst.yaml` — Parâmetros do Radar v1.1 (15 símbolos, `affects_execution: false`).
- `.github/workflows/ci.yml` — CI (pytest + `mkdocs build --strict` + trava SHA-256 de `strategies.yaml`).
- `src/main.py` — Orquestrador horário único + watchdog sub-horário (`QuantOrchestrator`: live → coleta → Chameleon → Radar → Auditor → backup → heartbeat).
- `src/core/config.py` — Pydantic estrito (`.env` + 2 YAMLs), `RunMode` (incl. Fase 2) e `validate_mode_invariants()`.
- `src/core/database.py` — SQLite WAL single-writer (`SCHEMA_DDL` v1, `write_tx()`, `upsert_regime_history()`, `backup_to()`, `prune_old_backups()`).
- `src/core/binance_client.py` — Cliente público/Ed25519, `WeightGovernor`, `MonotonicClock`, `Decimal`, `ExchangeInfoCache` (isolamento `-1121`), mock `/sapi/*` na Testnet.
- `src/core/state_reconciler.py` — Cura no boot/ciclo (`PENDING_ENTRY`, `CLOSING`, `HALT_REVIEW`, OCO parcial, taxas líquidas, C1-D).
- `src/core/healthcheck.py` — Ping dead-man switch (`HEALTHCHECK_URL`) e probe read-only do Docker.
- `src/core/offsite_backup.py` — Backup offsite `gzip` + Fernet via Telegram (`< 50 MB`) e CLI de restauração.
- `src/data/candle_collector.py` — Coleta `XX:00:03 UTC`, `close_time+1ms`, gaps e `merge_asof`.
- `src/features/` — `indicators.py`, `order_flow.py`, `price_action.py`, `feature_store.py`, `ml_judge.py` (`MLJudgeArtifact`, L2+Platt puro numpy, ECE).
- `src/regimes/regime_detector.py` — Fonte única MECE (`BEAR` → `BULL` → `SIDEWAYS` → `TRANSITION`, ADR-008).
- `src/strategies/` — `bull_trend_pullback.py` (ativa na Fase 1); `sideways_pingpong.py` e `bear_capitulation_snap.py` (desligadas).
- `src/risk/` — `position_sizer.py` (teto duplo + escada Fase 2B/CVaR99), `shadow_manager.py` (CPPI 4%, 4 kill-switches, auto-downgrade), `profit_splitter.py` (HWM → `FUNDING`).
- `src/execution/` — `oco_order_manager.py` (OCO líquida, `fees_usdt_equiv`, `-1007`/`-2010`/`-1021`/`418`, Watchdog, Time-Stop), `paper_executor.py` (L2 VWAP slippage), `executor_protocol.py`.
- `src/radar/` & `src/auditing/` — Radar L1–L5 (`structure_ema.py`, `orderbook_inspector.py`, `confluence_scorer.py`) e Auditor (`outcome_resolver.py`, `virtual_auditor.py`).
- `src/notifications/telegram.py` — Outbox persistente `telegram_outbox` + `HttpTelegramTransport`.
- `src/backtest/` — `causal_simulator.py` e `ablation_and_dsr.py` (Bloco 3).
- `src/tools/testnet_prepare.py` — Limpeza de saldos não rastreados e reset de halt só na Testnet.
- `tools/preflight_check.py` — Validador read-only de `.env`, SHA-256, chave Ed25519, Fernet, `MLJudge` e disco antes de cada gate.
- `tools/gate_report.py` — Relatório read-only (`mode=ro`) de Gates 6A/6B, Fase 1→2 e kill-switches.
- `tools/daily_gate_digest.py` — Resumo diário read-only (`mode=ro`) enviado no Telegram + gerador de `cron` (`--print-cron`).
- `tools/guided_gate_switch.py` — Transição guiada exclusiva `--to 6b` (bloqueia `FAIL`/`UNKNOWN` e recusa Fase 1/2).
- `tools/train_ml_judge.py` — Treinador offline read-only do `MLJudge` (Treino/Validação apenas, Holdout excluído).
- `tools/radar_ai_audit.py` — Auditoria read-only (`mode=ro`) da Camada 4, convergência Chameleon×Radar e ablação offline L4 no `MLJudge`.
- `tools/restore_drill.sh` — Teste isolado de restauração em `/tmp` (modo seguro `ANALYST_ONLY`, sem chaves).
- `docs/` — Diátaxis: `quickstart.md`, `runbook.md`, `backup-and-restore.md`, `unified-architecture.md`, `data-contract.md`, `methodology-and-roadmap.md`, `decisions.md`, `adr-016-regra-de-decisao-6b.md`, `backtest-edge-audit.md`, `next-steps.md`.
- `tests/` — Suíte `pytest` (Gates 1–6, paridade orquestrador×backtest, auditoria, healthcheck, backup, fault injection, `gate_report`, `daily_gate_digest`, `guided_gate_switch`, `restore_drill`, `preflight`, B1–B7).

## 4. Comandos

```bash
.venv/bin/python -m pytest -q                                                    # Testes (antes e depois)
.venv/bin/python tools/preflight_check.py --env-file .env --target-gate 6a       # Preflight read-only (6a|6b|phase1|phase2)
.venv/bin/python tools/gate_report.py --db data/quant.db --log-file /tmp/bot.log # Relatório read-only
.venv/bin/python tools/daily_gate_digest.py --db data/quant.db --dry-run         # Resumo Telegram (--print-cron p/ cron)
.venv/bin/python tools/guided_gate_switch.py --to 6b --db data/quant.db --log-file /tmp/bot.log --restore-drill-report /tmp/d.json # Troca 6A->6B (só 6B)
.venv/bin/python tools/train_ml_judge.py --db data/quant.db --output models/ml_judge_bull_v1.json # Treino MLJudge (sem Holdout)
.venv/bin/python tools/radar_ai_audit.py --db data/quant.db                      # Auditoria L4 & Radar×Chameleon (read-only)
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
