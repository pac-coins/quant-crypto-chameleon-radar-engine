#!/usr/bin/env python3
"""Relatório somente-leitura de Gates (6A, 6B, Fase 1 -> Fase 2) e Kill-Switches (§5.2).

Regras estritas:
- Abre o SQLite exclusivamente em modo read-only (`file:<path>?mode=ro` + `PRAGMA query_only=ON`),
  respeitando o single-writer do bot (ADR-002). Nunca cria -wal/-shm nem modifica o banco.
- Schema validado contra docs/data-contract.md e src/core/database.py.
- Critérios exatos de docs/methodology-and-roadmap.md (§4.1, §4.3, §5.2).
- Cada critério retorna PASS, FAIL ou UNKNOWN; na ausência de dados no banco ou nos logs,
  retorna UNKNOWN — nunca PASS por omissão.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

# Permite rodar diretamente como `python3 tools/gate_report.py` de qualquer diretório
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.features.ml_judge import compute_expected_calibration_error

Status = Literal["PASS", "FAIL", "UNKNOWN"]

ONE_HOUR_MS = 3_600_000
ONE_DAY_MS = 86_400_000

# Limiares exatos de docs/methodology-and-roadmap.md (§4.1, §4.3, §5.2) e config/strategies.yaml
GATE_6A_MIN_CONSECUTIVE_CYCLES = 168
GATE_6A_MAX_ABS_CLOCK_OFFSET_MS = 500.0

GATE_6B_MIN_DAYS = 30.0
GATE_6B_MIN_TRADES = 25
GATE_6B_MAX_MEAN_SLIPPAGE_PCT = 0.08
# Referência Bloco 3 K9.3 (docs/block-3-spec.md:350): 282 trades no histórico (~1,3/sem)
GATE_6B_DEFAULT_MIN_WEEKLY_TRADES = 0.25
GATE_6B_DEFAULT_MAX_WEEKLY_TRADES = 7.0

PHASE_1_TO_2_MIN_TRADES = 60
PHASE_1_TO_2_MIN_DAYS = 60.0
PHASE_1_TO_2_MIN_PROFIT_FACTOR = 1.15
PHASE_1_TO_2_MIN_MEAN_R = 0.10
PHASE_1_TO_2_MAX_DD_PCT = 10.0  # estritamente menor que 10%
PHASE_1_TO_2_MAX_SLIPPAGE_PCT = 0.10
PHASE_1_TO_2_MAX_ECE = 0.05

KS1_HARD_STOP_DD_PCT = 12.0
KS2_HARD_MIN_BNB_USDT = Decimal("2.00")
KS2_WARN_MIN_BNB_USDT = Decimal("5.00")
KS3_MAX_RECONCILE_FAIL_STREAK = 3
KS3_AUTH_CODES = (-1002, -1022, -2015, -2014)
KS4_DEFAULT_MAX_DAILY_TRADES_CAP = 6

_RE_TRACEBACK = re.compile(r"Traceback \(most recent call last\):")
_RE_UNHANDLED = re.compile(
    r"(Unhandled\s+(?:error|exception)|\[CRITICAL\])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CriterionResult:
    id: str
    group: str
    name: str
    status: Status
    measured: str
    threshold: str
    details: str


def _open_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    """
    Abre SQLite estritamente em modo somente-leitura (ADR-002).
    - Em banco ativo (modo WAL com diretório gravável ou -wal presente), usa `mode=ro` + `PRAGMA query_only=ON`
      (lê commits recentes do WAL sem nunca adquirir lock de escrita).
    - Em snapshot estático (sem `-wal`) dentro de diretório sem permissão de escrita (`chmod 0555`),
      o SQLite exige `immutable=1` para não tentar criar o arquivo `-shm`.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    resolved = path.resolve()
    uri = f"file:{resolved.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.OperationalError as exc:
        conn.close()
        wal_sidecar = Path(f"{resolved}-wal")
        if "readonly" in str(exc).lower() and not wal_sidecar.exists():
            imm_uri = f"file:{resolved.as_posix()}?mode=ro&immutable=1"
            imm_conn = sqlite3.connect(imm_uri, uri=True, timeout=2.0)
            imm_conn.execute("PRAGMA query_only=ON")
            imm_conn.row_factory = sqlite3.Row
            return imm_conn
        raise


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(r["name"]) for r in rows}


