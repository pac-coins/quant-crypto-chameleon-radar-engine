#!/usr/bin/env bash
# Gate 6A Disaster Restore Drill (docs/methodology-and-roadmap.md §4.1)
#
# Copia um backup SQLite (.db, .db.gz ou .db.gz.enc) para um diretório temporário
# isolado, valida PRAGMA integrity_check + schema_meta, sobe o sistema em modo seguro
# (MODE=ANALYST_ONLY, sem chaves privadas, sem chamadas externas, sem tocar em ./data
# nem em ./secrets), confirma que histórico e estado de trades foram reconhecidos,
# imprime PASS ou FAIL e remove o diretório temporário ao final.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
else
    PYTHON_BIN="$(command -v python3)"
fi

usage() {
    cat <<'EOF'
Uso: tools/restore_drill.sh <caminho_do_backup> [--key <fernet_key>] [--report-json <arquivo.json>]

Argumentos:
  <caminho_do_backup>       Arquivo .db, .db.gz ou .db.gz.enc gerado pelo backup.
  --key <fernet_key>        Chave Fernet para .db.gz.enc (padrão: $BACKUP_ENCRYPTION_KEY).
  --report-json <arquivo>   Opcional: grava relatório JSON compatível com
                            tools/gate_report.py --restore-drill-report <arquivo>.
                            (Nunca pode apontar para ./data ou ./secrets.)
EOF
}

BACKUP_INPUT=""
FERNET_KEY="${BACKUP_ENCRYPTION_KEY:-}"
REPORT_JSON=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --key)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --key requer um valor." >&2
                echo "RESTORE DRILL: FAIL"
                exit 2
            fi
            FERNET_KEY="$2"
            shift 2
            ;;
        --report-json)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --report-json requer um caminho." >&2
                echo "RESTORE DRILL: FAIL"
                exit 2
            fi
            REPORT_JSON="$2"
            shift 2
            ;;
        -*)
            echo "ERROR: argumento desconhecido: $1" >&2
            usage >&2
            echo "RESTORE DRILL: FAIL"
            exit 2
            ;;
        *)
            if [[ -z "${BACKUP_INPUT}" ]]; then
                BACKUP_INPUT="$1"
                shift
            else
                echo "ERROR: argumento posicional extra inesperado: $1" >&2
                echo "RESTORE DRILL: FAIL"
                exit 2
            fi
            ;;
    esac
done

if [[ -z "${BACKUP_INPUT}" ]]; then
    usage >&2
    echo "RESTORE DRILL: FAIL"
    exit 2
fi

# Bloqueia qualquer escrita em ./data ou ./secrets via --report-json
if [[ -n "${REPORT_JSON}" ]]; then
    if ! "${PYTHON_BIN}" - "${REPO_ROOT}" "${PWD}" "${REPORT_JSON}" <<'PY'
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
cwd = Path(sys.argv[2]).resolve()
target = Path(sys.argv[3]).resolve()

forbidden = [
    repo_root / "data",
    repo_root / "secrets",
    cwd / "data",
    cwd / "secrets",
]
for base in forbidden:
    if target == base or base in target.parents:
        print(f"ERROR: --report-json não pode escrever em diretório protegido: {base}", file=sys.stderr)
        sys.exit(1)
PY
    then
        echo "RESTORE DRILL: FAIL"
        exit 1
    fi
fi

DRILL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/quant_restore_drill.XXXXXX")"

cleanup() {
    if [[ -n "${DRILL_DIR:-}" && -d "${DRILL_DIR}" ]]; then
        rm -rf "${DRILL_DIR}"
    fi
}
trap cleanup EXIT INT TERM

