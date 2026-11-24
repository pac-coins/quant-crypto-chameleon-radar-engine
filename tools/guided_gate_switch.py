#!/usr/bin/env python3
"""Assistente guiado EXCLUSIVO para transição Gate 6A (Testnet) -> Gate 6B (Shadow).

Regras estritas de segurança (ADR-009, ADR-010, ADR-016):
- Suporta SOMENTE `--to 6b` (`MODE=SHADOW_CHAMELEON`, sem chaves Binance e sem dinheiro real).
- RECUSA qualquer tentativa de transição para Fase 1 (dinheiro real) ou Fase 2: essas
  transições permanecem 100% manuais.
- Executa `tools/preflight_check.py` (somente leitura) e `tools/gate_report.py` (SQLite
  `URI mode=ro` + `PRAGMA query_only=ON`).
- RECUSA continuar se houver QUALQUER `FAIL` ou `UNKNOWN` nos critérios do Gate 6A,
  nos Kill-Switches (§5.2) ou no preflight.
- Só altera o `.env` após confirmação explícita digitada pelo operador (`SWITCH TO 6B`),
  criando backup `.env.bak.<timestamp>` com permissão `0600` e validando o `.env` final
  com `preflight_check --target-gate 6b`.
- Nunca imprime segredos e nunca reinicia containers automaticamente.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.gate_report import generate_gate_report
from tools.preflight_check import run_preflight_check

ALLOWED_TARGET = "6b"
CONFIRMATION_PHRASE = "SWITCH TO 6B"

_KEYS_TO_REMOVE_FOR_6B: frozenset[str] = frozenset(
    {
        "BINANCE_API_KEY",
        "BINANCE_ED25519_PRIVATE_KEY_PATH",
        "LIVE_CONFIRMATION",
        "APPROVED_STRATEGIES_SHA256",
    }
)
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


@dataclass(frozen=True, slots=True)
class SwitchResult:
    switched: bool
    exit_code: int
    reason: str
    backup_path: Optional[Path] = None
    blocking_items: tuple[str, ...] = ()
    post_preflight_overall: Optional[str] = None


def _extract_env_key(raw_line: str) -> Optional[str]:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    m = _ENV_LINE_RE.match(raw_line)
    return m.group(1) if m else None


def build_6b_env_content(original_text: str, *, default_shadow_usdt: str = "300.00") -> tuple[str, list[str]]:
    """
    Transforma o conteúdo de `.env` para Gate 6B (`MODE=SHADOW_CHAMELEON`),
    removendo todas as credenciais Binance e travas LIVE (ADR-010).
    Retorna `(novo_conteudo, resumo_seguro_sem_segredos)`.
    """
    out_lines: list[str] = []
    changes: list[str] = []
    saw_mode = False
    saw_shadow_initial = False
    removed_keys: list[str] = []

    for raw_line in original_text.splitlines():
        key = _extract_env_key(raw_line)
        if key is None:
            out_lines.append(raw_line)
            continue

        if key == "MODE":
            out_lines.append("MODE=SHADOW_CHAMELEON")
            if not saw_mode:
                changes.append("MODE -> SHADOW_CHAMELEON")
            saw_mode = True
            continue

        if key in _KEYS_TO_REMOVE_FOR_6B:
            removed_keys.append(key)
            continue

        if key == "SHADOW_INITIAL_USDT":
            saw_shadow_initial = True
            out_lines.append(raw_line)
            continue

        out_lines.append(raw_line)

    if not saw_mode:
        out_lines.insert(0, "MODE=SHADOW_CHAMELEON")
        changes.append("MODE -> SHADOW_CHAMELEON (adicionado)")

    if not saw_shadow_initial:
        out_lines.append(f"SHADOW_INITIAL_USDT={default_shadow_usdt}")
        changes.append(f"SHADOW_INITIAL_USDT -> {default_shadow_usdt} (adicionado)")

    for rk in dict.fromkeys(removed_keys):
        changes.append(f"{rk} -> [REMOVIDO por ADR-010]")

    new_text = "\n".join(out_lines) + "\n"
    return new_text, changes


def _write_env_atomically(env_file: Path, content: str) -> Path:
    """Cria backup `.env.bak.<UTC>` (chmod 0600) e grava `.env` atomicamente (chmod 0600)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = env_file.with_name(f"{env_file.name}.bak.{ts}")
    counter = 1
    while backup_path.exists():
        backup_path = env_file.with_name(f"{env_file.name}.bak.{ts}_{counter}")
        counter += 1

    orig_bytes = env_file.read_bytes()
    fd_bak = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd_bak, "wb") as fh:
            fh.write(orig_bytes)
    except Exception:
        if backup_path.exists():
            backup_path.unlink(missing_ok=True)
        raise

    fd_tmp, tmp_name = tempfile.mkstemp(prefix=".env.6b.", dir=str(env_file.parent))
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd_tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, env_file)
        os.chmod(env_file, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return backup_path


def _collect_blocking_items(
    preflight_6a: dict[str, Any],
    gate_report: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """
    Retorna `(blockers, warnings)`.
    Qualquer `FAIL` ou `UNKNOWN` em `preflight_6a`, `gate_6a` ou `kill_switches` bloqueia!
    """
    blockers: list[str] = []
    warnings: list[str] = []

    for chk in preflight_6a.get("checks", []):
        status = str(chk.get("status", "UNKNOWN")).upper()
        cid = str(chk.get("check_id", "?"))
        name = str(chk.get("name", ""))
        details = str(chk.get("details", ""))
        if status in ("FAIL", "UNKNOWN"):
            blockers.append(f"[PREFLIGHT {status}] {cid} {name}: {details}")
        elif status == "WARN":
            warnings.append(f"[PREFLIGHT WARN] {cid} {name}: {details}")

    if str(preflight_6a.get("overall", "")).upper() in ("FAIL", "UNKNOWN") and not blockers:
        blockers.append(f"[PREFLIGHT {preflight_6a.get('overall')}] Preflight 6A reprovado.")

    sections = gate_report.get("sections", {})
    for sec_key, sec_label in (("gate_6a", "Gate 6A"), ("kill_switches", "Kill-Switches")):
        sec = sections.get(sec_key)
        if not isinstance(sec, dict):
            blockers.append(f"[GATE_REPORT UNKNOWN] Seção ausente: {sec_key}")
            continue
        sec_status = str(sec.get("status", "UNKNOWN")).upper()
        criteria = sec.get("criteria", [])
        sec_blocked = False
        for crit in criteria:
            st = str(crit.get("status", "UNKNOWN")).upper()
            if st != "PASS":
                sec_blocked = True
                blockers.append(
                    f"[{sec_label} {st}] {crit.get('id', '?')} — {crit.get('name', '')}: "
                    f"{crit.get('measured', 'N/A')} (esperado: {crit.get('threshold', '')})"
                )
        if sec_status != "PASS" and not sec_blocked:
            blockers.append(f"[{sec_label} {sec_status}] Status geral da seção != PASS.")

    return blockers, warnings


def run_guided_gate_switch(
    *,
    target: str,
    db_path: Path = Path("data/quant.db"),
    env_file: Path = Path(".env"),
    log_file: Optional[Path] = None,
    restore_drill_report: Optional[Path] = None,
    strategies_path: Path = Path("config/strategies.yaml"),
    analyst_path: Path = Path("config/analyst.yaml"),
    compose_path: Path = Path("docker-compose.yml"),
    host_root: Optional[Path] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> SwitchResult:
    """Executa a transição guiada exclusivamente para `--to 6b`."""
    normalized_target = str(target).strip().lower()
    if normalized_target != ALLOWED_TARGET:
        msg = (
            f"RECUSADO: alvo '--to {target}' não é permitido. "
            "Esta ferramenta suporta EXCLUSIVAMENTE a transição para o Gate 6B "
            "('--to 6b' -> MODE=SHADOW_CHAMELEON, sem chaves e sem capital real). "
            "Transições para Fase 1 (dinheiro real) e Fase 2 são ESTRITAMENTE MANUAIS "
            "(ADR-009, ADR-010 e ADR-016)."
        )
        output_fn(f"[BLOCKED] {msg}")
        return SwitchResult(switched=False, exit_code=2, reason=msg)

    if not env_file.is_file():
        msg = f"Arquivo .env não encontrado: {env_file}"
        output_fn(f"[BLOCKED] {msg}")
        return SwitchResult(switched=False, exit_code=1, reason=msg)

    if not db_path.is_file():
        msg = f"Banco SQLite não encontrado: {db_path}"
        output_fn(f"[BLOCKED] {msg}")
        return SwitchResult(switched=False, exit_code=1, reason=msg)

    # 1. Preflight do estado atual (Gate 6A) — somente leitura
    preflight_6a = run_preflight_check(
        env_file=env_file,
        strategies_path=strategies_path,
        analyst_path=analyst_path,
        compose_path=compose_path,
        target_gate="6a",
        host_root=host_root,
    )

    # 2. Relatório read-only dos critérios do Gate 6A e Kill-Switches (§5.2)
    gate_report = generate_gate_report(
        db_path=db_path,
        log_file=log_file,
        restore_drill_report=restore_drill_report,
    )

    blockers, warnings = _collect_blocking_items(preflight_6a, gate_report)

    output_fn("=== Verificação Pré-Transição (Gate 6A -> Gate 6B) ===")
    output_fn(f"Preflight 6A: {preflight_6a.get('overall')}")
    output_fn(f"Gate 6A:      {gate_report['sections']['gate_6a']['status']}")
    output_fn(f"Kill-Switch:  {gate_report['sections']['kill_switches']['status']}")

    if warnings:
        output_fn("\nAvisos não bloqueantes (WARN):")
        for w in warnings:
            output_fn(f"  - {w}")

    if blockers:
        output_fn("\n[RECUSADO] Transição para Gate 6B BLOQUEADA por FAIL ou UNKNOWN:")
        for b in blockers:
            output_fn(f"  * {b}")
        output_fn("\nNenhuma alteração foi feita no .env.")
        return SwitchResult(
            switched=False,
            exit_code=1,
            reason="Bloqueado por critérios FAIL ou UNKNOWN no Gate 6A / Preflight / Kill-Switches.",
            blocking_items=tuple(blockers),
        )

    # 3. Prepara diff seguro (ZERO segredos exibidos)
    orig_text = env_file.read_text(encoding="utf-8")
    new_text, changes = build_6b_env_content(orig_text)

    output_fn("\nTodos os critérios de Gate 6A, Kill-Switches e Preflight passaram (0 FAIL, 0 UNKNOWN).")
    output_fn(f"\nAlterações planejadas em {env_file} (segredos preservados e ocultados):")
    for ch in changes:
        output_fn(f"  ~ {ch}")
    output_fn("Nota: nenhum container será reiniciado automaticamente.")

    # 4. Confirmação explícita digitada pelo operador
    prompt = (
        f"\nPara confirmar a alteração de {env_file} para Gate 6B (SHADOW_CHAMELEON), "
        f"digite exatamente '{CONFIRMATION_PHRASE}': "
    )
    try:
        typed = input_fn(prompt).strip()
    except EOFError:
        typed = ""

    if typed != CONFIRMATION_PHRASE:
        msg = "Confirmação não correspondeu ou foi cancelada; .env mantido intacto."
        output_fn(f"\n[ABORTADO] {msg}")
        return SwitchResult(switched=False, exit_code=1, reason=msg)

    # 5. Backup + gravação atômica + validação pós-alteração com preflight 6B
    backup_path = _write_env_atomically(env_file, new_text)

    post_preflight = run_preflight_check(
        env_file=env_file,
        strategies_path=strategies_path,
        analyst_path=analyst_path,
        compose_path=compose_path,
        target_gate="6b",
        host_root=host_root,
    )
    post_overall = str(post_preflight.get("overall", "FAIL"))
    if post_overall == "FAIL":
        # Reverte automaticamente para o backup se o .env resultante falhar no preflight 6B
        os.replace(backup_path, env_file)
        os.chmod(env_file, 0o600)
        failed_post = [
            f"{c['check_id']} {c['name']}: {c['details']}"
            for c in post_preflight.get("checks", [])
            if c.get("status") == "FAIL"
        ]
        msg = "Preflight 6B falhou após gravação; .env restaurado a partir do backup."
        output_fn(f"\n[REVERTIDO] {msg}")
        for item in failed_post:
            output_fn(f"  * {item}")
        return SwitchResult(
            switched=False,
            exit_code=1,
            reason=msg,
            blocking_items=tuple(failed_post),
            post_preflight_overall=post_overall,
        )

    output_fn(f"\n[OK] .env atualizado para Gate 6B (MODE=SHADOW_CHAMELEON).")
    output_fn(f"Backup seguro salvo em: {backup_path} (chmod 0600)")
    output_fn(f"Preflight 6B pós-troca: {post_overall}")
    output_fn(
        "\nPróximo passo manual (quando estiver pronto para reiniciar o container):\n"
        "  docker compose up -d --build && docker compose logs -f --tail=100 quant-chameleon"
    )
    return SwitchResult(
        switched=True,
        exit_code=0,
        reason="Transição do .env para Gate 6B concluída e validada pelo preflight 6B.",
        backup_path=backup_path,
        post_preflight_overall=post_overall,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Transição guiada EXCLUSIVA para Gate 6B (--to 6b / SHADOW_CHAMELEON). "
            "Recusa qualquer FAIL ou UNKNOWN no preflight/gate_report e recusa Fase 1/Fase 2."
        )
    )
    p.add_argument(
        "--to",
        required=True,
        help="Alvo da transição. Apenas '6b' é aceito (Fase 1 e Fase 2 são manuais).",
    )
    p.add_argument("--db", type=Path, default=Path("data/quant.db"), help="Caminho do SQLite (somente leitura)")
    p.add_argument("--env-file", type=Path, default=Path(".env"), help="Arquivo .env a atualizar após confirmação")
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Arquivo de log exportado do container no Gate 6A (necessário para evitar UNKNOWN)",
    )
    p.add_argument(
        "--restore-drill-report",
        type=Path,
        default=None,
        help="Relatório JSON do tools/restore_drill.sh (necessário para evitar UNKNOWN)",
    )
    p.add_argument(
        "--strategies",
        type=Path,
        default=Path("config/strategies.yaml"),
        help="Caminho de config/strategies.yaml",
    )
    p.add_argument(
        "--analyst",
        type=Path,
        default=Path("config/analyst.yaml"),
        help="Caminho de config/analyst.yaml",
    )
    p.add_argument(
        "--compose",
        type=Path,
        default=Path("docker-compose.yml"),
        help="Caminho de docker-compose.yml",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    res = run_guided_gate_switch(
        target=args.to,
        db_path=args.db,
        env_file=args.env_file,
        log_file=args.log_file,
        restore_drill_report=args.restore_drill_report,
        strategies_path=args.strategies,
        analyst_path=args.analyst,
        compose_path=args.compose,
    )
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