def _safe_decimal(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    try:
        d = Decimal(str(val).strip())
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _load_system_state(conn: sqlite3.Connection, tables: set[str]) -> dict[str, str]:
    if "system_state" not in tables:
        return {}
    rows = conn.execute("SELECT key, value FROM system_state").fetchall()
    return {str(r["key"]): str(r["value"]) for r in rows}


def _scan_log_for_unhandled_exceptions(log_path: Optional[Path]) -> tuple[bool, int, list[str]]:
    """
    Lê o arquivo de logs e conta eventos de exceção não tratada / traceback / CRITICAL.
    Retorna (log_readable, event_count, sample_lines).
    Coalesce uma linha `Unhandled error...` seguida imediatamente (<= 2 linhas) por `Traceback`.
    """
    if log_path is None:
        return False, 0, []
    path = Path(log_path)
    if not path.is_file():
        return False, 0, []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, 0, []

    events = 0
    samples: list[str] = []
    last_unhandled_line = -10
    for lineno, line in enumerate(text.splitlines(), start=1):
        is_unhandled = bool(_RE_UNHANDLED.search(line))
        is_tb = bool(_RE_TRACEBACK.search(line))
        if is_unhandled:
            events += 1
            last_unhandled_line = lineno
            if len(samples) < 5:
                samples.append(f"L{lineno}: {line.strip()[:140]}")
        elif is_tb:
            if lineno - last_unhandled_line > 2:
                events += 1
                if len(samples) < 5:
                    samples.append(f"L{lineno}: {line.strip()[:140]}")
    return True, events, samples


def _longest_clean_hourly_streak(rows: Sequence[sqlite3.Row]) -> int:
    """Maior sequência contígua de ciclos horários (delta == 1h) com unhandled_errors == 0."""
    best = 0
    cur = 0
    prev_ts: Optional[int] = None
    for row in rows:
        ts = int(row["cycle_close_time_ms"])
        errs = int(row["unhandled_errors"])
        if errs != 0:
            cur = 0
            prev_ts = ts
            continue
        if prev_ts is not None and (ts - prev_ts) == ONE_HOUR_MS and cur > 0:
            cur += 1
        else:
            cur = 1
        prev_ts = ts
        if cur > best:
            best = cur
    return best


def _max_rolling_24h_count(timestamps_ms: Sequence[int]) -> int:
    """Máximo de eventos dentro de qualquer janela móvel de 24h (86_400_000 ms)."""
    if not timestamps_ms:
        return 0
    ordered = sorted(int(t) for t in timestamps_ms)
    best = 0
    left = 0
    for right, ts in enumerate(ordered):
        while ordered[left] < ts - ONE_DAY_MS:
            left += 1
        count = right - left + 1
        if count > best:
            best = count
    return best


def _aggregate_status(criteria: Sequence[CriterionResult]) -> Status:
    statuses = {c.status for c in criteria}
    if "FAIL" in statuses:
        return "FAIL"
    if "UNKNOWN" in statuses or not criteria:
        return "UNKNOWN"
    return "PASS"


# =============================================================================
# Avaliadores por Seção
# =============================================================================


def _evaluate_gate_6a(
    conn: sqlite3.Connection,
    tables: set[str],
    *,
    gate6a_mode: str,
    log_file: Optional[Path],
    restore_drill_report: Optional[Path],
) -> list[CriterionResult]:
    results: list[CriterionResult] = []

    if "cycle_health" in tables:
        rows = conn.execute(
            """
            SELECT cycle_close_time_ms, clock_offset_ms, unhandled_errors
            FROM cycle_health
            WHERE mode = ?
            ORDER BY cycle_close_time_ms ASC
            """,
            (gate6a_mode,),
        ).fetchall()
    else:
        rows = []

    total_cycles = len(rows)
    db_unhandled = sum(int(r["unhandled_errors"]) for r in rows)
    max_streak = _longest_clean_hourly_streak(rows)
    log_readable, log_events, log_samples = _scan_log_for_unhandled_exceptions(log_file)

    # 1. 168 ciclos consecutivos sem exceções não tratadas (DB + logs)
    if db_unhandled > 0 or (log_readable and log_events > 0):
        status_1: Status = "FAIL"
        detail_parts = [
            f"mode={gate6a_mode}",
            f"total_ciclos={total_cycles}",
            f"max_sequencia_limpa={max_streak}",
            f"db_unhandled_errors={db_unhandled}",
            f"log_unhandled={log_events if log_readable else 'nao_lido'}",
        ]
        if log_samples:
            detail_parts.append(f"amostra_log={log_samples[0]}")
        details_1 = "; ".join(detail_parts)
        measured_1 = (
            f"{max_streak} ciclos limpos consec. | db_err={db_unhandled} | "
            f"log_err={log_events if log_readable else 'N/A'}"
        )
    elif total_cycles == 0:
        status_1 = "UNKNOWN"
        measured_1 = "N/A (0 ciclos em cycle_health)"
        details_1 = f"Nenhum registro em cycle_health para mode={gate6a_mode}."
    elif max_streak < GATE_6A_MIN_CONSECUTIVE_CYCLES:
        status_1 = "FAIL"
        measured_1 = (
            f"{max_streak} ciclos limpos consec. (total={total_cycles}) | "
            f"db_err=0 | log_err={log_events if log_readable else 'N/A'}"
        )
        details_1 = (
            f"Sequência contígua de ciclos horários limpos ({max_streak}) abaixo de "
            f"{GATE_6A_MIN_CONSECUTIVE_CYCLES} em mode={gate6a_mode}."
        )
    elif not log_readable:
        status_1 = "UNKNOWN"
        measured_1 = f"{max_streak} ciclos limpos consec. | db_err=0 | log_err=UNKNOWN"
        details_1 = (
            "cycle_health atingiu >= 168 ciclos limpos, mas --log-file não foi fornecido ou "
            "não é legível; não é possível confirmar 0 exceções nos logs (nunca PASS por omissão)."
        )
    else:
        status_1 = "PASS"
        measured_1 = f"{max_streak} ciclos limpos consec. | db_err=0 | log_err=0"
        details_1 = (
            f"Verificados {total_cycles} ciclos em cycle_health (mode={gate6a_mode}) e "
            f"arquivo de logs {log_file}."
        )

    results.append(
        CriterionResult(
            id="gate_6a.cycles_168_no_unhandled",
            group="gate_6a",
            name="168 ciclos horários consecutivos sem exceções não tratadas",
            status=status_1,
            measured=measured_1,
            threshold=f">= {GATE_6A_MIN_CONSECUTIVE_CYCLES} ciclos consec., 0 erros (DB + logs)",
            details=details_1,
        )
    )

    # 2. Offset de relógio dentro de ±500 ms
    if total_cycles == 0:
        results.append(
            CriterionResult(
                id="gate_6a.clock_offset_within_500ms",
                group="gate_6a",
                name="Offset do relógio monotônico dentro de ±500 ms",
                status="UNKNOWN",
                measured="N/A",
                threshold=f"|clock_offset_ms| <= {GATE_6A_MAX_ABS_CLOCK_OFFSET_MS:.0f} ms",
                details=f"Sem dados de clock_offset_ms em cycle_health para mode={gate6a_mode}.",
            )
        )
    else:
        offsets = [float(r["clock_offset_ms"]) for r in rows]
        has_non_finite = any(not math.isfinite(x) for x in offsets)
        finite_abs = [abs(x) for x in offsets if math.isfinite(x)]
        max_abs = max(finite_abs) if finite_abs else float("inf")
        violations = sum(
            1 for x in offsets if (not math.isfinite(x)) or abs(x) > GATE_6A_MAX_ABS_CLOCK_OFFSET_MS
        )
        if has_non_finite or violations > 0:
            status_2: Status = "FAIL"
            details_2 = (
                f"{violations}/{total_cycles} ciclos violaram ±{GATE_6A_MAX_ABS_CLOCK_OFFSET_MS:.0f} ms "
                f"(mode={gate6a_mode})."
            )
        else:
            status_2 = "PASS"
            details_2 = (
                f"Todos os {total_cycles} ciclos mantiveram |offset| <= "
                f"{GATE_6A_MAX_ABS_CLOCK_OFFSET_MS:.0f} ms."
            )
        results.append(
            CriterionResult(
                id="gate_6a.clock_offset_within_500ms",
                group="gate_6a",
                name="Offset do relógio monotônico dentro de ±500 ms",
                status=status_2,
                measured=f"max |offset| = {max_abs:.1f} ms ({violations} violações / {total_cycles} ciclos)",
                threshold=f"|clock_offset_ms| <= {GATE_6A_MAX_ABS_CLOCK_OFFSET_MS:.0f} ms",
                details=details_2,
            )
        )

    # 3. Teste de Restauração de Desastre (evidência explícita via --restore-drill-report)
    if restore_drill_report is None:
        results.append(
            CriterionResult(
                id="gate_6a.disaster_restore_drill",
                group="gate_6a",
                name="Teste de Restauração de Desastre (backup SQLite)",
                status="UNKNOWN",
                measured="N/A",
                threshold="Restore drill PASS (tools/restore_drill.sh)",
                details=(
                    "Evidência não fornecida; execute tools/restore_drill.sh <backup> "
                    "--report-json <arquivo> e passe --restore-drill-report."
                ),
            )
        )
    else:
        rpath = Path(restore_drill_report)
        if not rpath.is_file():
            results.append(
                CriterionResult(
                    id="gate_6a.disaster_restore_drill",
                    group="gate_6a",
                    name="Teste de Restauração de Desastre (backup SQLite)",
                    status="UNKNOWN",
                    measured="N/A (arquivo não encontrado)",
                    threshold="Restore drill PASS (tools/restore_drill.sh)",
                    details=f"Relatório de restore drill não encontrado: {rpath}.",
                )
            )
        else:
            try:
                payload = json.loads(rpath.read_text(encoding="utf-8"))
                raw_st = str(payload.get("status", "")).upper()
                bkp_ref = str(payload.get("backup_path", "?"))
                verified_at = str(payload.get("verified_at_utc", "?"))
            except Exception as exc:
                results.append(
                    CriterionResult(
                        id="gate_6a.disaster_restore_drill",
                        group="gate_6a",
                        name="Teste de Restauração de Desastre (backup SQLite)",
                        status="UNKNOWN",
                        measured="N/A (JSON inválido)",
                        threshold="Restore drill PASS (tools/restore_drill.sh)",
                        details=f"Falha ao ler {rpath}: {exc}",
                    )
                )
            else:
                if raw_st == "PASS":
                    st_3: Status = "PASS"
                elif raw_st == "FAIL":
                    st_3 = "FAIL"
                else:
                    st_3 = "UNKNOWN"
                results.append(
                    CriterionResult(
                        id="gate_6a.disaster_restore_drill",
                        group="gate_6a",
                        name="Teste de Restauração de Desastre (backup SQLite)",
                        status=st_3,
                        measured=f"{raw_st or 'MISSING'} (backup={Path(bkp_ref).name})",
                        threshold="Restore drill PASS (tools/restore_drill.sh)",
                        details=f"Lido de {rpath} (verified_at_utc={verified_at}).",
                    )
                )

    return results


def _evaluate_gate_6b(
    conn: sqlite3.Connection,
    tables: set[str],
    *,
    min_weekly_trades: float,
    max_weekly_trades: float,
) -> list[CriterionResult]:
    results: list[CriterionResult] = []

    shadow_cycles: list[sqlite3.Row] = []
    if "cycle_health" in tables:
        shadow_cycles = conn.execute(
            """
            SELECT cycle_close_time_ms
            FROM cycle_health
            WHERE mode = 'SHADOW_CHAMELEON'
            ORDER BY cycle_close_time_ms ASC
            """
        ).fetchall()

    shadow_rows: list[sqlite3.Row] = []
    if "shadow_trades" in tables:
        shadow_rows = conn.execute(
            """
            SELECT shadow_id, symbol, signal_close_time_ms, status,
                   decision_ask_price, created_at_ms, closed_at_ms
            FROM shadow_trades
            ORDER BY created_at_ms ASC
            """
        ).fetchall()

    prod_trade_rows: list[sqlite3.Row] = []
    if "trades" in tables:
        prod_trade_rows = conn.execute(
            """
            SELECT trade_id, status, slippage_entry_bps, created_at_ms, closed_at_ms
            FROM trades
            WHERE environment = 'PRODUCTION' AND status != 'ENTRY_REJECTED'
            ORDER BY created_at_ms ASC
            """
        ).fetchall()

    # Determina fonte principal do Gate 6B: SHADOW_CHAMELEON (padrão) ou lote mínimo PRODUCTION
    has_shadow_data = bool(shadow_cycles or shadow_rows)
    has_prod_data = bool(prod_trade_rows)

    timestamps_ms: list[int] = []
    if has_shadow_data:
        source_label = "SHADOW_CHAMELEON"
        timestamps_ms.extend(int(r["cycle_close_time_ms"]) for r in shadow_cycles)
        for r in shadow_rows:
            timestamps_ms.append(int(r["created_at_ms"]))
            if r["closed_at_ms"] is not None:
                timestamps_ms.append(int(r["closed_at_ms"]))
        total_entries_6b = len(shadow_rows)
        closed_trades_6b = sum(1 for r in shadow_rows if str(r["status"]) == "CLOSED")
    elif has_prod_data:
        source_label = "PRODUCTION_ISOLATED"
        for r in prod_trade_rows:
            timestamps_ms.append(int(r["created_at_ms"]))
            if r["closed_at_ms"] is not None:
                timestamps_ms.append(int(r["closed_at_ms"]))
        total_entries_6b = len(prod_trade_rows)
        closed_trades_6b = sum(1 for r in prod_trade_rows if str(r["status"]) == "CLOSED")
    else:
        source_label = "NONE"
        total_entries_6b = 0
        closed_trades_6b = 0

    span_days: Optional[float] = None
    if len(timestamps_ms) >= 2:
        span_days = (max(timestamps_ms) - min(timestamps_ms)) / float(ONE_DAY_MS)
    elif len(timestamps_ms) == 1:
        span_days = 0.0

    # 1. Duração >= 30 dias OU >= 25 trades
    if source_label == "NONE":
        results.append(
            CriterionResult(
                id="gate_6b.duration_or_sample",
                group="gate_6b",
                name="Horizonte mínimo (>= 30 dias ou >= 25 trades)",
                status="UNKNOWN",
                measured="N/A",
                threshold=f">= {GATE_6B_MIN_DAYS:.0f} dias OU >= {GATE_6B_MIN_TRADES} trades fechados",
                details="Sem registros de SHADOW_CHAMELEON (cycle_health/shadow_trades) nem trades PRODUCTION.",
            )
        )
    else:
        assert span_days is not None
        ok_dur = span_days >= GATE_6B_MIN_DAYS or closed_trades_6b >= GATE_6B_MIN_TRADES
        results.append(
            CriterionResult(
                id="gate_6b.duration_or_sample",
                group="gate_6b",
                name="Horizonte mínimo (>= 30 dias ou >= 25 trades)",
                status="PASS" if ok_dur else "FAIL",
                measured=f"{span_days:.1f} dias | {closed_trades_6b} trades fechados ({source_label})",
                threshold=f">= {GATE_6B_MIN_DAYS:.0f} dias OU >= {GATE_6B_MIN_TRADES} trades fechados",
                details=f"Fonte={source_label}; total_entradas={total_entries_6b}.",
            )
        )

    # 2. Slippage médio realizado <= 0,08% (8,0 bps)
    # Prioridade 1: trades.slippage_entry_bps (PRODUCTION);
    # Prioridade 2: shadow_trades.decision_ask_price vs candles(1h).close no signal_close_time_ms.
    # Nunca usa sim_entry_price - decision_ask_price (que é constante hardcoded).
    slip_samples_pct: list[float] = []
    slip_source = "NONE"

    prod_slips = [
        float(r["slippage_entry_bps"]) / 100.0
        for r in prod_trade_rows
        if r["slippage_entry_bps"] is not None and math.isfinite(float(r["slippage_entry_bps"]))
    ]
    if prod_slips:
        slip_samples_pct = prod_slips
        slip_source = "trades.slippage_entry_bps (PRODUCTION)"
    elif shadow_rows and "candles" in tables:
        st_cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(shadow_trades)").fetchall()
        }
        has_vwap = "depth_vwap_ask" in st_cols
        price_expr = (
            "COALESCE(s.depth_vwap_ask, s.decision_ask_price)"
            if has_vwap
            else "s.decision_ask_price"
        )
        vwap_select = "s.depth_vwap_ask" if has_vwap else "NULL AS depth_vwap_ask"
        joined = conn.execute(
            f"""
            SELECT {price_expr} AS exec_ask_price,
                   {vwap_select},
                   c.close AS candle_close
            FROM shadow_trades s
            INNER JOIN candles c
                ON c.symbol = s.symbol
               AND c.interval = '1h'
               AND c.close_time_ms = s.signal_close_time_ms
            WHERE c.close > 0
            """
        ).fetchall()
        used_vwap_count = 0
        for jr in joined:
            ask = float(jr["exec_ask_price"])
            close_p = float(jr["candle_close"])
            if math.isfinite(ask) and math.isfinite(close_p) and close_p > 0.0:
                slip_samples_pct.append(((ask - close_p) / close_p) * 100.0)
                if jr["depth_vwap_ask"] is not None:
                    used_vwap_count += 1
        if slip_samples_pct:
            slip_source = (
                f"shadow_trades.depth_vwap_ask/decision_ask_price vs candles(1h).close "
                f"(vwap_l2={used_vwap_count}/{len(slip_samples_pct)})"
                if used_vwap_count > 0
                else "shadow_trades.decision_ask_price vs candles(1h).close"
            )

    if not slip_samples_pct:
        results.append(
            CriterionResult(
                id="gate_6b.mean_slippage_le_0_08pct",
                group="gate_6b",
                name="Slippage médio realizado <= 0,08%",
                status="UNKNOWN",
                measured="N/A",
                threshold=f"<= {GATE_6B_MAX_MEAN_SLIPPAGE_PCT:.2f}% (8.0 bps)",
                details=(
                    "Sem amostras de slippage real (trades.slippage_entry_bps em PRODUCTION ou "
                    "shadow_trades casados com candles 1h no signal_close_time_ms)."
                ),
            )
        )
    else:
        mean_slip_pct = sum(slip_samples_pct) / len(slip_samples_pct)
        mean_slip_bps = mean_slip_pct * 100.0
        st_slip: Status = "PASS" if mean_slip_pct <= GATE_6B_MAX_MEAN_SLIPPAGE_PCT else "FAIL"
        results.append(
            CriterionResult(
                id="gate_6b.mean_slippage_le_0_08pct",
                group="gate_6b",
                name="Slippage médio realizado <= 0,08%",
                status=st_slip,
                measured=f"{mean_slip_pct:.4f}% ({mean_slip_bps:.2f} bps, n={len(slip_samples_pct)})",
                threshold=f"<= {GATE_6B_MAX_MEAN_SLIPPAGE_PCT:.2f}% (8.0 bps)",
                details=f"Fonte: {slip_source}.",
            )
        )

    # 3. Frequência semanal compatível com a distribuição histórica do Bloco 3
    if span_days is None or span_days < 7.0:
        results.append(
            CriterionResult(
                id="gate_6b.weekly_trade_frequency",
                group="gate_6b",
                name="Frequência semanal compatível com o Bloco 3",
                status="UNKNOWN",
                measured=f"N/A (janela={span_days:.1f} dias)" if span_days is not None else "N/A",
                threshold=f"[{min_weekly_trades:.2f}, {max_weekly_trades:.2f}] trades/semana (ref. K9.3 ~1.3/sem)",
                details=(
                    "Janela observada < 7 dias ou ausente; não é possível estimar taxa semanal "
                    "compatível com Bloco 3 K9.3 (docs/block-3-spec.md:350: 282 trades)."
                ),
            )
        )
    else:
        weeks = span_days / 7.0
        rate_per_week = total_entries_6b / weeks
        ok_freq = min_weekly_trades <= rate_per_week <= max_weekly_trades
        results.append(
            CriterionResult(
                id="gate_6b.weekly_trade_frequency",
                group="gate_6b",
                name="Frequência semanal compatível com o Bloco 3",
                status="PASS" if ok_freq else "FAIL",
                measured=f"{rate_per_week:.2f} trades/semana ({total_entries_6b} em {weeks:.2f} sem)",
                threshold=f"[{min_weekly_trades:.2f}, {max_weekly_trades:.2f}] trades/semana (ref. K9.3 ~1.3/sem)",
                details=(
                    f"Fonte={source_label}; ref. Bloco 3 K9.3 (docs/block-3-spec.md:350: 282 trades). "
                    "Faixa configurável via --min-weekly-trades / --max-weekly-trades."
                ),
            )
        )

    return results


