# Runbook Operacional — QuantCrypto Chameleon & Radar Engine

Guia passo a passo de resposta a incidentes e manutenção operacional, baseado estritamente no comportamento implementado no código.

> [!CAUTION]
> **Regra ADR-002 (Single-Writer):** nunca execute comandos de escrita no SQLite (`data/quant.db`) enquanto o container `quant-chameleon` estiver rodando. Para diagnóstico com o bot ativo, use apenas leitura (`URI mode=ro` / `tools/gate_report.py`). Para qualquer escrita no SQLite ou restauração, pare o container primeiro (`docker compose stop quant-chameleon`).

---

## 1. `HALT_REVIEW` e `EMERGENCY_HALT`

### 1.1. Causas reais no código

| Estado | Gatilho | Onde ocorre no código |
| --- | --- | --- |
| **`HALT_REVIEW`** (na tabela `trades` + `emergency_halt='1'`) | Saldo excedente não rastreado `>= minNotional` em ativo da cesta, ou sem preço de referência para provar que é poeira (**C1-D fail-closed**) | `src/core/state_reconciler.py:572-615` |
| **`HALT_REVIEW`** (na tabela `trades` + `emergency_halt='1'`) | Watchdog de stop pendurado falhou na venda emergencial por saldo insuficiente mesmo após reler saldo e tentar 1 vez | `src/execution/oco_order_manager.py:1318-1352`, `1587-1617` |
| **`HALT_REVIEW_PERSISTENT`** | Trade já estava em `HALT_REVIEW`: o reconciliador mantém o bloqueio até intervenção humana (nunca destrava sozinho) | `src/core/state_reconciler.py:625-639` |
| **`EMERGENCY_HALT`** (`HARD_STOP_DRAWDOWN_12PCT`) | Drawdown tático `>= 12,0%` abaixo do HWM | `src/risk/shadow_manager.py:26,34,288-291` |
| **`EMERGENCY_HALT`** (`BNB_FEE_RESERVE_DEPLETED`) | Saldo livre de `BNB` equivalente a `< $2,00 USDT` (ou falha de ticker + candle de `BNBUSDT` → fail-closed `0`) | `src/risk/shadow_manager.py:27,35,292-295`, `src/main.py:324-337` |
| **`EMERGENCY_HALT`** (`RECONCILIATION_OR_AUTH_FAILURE`) | 3 falhas consecutivas de reconciliação, erro de autenticação (`-1002`, `-1022`, `-2015`, `-2014`), HTTP `418` (IP ban) ou entrada em `HALT_REVIEW` | `src/risk/shadow_manager.py:25,114-126`, `src/core/state_reconciler.py:33,139-154`, `src/execution/oco_order_manager.py:258-291` |
| **`EMERGENCY_HALT`** (`DAILY_TRADES_CAP_EXCEEDED`) | `>= 6` entradas não-rejeitadas em 24h (`max_daily_trades_cap: 6`) | `config/strategies.yaml:18`, `src/main.py:298`, `src/risk/shadow_manager.py:296-299` |

Quando `emergency_halt='1'`, **novas entradas ficam bloqueadas** (`src/main.py:807,887`), mas a gestão/proteção de posições já abertas continua rodando (`src/main.py:789-792`).

### 1.2. Diagnóstico (somente-leitura, seguro com o bot rodando)

1. **Inspecione os logs do container (ou systemd):**
   ```bash
   docker compose logs --tail=200 quant-chameleon
   # Se rodando via systemd (deploy/quant-chameleon.service):
   journalctl -u quant-chameleon -n 200 --no-pager
   ```
2. **Rode o relatório read-only de gates e kill-switches:**
   ```bash
   .venv/bin/python tools/gate_report.py --db data/quant.db --format table
   ```
3. **Consulte o estado exato no SQLite em modo estritamente read-only (`mode=ro`):**
   ```bash
   .venv/bin/python - <<'PY'
   import sqlite3
   conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True, timeout=2.0)
   conn.execute("PRAGMA query_only=ON")
   print("=== system_state ===")
   for k, v in conn.execute("SELECT key, value FROM system_state ORDER BY key"):
       print(f"  {k} = {v}")
   print("=== active / HALT_REVIEW trades ===")
   for r in conn.execute(
       "SELECT trade_id, environment, symbol, status, entry_executed_qty, oco_order_list_id, oco_qty "
       "FROM trades WHERE status IN ('PENDING_ENTRY','ENTRY_FILLED','PROTECTED','CLOSING','HALT_REVIEW')"
   ):
       print(" ", r)
   print("=== recent order_events (last 10) ===")
   for r in conn.execute(
       "SELECT event_id, trade_id, method, endpoint, http_status, binance_code, response_json "
       "FROM order_events ORDER BY event_id DESC LIMIT 10"
   ):
       print(" ", r)
   conn.close()
   PY
   ```