write_report_json() {
    local status="$1"
    local detail="$2"
    local summary_file="${3:-}"
    if [[ -z "${REPORT_JSON}" ]]; then
        return 0
    fi
    "${PYTHON_BIN}" - "${REPORT_JSON}" "${status}" "${BACKUP_INPUT}" "${detail}" "${summary_file}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

out_path = Path(sys.argv[1])
status = sys.argv[2]
backup_path = sys.argv[3]
detail = sys.argv[4]
summary_file = sys.argv[5]

payload = {
    "status": status,
    "backup_path": str(Path(backup_path).resolve()) if Path(backup_path).exists() else backup_path,
    "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    "detail": detail,
}
if summary_file and Path(summary_file).is_file():
    try:
        payload["verification"] = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    except Exception:
        pass

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

fail_drill() {
    local msg="$1"
    echo "[restore_drill] ERROR: ${msg}" >&2
    write_report_json "FAIL" "${msg}"
    echo "RESTORE DRILL: FAIL"
    exit 1
}

if [[ ! -f "${BACKUP_INPUT}" ]]; then
    fail_drill "Arquivo de backup não encontrado: ${BACKUP_INPUT}"
fi

# 1. Copia o backup para o diretório temporário (nunca modifica o original)
mkdir -p "${DRILL_DIR}/source" "${DRILL_DIR}/data" "${DRILL_DIR}/backups"
BACKUP_BASENAME="$(basename "${BACKUP_INPUT}")"
COPIED_BACKUP="${DRILL_DIR}/source/${BACKUP_BASENAME}"
RESTORED_DB="${DRILL_DIR}/data/quant.db"
cp -p "${BACKUP_INPUT}" "${COPIED_BACKUP}"

# 2. Descompacta / descriptografa para ${RESTORED_DB} se for .gz ou .enc
LOWER_NAME="$(echo "${BACKUP_BASENAME}" | tr '[:upper:]' '[:lower:]')"
if [[ "${LOWER_NAME}" == *.enc || "${LOWER_NAME}" == *.gz ]]; then
    RESTORE_CMD=("${PYTHON_BIN}" -m src.core.offsite_backup restore "${COPIED_BACKUP}" "${RESTORED_DB}")
    if [[ -n "${FERNET_KEY}" ]]; then
        RESTORE_CMD+=(--key "${FERNET_KEY}")
    fi
    if ! PYTHONPATH="${REPO_ROOT}" "${RESTORE_CMD[@]}" >"${DRILL_DIR}/unpack.log" 2>&1; then
        UNPACK_ERR="$(cat "${DRILL_DIR}/unpack.log" 2>/dev/null || true)"
        fail_drill "Falha ao restaurar artefato (${BACKUP_BASENAME}): ${UNPACK_ERR}"
    fi
else
    cp -p "${COPIED_BACKUP}" "${RESTORED_DB}"
fi

# Limpa TODAS as credenciais/variáveis sensíveis do ambiente antes de subir o sistema em modo seguro
unset BINANCE_API_KEY BINANCE_ED25519_PRIVATE_KEY_PATH LIVE_CONFIRMATION \
      APPROVED_STRATEGIES_SHA256 HEALTHCHECK_URL OFFSITE_BACKUP_ENABLED \
      BACKUP_TELEGRAM_CHAT_ID BACKUP_ENCRYPTION_KEY MODE BINANCE_ENV \
      DB_PATH BACKUP_DIR TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID FERNET_KEY

# 3. Cria .env isolado em modo seguro (ANALYST_ONLY, zero chaves, apontando somente para ${DRILL_DIR})
DRILL_ENV="${DRILL_DIR}/.env.drill"
cat > "${DRILL_ENV}" <<EOF
MODE=ANALYST_ONLY
BINANCE_ENV=TESTNET
TELEGRAM_BOT_TOKEN=000000:RESTORE_DRILL_NOOP
TELEGRAM_CHAT_ID=0
DB_PATH=${RESTORED_DB}
BACKUP_DIR=${DRILL_DIR}/backups
HEALTHCHECK_URL=
OFFSITE_BACKUP_ENABLED=false
EOF

SUMMARY_JSON="${DRILL_DIR}/verification.json"

# 4. Roda PRAGMA integrity_check, sobe o sistema real em modo seguro no diretório temporário
#    e confirma reconhecimento integral do histórico e do estado de trades
if ! (
    cd "${DRILL_DIR}"
    PYTHONPATH="${REPO_ROOT}" "${PYTHON_BIN}" - \
        "${REPO_ROOT}" "${DRILL_DIR}" "${DRILL_ENV}" "${RESTORED_DB}" "${SUMMARY_JSON}" <<'PY'
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
drill_dir = Path(sys.argv[2]).resolve()
drill_env = Path(sys.argv[3]).resolve()
restored_db = Path(sys.argv[4]).resolve()
summary_out = Path(sys.argv[5]).resolve()

# Passo A: PRAGMA integrity_check + snapshot pré-boot no SQLite restaurado
conn = sqlite3.connect(f"file:{restored_db.as_posix()}?mode=ro", uri=True, timeout=2.0)
conn.execute("PRAGMA query_only=ON")
conn.row_factory = sqlite3.Row
try:
    ic_rows = [str(r[0]).lower() for r in conn.execute("PRAGMA integrity_check").fetchall()]
    if ic_rows != ["ok"]:
        raise RuntimeError(f"PRAGMA integrity_check falhou: {ic_rows}")

    ver_row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    if ver_row is None or str(ver_row["value"]) != "1":
        raise RuntimeError(f"schema_meta.version inválido: {dict(ver_row) if ver_row else None}")

    tracked_tables = (
        "candles",
        "trades",
        "shadow_trades",
        "cycle_health",
        "system_state",
        "regime_history",
        "virtual_signals",
        "order_events",
        "telegram_outbox",
    )
    pre_counts = {
        tbl: int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
        for tbl in tracked_tables
    }
    active_row = conn.execute(
        """
        SELECT trade_id, symbol, status, oco_order_list_id
        FROM trades
        WHERE status IN ('PENDING_ENTRY', 'ENTRY_FILLED', 'PROTECTED', 'CLOSING', 'HALT_REVIEW')
        LIMIT 1
        """
    ).fetchone()
    pre_active_trade = dict(active_row) if active_row is not None else None

    closed_live_count = int(
        conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'CLOSED'").fetchone()[0]
    )
    open_shadow_row = conn.execute(
        "SELECT shadow_id, symbol, status FROM shadow_trades WHERE status = 'OPEN' LIMIT 1"
    ).fetchone()
    pre_open_shadow = dict(open_shadow_row) if open_shadow_row is not None else None

    pre_state = {
        str(r["key"]): str(r["value"])
        for r in conn.execute("SELECT key, value FROM system_state").fetchall()
    }
finally:
    conn.close()

# Passo B: Sobe o sistema real em modo seguro (ANALYST_ONLY, sem chaves, isolado em drill_dir)
from src.core.binance_client import ExchangeInfoCache, build_clients
from src.core.config import RunMode, load_config
from src.core.database import Database
from src.main import QuantOrchestrator
from src.notifications.telegram import TelegramOutbox

cfg = load_config(
    env_file=drill_env,
    strategies_path=repo_root / "config" / "strategies.yaml",
    analyst_path=repo_root / "config" / "analyst.yaml",
)
if cfg.runtime.mode != RunMode.ANALYST_ONLY:
    raise RuntimeError(f"Modo inseguro no drill: {cfg.runtime.mode}")
if cfg.runtime.binance_api_key is not None or cfg.runtime.binance_ed25519_private_key_path is not None:
    raise RuntimeError("Credenciais Binance não podem estar presentes no restore drill!")
resolved_cfg_db = cfg.runtime.db_path.resolve()
if resolved_cfg_db != restored_db or drill_dir not in resolved_cfg_db.parents:
    raise RuntimeError(f"DB_PATH fora do diretório temporário do drill: {resolved_cfg_db}")

db = Database(cfg.runtime.db_path)
db.connect()
try:
    db.migrate()
    public_client, signed_client, _governor, clock = build_clients(cfg)
    if signed_client is not None:
        raise RuntimeError("signed_client deve ser None em modo seguro (ANALYST_ONLY)!")

    exchange_cache = ExchangeInfoCache(public_client, db, cfg.market_data_universe)
    outbox = TelegramOutbox(db, cfg.runtime.telegram_chat_id, transport=None)
    orch = QuantOrchestrator(
        settings=cfg.runtime,
        strategies_cfg=cfg.strategies,
        analyst_cfg=cfg.analyst,
        db=db,
        public_client=public_client,
        signed_client=None,
        clock=clock,
        exchange_cache=exchange_cache,
        outbox=outbox,
        sleep_fn=lambda _: None,
    )

    # Passo C: Confirma que o sistema reconheceu histórico e estado de trades integralmente
    with db.read() as rconn:
        post_counts = {
            tbl: int(rconn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
            for tbl in tracked_tables
        }
    if post_counts != pre_counts:
        raise RuntimeError(f"Divergência de contagem de histórico pós-boot: {pre_counts} != {post_counts}")

    boot_active = orch._has_open_live_trade()
    if pre_active_trade is None:
        if boot_active is not None:
            raise RuntimeError(f"Trade ativo inesperado após boot: {dict(boot_active)}")
    else:
        if boot_active is None:
            raise RuntimeError(f"Trade ativo do backup não foi reconhecido no boot: {pre_active_trade}")
        if (
            int(boot_active["trade_id"]) != int(pre_active_trade["trade_id"])
            or str(boot_active["status"]) != str(pre_active_trade["status"])
            or str(boot_active["symbol"]) != str(pre_active_trade["symbol"])
        ):
            raise RuntimeError(
                f"Estado do trade ativo divergente: esperado={pre_active_trade}, boot={dict(boot_active)}"
            )

    boot_has_shadow = orch.paper_executor.has_open_shadow_trade()
    if boot_has_shadow != (pre_open_shadow is not None):
        raise RuntimeError(
            f"Reconhecimento de shadow_trade OPEN divergente: esperado={pre_open_shadow is not None}, boot={boot_has_shadow}"
        )

    for key in ("emergency_halt", "kill_switch_reason", "tactical_hwm_usdt", "reconcile_fail_streak"):
        if key in pre_state:
            got = orch.shadow_manager._get(key, "")
            if got != pre_state[key]:
                raise RuntimeError(f"system_state['{key}'] divergente: esperado={pre_state[key]!r}, boot={got!r}")
finally:
    db.close()

if not Database.verify_backup(restored_db):
    raise RuntimeError("Database.verify_backup() falhou após o boot em modo seguro.")

summary = {
    "integrity_check": "ok",
    "schema_version": "1",
    "safe_mode": cfg.runtime.mode.value,
    "signed_client_present": False,
    "table_counts": post_counts,
    "closed_live_trades": closed_live_count,
    "active_live_trade": pre_active_trade,
    "open_shadow_trade": pre_open_shadow,
    "system_state_keys": sorted(pre_state.keys()),
}
summary_out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(
    f"[restore_drill] integrity_check=ok | mode={cfg.runtime.mode.value} | "
    f"candles={post_counts['candles']} trades={post_counts['trades']} "
    f"(closed={closed_live_count}, active={pre_active_trade['status'] if pre_active_trade else 'NONE'}) | "
    f"shadow_trades={post_counts['shadow_trades']} cycle_health={post_counts['cycle_health']} "
    f"system_state={post_counts['system_state']}"
)
PY
); then
    fail_drill "Verificação de integridade ou boot em modo seguro falhou."
fi

write_report_json "PASS" "integrity_check=ok e histórico/estado reconhecidos em modo seguro (ANALYST_ONLY)" "${SUMMARY_JSON}"
echo "RESTORE DRILL: PASS"
exit 0
