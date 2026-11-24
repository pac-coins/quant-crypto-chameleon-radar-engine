#!/usr/bin/env python3
"""Resumo diário somente-leitura de Gate (6A / 6B / Fase 1) enviado via Telegram.

Regras de segurança e arquitetura:
- Abre o SQLite exclusivamente em modo somente-leitura (`URI mode=ro` + `PRAGMA query_only=ON`)
  via `tools/gate_report.py` (ADR-002). Nunca escreve no banco nem cria locks de escrita.
- Nunca imprime nem registra segredos (`TELEGRAM_BOT_TOKEN`, chaves Binance, Fernet, etc.),
  inclusive sanitizando mensagens de erro HTTP que poderiam conter a URL do bot Telegram.
- Gera exemplo pronto de agendamento `cron` para a VM (`--print-cron`).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.notifications.telegram import HttpTelegramTransport, TelegramTransport
from tools.gate_report import (
    GATE_6A_MIN_CONSECUTIVE_CYCLES,
    GATE_6B_MIN_DAYS,
    ONE_DAY_MS,
    PHASE_1_TO_2_MIN_DAYS,
    _open_sqlite_readonly,
    generate_gate_report,
)

GateName = Literal["6a", "6b", "phase1"]
GateChoice = Literal["auto", "6a", "6b", "phase1"]

_GATE_METADATA: dict[GateName, tuple[str, str, int]] = {
    "6a": ("gate_6a", "Gate 6A (Testnet)", int(GATE_6A_MIN_CONSECUTIVE_CYCLES // 24)),
    "6b": ("gate_6b", "Gate 6B (Shadow)", int(GATE_6B_MIN_DAYS)),
    "phase1": ("phase_1_to_2", "Fase 1 -> Fase 2 (Production)", int(PHASE_1_TO_2_MIN_DAYS)),
}

_STATUS_ICON: dict[str, str] = {
    "PASS": "✅",
    "FAIL": "❌",
    "UNKNOWN": "❓",
}

MAX_TELEGRAM_CHARS = 3800


@dataclass(frozen=True, slots=True)
class GateDayProgress:
    gate: GateName
    label: str
    target_days: int
    current_day: int
    elapsed_days: Optional[float]
    first_ts_ms: Optional[int]
    latest_ts_ms: Optional[int]
    sample_source: str


def parse_env_file_readonly(env_file: Optional[Path]) -> dict[str, str]:
    """Lê pares KEY=VALUE de um arquivo .env sem modificar o ambiente nem logar valores."""
    if env_file is None or not env_file.is_file():
        return {}
    out: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
            if "=" not in line:
                continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def redact_secrets(text: str, secrets: Sequence[str]) -> str:
    """Remove qualquer ocorrência literal de segredos antes de imprimir ou propagar erro."""
    sanitized = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def detect_active_gate(
    *,
    db_path: Path,
    env_values: dict[str, str],
) -> GateName:
    """Inferência determinística do gate ativo a partir do .env ou do SQLite (read-only)."""
    mode = env_values.get("MODE", "").strip().upper()
    binance_env = env_values.get("BINANCE_ENV", "").strip().upper()

    if mode == "SHADOW_CHAMELEON":
        return "6b"
    if mode == "LIVE_CHAMELEON_PHASE_1":
        return "phase1" if binance_env == "PRODUCTION" else "6a"
    if mode == "LIVE_CHAMELEON_PHASE_2":
        return "phase1"

    conn = _open_sqlite_readonly(db_path)
    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "cycle_health" in tables:
            row = conn.execute(
                "SELECT mode FROM cycle_health ORDER BY cycle_close_time_ms DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                last_mode = str(row["mode"]).upper()
                if last_mode == "SHADOW_CHAMELEON":
                    return "6b"
                if last_mode == "LIVE_CHAMELEON_PHASE_1":
                    if "trades" in tables:
                        prod_row = conn.execute(
                            "SELECT 1 FROM trades WHERE environment = 'PRODUCTION' LIMIT 1"
                        ).fetchone()
                        if prod_row is not None:
                            return "phase1"
                    return "6a"
        if "shadow_trades" in tables:
            sh = conn.execute("SELECT 1 FROM shadow_trades LIMIT 1").fetchone()
            if sh is not None:
                return "6b"
    finally:
        conn.close()
    return "6a"


def compute_gate_day_progress(
    db_path: Path,
    gate: GateName,
    *,
    now_ms: Optional[int] = None,
) -> GateDayProgress:
    """Calcula em que dia do gate estamos lendo o SQLite em modo estritamente read-only."""
    _, label, target_days = _GATE_METADATA[gate]
    conn = _open_sqlite_readonly(db_path)
    timestamps: list[int] = []
    source = "sem registros"

    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if gate == "6a":
            if "cycle_health" in tables:
                rows = conn.execute(
                    "SELECT MIN(cycle_close_time_ms) AS mn, MAX(cycle_close_time_ms) AS mx "
                    "FROM cycle_health WHERE mode = 'LIVE_CHAMELEON_PHASE_1'"
                ).fetchone()
                if rows is not None and rows["mn"] is not None:
                    timestamps.extend([int(rows["mn"]), int(rows["mx"])])
                    source = "cycle_health(LIVE_CHAMELEON_PHASE_1)"
            if not timestamps and "trades" in tables:
                rows = conn.execute(
                    "SELECT MIN(created_at_ms) AS mn, MAX(COALESCE(closed_at_ms, updated_at_ms)) AS mx "
                    "FROM trades WHERE environment = 'TESTNET' AND status != 'ENTRY_REJECTED'"
                ).fetchone()
                if rows is not None and rows["mn"] is not None:
                    timestamps.extend([int(rows["mn"]), int(rows["mx"])])
                    source = "trades(TESTNET)"
        elif gate == "6b":
            if "cycle_health" in tables:
                rows = conn.execute(
                    "SELECT MIN(cycle_close_time_ms) AS mn, MAX(cycle_close_time_ms) AS mx "
                    "FROM cycle_health WHERE mode = 'SHADOW_CHAMELEON'"
                ).fetchone()
                if rows is not None and rows["mn"] is not None:
                    timestamps.extend([int(rows["mn"]), int(rows["mx"])])
                    source = "cycle_health(SHADOW_CHAMELEON)"
            if "shadow_trades" in tables:
                rows = conn.execute(
                    "SELECT MIN(created_at_ms) AS mn, MAX(COALESCE(closed_at_ms, created_at_ms)) AS mx "
                    "FROM shadow_trades"
                ).fetchone()
                if rows is not None and rows["mn"] is not None:
                    timestamps.extend([int(rows["mn"]), int(rows["mx"])])
                    source = (
                        "cycle_health+shadow_trades"
                        if source != "sem registros"
                        else "shadow_trades"
                    )
        else:
            if "trades" in tables:
                rows = conn.execute(
                    "SELECT MIN(created_at_ms) AS mn, MAX(COALESCE(closed_at_ms, updated_at_ms)) AS mx "
                    "FROM trades WHERE environment = 'PRODUCTION' AND status != 'ENTRY_REJECTED'"
                ).fetchone()
                if rows is not None and rows["mn"] is not None:
                    timestamps.extend([int(rows["mn"]), int(rows["mx"])])
                    source = "trades(PRODUCTION)"
            if not timestamps and "cycle_health" in tables:
                rows = conn.execute(
                    "SELECT MIN(cycle_close_time_ms) AS mn, MAX(cycle_close_time_ms) AS mx "
                    "FROM cycle_health WHERE mode IN ('LIVE_CHAMELEON_PHASE_1', 'LIVE_CHAMELEON_PHASE_2')"
                ).fetchone()
                if rows is not None and rows["mn"] is not None:
                    timestamps.extend([int(rows["mn"]), int(rows["mx"])])
                    source = "cycle_health(LIVE)"
    finally:
        conn.close()

    if not timestamps:
        return GateDayProgress(
            gate=gate,
            label=label,
            target_days=target_days,
            current_day=0,
            elapsed_days=None,
            first_ts_ms=None,
            latest_ts_ms=None,
            sample_source=source,
        )

    first_ms = min(timestamps)
    latest_ms = max(timestamps)
    ref_end_ms = max(latest_ms, int(now_ms)) if now_ms is not None else latest_ms
    elapsed_days = max(0.0, (ref_end_ms - first_ms) / float(ONE_DAY_MS))
    current_day = int(elapsed_days) + 1

    return GateDayProgress(
        gate=gate,
        label=label,
        target_days=target_days,
        current_day=current_day,
        elapsed_days=elapsed_days,
        first_ts_ms=first_ms,
        latest_ts_ms=latest_ms,
        sample_source=source,
    )


def format_daily_digest(
    report: dict[str, Any],
    progress: GateDayProgress,
) -> str:
    """Formata um resumo curto e legível para Telegram (<= 3800 caracteres, zero segredos)."""
    section_key, label, _ = _GATE_METADATA[progress.gate]
    gate_section = report["sections"][section_key]
    ks_section = report["sections"]["kill_switches"]

    gate_criteria: list[dict[str, Any]] = list(gate_section["criteria"])
    ks_criteria: list[dict[str, Any]] = list(ks_section["criteria"])

    all_criteria = gate_criteria + ks_criteria
    n_pass = sum(1 for c in all_criteria if c["status"] == "PASS")
    n_fail = sum(1 for c in all_criteria if c["status"] == "FAIL")
    n_unknown = sum(1 for c in all_criteria if c["status"] == "UNKNOWN")

    overall_icon = _STATUS_ICON.get(gate_section["status"], "❓")
    ks_icon = _STATUS_ICON.get(ks_section["status"], "❓")

    if progress.elapsed_days is not None:
        day_line = (
            f"📅 Progresso: Dia {progress.current_day} / {progress.target_days} "
            f"({progress.elapsed_days:.1f} dias decorridos)"
        )
    else:
        day_line = f"📅 Progresso: Dia 0 / {progress.target_days} (sem registros ainda)"

    gen_utc = str(report.get("generated_at_utc", datetime.now(timezone.utc).isoformat()))
    lines = [
        f"📊 RESUMO DIÁRIO — {label}",
        day_line,
        f"Status Gate: {overall_icon} {gate_section['status']} | Kill-Switches: {ks_icon} {ks_section['status']}",
        f"Totais: ✅ {n_pass} PASS | ❌ {n_fail} FAIL | ❓ {n_unknown} UNKNOWN",
        "",
        f"Critérios ({label}):",
    ]

    for crit in gate_criteria:
        icon = _STATUS_ICON.get(str(crit["status"]), "❓")
        lines.append(f"{icon} {crit['name']}: {crit['measured']}")

    lines.append("")
    lines.append("Kill-Switches (§5.2):")
    for crit in ks_criteria:
        icon = _STATUS_ICON.get(str(crit["status"]), "❓")
        lines.append(f"{icon} {crit['name']}: {crit['measured']}")

    lines.append("")
    lines.append(f"UTC: {gen_utc}")

    text = "\n".join(lines)
    if len(text) > MAX_TELEGRAM_CHARS:
        text = text[: MAX_TELEGRAM_CHARS - 20].rstrip() + "\n... [truncado]"
    return text


def build_cron_example(
    *,
    repo_dir: Path = _REPO_ROOT,
    gate: GateChoice = "auto",
    db_rel_path: str = "data/quant.db",
    env_rel_path: str = ".env",
    cron_hour_utc: int = 9,
    cron_minute: int = 5,
    restore_drill_report: Optional[str] = None,
) -> str:
    """Gera bloco pronto para `crontab -e` na VM (sem credenciais embutidas)."""
    hour = max(0, min(23, int(cron_hour_utc)))
    minute = max(0, min(59, int(cron_minute)))
    repo_str = Path(repo_dir).as_posix()
    restore_flag = (
        f" --restore-drill-report {restore_drill_report}" if restore_drill_report else ""
    )
    cron_line = (
        f"{minute} {hour} * * * cd {repo_str} && "
        "docker compose logs --no-color --since 192h quant-chameleon > /tmp/bot_digest.log 2>&1 && "
        f".venv/bin/python tools/daily_gate_digest.py "
        f"--db {db_rel_path} --env-file {env_rel_path} --gate {gate} "
        f"--log-file /tmp/bot_digest.log{restore_flag} "
        ">> /tmp/daily_gate_digest.log 2>&1"
    )
    return (
        "# Exemplo para agendar no cron da VM (`crontab -e`, horário UTC do container/VPS):\n"
        "# Exporta os logs recentes do container e envia o resumo somente-leitura no Telegram.\n"
        f"{cron_line}\n"
    )


def run_daily_digest(
    *,
    db_path: Path,
    gate: GateChoice = "auto",
    env_file: Optional[Path] = Path(".env"),
    log_file: Optional[Path] = None,
    restore_drill_report: Optional[Path] = None,
    dry_run: bool = False,
    now_ms: Optional[int] = None,
    transport: Optional[TelegramTransport] = None,
) -> dict[str, Any]:
    """Executa o gate_report somente-leitura, formata o resumo e envia no Telegram (exceto --dry-run)."""
    env_vals = parse_env_file_readonly(env_file)
    resolved_gate: GateName = (
        detect_active_gate(db_path=db_path, env_values=env_vals)
        if gate == "auto"
        else gate
    )

    report = generate_gate_report(
        db_path=db_path,
        log_file=log_file,
        restore_drill_report=restore_drill_report,
    )
    progress = compute_gate_day_progress(db_path, resolved_gate, now_ms=now_ms)
    message = format_daily_digest(report, progress)

    bot_token = (
        env_vals.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    chat_id = (
        env_vals.get("TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    secrets_to_scrub = [
        v
        for k, v in env_vals.items()
        if any(tok in k.upper() for tok in ("TOKEN", "KEY", "SECRET", "PASSWORD", "URL")) and v
    ]
    if bot_token:
        secrets_to_scrub.append(bot_token)

    message = redact_secrets(message, secrets_to_scrub)

    sent = False
    message_id: Optional[int] = None
    if not dry_run:
        if not chat_id:
            raise RuntimeError(
                "TELEGRAM_CHAT_ID nao configurado no --env-file nem nas variaveis de ambiente."
            )
        active_transport = transport
        if active_transport is None:
            if not bot_token:
                raise RuntimeError(
                    "TELEGRAM_BOT_TOKEN nao configurado no --env-file nem nas variaveis de ambiente."
                )
            active_transport = HttpTelegramTransport(bot_token)
        try:
            message_id = active_transport.send_message(chat_id=chat_id, text=message)
            sent = True
        except Exception as exc:
            safe_err = redact_secrets(str(exc), secrets_to_scrub)
            raise RuntimeError(f"Falha ao enviar resumo diario no Telegram: {safe_err}") from None

    return {
        "gate": resolved_gate,
        "day_progress": {
            "current_day": progress.current_day,
            "target_days": progress.target_days,
            "elapsed_days": progress.elapsed_days,
            "sample_source": progress.sample_source,
        },
        "message": message,
        "sent": sent,
        "telegram_message_id": message_id,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Executa tools/gate_report.py (somente leitura) e envia resumo curto no Telegram "
            "com status dos critérios e dia do gate. Nunca imprime segredos."
        )
    )
    p.add_argument("--db", type=Path, default=Path("data/quant.db"), help="Caminho do SQLite (somente leitura)")
    p.add_argument(
        "--gate",
        choices=("auto", "6a", "6b", "phase1"),
        default="auto",
        help="Gate alvo do resumo (padrão: auto)",
    )
    p.add_argument("--env-file", type=Path, default=Path(".env"), help="Arquivo .env para ler TELEGRAM_* e MODE")
    p.add_argument("--log-file", type=Path, default=None, help="Arquivo de log exportado do container (opcional)")
    p.add_argument(
        "--restore-drill-report",
        type=Path,
        default=None,
        help="JSON gerado por tools/restore_drill.sh (opcional)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Exibe o resumo no stdout sem enviar requisição HTTP ao Telegram",
    )
    p.add_argument(
        "--print-cron",
        action="store_true",
        help="Imprime comando de exemplo pronto para o cron da VM e encerra",
    )
    p.add_argument(
        "--cron-hour",
        type=int,
        default=9,
        help="Hora UTC (0-23) usada no exemplo gerado por --print-cron (padrão: 9)",
    )
    p.add_argument(
        "--repo-dir",
        type=Path,
        default=_REPO_ROOT,
        help="Diretório do repositório na VM usado por --print-cron",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.print_cron:
        snippet = build_cron_example(
            repo_dir=args.repo_dir,
            gate=args.gate,
            db_rel_path=args.db.as_posix(),
            env_rel_path=args.env_file.as_posix(),
            cron_hour_utc=args.cron_hour,
            restore_drill_report=(
                args.restore_drill_report.as_posix() if args.restore_drill_report else None
            ),
        )
        print(snippet, end="")
        return 0

    try:
        res = run_daily_digest(
            db_path=args.db,
            gate=args.gate,
            env_file=args.env_file,
            log_file=args.log_file,
            restore_drill_report=args.restore_drill_report,
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(res["message"])
    else:
        print(
            f"[OK] Resumo diário ({res['gate'].upper()}, Dia {res['day_progress']['current_day']}/"
            f"{res['day_progress']['target_days']}) enviado ao Telegram "
            f"(message_id={res['telegram_message_id']})."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
