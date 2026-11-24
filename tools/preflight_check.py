#!/usr/bin/env python3
"""
Read-only preflight validator for gate transitions (6A -> 6B -> Phase 1 -> Phase 2).

Verifies .env, YAML invariants, strategies.yaml SHA-256, Ed25519 key permissions,
dead-man switch HEALTHCHECK_URL, encrypted offsite backup Fernet key, calibrated
MLJudge artifact integrity/ECE, Docker volume mounts, and VPS disk usage.
NEVER writes to ./data or ./secrets and NEVER prints secret values.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml
from cryptography.fernet import Fernet
from pydantic import ValidationError

from src.core.config import (
    AnalystConfig,
    AppConfig,
    BinanceEnv,
    ConfigError,
    RunMode,
    RuntimeSettings,
    StrategiesConfig,
    load_yaml_strict,
    sha256_file,
    validate_mode_invariants,
)
from src.features.ml_judge import MLJudgeArtifact

Status = Literal["PASS", "WARN", "FAIL"]
TargetGate = Literal["auto", "6a", "6b", "phase1", "phase2"]


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    name: str
    status: Status
    measured: str
    threshold: str
    details: str = ""


def _resolve_container_path(path: Optional[Path], host_root: Path) -> tuple[Optional[Path], bool]:
    """Map /app/<rel> to <host_root>/<rel> when running on host outside Docker."""
    if path is None:
        return None, False
    if path.exists():
        return path, False
    raw = path.as_posix()
    if raw.startswith("/app/"):
        rel = raw[len("/app/") :]
        mapped = host_root / rel
        return mapped, True
    if not path.is_absolute():
        return host_root / path, False
    return path, False


def _is_production_target(target_gate: TargetGate, rt: RuntimeSettings) -> bool:
    if target_gate in ("phase1", "phase2"):
        return True
    if target_gate == "auto":
        return rt.mode in (
            RunMode.LIVE_CHAMELEON_PHASE_1,
            RunMode.LIVE_CHAMELEON_PHASE_2,
        ) and rt.binance_env == BinanceEnv.PRODUCTION
    return False


def run_preflight_check(
    *,
    env_file: Path = Path(".env"),
    strategies_path: Path = Path("config/strategies.yaml"),
    analyst_path: Path = Path("config/analyst.yaml"),
    compose_path: Path = Path("docker-compose.yml"),
    target_gate: TargetGate = "auto",
    host_root: Optional[Path] = None,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    root = (host_root if host_root is not None else REPO_ROOT).resolve()
    checks: list[CheckResult] = []

    if not env_file.is_file():
        checks.append(
            CheckResult(
                check_id="PF-01",
                name="Env & Config Load",
                status="FAIL",
                measured=str(env_file),
                threshold="Arquivo .env legivel",
                details=f"Arquivo .env nao encontrado: {env_file}",
            )
        )
        return _finalize_report(checks, target_gate=target_gate, strategies_sha256=None)

    try:
        st_sha = sha256_file(strategies_path)
        st_cfg = StrategiesConfig.model_validate(load_yaml_strict(strategies_path))
        an_cfg = AnalystConfig.model_validate(load_yaml_strict(analyst_path))
    except (ConfigError, ValidationError, OSError) as exc:
        checks.append(
            CheckResult(
                check_id="PF-01",
                name="Env & Config Load",
                status="FAIL",
                measured="YAML_ERROR",
                threshold="YAML valido",
                details=str(exc),
            )
        )
        return _finalize_report(checks, target_gate=target_gate, strategies_sha256=None)

    # Isolate RuntimeSettings load to env_file (avoid picking up unrelated shell env vars in tests/host)
    saved_env: dict[str, str] = {}
    managed_keys = {
        "MODE",
        "BINANCE_ENV",
        "BINANCE_API_KEY",
        "BINANCE_ED25519_PRIVATE_KEY_PATH",
        "LIVE_CONFIRMATION",
        "APPROVED_STRATEGIES_SHA256",
        "SHADOW_INITIAL_USDT",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DB_PATH",
        "BACKUP_DIR",
        "BACKUP_RETENTION_DAYS",
        "WATCHDOG_POLL_INTERVAL_SECONDS",
        "HEALTHCHECK_URL",
        "OFFSITE_BACKUP_ENABLED",
        "BACKUP_TELEGRAM_CHAT_ID",
        "BACKUP_ENCRYPTION_KEY",
        "ML_JUDGE_ARTIFACT_PATH",
    }
    try:
        for k in list(os.environ):
            if k in managed_keys:
                saved_env[k] = os.environ.pop(k)
        rt = RuntimeSettings(_env_file=env_file, _env_file_encoding="utf-8")  # type: ignore[call-arg]
    except ValidationError as exc:
        checks.append(
            CheckResult(
                check_id="PF-01",
                name="Env & Config Load",
                status="FAIL",
                measured="ENV_INVALID",
                threshold="Schema Pydantic valido",
                details=str(exc),
            )
        )
        return _finalize_report(checks, target_gate=target_gate, strategies_sha256=st_sha)
    finally:
        os.environ.update(saved_env)

    # Resolve container-style /app/... paths to host_root for host-side verification
    resolved_key_path, key_mapped = _resolve_container_path(
        rt.binance_ed25519_private_key_path, root
    )
    resolved_art_path, art_mapped = _resolve_container_path(rt.ml_judge_artifact_path, root)
    resolved_db_path, _ = _resolve_container_path(rt.db_path, root)
    rt_resolved = rt.model_copy(
        update={
            "binance_ed25519_private_key_path": resolved_key_path,
            "ml_judge_artifact_path": resolved_art_path,
        }
    )
    cfg = AppConfig(runtime=rt_resolved, strategies=st_cfg, analyst=an_cfg, strategies_sha256=st_sha)

    # PF-01: Mode invariants
    try:
        validate_mode_invariants(cfg)
        checks.append(
            CheckResult(
                check_id="PF-01",
                name="Mode Invariants (ADR-004/009/010/C1-C)",
                status="PASS",
                measured=f"{rt.mode.value} ({rt.binance_env.value})",
                threshold="Todas as invariantes de boot satisfeitas",
                details="validate_mode_invariants() aprovado sem erros.",
            )
        )
    except ConfigError as exc:
        checks.append(
            CheckResult(
                check_id="PF-01",
                name="Mode Invariants (ADR-004/009/010/C1-C)",
                status="FAIL",
                measured=f"{rt.mode.value} ({rt.binance_env.value})",
                threshold="Todas as invariantes de boot satisfeitas",
                details=str(exc),
            )
        )

    # PF-02: Target gate alignment
    expected_by_gate: dict[str, tuple[RunMode, Optional[BinanceEnv]]] = {
        "6a": (RunMode.LIVE_CHAMELEON_PHASE_1, BinanceEnv.TESTNET),
        "6b": (RunMode.SHADOW_CHAMELEON, None),
        "phase1": (RunMode.LIVE_CHAMELEON_PHASE_1, BinanceEnv.PRODUCTION),
        "phase2": (RunMode.LIVE_CHAMELEON_PHASE_2, BinanceEnv.PRODUCTION),
    }
    if target_gate == "auto":
        checks.append(
            CheckResult(
                check_id="PF-02",
                name="Gate Target Alignment",
                status="PASS",
                measured=f"{rt.mode.value}/{rt.binance_env.value}",
                threshold="auto",
                details="Validado contra o modo declarado no .env.",
            )
        )
    else:
        exp_mode, exp_env = expected_by_gate[target_gate]
        mode_ok = rt.mode == exp_mode
        env_ok = exp_env is None or rt.binance_env == exp_env
        checks.append(
            CheckResult(
                check_id="PF-02",
                name="Gate Target Alignment",
                status="PASS" if (mode_ok and env_ok) else "FAIL",
                measured=f"MODE={rt.mode.value}, BINANCE_ENV={rt.binance_env.value}",
                threshold=(
                    f"MODE={exp_mode.value}"
                    + (f", BINANCE_ENV={exp_env.value}" if exp_env is not None else "")
                ),
                details="" if (mode_ok and env_ok) else f"Configuracao do .env nao corresponde ao gate '{target_gate}'.",
            )
        )

    prod_required = _is_production_target(target_gate, rt)

    # PF-03: strategies.yaml SHA-256
    approved_sha = (rt.approved_strategies_sha256 or "").strip().lower()
    if approved_sha:
        sha_match = approved_sha == st_sha.lower()
        checks.append(
            CheckResult(
                check_id="PF-03",
                name="strategies.yaml SHA-256 Pin (ADR-009)",
                status="PASS" if sha_match else "FAIL",
                measured=st_sha[:16] + "...",
                threshold=f"APPROVED={approved_sha[:16]}...",
                details=f"SHA-256 atual: {st_sha}" + ("" if sha_match else " (DIVERGENTE de APPROVED_STRATEGIES_SHA256!)"),
            )
        )
    else:
        checks.append(
            CheckResult(
                check_id="PF-03",
                name="strategies.yaml SHA-256 Pin (ADR-009)",
                status="FAIL" if prod_required else "PASS",
                measured="nao definido",
                threshold="obrigatorio em PRODUCTION" if prod_required else "opcional fora de PRODUCTION",
                details=f"SHA-256 atual de config/strategies.yaml: {st_sha}",
            )
        )

    # PF-04: Ed25519 private key permissions & least privilege
    if rt.mode in (RunMode.ANALYST_ONLY, RunMode.SHADOW_CHAMELEON):
        has_any_cred = (
            (rt.binance_api_key is not None and bool(rt.binance_api_key.get_secret_value().strip()))
            or rt.binance_ed25519_private_key_path is not None
        )
        checks.append(
            CheckResult(
                check_id="PF-04",
                name="Credentials & Key Permissions (ADR-010)",
                status="FAIL" if has_any_cred else "PASS",
                measured="credenciais presentes" if has_any_cred else "sem chaves Binance",
                threshold="zero credenciais em ANALYST_ONLY/SHADOW_CHAMELEON",
                details="Menor privilegio preservado." if not has_any_cred else "Remova BINANCE_API_KEY e BINANCE_ED25519_PRIVATE_KEY_PATH.",
            )
        )
    else:
        if resolved_key_path is None or not resolved_key_path.is_file():
            checks.append(
                CheckResult(
                    check_id="PF-04",
                    name="Credentials & Key Permissions (ADR-010)",
                    status="FAIL",
                    measured=str(rt.binance_ed25519_private_key_path),
                    threshold="arquivo Ed25519 existente com permissao 0600/0400",
                    details="Arquivo de chave privada Ed25519 nao encontrado.",
                )
            )
        else:
            mode_bits = stat.S_IMODE(resolved_key_path.stat().st_mode)
            too_open = (mode_bits & 0o077) != 0
            map_note = f" (mapeado de /app -> {resolved_key_path})" if key_mapped else ""
            checks.append(
                CheckResult(
                    check_id="PF-04",
                    name="Credentials & Key Permissions (ADR-010)",
                    status="WARN" if too_open else "PASS",
                    measured=f"chmod {oct(mode_bits)}",
                    threshold="chmod 0o600 ou 0o400 (sem leitura group/other)",
                    details=(
                        f"Execute chmod 600 {resolved_key_path}{map_note}"
                        if too_open
                        else f"Chave Ed25519 com permissao segura{map_note}."
                    ),
                )
            )

    # PF-05: Dead-man switch HEALTHCHECK_URL
    hc_raw = rt.healthcheck_url.get_secret_value().strip() if rt.healthcheck_url is not None else ""
    if hc_raw:
        parsed_hc = urlparse(hc_raw)
        valid_hc = parsed_hc.scheme in ("http", "https") and bool(parsed_hc.netloc)
        checks.append(
            CheckResult(
                check_id="PF-05",
                name="External Heartbeat (HEALTHCHECK_URL)",
                status="PASS" if valid_hc else "FAIL",
                measured=f"{parsed_hc.scheme}://{parsed_hc.netloc}" if valid_hc else "URL invalida",
                threshold="URL http(s) configurada",
                details="Dead-man switch configurado (caminho/token ocultado)." if valid_hc else "HEALTHCHECK_URL malformada.",
            )
        )
    else:
        checks.append(
            CheckResult(
                check_id="PF-05",
                name="External Heartbeat (HEALTHCHECK_URL)",
                status="FAIL" if prod_required else "WARN",
                measured="vazio",
                threshold="obrigatorio em PRODUCTION" if prod_required else "recomendado",
                details="Configure HEALTHCHECK_URL (ex.: Healthchecks.io) antes de operar capital real.",
            )
        )

    # PF-06: Encrypted Offsite Backup
    if rt.offsite_backup_enabled:
        key_ok = False
        if rt.backup_encryption_key is not None:
            try:
                Fernet(rt.backup_encryption_key.get_secret_value().encode("utf-8"))
                key_ok = True
            except Exception:
                key_ok = False
        checks.append(
            CheckResult(
                check_id="PF-06",
                name="Encrypted Offsite Backup (ADR-012)",
                status="PASS" if key_ok else "FAIL",
                measured="OFFSITE=true, Fernet=" + ("valido" if key_ok else "invalido"),
                threshold="OFFSITE_BACKUP_ENABLED=true + chave Fernet valida",
                details="Backup offsite criptografado pronto." if key_ok else "BACKUP_ENCRYPTION_KEY nao e uma chave Fernet valida.",
            )
        )
    else:
        checks.append(
            CheckResult(
                check_id="PF-06",
                name="Encrypted Offsite Backup (ADR-012)",
                status="FAIL" if prod_required else "WARN",
                measured="OFFSITE_BACKUP_ENABLED=false",
                threshold="true em PRODUCTION" if prod_required else "recomendado",
                details="Habilite OFFSITE_BACKUP_ENABLED=true com BACKUP_ENCRYPTION_KEY antes de PRODUCTION.",
            )
        )

    # PF-07: Calibrated MLJudge artifact (B4)
    max_ece = float(st_cfg.ml_judge_and_validation.max_allowed_ece)
    if resolved_art_path is None:
        if prod_required:
            art_status: Status = "FAIL"
            art_details = (
                "Sem ML_JUDGE_ARTIFACT_PATH a Fase 1 gravaria Platt nao treinado (a=-1, b=0), "
                "impedindo o Req. 5 de F1->F2. Gere via tools/train_ml_judge.py."
            )
        elif target_gate == "6b":
            art_status = "WARN"
            art_details = "Opcional no inicio do 6B; gere via tools/train_ml_judge.py antes da Fase 1."
        else:
            art_status = "PASS"
            art_details = "Opcional fora de PRODUCTION."
        checks.append(
            CheckResult(
                check_id="PF-07",
                name="Calibrated MLJudge Artifact (B4)",
                status=art_status,
                measured="nao configurado",
                threshold=f"artefato assinado com ECE <= {max_ece:.2f}",
                details=art_details,
            )
        )
    else:
        try:
            art = MLJudgeArtifact.load(resolved_art_path)
            ece_ok = art.ece_out_of_sample <= max_ece
            map_note = f" (mapeado de /app -> {resolved_art_path})" if art_mapped else ""
            checks.append(
                CheckResult(
                    check_id="PF-07",
                    name="Calibrated MLJudge Artifact (B4)",
                    status="PASS" if ece_ok else ("FAIL" if prod_required else "WARN"),
                    measured=f"ECE={art.ece_out_of_sample:.4f}, n_val={art.validation_samples}",
                    threshold=f"SHA-256 valido e ECE <= {max_ece:.2f}",
                    details=f"Artefato verificado{map_note}."
                    if ece_ok
                    else f"ECE fora da amostra ({art.ece_out_of_sample:.4f}) excede {max_ece:.2f}.",
                )
            )
        except Exception as exc:
            checks.append(
                CheckResult(
                    check_id="PF-07",
                    name="Calibrated MLJudge Artifact (B4)",
                    status="FAIL",
                    measured=str(rt.ml_judge_artifact_path),
                    threshold=f"artefato JSON valido com SHA-256 integro",
                    details=f"Falha ao carregar artefato MLJudge: {exc}",
                )
            )

    # PF-08: Watchdog, backup retention & Docker ./models mount
    hygiene_issues: list[str] = []
    if rt.watchdog_poll_interval_seconds > 30.0:
        hygiene_issues.append(
            f"WATCHDOG_POLL_INTERVAL_SECONDS={rt.watchdog_poll_interval_seconds:.1f}s > 30s"
        )
    if rt.backup_retention_days < 7:
        hygiene_issues.append(f"BACKUP_RETENTION_DAYS={rt.backup_retention_days} < 7")
    if (
        rt.ml_judge_artifact_path is not None
        and rt.ml_judge_artifact_path.as_posix().startswith("/app/models/")
        and compose_path.is_file()
    ):
        try:
            comp = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
            vols = (
                comp.get("services", {})
                .get("quant-chameleon", {})
                .get("volumes", [])
            )
            has_models_mount = any(
                isinstance(v, str) and ":/app/models" in v for v in vols
            )
            if not has_models_mount:
                hygiene_issues.append("docker-compose.yml nao monta ./models:/app/models:ro")
        except Exception as exc:
            hygiene_issues.append(f"Nao foi possivel validar docker-compose.yml: {exc}")

    checks.append(
        CheckResult(
            check_id="PF-08",
            name="Watchdog, Backup Retention & Docker Mounts",
            status="WARN" if hygiene_issues else "PASS",
            measured=(
                f"watchdog={rt.watchdog_poll_interval_seconds:.0f}s, "
                f"retention={rt.backup_retention_days}d"
            ),
            threshold="watchdog <= 30s, retention >= 7d, mount /app/models presente",
            details="; ".join(hygiene_issues) if hygiene_issues else "Parametros operacionais adequados.",
        )
    )

    # PF-09: VPS disk usage
    disk_target = (
        resolved_db_path.parent
        if (resolved_db_path is not None and resolved_db_path.parent.exists())
        else root
    )
    try:
        usage = disk_usage_fn(disk_target)
        used_pct = (int(usage.used) / int(usage.total)) * 100.0 if int(usage.total) > 0 else 100.0
        free_gb = int(usage.free) / (1024**3)
        if used_pct >= 85.0:
            d_status: Status = "FAIL"
        elif used_pct >= 70.0:
            d_status = "WARN"
        else:
            d_status = "PASS"
        checks.append(
            CheckResult(
                check_id="PF-09",
                name="Host Disk Space",
                status=d_status,
                measured=f"{used_pct:.1f}% usado ({free_gb:.2f} GB livre)",
                threshold="< 70% (WARN 70-85%, FAIL >= 85%)",
                details=f"Diretorio verificado: {disk_target}",
            )
        )
    except Exception as exc:
        checks.append(
            CheckResult(
                check_id="PF-09",
                name="Host Disk Space",
                status="WARN",
                measured="indisponivel",
                threshold="< 70%",
                details=str(exc),
            )
        )

    return _finalize_report(checks, target_gate=target_gate, strategies_sha256=st_sha)


def _finalize_report(
    checks: Sequence[CheckResult],
    *,
    target_gate: TargetGate,
    strategies_sha256: Optional[str],
) -> dict[str, Any]:
    n_fail = sum(1 for c in checks if c.status == "FAIL")
    n_warn = sum(1 for c in checks if c.status == "WARN")
    n_pass = sum(1 for c in checks if c.status == "PASS")
    overall: Status = "FAIL" if n_fail > 0 else ("WARN" if n_warn > 0 else "PASS")
    return {
        "target_gate": target_gate,
        "overall": overall,
        "strategies_sha256": strategies_sha256,
        "summary": {"pass": n_pass, "warn": n_warn, "fail": n_fail},
        "checks": [asdict(c) for c in checks],
    }


def format_preflight_table(report: dict[str, Any]) -> str:
    lines = [
        f"=== QuantCrypto Preflight Check (target_gate={report['target_gate']}) ===",
        f"Overall: {report['overall']} | PASS={report['summary']['pass']} WARN={report['summary']['warn']} FAIL={report['summary']['fail']}",
    ]
    if report.get("strategies_sha256"):
        lines.append(f"config/strategies.yaml SHA-256: {report['strategies_sha256']}")
    lines.append("")
    lines.append(f"{'ID':<7} | {'Status':<6} | {'Check':<42} | {'Measured':<30} | Details")
    lines.append("-" * 130)
    for c in report["checks"]:
        lines.append(
            f"{c['check_id']:<7} | {c['status']:<6} | {c['name']:<42} | {c['measured']:<30} | {c['details']}"
        )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only preflight check before starting or switching QuantCrypto gates."
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--strategies", type=Path, default=Path("config/strategies.yaml"))
    parser.add_argument("--analyst", type=Path, default=Path("config/analyst.yaml"))
    parser.add_argument("--compose", type=Path, default=Path("docker-compose.yml"))
    parser.add_argument(
        "--target-gate",
        choices=("auto", "6a", "6b", "phase1", "phase2"),
        default="auto",
    )
    parser.add_argument("--host-root", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--strict", action="store_true", help="Return non-zero on WARN as well as FAIL.")
    args = parser.parse_args(argv)

    report = run_preflight_check(
        env_file=args.env_file,
        strategies_path=args.strategies,
        analyst_path=args.analyst,
        compose_path=args.compose,
        target_gate=args.target_gate,
        host_root=args.host_root,
    )
    print(format_preflight_table(report))
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if report["overall"] == "FAIL":
        return 1
    if args.strict and report["overall"] == "WARN":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