def _evaluate_phase_1_to_2(
    conn: sqlite3.Connection,
    tables: set[str],
    state: dict[str, str],
    *,
    phase1_env: str,
    initial_equity_usdt: Optional[Decimal],
) -> list[CriterionResult]:
    results: list[CriterionResult] = []

    if "trades" in tables:
        closed_rows = conn.execute(
            """
            SELECT trade_id, status, realized_pnl_usdt, fees_usdt_equiv, realized_r,
                   slippage_entry_bps, ml_grade_silent, ml_p_cal_silent,
                   created_at_ms, updated_at_ms, closed_at_ms
            FROM trades
            WHERE environment = ? AND status = 'CLOSED'
            ORDER BY COALESCE(closed_at_ms, updated_at_ms) ASC, trade_id ASC
            """,
            (phase1_env,),
        ).fetchall()
        orphan_rows = conn.execute(
            """
            SELECT trade_id, status
            FROM trades
            WHERE environment = ?
              AND status IN ('HALT_REVIEW', 'ENTRY_FILLED', 'CLOSING')
            """,
            (phase1_env,),
        ).fetchall()
    else:
        closed_rows = []
        orphan_rows = []

    halt_event_trade_ids: set[int] = set()
    if "order_events" in tables and "trades" in tables:
        ev_rows = conn.execute(
            """
            SELECT DISTINCT oe.trade_id
            FROM order_events oe
            INNER JOIN trades t ON t.trade_id = oe.trade_id
            WHERE t.environment = ? AND oe.method = 'HALT_REVIEW'
            """,
            (phase1_env,),
        ).fetchall()
        halt_event_trade_ids = {int(r["trade_id"]) for r in ev_rows if r["trade_id"] is not None}

    n_closed = len(closed_rows)

    # Requisito 1: >= 60 operações reais fechadas E >= 60 dias corridos (§4.3 #1)
    if n_closed == 0:
        results.append(
            CriterionResult(
                id="phase_1_to_2.req1_sample_and_days",
                group="phase_1_to_2",
                name="Req. 1 — Amostra mínima (>= 60 trades fechados e >= 60 dias)",
                status="UNKNOWN",
                measured=f"0 trades fechados (env={phase1_env})",
                threshold=f">= {PHASE_1_TO_2_MIN_TRADES} trades E >= {PHASE_1_TO_2_MIN_DAYS:.0f} dias",
                details=f"Nenhuma operação CLOSED em trades para environment={phase1_env}.",
            )
        )
    else:
        min_ts = min(int(r["created_at_ms"]) for r in closed_rows)
        max_ts = max(
            int(r["closed_at_ms"] if r["closed_at_ms"] is not None else r["updated_at_ms"])
            for r in closed_rows
        )
        elapsed_days = max(0.0, (max_ts - min_ts) / float(ONE_DAY_MS))
        ok_req1 = n_closed >= PHASE_1_TO_2_MIN_TRADES and elapsed_days >= PHASE_1_TO_2_MIN_DAYS
        results.append(
            CriterionResult(
                id="phase_1_to_2.req1_sample_and_days",
                group="phase_1_to_2",
                name="Req. 1 — Amostra mínima (>= 60 trades fechados e >= 60 dias)",
                status="PASS" if ok_req1 else "FAIL",
                measured=f"{n_closed} trades | {elapsed_days:.1f} dias (env={phase1_env})",
                threshold=f">= {PHASE_1_TO_2_MIN_TRADES} trades E >= {PHASE_1_TO_2_MIN_DAYS:.0f} dias",
                details=f"Janela entre created_at_ms inicial e closed_at_ms final em environment={phase1_env}.",
            )
        )

    # Requisito 2: Lucro líquido acumulado positivo E Profit Factor >= 1,15 (§4.3 #2)
    net_pnls: list[Decimal] = []
    missing_pnl = 0
    for r in closed_rows:
        pnl = _safe_decimal(r["realized_pnl_usdt"])
        if pnl is None:
            missing_pnl += 1
            continue
        fee = _safe_decimal(r["fees_usdt_equiv"]) or Decimal("0")
        net_pnls.append(pnl - fee)

    if n_closed == 0 or missing_pnl > 0:
        results.append(
            CriterionResult(
                id="phase_1_to_2.req2_profit_and_pf",
                group="phase_1_to_2",
                name="Req. 2 — Lucro líquido positivo e Profit Factor >= 1,15",
                status="UNKNOWN",
                measured=f"N/A (n={n_closed}, sem_pnl={missing_pnl})",
                threshold=f"PnL líquido > 0 USDT E PF >= {PHASE_1_TO_2_MIN_PROFIT_FACTOR:.2f}",
                details=(
                    f"Dados de realized_pnl_usdt ausentes ou incompletos em trades "
                    f"(env={phase1_env}, faltando={missing_pnl})."
                ),
            )
        )
    else:
        total_pnl = sum(net_pnls, Decimal("0"))
        gross_profit = sum((p for p in net_pnls if p > Decimal("0")), Decimal("0"))
        gross_loss = sum((-p for p in net_pnls if p < Decimal("0")), Decimal("0"))
        if gross_loss > Decimal("0"):
            pf = float(gross_profit / gross_loss)
            pf_str = f"{pf:.2f}"
        elif gross_profit > Decimal("0"):
            pf = float("inf")
            pf_str = "inf"
        else:
            pf = 0.0
            pf_str = "0.00"
        ok_req2 = total_pnl > Decimal("0") and pf >= PHASE_1_TO_2_MIN_PROFIT_FACTOR
        results.append(
            CriterionResult(
                id="phase_1_to_2.req2_profit_and_pf",
                group="phase_1_to_2",
                name="Req. 2 — Lucro líquido positivo e Profit Factor >= 1,15",
                status="PASS" if ok_req2 else "FAIL",
                measured=f"PnL={total_pnl:+.2f} USDT | PF={pf_str} (n={len(net_pnls)})",
                threshold=f"PnL líquido > 0 USDT E PF >= {PHASE_1_TO_2_MIN_PROFIT_FACTOR:.2f}",
                details=(
                    f"gross_profit={gross_profit:.2f} USDT, gross_loss={gross_loss:.2f} USDT "
                    f"(deduzindo fees_usdt_equiv quando preenchido)."
                ),
            )
        )

    # Requisito 3: Média R_live >= +0,10R E Max Drawdown real < 10% (§4.3 #3)
    r_vals: list[float] = []
    missing_r = 0
    for r in closed_rows:
        if r["realized_r"] is None or not math.isfinite(float(r["realized_r"])):
            missing_r += 1
        else:
            r_vals.append(float(r["realized_r"]))

    init_eq = initial_equity_usdt
    if init_eq is None:
        for k in ("initial_tactical_equity_usdt", "phase1_initial_equity_usdt", "tactical_initial_usdt"):
            cand = _safe_decimal(state.get(k))
            if cand is not None and cand > Decimal("0"):
                init_eq = cand
                break

    max_dd_pct: Optional[float] = None
    dd_source = "N/A"
    if init_eq is not None and init_eq > Decimal("0") and n_closed > 0 and missing_pnl == 0:
        eq = init_eq
        peak = init_eq
        max_dd = Decimal("0")
        for p in net_pnls:
            eq += p
            if eq > peak:
                peak = eq
            if peak > Decimal("0"):
                dd = max(Decimal("0"), (peak - eq) / peak)
                if dd > max_dd:
                    max_dd = dd
        stored_hwm = _safe_decimal(state.get("tactical_hwm_usdt"))
        if stored_hwm is not None and stored_hwm > peak and stored_hwm > Decimal("0"):
            dd_hwm = max(Decimal("0"), (stored_hwm - eq) / stored_hwm)
            if dd_hwm > max_dd:
                max_dd = dd_hwm
        max_dd_pct = float(max_dd * Decimal("100"))
        dd_source = f"curva real de equity (init={init_eq:.2f} USDT)"
    else:
        stored_dd = _safe_decimal(state.get("max_drawdown_pct"))
        if stored_dd is not None:
            # Aceita tanto fração (0.05) quanto percentual? Somente valores em [0, 100]:
            # convenção documentada no system_state: percentual (ex.: 5.2 para 5.2%)
            max_dd_pct = float(stored_dd)
            dd_source = "system_state.max_drawdown_pct"

    if n_closed == 0 or missing_r > 0:
        results.append(
            CriterionResult(
                id="phase_1_to_2.req3_expectancy_and_max_dd",
                group="phase_1_to_2",
                name="Req. 3 — Expectância >= +0,10R e Max Drawdown < 10%",
                status="UNKNOWN",
                measured=f"N/A (n={n_closed}, sem_r={missing_r})",
                threshold=f"mean(R) >= +{PHASE_1_TO_2_MIN_MEAN_R:.2f}R E MaxDD < {PHASE_1_TO_2_MAX_DD_PCT:.1f}%",
                details=f"Sem dados completos de realized_r em trades (env={phase1_env}).",
            )
        )
    else:
        mean_r = sum(r_vals) / len(r_vals)
        if mean_r < PHASE_1_TO_2_MIN_MEAN_R:
            st_req3: Status = "FAIL"
            dd_txt = f"{max_dd_pct:.2f}%" if max_dd_pct is not None else "UNKNOWN"
            det_req3 = f"Média realizada {mean_r:+.3f}R < +{PHASE_1_TO_2_MIN_MEAN_R:.2f}R (MaxDD={dd_txt})."
        elif max_dd_pct is None:
            st_req3 = "UNKNOWN"
            dd_txt = "UNKNOWN"
            det_req3 = (
                f"Média R={mean_r:+.3f}R atende limiar, mas falta capital inicial "
                "(system_state.initial_tactical_equity_usdt ou --initial-equity-usdt) para calcular MaxDD real."
            )
        elif max_dd_pct >= PHASE_1_TO_2_MAX_DD_PCT:
            st_req3 = "FAIL"
            dd_txt = f"{max_dd_pct:.2f}%"
            det_req3 = f"MaxDD real {max_dd_pct:.2f}% >= {PHASE_1_TO_2_MAX_DD_PCT:.1f}% ({dd_source})."
        else:
            st_req3 = "PASS"
            dd_txt = f"{max_dd_pct:.2f}%"
            det_req3 = f"mean(R)={mean_r:+.3f}R, MaxDD={max_dd_pct:.2f}% ({dd_source})."
        results.append(
            CriterionResult(
                id="phase_1_to_2.req3_expectancy_and_max_dd",
                group="phase_1_to_2",
                name="Req. 3 — Expectância >= +0,10R e Max Drawdown < 10%",
                status=st_req3,
                measured=f"mean(R)={mean_r:+.3f}R | MaxDD={dd_txt}",
                threshold=f"mean(R) >= +{PHASE_1_TO_2_MIN_MEAN_R:.2f}R E MaxDD < {PHASE_1_TO_2_MAX_DD_PCT:.1f}%",
                details=det_req3,
            )
        )

    # Requisito 4: Zero posições órfãs não recuperadas e slippage médio <= 0,10% (§4.3 #4)
    orphan_ids = {int(r["trade_id"]) for r in orphan_rows} | halt_event_trade_ids
    orphan_count = len(orphan_ids)
    live_slips_pct = [
        float(r["slippage_entry_bps"]) / 100.0
        for r in closed_rows
        if r["slippage_entry_bps"] is not None and math.isfinite(float(r["slippage_entry_bps"]))
    ]
    if orphan_count > 0:
        slip_txt = (
            f"{sum(live_slips_pct)/len(live_slips_pct):.4f}%"
            if live_slips_pct
            else "N/A"
        )
        results.append(
            CriterionResult(
                id="phase_1_to_2.req4_zero_orphans_and_slippage",
                group="phase_1_to_2",
                name="Req. 4 — Zero posições órfãs/HALT_REVIEW e slippage <= 0,10%",
                status="FAIL",
                measured=f"órfãs/HALT_REVIEW={orphan_count} | slippage={slip_txt}",
                threshold=f"0 órfãs/HALT_REVIEW E slippage <= {PHASE_1_TO_2_MAX_SLIPPAGE_PCT:.2f}% (10.0 bps)",
                details=f"Trade IDs com falha grave/HALT_REVIEW em {phase1_env}: {sorted(orphan_ids)}.",
            )
        )
    elif not live_slips_pct:
        results.append(
            CriterionResult(
                id="phase_1_to_2.req4_zero_orphans_and_slippage",
                group="phase_1_to_2",
                name="Req. 4 — Zero posições órfãs/HALT_REVIEW e slippage <= 0,10%",
                status="UNKNOWN",
                measured="órfãs/HALT_REVIEW=0 | slippage=N/A",
                threshold=f"0 órfãs/HALT_REVIEW E slippage <= {PHASE_1_TO_2_MAX_SLIPPAGE_PCT:.2f}% (10.0 bps)",
                details=f"Sem registros de slippage_entry_bps em trades CLOSED (env={phase1_env}).",
            )
        )
    else:
        mean_live_slip = sum(live_slips_pct) / len(live_slips_pct)
        ok_req4 = mean_live_slip <= PHASE_1_TO_2_MAX_SLIPPAGE_PCT
        results.append(
            CriterionResult(
                id="phase_1_to_2.req4_zero_orphans_and_slippage",
                group="phase_1_to_2",
                name="Req. 4 — Zero posições órfãs/HALT_REVIEW e slippage <= 0,10%",
                status="PASS" if ok_req4 else "FAIL",
                measured=f"órfãs/HALT_REVIEW=0 | slippage={mean_live_slip:.4f}% ({mean_live_slip*100:.2f} bps, n={len(live_slips_pct)})",
                threshold=f"0 órfãs/HALT_REVIEW E slippage <= {PHASE_1_TO_2_MAX_SLIPPAGE_PCT:.2f}% (10.0 bps)",
                details=f"Verificados trades e order_events (method='HALT_REVIEW') em env={phase1_env}.",
            )
        )

    # Requisito 5: Prova ao vivo da IA silenciosa (B/A+ > C e ECE <= 0,05 em 60+ trades) (§4.3 #5)
    b_aplus_r: list[float] = []
    c_r: list[float] = []
    cal_probs: list[float] = []
    cal_outcomes: list[int] = []
    for r in closed_rows:
        if r["realized_r"] is None or not math.isfinite(float(r["realized_r"])):
            continue
        rv = float(r["realized_r"])
        grade = r["ml_grade_silent"]
        if grade in ("B", "A+"):
            b_aplus_r.append(rv)
        elif grade == "C":
            c_r.append(rv)
        if r["ml_p_cal_silent"] is not None and math.isfinite(float(r["ml_p_cal_silent"])):
            cal_probs.append(float(r["ml_p_cal_silent"]))
            cal_outcomes.append(1 if rv > 0.0 else 0)

    n_graded = len(b_aplus_r) + len(c_r)
    if not b_aplus_r or not c_r or not cal_probs:
        results.append(
            CriterionResult(
                id="phase_1_to_2.req5_silent_ai_proof",
                group="phase_1_to_2",
                name="Req. 5 — IA silenciosa: mean R (B/A+) > mean R (C) e ECE <= 0,05",
                status="UNKNOWN",
                measured=f"n_B/A+={len(b_aplus_r)}, n_C={len(c_r)}, n_p_cal={len(cal_probs)}",
                threshold=f"n >= {PHASE_1_TO_2_MIN_TRADES}, mean(B/A+) > mean(C) E ECE <= {PHASE_1_TO_2_MAX_ECE:.2f}",
                details=(
                    "Dados insuficientes em trades (ml_grade_silent com ambos grupos B/A+ e C, "
                    "e ml_p_cal_silent) para provar separação e calibração da IA."
                ),
            )
        )
    else:
        mean_ba = sum(b_aplus_r) / len(b_aplus_r)
        mean_c = sum(c_r) / len(c_r)
        ece = compute_expected_calibration_error(cal_probs, cal_outcomes, n_bins=10)
        ok_req5 = (
            n_graded >= PHASE_1_TO_2_MIN_TRADES
            and mean_ba > mean_c
            and ece <= PHASE_1_TO_2_MAX_ECE
        )
        results.append(
            CriterionResult(
                id="phase_1_to_2.req5_silent_ai_proof",
                group="phase_1_to_2",
                name="Req. 5 — IA silenciosa: mean R (B/A+) > mean R (C) e ECE <= 0,05",
                status="PASS" if ok_req5 else "FAIL",
                measured=(
                    f"B/A+={mean_ba:+.3f}R (n={len(b_aplus_r)}) vs C={mean_c:+.3f}R (n={len(c_r)}) | "
                    f"ECE={ece:.4f}"
                ),
                threshold=f"n >= {PHASE_1_TO_2_MIN_TRADES}, mean(B/A+) > mean(C) E ECE <= {PHASE_1_TO_2_MAX_ECE:.2f}",
                details="ECE calculado com compute_expected_calibration_error (src/features/ml_judge.py:65, 10 bins).",
            )
        )

    return results


