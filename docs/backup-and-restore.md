# Backup SQLite Fora da VM e Procedimento de Restauração

## 1. Constatação sobre Criptografia do Backup Local

Embora o comentário em `config/strategies.yaml:31` (e `docs/architecture.md:386`) descreva `daily_sqlite_backup_enabled: true` como *"Backup criptografado diário contra perda de disco em VPS efêmera"*, a implementação de `Database.backup_to()` em `src/core/database.py:713-724` executa apenas `sqlite3.connect(str(dest_path))` + `conn.backup(dst_conn, pages=-1)`.

* **O backup local gravado em `BACKUP_DIR` (`*.db`) NÃO é criptografado** (é um arquivo SQLite padrão com cabeçalho `SQLite format 3\0`).
* Para o envio fora da VM (`src/core/offsite_backup.py`), foi adicionada compressão `gzip` e **criptografia autenticada obrigatória via Fernet (AES-128-CBC + HMAC-SHA256, usando `cryptography` já presente em `requirements.txt:7`)** controlada pela variável `BACKUP_ENCRYPTION_KEY`. Se `OFFSITE_BACKUP_ENABLED=true` e `BACKUP_ENCRYPTION_KEY` estiver vazia, o boot falha explicitamente (`src/core/config.py`).

---

## 2. Backup Offsite via Telegram (`sendDocument`, C2-8 < 50 MB)

Alinhado ao requisito **C2-8** (`docs/integration-audit.md:147`), que limita o banco para caber no teto gratuito de **50 MB** da Telegram Bot API:

* **Desligado por padrão:** `OFFSITE_BACKUP_ENABLED=false` no `.env`.
* **O que é enviado:** exclusivamente o arquivo `.db` único retornado por `Database.backup_to()`, **somente após** `Database.verify_backup()` retornar `True` (`src/main.py:913-916`), compactado em `gzip` e criptografado com Fernet (`.db.gz.enc`, exigido no boot quando `OFFSITE_BACKUP_ENABLED=true`).
* **O que NUNCA é incluído:** `.env`, `secrets/`, chaves privadas `.pem`/`.key` ou qualquer outro arquivo de diretório (`src/core/offsite_backup.py:29-39`).
* **Isolamento de falhas:** erros de rede/API ou limite de 50 MB geram apenas `logger.error` + alerta `BACKUP_ERROR` na fila `telegram_outbox` (`src/main.py:937-946`), nunca derrubam o ciclo horário e preservam o backup local verificado. O artefato temporário `.gz`/`.enc` é removido no bloco `finally`.

### Como ativar no `.env`

1. Gere uma chave Fernet e guarde-a em um cofre de senhas fora da VPS (você precisará dela para restaurar):
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. Configure no `.env` (sem alterar `config/strategies.yaml`):
   ```dotenv
   OFFSITE_BACKUP_ENABLED=true
   # Opcional: ID de um canal/grupo privado dedicado a backups (vazio = usa TELEGRAM_CHAT_ID)
   BACKUP_TELEGRAM_CHAT_ID=
   # Obrigatório quando OFFSITE_BACKUP_ENABLED=true (falha no boot se vazia)
   BACKUP_ENCRYPTION_KEY=<sua_chave_fernet_aqui>
   ```

---

## 3. Procedimento de Restauração a Partir do Backup Offsite

> [!CAUTION]
> **Nunca** sobrescreva `quant.db` com o container em execução (risco de corrupção WAL — ADR-002). Pare o processo primeiro e remova quaisquer arquivos `-wal`/`-shm` residuais.

### Passo a passo (Docker Compose)

1. **Pare o container do bot** (aguarde o encerramento limpo):
   ```bash
   docker compose stop quant-chameleon
   ```
2. **Baixe o arquivo de backup mais recente** (`.db.gz.enc` ou `.db.gz`) do chat/canal do Telegram para a máquina de recuperação (ex.: `/tmp/quant_backup_20261123_000003_123456.db.gz.enc`).
3. **Preserve uma cópia do banco atual** (se existir):
   ```bash
   cp -a ./data/quant.db ./data/quant.db.pre-restore.bak 2>/dev/null || true
   ```
4. **Restaure e valide a integridade usando o utilitário oficial** (`src/core/offsite_backup.py`):
   * Se o backup estiver **criptografado** (`.db.gz.enc`):
     ```bash
     BACKUP_ENCRYPTION_KEY='<sua_chave_fernet>' \
       python3 -m src.core.offsite_backup restore \
       /tmp/quant_backup_20261123_000003_123456.db.gz.enc \
       ./data/quant.db
     ```
   * Se o backup for **somente gzip** (`.db.gz`):
     ```bash
     python3 -m src.core.offsite_backup restore \
       /tmp/quant_backup_20261123_000003_123456.db.gz \
       ./data/quant.db
     ```
   O comando grava primeiro em arquivo temporário, valida `Database.verify_backup()` (`PRAGMA integrity_check == 'ok'` e `schema_meta.version == 1`), faz substituição atômica (`os.replace`) e remove `-wal`/`-shm` antigos.
   * Alternativa manual sem CLI (para `.db.gz` sem criptografia):
     ```bash
     gunzip -c /tmp/quant_backup_20261123_000003_123456.db.gz > ./data/quant.db
     rm -f ./data/quant.db-wal ./data/quant.db-shm
     sqlite3 ./data/quant.db "PRAGMA integrity_check; SELECT key, value FROM schema_meta;"
     ```
5. **Religue o container e acompanhe o `boot_reconcile` nos logs:**
   ```bash
   docker compose up -d quant-chameleon
   docker compose logs -f --tail=100 quant-chameleon
   ```
   Em modo `LIVE_CHAMELEON_PHASE_1`, o `StateReconciler` (`src/core/state_reconciler.py`) reconciliará imediatamente o estado restaurado contra os saldos e OCOs reais na Binance no boot.
