# QuantCrypto Chameleon & Radar Engine

> Bot quantitativo **Binance Spot** (`1h`/`4h`, sem alavancagem) em **1 processo Python** e **1 banco SQLite (WAL)**, unificando execução blindada (**Chameleon v6.0**) e inteligência de mercado somente-leitura (**Radar v1.1 + Auditor Virtual**).

[![Docs](https://img.shields.io/badge/docs-MkDocs%20Material-blue)](https://pac-coins.github.io/quant-crypto-chameleon-radar-engine/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](#inicio-rapido)
[![Runtime](https://img.shields.io/badge/docker-mem__limit%20512m-informational)](docker-compose.yml)

---

## Arquitetura Resumida

| Subsistema | Papel | Universo | Impacto em Ordens (Fase 1) |
| --- | --- | --- | --- |
| **Chameleon v6.0** | Motor executor Spot (Single-Slot, OCO obrigatória, Watchdog sub-horário `<= 20 s`, Reconciliador de Boot, Teto Duplo `1%`/`50%`, CPPI `-4%`, Hard Stop `-12%`) | 7 pares (`BTC`, `ETH`, `SOL`, `XRP`, `ADA`, `LINK`, `AVAX`; `BNBUSDT` proibido) | **Executor único** (`bull_trend_pullback` em `BULL`) |
| **Radar v1.1 + Auditor** | Scanner de 5 camadas (EMA 9/50/200, Price Action, Order Flow, Order Book L2 anti-spoofing) + resolução automática em `virtual_signals` | 15 pares (`config/analyst.yaml`) | **Nenhum** (`affects_execution: false`, ADR-004) |

### Modos de Operação (`MODE` no `.env`)

| `MODE` | Chaves Binance | O que executa |
| --- | --- | --- |
| `ANALYST_ONLY` | **Proibidas** (falha no boot, ADR-010) | Radar L1–L5, Auditor Virtual e alertas Telegram |
| `SHADOW_CHAMELEON` | **Proibidas** (falha no boot, ADR-010) | Radar + `PaperExecutor` com slippage VWAP L2 real (Gate 6B) |
| `LIVE_CHAMELEON_PHASE_1` | Ed25519 obrigatória | Execução real Spot restrita à Fase 1 (Testnet Gate 6A ou Produção travada por `LIVE_CONFIRMATION` + SHA-256) |
| `LIVE_CHAMELEON_PHASE_2` | Ed25519 + `ML_JUDGE_ARTIFACT_PATH` | Fase 2 (`ProfitSplitter`, filtro Nota C, escada `1,5% -> 2,0%` + `CVaR 99%`, auto-downgrade; travada por `PHASE_2_F1_TO_F2_PASSED`) |

---

## Início Rápido

```bash
# 1. Ambiente e dependências
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 2. Rodar suíte completa de testes (116 passed)
.venv/bin/python -m pytest -q

# 3. Configurar .env e validar pré-requisitos em modo somente-leitura
cp .env.example .env
.venv/bin/python tools/preflight_check.py --env-file .env --target-gate 6a
```

### Ferramentas Operacionais Somente-Leitura (`tools/`)

```bash
# Relatório de Gates 6A / 6B / Fase 1->2 e Kill-Switches (URI mode=ro)
.venv/bin/python tools/gate_report.py --db data/quant.db --log-file /tmp/bot.log

# Teste isolado de restauração de backup em /tmp (modo seguro ANALYST_ONLY)
tools/restore_drill.sh data/backups/quant_backup_....db --report-json /tmp/drill.json

# Treino offline do MLJudge (somente Treino/Validação; Holdout estritamente excluído)
.venv/bin/python tools/train_ml_judge.py --db data/quant.db --output models/ml_judge_bull_v1.json

# Auditoria da Camada 4 do Radar e convergência Chameleon x Radar (URI mode=ro)
.venv/bin/python tools/radar_ai_audit.py --db data/quant.db
```

---

## Documentação

A documentação completa está publicada em **[pac-coins.github.io/quant-crypto-chameleon-radar-engine](https://pac-coins.github.io/quant-crypto-chameleon-radar-engine/)** (ou localmente via `pip install -r requirements-docs.txt && mkdocs serve`):

* **Operação:** [Guia Rápido e Transição de Gates](docs/quickstart.md) · [Runbook de Incidentes](docs/runbook.md) · [Backup e Restauração](docs/backup-and-restore.md)
* **Arquitetura:** [Arquitetura Unificada](docs/unified-architecture.md) · [Contrato de Dados (SQLite WAL)](docs/data-contract.md) · [Chameleon v6.0](docs/architecture.md) · [Radar v1.1](docs/radar.md)
* **Governança & Auditorias:** [Metodologia e Roadmap](docs/methodology-and-roadmap.md) · [Decisões (ADR-001..015)](docs/decisions.md) · [Auditoria do Backtest e Edge (K9.3)](docs/backtest-edge-audit.md) · [Próximos Passos e Pré-mortem](docs/next-steps.md)
* **Guia rápido para IA / Contribuidores:** [AGENTS.md](AGENTS.md)

---

## Governança e Segurança

* **Estratégia congelada (ADR-009):** `config/strategies.yaml` está congelado na configuração `K9.3` e protegido por verificação automática de SHA-256 no boot de produção e no CI (`.github/workflows/ci.yml`).
* **Escritor único (ADR-002):** apenas a thread principal do bot escreve no SQLite WAL; todas as ferramentas em `tools/` e o `HEALTHCHECK` abrem o banco em `URI mode=ro` + `PRAGMA query_only=ON`.
* **Estado atual:** **Blocos 1–6 + Backlog `B1`–`B7` + correções `AUD-01/05/06`** implementados (`pytest`: 116 passed). O projeto encontra-se em validação operacional (**Gate 6A** → **Gate 6B**) e não deve operar capital real sem aprovação formal dos gates e leitura cética de [docs/backtest-edge-audit.md](docs/backtest-edge-audit.md).

> **Aviso:** este software é um projeto de engenharia quantitativa. Não constitui recomendação financeira.