def _evaluate_kill_switches(
    conn: sqlite3.Connection,
    tables: set[str],
    state: dict[str, str],
    *,
    phase1_env: str,
    initial_equity_usdt: Optional[Decimal],
    max_daily_trades_cap: int,
) -> list[CriterionResult]:
    results: list[CriterionResult] = []

    halt_raw = state.get("emergency_halt")
    reason_raw = state.get("kill_switch_reason", "NONE")
    halt_review_reason = state.get("halt_review_reason")

    # 0. Estado global EMERGENCY_HALT
    if halt_raw is None:
        results.append(
            CriterionResult(
                id="kill_switches.emergency_halt",
                group="kill_switches",
                name="Estado global EMERGENCY_HALT",
                status="UNKNOWN",
                measured="N/A (ausente em system_state)",
                threshold="emergency_halt == '0'",
                details="Chave emergency_halt não encontrada em system_state.",
            )
        )
    elif halt_raw == "1":
        extra = f"; halt_review_reason={halt_review_reason}" if halt_review_reason else ""
        results.append(
            CriterionResult(
                id="kill_switches.emergency_halt",
                group="kill_switches",
                name="Estado global EMERGENCY_HALT",
                status="FAIL",
                measured=f"ATIVO (reason={reason_raw})",
                threshold="emergency_halt == '0'",
                details=f"system_state.emergency_halt='1', kill_switch_reason='{reason_raw}'{extra}.",
            )
        )
    else:
        results.append(
            CriterionResult(
                id="kill_switches.emergency_halt",
                group="kill_switches",
                name="Estado global EMERGENCY_HALT",
                status="PASS",
                measured=f"INATIVO (reason={reason_raw})",
                threshold="emergency_halt == '0'",
                details=f"system_state.emergency_halt='{halt_raw}'.",
            )
        )

    # KS-1: Hard Stop Patrimonial (-12,0% abaixo do HWM) (§5.2 #1)
    hwm = _safe_decimal(state.get("tactical_hwm_usdt"))
    cur_eq = _safe_decimal(state.get("current_equity_usdt")) or _safe_decimal(
        state.get("shadow_virtual_equity_usdt")
    )
    if cur_eq is None and initial_equity_usdt is not None and "trades" in tables:
        pnl_rows = conn.execute(
            """
            SELECT realized_pnl_usdt, fees_usdt_equiv
            FROM trades
            WHERE environment = ? AND status = 'CLOSED' AND realized_pnl_usdt IS NOT NULL
            """,
            (phase1_env,),
        ).fetchall()
        if pnl_rows:
            net_sum = sum(
                (
                    (_safe_decimal(r["realized_pnl_usdt"]) or Decimal("0"))
                    - (_safe_decimal(r["fees_usdt_equiv"]) or Decimal("0"))
                )
                for r in pnl_rows
            )
            cur_eq = initial_equity_usdt + net_sum

    if reason_raw == "HARD_STOP_DRAWDOWN_12PCT":
        results.append(
            CriterionResult(
                id="kill_switches.ks1_hard_stop_drawdown_12pct",
                group="kill_switches",
                name="KS-1 — Hard Stop Patrimonial (-12,0% do HWM)",
                status="FAIL",
                measured="DISPARADO (HARD_STOP_DRAWDOWN_12PCT)",
                threshold=f"drawdown < {KS1_HARD_STOP_DD_PCT:.1f}%",
                details=f"kill_switch_reason=HARD_STOP_DRAWDOWN_12PCT (hwm={hwm}, eq={cur_eq}).",
            )
        )
    elif hwm is not None and hwm > Decimal("0") and cur_eq is not None:
        dd_pct = float(max(Decimal("0"), (hwm - cur_eq) / hwm) * Decimal("100"))
        cppi_note = " | CPPI (>=4%) ativo" if dd_pct >= 4.0 else ""
        results.append(
            CriterionResult(
                id="kill_switches.ks1_hard_stop_drawdown_12pct",
                group="kill_switches",
                name="KS-1 — Hard Stop Patrimonial (-12,0% do HWM)",
                status="FAIL" if dd_pct >= KS1_HARD_STOP_DD_PCT else "PASS",
                measured=f"DD={dd_pct:.2f}% (eq={cur_eq:.2f} / hwm={hwm:.2f} USDT)",
                threshold=f"drawdown < {KS1_HARD_STOP_DD_PCT:.1f}%",
                details=f"Calculado via system_state.tactical_hwm_usdt e equity atual{cppi_note}.",
            )
        )
    else:
        results.append(
            CriterionResult(
                id="kill_switches.ks1_hard_stop_drawdown_12pct",
                group="kill_switches",
                name="KS-1 — Hard Stop Patrimonial (-12,0% do HWM)",
                status="UNKNOWN",
                measured=f"N/A (hwm={hwm}, eq={cur_eq})",
                threshold=f"drawdown < {KS1_HARD_STOP_DD_PCT:.1f}%",
                details="Falta tactical_hwm_usdt ou equity atual em system_state para medir drawdown.",
            )
        )

    # KS-2: Reserva de BNB >= $2,00 USDT (alerta < $5,00) (§5.2 #2)
    bnb_val = _safe_decimal(state.get("bnb_free_usdt_equiv")) or _safe_decimal(
        state.get("bnb_reserve_usdt")
    )
    if reason_raw == "BNB_FEE_RESERVE_DEPLETED":
        results.append(
            CriterionResult(
                id="kill_switches.ks2_bnb_fee_reserve_2usdt",
                group="kill_switches",
                name="KS-2 — Reserva de Taxas em BNB (>= 2,00 USDT)",
                status="FAIL",
                measured=f"DISPARADO (bnb={bnb_val} USDT)" if bnb_val is not None else "DISPARADO",
                threshold=f"BNB >= {KS2_HARD_MIN_BNB_USDT:.2f} USDT (alerta < {KS2_WARN_MIN_BNB_USDT:.2f})",
                details="kill_switch_reason=BNB_FEE_RESERVE_DEPLETED em system_state.",
            )
        )
    elif bnb_val is not None:
        warn = " (abaixo da reserva preventiva de 5.00 USDT)" if bnb_val < KS2_WARN_MIN_BNB_USDT else ""
        results.append(
            CriterionResult(
                id="kill_switches.ks2_bnb_fee_reserve_2usdt",
                group="kill_switches",
                name="KS-2 — Reserva de Taxas em BNB (>= 2,00 USDT)",
                status="FAIL" if bnb_val < KS2_HARD_MIN_BNB_USDT else "PASS",
                measured=f"{bnb_val:.2f} USDT",
                threshold=f"BNB >= {KS2_HARD_MIN_BNB_USDT:.2f} USDT (alerta < {KS2_WARN_MIN_BNB_USDT:.2f})",
                details=f"Lido de system_state{warn}.",
            )
        )
    else:
        results.append(
            CriterionResult(
                id="kill_switches.ks2_bnb_fee_reserve_2usdt",
                group="kill_switches",
                name="KS-2 — Reserva de Taxas em BNB (>= 2,00 USDT)",
                status="UNKNOWN",
                measured="N/A (não persistido em SQLite)",
                threshold=f"BNB >= {KS2_HARD_MIN_BNB_USDT:.2f} USDT (alerta < {KS2_WARN_MIN_BNB_USDT:.2f})",
                details=(
                    "Saldo livre de BNB é consultado em memória pela API Binance e não está "
                    "persistido em system_state (nunca PASS por omissão)."
                ),
            )
        )

    # KS-3: Reconciliação (< 3 falhas consecutivas) e zero erro auth / HTTP 418 (§5.2 #3)
    streak_raw = state.get("reconcile_fail_streak")
    streak_int: Optional[int] = None
    if streak_raw is not None:
        try:
            streak_int = int(streak_raw)
        except ValueError:
            streak_int = None

    active_halt_review = 0
    if "trades" in tables:
        active_halt_review = int(
            conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'HALT_REVIEW'"
            ).fetchone()[0]
        )

    auth_or_ban_events = 0
    if "order_events" in tables:
        placeholders = ",".join("?" for _ in KS3_AUTH_CODES)
        auth_or_ban_events = int(
            conn.execute(
                f"""
                SELECT COUNT(*) FROM order_events
                WHERE binance_code IN ({placeholders}) OR http_status = 418
                """,
                KS3_AUTH_CODES,
            ).fetchone()[0]
        )

    if (
        reason_raw == "RECONCILIATION_OR_AUTH_FAILURE"
        or (streak_int is not None and streak_int >= KS3_MAX_RECONCILE_FAIL_STREAK)
        or active_halt_review > 0
        or auth_or_ban_events > 0
    ):
        results.append(
            CriterionResult(
                id="kill_switches.ks3_reconciliation_or_auth",
                group="kill_switches",
                name="KS-3 — Reconciliação (< 3 falhas) e Auth/IP Ban",
                status="FAIL",
                measured=(
                    f"streak={streak_int if streak_int is not None else 'N/A'} | "
                    f"HALT_REVIEW={active_halt_review} | auth/418={auth_or_ban_events}"
                ),
                threshold=f"streak < {KS3_MAX_RECONCILE_FAIL_STREAK}, 0 HALT_REVIEW, 0 auth/418",
                details=f"kill_switch_reason={reason_raw}.",
            )
        )
    elif streak_int is not None:
        results.append(
            CriterionResult(
                id="kill_switches.ks3_reconciliation_or_auth",
                group="kill_switches",
                name="KS-3 — Reconciliação (< 3 falhas) e Auth/IP Ban",
                status="PASS",
                measured=f"streak={streak_int} | HALT_REVIEW=0 | auth/418=0",
                threshold=f"streak < {KS3_MAX_RECONCILE_FAIL_STREAK}, 0 HALT_REVIEW, 0 auth/418",
                details="Lido de system_state.reconcile_fail_streak, trades e order_events.",
            )
        )
    else:
        results.append(
            CriterionResult(
                id="kill_switches.ks3_reconciliation_or_auth",
                group="kill_switches",
                name="KS-3 — Reconciliação (< 3 falhas) e Auth/IP Ban",
                status="UNKNOWN",
                measured="streak=N/A | HALT_REVIEW=0 | auth/418=0",
                threshold=f"streak < {KS3_MAX_RECONCILE_FAIL_STREAK}, 0 HALT_REVIEW, 0 auth/418",
                details="Chave reconcile_fail_streak ausente em system_state.",
            )
        )

    # KS-4: Anomalia de Frequência (< max_daily_trades_cap = 6 ordens em 24h) (§5.2 #4)
    trade_ts: list[int] = []
    shadow_ts: list[int] = []
    cycle_count_total = 0
    if "trades" in tables:
        trade_ts = [
            int(r["created_at_ms"])
            for r in conn.execute(
                "SELECT created_at_ms FROM trades WHERE status != 'ENTRY_REJECTED'"
            ).fetchall()
        ]
    if "shadow_trades" in tables:
        shadow_ts = [
            int(r["created_at_ms"])
            for r in conn.execute("SELECT created_at_ms FROM shadow_trades").fetchall()
        ]
    if "cycle_health" in tables:
        cycle_count_total = int(conn.execute("SELECT COUNT(*) FROM cycle_health").fetchone()[0])

    max_24h_live = _max_rolling_24h_count(trade_ts)
    max_24h_shadow = _max_rolling_24h_count(shadow_ts)
    max_24h = max(max_24h_live, max_24h_shadow)

    if reason_raw == "DAILY_TRADES_CAP_EXCEEDED" or max_24h >= max_daily_trades_cap:
        results.append(
            CriterionResult(
                id="kill_switches.ks4_daily_trades_cap",
                group="kill_switches",
                name="KS-4 — Anomalia de Frequência (teto 24h)",
                status="FAIL",
                measured=f"pico 24h={max_24h} (live={max_24h_live}, shadow={max_24h_shadow})",
                threshold=f"< {max_daily_trades_cap} entradas em qualquer janela móvel de 24h",
                details=f"kill_switch_reason={reason_raw}.",
            )
        )
    elif cycle_count_total == 0 and not trade_ts and not shadow_ts:
        results.append(
            CriterionResult(
                id="kill_switches.ks4_daily_trades_cap",
                group="kill_switches",
                name="KS-4 — Anomalia de Frequência (teto 24h)",
                status="UNKNOWN",
                measured="N/A (banco sem histórico operacional)",
                threshold=f"< {max_daily_trades_cap} entradas em qualquer janela móvel de 24h",
                details="Sem ciclos em cycle_health nem trades registrados.",
            )
        )
    else:
        results.append(
            CriterionResult(
                id="kill_switches.ks4_daily_trades_cap",
                group="kill_switches",
                name="KS-4 — Anomalia de Frequência (teto 24h)",
                status="PASS",
                measured=f"pico 24h={max_24h} (live={max_24h_live}, shadow={max_24h_shadow})",
                threshold=f"< {max_daily_trades_cap} entradas em qualquer janela móvel de 24h",
                details=(
                    "Exclui ENTRY_REJECTED em trades (alinhado a src/main.py:298) e inclui "
                    "shadow_trades."
                ),
            )
        )

    return results