4. **Confira a realidade na Binance (UI ou conta Spot):** verifique saldos `free`/`locked` dos 7 pares de `config/strategies.yaml:70-76` e ordens OCO abertas.

### 1.3. Como destravar com segurança

#### Caso A — Ambiente **TESTNET** (`BINANCE_ENV=TESTNET`, ex.: preparação/incidente no Gate 6A)

O utilitário `src/tools/testnet_prepare.py` inspeciona saldos não rastreados nos 7 ativos da cesta, vende excedentes na Testnet, recarrega BNB se necessário e chama `ShadowManager.reset_emergency_halt()`. Ele **recusa estritamente** `BINANCE_ENV=PRODUCTION` (`src/tools/testnet_prepare.py:55-59`).

```bash
# 1. Preview sem alterar nada (dry-run)
.venv/bin/python -m src.tools.testnet_prepare

# 2. Pare o container antes de qualquer escrita no SQLite (ADR-002)
docker compose stop quant-chameleon

# 3. Se houver trade em HALT_REVIEW já inspecionado (e que será zerado na Testnet), faça backup antes:
cp -a data/quant.db data/quant.db.pre-reset.bak

# 4. Limpe saldos excedentes na Testnet e resete emergency_halt / kill_switch_reason / reconcile_fail_streak
.venv/bin/python -m src.tools.testnet_prepare --execute

# 5. Se havia linha presa em status='HALT_REVIEW' no SQLite após zerar na Testnet, encerre-a explicitamente:
.venv/bin/python - <<'PY'
import time
from pathlib import Path
from src.core.database import Database
db = Database(Path("data/quant.db"))
db.connect()
with db.write_tx() as tx:
    now_ms = int(time.time() * 1000)
    tx.execute(
        "UPDATE trades SET status='CLOSED', exit_reason=COALESCE(exit_reason,'RECONCILER'), "
        "updated_at_ms=?, closed_at_ms=COALESCE(closed_at_ms,?) WHERE status='HALT_REVIEW'",
        (now_ms, now_ms),
    )
db.close()
PY

# 6. Religue o container e confirme boot_reconcile limpo nos logs
docker compose up -d quant-chameleon
docker compose logs -f --tail=100 quant-chameleon
```

#### Caso B — Ambiente **PRODUCTION** (`BINANCE_ENV=PRODUCTION`)

> [!WARNING]
> **Nunca** use `src.tools.testnet_prepare` em Produção (ele aborta com `ConfigError`). Resolva a causa raiz na corretora **antes** de resetar o halt.

1. **Resolva a causa raiz na Binance primeiro:**
   - **Saldo não rastreado (C1-D):** mova/venda manualmente o ativo excedente na Binance (ou transfira para uma subconta dedicada a operações manuais — ADR-012) para que o saldo Spot da conta da API corresponda exatamente ao trade rastreado (ou zero).
   - **Erro de autenticação (`-1002`, `-1022`, `-2015`, `-2014`):** verifique IP permitido, permissões da API Key ou rotacione a chave Ed25519 (Seção 3).
   - **HTTP `418` (IP ban):** aguarde o `Retry-After` expirar completamente e investigue a causa do excesso de peso (`used_weight_1m_peak` em `cycle_health`).
   - **Hard stop `-12%` (`HARD_STOP_DRAWDOWN_12PCT`):** revise os trades fechados e só retome após decisão consciente de risco.
   - **BNB `< $2,00` (`BNB_FEE_RESERVE_DEPLETED`):** recarregue BNB na conta Spot (Seção 4).
   - **Teto diário (`DAILY_TRADES_CAP_EXCEEDED`):** audite por que ocorreram `>= 6` entradas em 24h (`trades` e `order_events`) antes de destravar.
