# Guia Rápido e Transição de Gates

Este guia reúne o fluxo operacional prático para instalar o ambiente, validar pré-requisitos com `tools/preflight_check.py` e conduzir com segurança cada transição de gate definida na [Metodologia e Roadmap](methodology-and-roadmap.md).

!!! warning "Regra operacional inegociável"
    **Nunca** reinicie containers nem faça deploy durante um **Gate 6A** ou **Gate 6B** em andamento, **nunca** edite `config/strategies.yaml` fora de aprovação formal de gate (ADR-009) e **nunca** abra `data/quant.db` em escrita por fora do processo do bot (ADR-002).

---

## 1. Instalação Local e Testes

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# Suíte completa de testes unitários e de injeção de falhas
.venv/bin/python -m pytest -q
```

Para servir a documentação localmente:

```bash
.venv/bin/python -m pip install -r requirements-docs.txt
.venv/bin/mkdocs serve
```

---

## 2. Configuração do `.env` e Preflight Obrigatório

Copie `.env.example` para `.env` e preencha apenas o necessário para o modo desejado:

```bash
cp .env.example .env
```

Antes de **qualquer** subida ou troca de gate, valide o `.env` em modo somente-leitura (não toca em `./data` nem imprime segredos):

```bash
# Substitua --target-gate por: 6a | 6b | phase1 | phase2
.venv/bin/python tools/preflight_check.py --env-file .env --target-gate 6a
```

O `preflight_check.py` verifica automaticamente:

* Invariantes de boot (`validate_mode_invariants`: menor privilégio ADR-010, `BNBUSDT` proibido C1-C, `affects_execution: false` ADR-004);
* Hash SHA-256 exato de `config/strategies.yaml` (ADR-009);
* Permissões seguras (`chmod 600`) da chave privada Ed25519 em modos `LIVE`;
* Dead-man switch (`HEALTHCHECK_URL`) e backup offsite criptografado (`OFFSITE_BACKUP_ENABLED=true` + chave Fernet válida);
* Integridade SHA-256 e `ECE <= 0,05` do artefato calibrado `ML_JUDGE_ARTIFACT_PATH` (obrigatório em `phase1`/`phase2`);
* Presença do mount `./models:/app/models:ro` em `docker-compose.yml` e espaço em disco livre (`< 70%`).

---

## 3. Fluxo por Gate (`6A → 6B → Fase 1 → Fase 2`)

### Gate 6A — 7 Dias na Binance Spot Testnet

* **Configuração no `.env`:** `MODE=LIVE_CHAMELEON_PHASE_1`, `BINANCE_ENV=TESTNET`, `BINANCE_API_KEY`, `BINANCE_ED25519_PRIVATE_KEY_PATH=/app/secrets/ed25519.pem`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
* **Preparação de conta Testnet (preview seguro — recusa `PRODUCTION`):**
  ```bash
  .venv/bin/python -m src.tools.testnet_prepare
  ```
* **Auditoria ao completar 168 ciclos horários consecutivos:**
  ```bash
  # Exporta os logs reais do container (sem reiniciar o bot)
  docker compose logs --no-color quant-chameleon > /tmp/bot_6a.log

  # Relatório read-only (URI mode=ro)
  .venv/bin/python tools/gate_report.py \
    --db data/quant.db \
    --log-file /tmp/bot_6a.log \
    --json-out /tmp/gate_6a_report.json

  # Teste isolado de restauração do backup mais recente em /tmp
  LATEST_BKP=$(ls -1t data/backups/quant_backup_*.db | head -n 1)
  tools/restore_drill.sh "$LATEST_BKP" --report-json /tmp/restore_drill_6a.json
  ```

### Gate 6B — Shadow Live (`>= 30 dias` ou `>= 25 trades`)

* **Regra de decisão pré-registrada:** antes de avaliar os resultados, consulte [ADR-016 — Regra de Decisão do Gate 6B](adr-016-regra-de-decisao-6b.md) (`GO` Fase 1, `HOLD`/ajustar, `NO-GO`/abandonar e protocolo de amostra pequena).
* **Transição guiada (somente `6A → 6B`):** após `PASS` sem nenhum `UNKNOWN` no Gate 6A, você pode usar `tools/guided_gate_switch.py --to 6b` ([Runbook §7.2](runbook.md)). Transições para Fase 1 e Fase 2 são **estritamente manuais**.
* **Configuração no `.env`:** `MODE=SHADOW_CHAMELEON`, `SHADOW_INITIAL_USDT=300.00`, **remover completamente** `BINANCE_API_KEY` e `BINANCE_ED25519_PRIVATE_KEY_PATH` (ADR-010: o boot falha se existirem chaves Binance neste modo).
* **Validação antes de subir:**
  ```bash
  .venv/bin/python tools/preflight_check.py --env-file .env --target-gate 6b
  ```
* **Preparação do observador silencioso `MLJudge` antes da Fase 1 (sem tocar no Holdout):**
  ```bash
  .venv/bin/python tools/train_ml_judge.py \
    --db data/quant.db \
    --output models/ml_judge_bull_v1.json
  ```
* **Auditoria da Camada 4 do Radar e convergência Chameleon × Radar (somente-leitura):**
  ```bash
  .venv/bin/python tools/radar_ai_audit.py --db data/quant.db
  ```

### Fase 1 — Capital Real (`LIVE_CHAMELEON_PHASE_1` + `PRODUCTION`)

Somente após `PASS` formal nos Gates 6A e 6B (`tools/gate_report.py`) e cumprimento de [ADR-016](adr-016-regra-de-decisao-6b.md):

1. Defina no `.env`:
   * `MODE=LIVE_CHAMELEON_PHASE_1` e `BINANCE_ENV=PRODUCTION`
   * `LIVE_CONFIRMATION=PHASE_1_GATES_6A_6B_PASSED`
   * `APPROVED_STRATEGIES_SHA256=<sha256 exacto de config/strategies.yaml>`
   * `HEALTHCHECK_URL=<url externa>` e `OFFSITE_BACKUP_ENABLED=true` + `BACKUP_ENCRYPTION_KEY=<chave Fernet>`
   * `ML_JUDGE_ARTIFACT_PATH=/app/models/ml_judge_bull_v1.json` (observador silencioso calibrado; `use_ml_filter` continua `false`)
2. Execute o preflight estrito:
   ```bash
   .venv/bin/python tools/preflight_check.py --env-file .env --target-gate phase1 --strict
   ```
3. Somente após `Overall: PASS`, faça deploy controlado:
   ```bash
   docker compose up -d --build
   docker compose logs -f --tail=100 quant-chameleon
   ```

### Fase 2 — `LIVE_CHAMELEON_PHASE_2` (Após 5 Requisitos de `F1 → F2`)

Exige `PASS` simultâneo nos 5 critérios de `phase_1_to_2` em `tools/gate_report.py` ([Metodologia §4.3](methodology-and-roadmap.md)), novo hash `APPROVED_STRATEGIES_SHA256`, `LIVE_CONFIRMATION=PHASE_2_F1_TO_F2_PASSED` e aprovação em `tools/preflight_check.py --target-gate phase2 --strict`.

---

## 4. Resumo das Ferramentas CLI Operacionais (`tools/`)

| Ferramenta | Finalidade | Acesso ao SQLite |
| --- | --- | --- |
| `tools/preflight_check.py` | Valida `.env`, SHA-256, permissões Ed25519, Fernet, artefato `MLJudge`, Docker mounts e disco antes de subir | Não abre SQLite |
| `tools/gate_report.py` | Avalia Gates 6A, 6B, Fase 1→2 e kill-switches (`PASS`/`FAIL`/`UNKNOWN`) | `URI mode=ro` + `query_only=ON` |
| `tools/daily_gate_digest.py` | Envia resumo diário curto no Telegram (critérios + dia do gate) e gera linha de `cron` (`--print-cron`) | `URI mode=ro` + `query_only=ON` |
| `tools/guided_gate_switch.py` | Transição guiada **exclusiva `--to 6b`** (recusa `FAIL`/`UNKNOWN` e recusa Fase 1/Fase 2; exige confirmação) | `URI mode=ro` + `query_only=ON` |
| `tools/restore_drill.sh` | Testa restauração de backup em diretório isolado `/tmp` (`ANALYST_ONLY`) | Somente cópia em `/tmp` |
| `tools/train_ml_judge.py` | Treina e exporta artefato JSON calibrado do `MLJudge` (Treino/Validação apenas, Holdout excluído) | `URI mode=ro` + `query_only=ON` |
| `tools/radar_ai_audit.py` | Audita Camada 4 (`virtual_signals` + `orderbook_snapshots`), convergência Chameleon×Radar e ablação offline L4 | `URI mode=ro` + `query_only=ON` |