def generate_gate_report(
    db_path: Path,
    *,
    log_file: Optional[Path] = None,
    restore_drill_report: Optional[Path] = None,
    gate6a_mode: str = "LIVE_CHAMELEON_PHASE_1",
    phase1_env: str = "PRODUCTION",
    initial_equity_usdt: Optional[Decimal] = None,
    min_weekly_trades: float = GATE_6B_DEFAULT_MIN_WEEKLY_TRADES,
    max_weekly_trades: float = GATE_6B_DEFAULT_MAX_WEEKLY_TRADES,
    max_daily_trades_cap: int = KS4_DEFAULT_MAX_DAILY_TRADES_CAP,
) -> dict[str, Any]:
    """Gera o relatório estruturado completo em modo estritamente read-only."""
    conn = _open_sqlite_readonly(db_path)
    try:
        tables = _existing_tables(conn)
        schema_version: Optional[str] = None
        if "schema_meta" in tables:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            if row is not None:
                schema_version = str(row["value"])

        state = _load_system_state(conn, tables)

        g6a = _evaluate_gate_6a(
            conn,
            tables,
            gate6a_mode=gate6a_mode,
            log_file=log_file,
            restore_drill_report=restore_drill_report,
        )
        g6b = _evaluate_gate_6b(
            conn,
            tables,
            min_weekly_trades=min_weekly_trades,
            max_weekly_trades=max_weekly_trades,
        )
        p12 = _evaluate_phase_1_to_2(
            conn,
            tables,
            state,
            phase1_env=phase1_env,
            initial_equity_usdt=initial_equity_usdt,
        )
        ks = _evaluate_kill_switches(
            conn,
            tables,
            state,
            phase1_env=phase1_env,
            initial_equity_usdt=initial_equity_usdt,
            max_daily_trades_cap=max_daily_trades_cap,
        )
    finally:
        conn.close()

    sections = {
        "gate_6a": {
            "status": _aggregate_status(g6a),
            "criteria": [asdict(c) for c in g6a],
        },
        "gate_6b": {
            "status": _aggregate_status(g6b),
            "criteria": [asdict(c) for c in g6b],
        },
        "phase_1_to_2": {
            "status": _aggregate_status(p12),
            "criteria": [asdict(c) for c in p12],
        },
        "kill_switches": {
            "status": _aggregate_status(ks),
            "criteria": [asdict(c) for c in ks],
        },
    }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "db_path": str(Path(db_path).resolve()),
        "read_only": True,
        "schema_version": schema_version,
        "inputs": {
            "log_file": str(log_file) if log_file is not None else None,
            "restore_drill_report": str(restore_drill_report) if restore_drill_report is not None else None,
            "gate6a_mode": gate6a_mode,
            "phase1_env": phase1_env,
            "initial_equity_usdt": str(initial_equity_usdt) if initial_equity_usdt is not None else None,
            "min_weekly_trades": min_weekly_trades,
            "max_weekly_trades": max_weekly_trades,
            "max_daily_trades_cap": max_daily_trades_cap,
        },
        "sections": sections,
    }