2. **Pare o container e crie backup local de segurança:**
   ```bash
   docker compose stop quant-chameleon
   .venv/bin/python -c "from pathlib import Path; from src.core.database import Database; db=Database(Path('data/quant.db')); db.connect(); p=db.backup_to(Path('data/backups')); db.close(); print('Backup:', p, 'ok=', Database.verify_backup(p))"
   ```
3. **Se havia trade em `HALT_REVIEW` cuja posição já foi encerrada na Binance**, marque-o como `CLOSED` no SQLite (se a posição ainda existe e está protegida por OCO válida, ajuste o registro com o `oco_order_list_id` real em vez de fechar).
4. **Resete `emergency_halt`, `kill_switch_reason` e `reconcile_fail_streak` usando `ShadowManager.reset_emergency_halt()` (`src/risk/shadow_manager.py:133-137`):**
   ```bash
   .venv/bin/python - <<'PY'
   from src.core.config import load_config
   from src.core.database import Database
   from src.risk.shadow_manager import ShadowManager

   cfg = load_config()
   db = Database(cfg.runtime.db_path)
   db.connect()
   sm = ShadowManager(cfg.strategies.drawdown_and_shadow_protection, cfg.strategies.portfolio, db=db)
   sm.reset_emergency_halt()
   db.close()
   print("OK: emergency_halt resetado.")
   PY
   ```
5. **Religue o container e acompanhe o `boot_reconcile` nos logs:**
   ```bash
   docker compose up -d quant-chameleon
   docker compose logs -f --tail=100 quant-chameleon
   ```

---

## 2. Restauração de Backup SQLite

Consulte também o guia detalhado em [Backup e Restauração](backup-and-restore.md).

### 2.1. Validar o backup em ambiente isolado antes de restaurar (obrigatório no Gate 6A)

O script `tools/restore_drill.sh` copia o backup (`.db`, `.db.gz` ou `.db.gz.enc`) para um diretório temporário novo, roda `PRAGMA integrity_check`, sobe `load_config` + `Database` + `QuantOrchestrator` em modo seguro (`MODE=ANALYST_ONLY`, sem chaves, sem tocar em `./data` nem em `./secrets`), confirma o reconhecimento do histórico/trades e remove o diretório temporário:

```bash
# Backup local (.db)
tools/restore_drill.sh data/backups/quant_backup_YYYYMMDD_HHMMSS_ffffff.db --report-json /tmp/restore_drill.json

# Backup offsite criptografado (.db.gz.enc)
BACKUP_ENCRYPTION_KEY='<sua_chave_fernet>' \
  tools/restore_drill.sh /tmp/quant_backup_YYYYMMDD_HHMMSS_ffffff.db.gz.enc --report-json /tmp/restore_drill.json
```

Confirme `RESTORE DRILL: PASS` antes de substituir o banco real.

### 2.2. Restauração real em `./data/quant.db`

```bash
# 1. Pare o container (nunca sobrescreva quant.db em WAL com o bot ativo — ADR-002)
docker compose stop quant-chameleon

# 2. Preserve o banco atual (se existir)
cp -a ./data/quant.db ./data/quant.db.pre-restore.bak 2>/dev/null || true

# 3. Restaure com verificação automática (Database.verify_backup + os.replace + limpeza de -wal/-shm)
BACKUP_ENCRYPTION_KEY='<sua_chave_fernet>' \
  .venv/bin/python -m src.core.offsite_backup restore \
  /tmp/quant_backup_YYYYMMDD_HHMMSS_ffffff.db.gz.enc \
  ./data/quant.db
# (Para backup local .db sem criptografia, passe diretamente o arquivo .db sem BACKUP_ENCRYPTION_KEY.)

# 4. Religue e acompanhe o boot_reconcile nos logs
docker compose up -d quant-chameleon
docker compose logs -f --tail=100 quant-chameleon
```

---

## 3. Rotação das Chaves Ed25519

O bot carrega `BINANCE_API_KEY` e `BINANCE_ED25519_PRIVATE_KEY_PATH` no boot (`src/core/config.py:50-51,488-503`; `_load_ed25519_key()` em `src/core/binance_client.py:678-683` exige `Ed25519PrivateKey` em PEM sem senha). No Docker, `./secrets` é montado somente-leitura em `/app/secrets:ro` (`docker-compose.yml:12`).

