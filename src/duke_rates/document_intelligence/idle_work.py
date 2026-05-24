"""Idle-work registry for the autonomous loop.

When the main corrective-action cycle has no actionable work
(every category exhausted or in cooldown, no acquisition leads),
the loop falls through to one of these productive LLM/maintenance
tasks rather than stopping early.

Each idle action is a self-contained CLI command that's safe to
run unattended with its own internal `--limit` discipline. They
don't draw from `historical_reprocess_queue` and don't compete
with the main drain step.

Selection is yield-EMA biased (same math as category routing):
actions that historically produced more charges per run float to
the top. First-time actions get a neutral baseline.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class IdleAction:
    """A productive LLM/maintenance command for the idle-work pool."""
    name: str
    cli_command: str  # space-separated, e.g. "doc-intel run-overnight"
    args: list[str] = field(default_factory=list)
    timeout_s: int = 1800
    description: str = ""
    # Some idle actions only make sense when prior LLM work has
    # staged proposals. ``requires_pool`` is an optional SQL query
    # (or None) that returns 0 when there's nothing to do. The
    # loop will skip the action when this returns 0.
    requires_pool_sql: str | None = None


# The default registry. Tuned for the NCUC corpus's current state.
# Order doesn't matter — selection is by yield EMA.
IDLE_REGISTRY: list[IdleAction] = [
    IdleAction(
        name="parser-improvement-overnight",
        cli_command="run-overnight-parse-improvement-nc",
        args=["--limit", "50"],
        timeout_s=1800,
        description=(
            "LLM-suggests parser regex fixes from failed extractions. "
            "Doesn't directly add charges but improves future cycles."
        ),
    ),
    IdleAction(
        name="llm-promotion-overnight",
        cli_command="run-llm-promotion-overnight-nc",
        args=["--limit", "50"],
        timeout_s=1800,
        description=(
            "Promotes already-validated LLM proposals to tariff_charges. "
            "Direct charge growth from staged work."
        ),
        # No-op when no validated proposals exist
        requires_pool_sql=(
            "SELECT COUNT(*) FROM llm_charge_proposals "
            "WHERE validation_status='accepted' AND promoted_at IS NULL"
        ),
    ),
    IdleAction(
        name="llm-propose-charges",
        cli_command="propose-llm-charge-promotions-nc",
        args=["--limit", "100"],
        timeout_s=1800,
        description=(
            "Generates LLM charge proposals from borderline extractions. "
            "Feeds the llm-promotion-overnight pool."
        ),
    ),
    IdleAction(
        name="apply-llm-row-repairs",
        cli_command="apply-deterministic-llm-row-repairs-nc",
        args=["--execute", "--limit", "100"],
        timeout_s=900,
        description=(
            "Auto-fixes deterministic LLM row issues (unit_evidence, "
            "row_reclassification, lighting_table_repair)."
        ),
    ),
    IdleAction(
        name="doc-intel-overnight",
        cli_command="doc-intel run-overnight",
        args=["--limit", "50"],
        timeout_s=1800,
        description=(
            "Classification + embedding maintenance pass. Improves "
            "doc routing for the next cycle's corrective actions."
        ),
    ),
]


def _has_pool_work(database_path: str, sql: str | None) -> bool:
    """Return True if the idle action's prerequisite pool is non-empty.

    None / empty SQL -> always considered ready. SQL errors -> log
    and treat as ready (the action's own dry-run path will handle
    the empty case gracefully).
    """
    if not sql:
        return True
    from duke_rates.db.sqlite import connect
    try:
        conn = connect(database_path)
        try:
            row = conn.execute(sql).fetchone()
            count = int(row[0]) if row and row[0] is not None else 0
            return count > 0
        finally:
            conn.close()
    except Exception:
        logger.debug("requires_pool_sql failed; treating as ready", exc_info=True)
        return True


def select_idle_action(
    *,
    database_path: str,
    idle_yield: dict[str, float],
    consecutive_skipped: dict[str, int] | None = None,
    registry: list[IdleAction] | None = None,
) -> IdleAction | None:
    """Pick the next idle action to run, biased by yield EMA.

    Actions whose ``requires_pool_sql`` returns 0 are skipped.
    Within the eligible set, the action with the highest yield EMA
    wins (ties broken by name for determinism). Actions with no
    yield history get a small positive baseline so they're tried
    at least once before EMA establishes the ordering.

    ``consecutive_skipped`` (when provided) is incremented for
    actions skipped this cycle — useful for round-robin behavior
    when all yields are zero. Currently not used for selection but
    persisted for telemetry.
    """
    reg = registry if registry is not None else IDLE_REGISTRY
    skipped = consecutive_skipped or {}
    eligible: list[tuple[float, str, IdleAction]] = []
    BASELINE = 5.0  # untried actions get a small boost so we try them once

    for action in reg:
        if not _has_pool_work(database_path, action.requires_pool_sql):
            continue
        yield_score = idle_yield.get(action.name)
        if yield_score is None or yield_score == 0:
            # Untried or zero-yield: use baseline minus how many times we've
            # skipped this action recently (encourages round-robin among
            # zero-yield options).
            score = BASELINE - 0.5 * skipped.get(action.name, 0)
        else:
            score = yield_score
        eligible.append((score, action.name, action))

    if not eligible:
        return None
    # Highest score first; ties broken by name for stable order
    eligible.sort(key=lambda t: (-t[0], t[1]))
    return eligible[0][2]


def run_idle_action(
    action: IdleAction,
    *,
    dry_run: bool,
    heartbeat: Any = None,
) -> dict[str, Any]:
    """Run a single idle action and return a structured result.

    Streams stderr through ``heartbeat`` (the loop's _heartbeat
    function) when provided, mirroring the drain step's
    observability. Captures stdout silently for downstream
    effective-count parsing.
    """
    cmd = action.cli_command
    args = list(action.args)
    full_cmd = [sys.executable, "-m", "duke_rates", *cmd.split(), *args]

    if dry_run:
        return {
            "name": action.name,
            "command": cmd,
            "args": args,
            "return_code": None,
            "success": False,
            "skipped_dry_run": True,
            "duration_ms": 0,
            "effective_count": None,
        }

    if heartbeat:
        heartbeat(f"  [IDLE] {action.name}: {cmd} {' '.join(args)} (deadline {action.timeout_s}s)...")

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=action.timeout_s,
            check=False,
        )
        from duke_rates.document_intelligence.action_registry import _parse_effective_count
        effective_count = _parse_effective_count(proc.stdout or "")
        result: dict[str, Any] = {
            "name": action.name,
            "command": cmd,
            "args": args,
            "return_code": proc.returncode,
            "success": proc.returncode == 0,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "effective_count": effective_count,
        }
        if proc.returncode != 0:
            result["stderr_tail"] = (proc.stderr or "")[-300:]
        if heartbeat:
            tag = "OK" if proc.returncode == 0 else f"FAIL rc={proc.returncode}"
            ec = f", moved={effective_count}" if effective_count is not None else ""
            heartbeat(f"        [IDLE] {action.name} {tag} in {(time.perf_counter() - t0):.1f}s{ec}")
        return result
    except subprocess.TimeoutExpired:
        if heartbeat:
            heartbeat(f"        [IDLE] {action.name} TIMEOUT at {action.timeout_s}s")
        return {
            "name": action.name,
            "command": cmd,
            "args": args,
            "return_code": None,
            "success": False,
            "timed_out": True,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "effective_count": None,
        }
    except Exception as exc:
        if heartbeat:
            heartbeat(f"        [IDLE] {action.name} ERROR: {exc}")
        return {
            "name": action.name,
            "command": cmd,
            "args": args,
            "return_code": None,
            "success": False,
            "error": str(exc),
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "effective_count": None,
        }