def format_report_table(report: dict[str, Any]) -> str:
    """Renderiza o relatório em tabela ASCII/UTF-8 legível."""
    lines: list[str] = [
        "====================================================================================================",
        "QUANTCRYPTO CHAMELEON — RELATÓRIO DE GATES & KILL-SWITCHES (SQLite READ-ONLY / ADR-002)",
        f"DB: {report['db_path']} (schema v{report.get('schema_version') or '?'}) | UTC: {report['generated_at_utc']}",
        "====================================================================================================",
    ]
    titles = {
        "gate_6a": "GATE 6A — Estabilidade de Engenharia (Testnet 7 dias)",
        "gate_6b": "GATE 6B — Estabilidade de Microestrutura (Shadow Live)",
        "phase_1_to_2": "TRANSIÇÃO FASE 1 -> FASE 2 (§4.3 — 5 Requisitos)",
        "kill_switches": "KILL-SWITCHES (§5.2 — Disjuntores de Parada Total)",
    }
    for key in ("gate_6a", "gate_6b", "phase_1_to_2", "kill_switches"):
        sec = report["sections"][key]
        lines.append("")
        lines.append(f"[{sec['status']:<7}] {titles[key]}")
        lines.append("-" * 100)
        lines.append(f"{'STATUS':<8} | {'CRITÉRIO':<44} | {'MEDIDO':<32} | {'LIMIAR'}")
        lines.append("-" * 100)
        for c in sec["criteria"]:
            lines.append(
                f"{c['status']:<8} | {c['name'][:44]:<44} | {c['measured'][:32]:<32} | {c['threshold']}"
            )
            lines.append(f"         -> {c['details']}")
    lines.append("====================================================================================================")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/gate_report.py",
        description=(
            "Avalia Gate 6A, Gate 6B, requisitos Fase 1 -> Fase 2 (§4.3) e Kill-Switches (§5.2) "
            "abrindo o SQLite estritamente em modo read-only (URI mode=ro)."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/quant.db"),
        help="Caminho para o arquivo SQLite (aberto exclusivamente com mode=ro).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Arquivo de logs do container/bot para checagem de exceções não tratadas (Gate 6A).",
    )
    parser.add_argument(
        "--restore-drill-report",
        type=Path,
        default=None,
        help="JSON gerado por tools/restore_drill.sh --report-json para validar restauração (Gate 6A).",
    )
    parser.add_argument(
        "--gate6a-mode",
        default="LIVE_CHAMELEON_PHASE_1",
        help="Valor de cycle_health.mode filtrado no Gate 6A (padrão: LIVE_CHAMELEON_PHASE_1).",
    )
    parser.add_argument(
        "--phase1-env",
        choices=("PRODUCTION", "TESTNET"),
        default="PRODUCTION",
        help="Valor de trades.environment avaliado na transição Fase 1 -> Fase 2 (padrão: PRODUCTION).",
    )
    parser.add_argument(
        "--initial-equity-usdt",
        type=str,
        default=None,
        help="Capital tático inicial em USDT para cálculo exato de MaxDD (se ausente em system_state).",
    )
    parser.add_argument(
        "--min-weekly-trades",
        type=float,
        default=GATE_6B_DEFAULT_MIN_WEEKLY_TRADES,
        help="Limite inferior da faixa semanal compatível com o Bloco 3 no Gate 6B.",
    )
    parser.add_argument(
        "--max-weekly-trades",
        type=float,
        default=GATE_6B_DEFAULT_MAX_WEEKLY_TRADES,
        help="Limite superior da faixa semanal compatível com o Bloco 3 no Gate 6B.",
    )
    parser.add_argument(
        "--max-daily-trades-cap",
        type=int,
        default=KS4_DEFAULT_MAX_DAILY_TRADES_CAP,
        help="Teto de ordens de entrada em 24h (padrão: 6, conforme config/strategies.yaml:18).",
    )
    parser.add_argument(
        "--format",
        choices=("both", "table", "json"),
        default="both",
        help="Formato da saída padrão: tabela + JSON (both), só tabela (table) ou só JSON (json).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Caminho opcional para gravar a saída JSON em arquivo.",
    )

    args = parser.parse_args(argv)
    init_eq: Optional[Decimal] = None
    if args.initial_equity_usdt is not None:
        init_eq = _safe_decimal(args.initial_equity_usdt)
        if init_eq is None or init_eq <= Decimal("0"):
            print("ERROR: --initial-equity-usdt deve ser um número positivo.", file=sys.stderr)
            return 2

    try:
        report = generate_gate_report(
            args.db,
            log_file=args.log_file,
            restore_drill_report=args.restore_drill_report,
            gate6a_mode=args.gate6a_mode,
            phase1_env=args.phase1_env,
            initial_equity_usdt=init_eq,
            min_weekly_trades=args.min_weekly_trades,
            max_weekly_trades=args.max_weekly_trades,
            max_daily_trades_cap=args.max_daily_trades_cap,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ERROR: falha ao abrir SQLite em modo somente-leitura: {exc}", file=sys.stderr)
        return 1

    json_str = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json_str + "\n", encoding="utf-8")

    if args.format in ("both", "table"):
        print(format_report_table(report))
    if args.format == "both":
        print("\n--- JSON ---")
        print(json_str)
    elif args.format == "json":
        print(json_str)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