1. **Gere um novo par Ed25519 localmente na VPS (com permissão restrita `600`):**
   ```bash
   umask 077
   openssl genpkey -algorithm ED25519 -out secrets/binance_ed25519_new.pem
   openssl pkey -in secrets/binance_ed25519_new.pem -pubout -out /tmp/binance_ed25519_new.pub
   chmod 600 secrets/binance_ed25519_new.pem
   cat /tmp/binance_ed25519_new.pub
   ```
2. **Crie a nova API Key na Binance** (Testnet ou Produção) usando a chave pública gerada:
   - **Habilitar:** *Enable Reading*, *Enable Spot & Margin Trading* (e *Permits Universal Transfer* na Fase 2).
   - **Proibir:** *Enable Withdrawals* (sempre desligado).
   - **Restringir:** vincular exclusivamente ao IP fixo da VPS (`docs/unified-architecture.md:93-98`).
3. **Escolha uma janela fora do fechamento de hora (`XX:05`–`XX:50 UTC`) e pare o container:**
   ```bash
   docker compose stop quant-chameleon
   ```
4. **Atualize `.env`** com o novo `BINANCE_API_KEY` e `BINANCE_ED25519_PRIVATE_KEY_PATH=/app/secrets/binance_ed25519_new.pem` (ou substitua o arquivo PEM mantendo o caminho).
5. **Valide a configuração antes de religar** (sem abrir ordens):
   ```bash
   .venv/bin/python -c "from src.core.config import load_config; cfg = load_config(); print('OK mode=', cfg.runtime.mode.value, 'env=', cfg.runtime.binance_env.value)"
   ```
6. **Religue o container e valide `boot_reconcile` sem erros `-2015`/`-1022`/`-2014`:**
   ```bash
   docker compose up -d quant-chameleon
   docker compose logs -f --tail=100 quant-chameleon
   ```
7. **Somente após confirmar boot e reconciliação limpos**, revogue a API Key antiga na Binance e apague a chave privada antiga + `.pub` temporário:
   ```bash
   rm -f /tmp/binance_ed25519_new.pub secrets/binance_ed25519_old.pem
   ```

---

## 4. BNB Abaixo da Reserva

- **Reserva preventiva:** `< $5,00 USDT` (`min_bnb_fee_reserve_usdt_equiv: 5.00` em `config/strategies.yaml:13`, sinalizado em `DefenseStatus.bnb_below_recommended_reserve` em `src/risk/shadow_manager.py:262-263`).
- **Hard kill-switch:** `< $2,00 USDT` (`HARD_MIN_BNB_RESERVE_USDT = Decimal("2.00")` em `src/risk/shadow_manager.py:27,292-295` → `BNB_FEE_RESERVE_DEPLETED` + `emergency_halt='1'`).
- **Proteção C1-C:** `BNBUSDT` é proibido no universo de execução (`src/core/config.py:21`), portanto recarregar BNB na conta Spot **nunca** dispara `HALT_REVIEW`.

### Procedimento

- **Na Testnet:**
  ```bash
  docker compose stop quant-chameleon
  .venv/bin/python -m src.tools.testnet_prepare --execute   # compra 20 USDT em BNBUSDT se < 10 USDT e reseta halt
  docker compose up -d quant-chameleon
  ```
- **Em Produção:**
  1. Compre ou transfira pelo menos `~$10–$20 USDT` equivalentes em `BNB` para a carteira Spot (`MAIN`) e confirme na Binance que o desconto de taxas em BNB está ativo.
  2. Se o kill-switch `BNB_FEE_RESERVE_DEPLETED` **não** chegou a disparar (`emergency_halt='0'`), nenhuma reinicialização é necessária — o próximo ciclo horário lerá o saldo atualizado via `GET /api/v3/account` (`src/main.py:309-338`).
  3. Se `emergency_halt='1'` já tiver sido acionado, execute o reset da Seção 1.3 (Caso B) com o container parado e religue.

---

## 5. Manutenção da Binance

- **Como o código reage hoje:** `QuantOrchestrator._check_maintenance()` (`src/main.py:274-285`) chama `SignedBinanceClient.sapi_system_status()` (`src/core/binance_client.py:833-845`), que consulta `GET /sapi/v1/system/status` (mockado como `normal=True` quando `BINANCE_ENV=TESTNET`).
  - Se `status != 0` ou a chamada falhar, `maintenance_blocked=True` (fail-closed) bloqueia **novas entradas** (`src/main.py:887`), preservando o acompanhamento de posições já abertas.
  - **Atenção:** o endpoint `/sapi/v1/system/status` da Binance informa apenas manutenção já em andamento (`status == 1`), **não** fornece aviso programado com 2 horas de antecedência.
- **Procedimento operacional:**
  1. **Antes de manutenção programada anunciada pela Binance:** verifique se há trade aberto (`tools/gate_report.py` / query read-only da Seção 1.2) e confirme que a OCO está ativa na corretora (as ordens OCO vivem no motor da Binance, não na VPS).
  2. **Durante a manutenção:** **não** reinicie o bot repetidamente (evita falhas de `boot_reconcile` e acúmulo de `reconcile_fail_streak`). Deixe `maintenance_blocked=True` segurar entradas.
  3. **Após a manutenção:** confirme nos logs que `CandleCollector.repair_gaps()` (`src/main.py:819`) preencheu eventuais lacunas, que `clock_offset_ms` continua dentro de `±500 ms` (`tools/gate_report.py`) e que `emergency_halt` segue inativo (se a indisponibilidade gerou 3 falhas consecutivas de reconciliação, siga a Seção 1.3 após confirmar estabilidade da corretora).

---

## 6. O Que Fazer Se a API da Binance Mudar

### 6.1. Pontos de acoplamento com a Binance Spot API no código

- **Endpoints, pesos e assinaturas:** `ENDPOINT_WEIGHTS` (`src/core/binance_client.py:23-56`), `PublicBinanceClient` (`binance_client.py:430-627`) e `SignedBinanceClient` (`binance_client.py:686-973`, incluindo `POST /api/v3/orderList/oco` em `:911-941`).
- **Parsing de respostas de ordem e OCO:** `extract_fill_summary()` (`src/execution/oco_order_manager.py:66-88`), `_extract_oco_executed_qty()` (`oco_order_manager.py:1169-1225`) e `_inspect_completed_oco_outcome()` (`src/core/state_reconciler.py:314-372`).
- **Códigos de erro tratados:** `-1007`/5XX (`binance_client.py:751`), `-1021` (`oco_order_manager.py:238,996,1189`), `-2010`/`-1013` (`oco_order_manager.py:1027-1089`), `-2013` (`oco_order_manager.py:493,1161`), `429`/`418` (`binance_client.py:457-475`), auth `-1002`/`-1022`/`-2015`/`-2014` (`shadow_manager.py:25`, `state_reconciler.py:33`).

### 6.2. Protocolo de resposta

1. **Contenção imediata (se em Produção com rejeições em série):**
   - Verifique na Binance se alguma posição aberta ficou sem OCO — se sim, proteja/feche manualmente pela interface da Binance primeiro.
   - Se necessário impedir novas entradas enquanto corrige, pare o container (`docker compose stop quant-chameleon`) ou mantenha `emergency_halt='1'`.
2. **Diagnóstico exato do payload:**
   - Leia os últimos registros em `order_events` (query read-only da Seção 1.2) para inspecionar `endpoint`, `http_status`, `binance_code`, `request_json` e `response_json` reais devolvidos pela Binance.
3. **Correção isolada em branch nova:**
   - **Nunca** altere `config/strategies.yaml` nem parâmetros da estratégia `K9.3` para contornar mudança de API.
   - Atualize exclusivamente a camada de integração (`binance_client.py`, `oco_order_manager.py` e/ou `state_reconciler.py`) e crie testes de regressão em `tests/` espelhando o payload real novo da Binance.
4. **Validação obrigatória antes de voltar à Produção:**
   ```bash
   .venv/bin/python -m pytest -q
   ```
   Valide primeiro na **Binance Spot Testnet** (`BINANCE_ENV=TESTNET`) confirmando compra, OCO, `get_order_list` e cancelamento antes de atualizar Produção.

---

## 7. Operação Diária de Gates e Transição Guiada (6A → 6B)

### 7.1. Resumo diário no Telegram (`tools/daily_gate_digest.py`)

A ferramenta `tools/daily_gate_digest.py` executa `tools/gate_report.py` estritamente em modo somente-leitura (`URI mode=ro` + `PRAGMA query_only=ON`, ADR-002), calcula o **dia atual do gate** (`Dia X / 7` no Gate 6A, `Dia X / 30` no Gate 6B, `Dia X / 60` na Fase 1) e envia um resumo curto no Telegram com os critérios `PASS`, `FAIL` e `UNKNOWN` (além dos Kill-Switches §5.2). **Nunca imprime segredos** e sanitiza tokens mesmo em falhas HTTP.

```bash
# 1. Teste local sem enviar ao Telegram (--dry-run)
docker compose logs --no-color --since 192h quant-chameleon > /tmp/bot_digest.log
.venv/bin/python tools/daily_gate_digest.py \
  --db data/quant.db \
  --env-file .env \
  --gate auto \
  --log-file /tmp/bot_digest.log \
  --dry-run

# 2. Envio real do resumo diário ao Telegram
.venv/bin/python tools/daily_gate_digest.py \
  --db data/quant.db \
  --env-file .env \
  --gate auto \
  --log-file /tmp/bot_digest.log

# 3. Gerar comando pronto para agendar no cron da VM (crontab -e)
.venv/bin/python tools/daily_gate_digest.py --print-cron --gate 6a --cron-hour 9
```

Exemplo gerado para `crontab -e` na VM (roda diariamente às `09:05 UTC` sem reiniciar o container):

```cron
5 9 * * * cd /srv/quant-crypto-chameleon-radar-engine && docker compose logs --no-color --since 192h quant-chameleon > /tmp/bot_digest.log 2>&1 && .venv/bin/python tools/daily_gate_digest.py --db data/quant.db --env-file .env --gate 6a --log-file /tmp/bot_digest.log >> /tmp/daily_gate_digest.log 2>&1
```

> **Dica:** após executar o `tools/restore_drill.sh` no Gate 6A, passe também `--restore-drill-report /tmp/restore_drill_6a.json` para que o critério `gate_6a.disaster_restore_drill` deixe de aparecer como `UNKNOWN`.

### 7.2. Transição guiada exclusiva Gate 6A → Gate 6B (`tools/guided_gate_switch.py --to 6b`)

Quando o Gate 6A concluir os 7 dias (168 ciclos horários consecutivos), use `tools/guided_gate_switch.py --to 6b` para auditar e alternar o `.env` para `MODE=SHADOW_CHAMELEON` com segurança:

```bash
# 1. Gere os artefatos exigidos pelo gate_report (sem parar o bot)
docker compose logs --no-color quant-chameleon > /tmp/bot_6a.log
LATEST_BKP=$(ls -1t data/backups/quant_backup_*.db | head -n 1)
tools/restore_drill.sh "$LATEST_BKP" --report-json /tmp/restore_drill_6a.json

# 2. Execute o assistente guiado EXCLUSIVO para 6B
.venv/bin/python tools/guided_gate_switch.py \
  --to 6b \
  --db data/quant.db \
  --env-file .env \
  --log-file /tmp/bot_6a.log \
  --restore-drill-report /tmp/restore_drill_6a.json
```

**Salvaguardas inegociáveis desta ferramenta:**

1. **Bloqueio absoluto em `FAIL` ou `UNKNOWN`:** executa `tools/preflight_check.py --target-gate 6a` e `tools/gate_report.py`; se **qualquer** critério do Gate 6A, Kill-Switch ou Preflight estiver `FAIL` ou `UNKNOWN`, recusa prosseguir sem alterar `.env`.
2. **Confirmação explícita obrigatória:** exibe o resumo das alterações (sem exibir segredos) e só grava o `.env` depois de o operador digitar exatamente `SWITCH TO 6B`.
3. **Backup atômico + menor privilégio (ADR-010):** cria `.env.bak.<UTC>` (`chmod 0600`), altera `MODE=SHADOW_CHAMELEON`, **remove** `BINANCE_API_KEY`, `BINANCE_ED25519_PRIVATE_KEY_PATH`, `LIVE_CONFIRMATION` e `APPROVED_STRATEGIES_SHA256`, e revalida o `.env` final com `preflight_check --target-gate 6b` (revertendo automaticamente para o backup em caso de falha).
4. **Sem reinício automático:** não executa `docker compose up`; o operador reinicia manualmente quando estiver pronto.
5. **Fase 1 (dinheiro real) e Fase 2 são ESTRITAMENTE MANUAIS:** a ferramenta **recusa** qualquer alvo diferente de `6b` (ex.: `--to phase1` ou `--to phase2` encerra com código `2`). A decisão pós-Gate 6B segue a regra pré-registrada em [ADR-016](adr-016-regra-de-decisao-6b.md).

