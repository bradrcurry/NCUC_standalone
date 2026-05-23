"""
Action registry and decision layer for the autonomous loop controller.

Maps database intelligence findings to corrective CLI commands with
estimated impact, risk assessment, and measurement strategies.

Does NOT execute commands — callers decide whether to invoke subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------


@dataclass
class CorrectiveAction:
    finding_category: str
    label: str
    cli_command: str
    args: list[str] = field(default_factory=list)
    estimated_impact: str = ""
    risk: str = "low"  # low | medium | high
    measurement: str = ""
    max_per_cycle: int = 100  # safety cap
    requires_execute: bool = True  # all commands default to dry-run
    supports_limit_flag: bool = False  # accepts ``--limit N`` to drain in batches

    # How success of this action shows up in summary_counts:
    # "drain"     -> the action's OWN category count should DECREASE
    #                (dedup removes duplicate_documents groups; bootstrap
    #                creates versions and so reduces family_lineage_gaps).
    # "redirect"  -> success doesn't change this action's own category;
    #                it moves work to a different category. The action's
    #                effect is measurable downstream via outcome metrics
    #                (charges, evidence coverage) once the queue is drained.
    #                Examples: enqueue-* commands populate the reprocess
    #                queue; the queue drainer eventually moves the metric.
    # "neutral"   -> read-only commands.
    #
    # The autonomous loop uses this to decide whether a successful run
    # that produced no per-category improvement counts as "stuck".
    delta_semantics: str = "drain"

    def build_args(self, *, batch_limit: int | None = None) -> list[str]:
        """Return the args to invoke this action with.

        When ``batch_limit`` is provided AND the action supports
        ``--limit``, replace the registry-default limit with the
        caller-supplied value (clamped at ``max_per_cycle``). This
        lets the loop drain large backlogs in bigger chunks instead
        of always-50 per cycle.
        """
        if not self.supports_limit_flag or batch_limit is None:
            return list(self.args)

        cap = max(1, min(batch_limit, self.max_per_cycle))
        out: list[str] = []
        skip_next = False
        replaced = False
        for tok in self.args:
            if skip_next:
                skip_next = False
                continue
            if tok == "--limit":
                out.extend(["--limit", str(cap)])
                skip_next = True
                replaced = True
            else:
                out.append(tok)
        if not replaced:
            out.extend(["--limit", str(cap)])
        return out


ACTION_REGISTRY: dict[str, CorrectiveAction] = {
    "duplicate_documents": CorrectiveAction(
        finding_category="duplicate_documents",
        label="Duplicate Documents",
        cli_command="lineage deduplicate-documents-nc",
        args=["--execute", "--limit", "50"],
        estimated_impact="Removes duplicate rows, consolidates charges under one survivor per hash group.",
        risk="low",
        measurement="Re-run find_duplicate_documents() and compare count.",
        max_per_cycle=500,
        supports_limit_flag=True,
    ),
    "missing_versions": CorrectiveAction(
        finding_category="missing_versions",
        label="Missing Versions",
        cli_command="bootstrap-missing-versions-nc",
        # bootstrap-missing-versions-nc has NO --execute flag; it
        # mutates by default and exposes only --dry-run for preview.
        # Passing --execute makes argparse exit 2.
        args=["--limit", "100"],
        estimated_impact="Creates new tariff_versions by importing missing documents for version-gap families.",
        risk="medium",
        measurement="Re-run find_missing_versions() and compare gap count.",
        max_per_cycle=500,
        supports_limit_flag=True,
    ),
    "stale_artifacts": CorrectiveAction(
        finding_category="stale_artifacts",
        label="Stale Artifacts",
        cli_command="reprocess enqueue-stale-nc",
        args=["--execute", "--limit", "100"],
        estimated_impact="Enqueues documents with missing/outdated artifacts for reprocessing.",
        risk="low",
        measurement="Re-run find_stale_artifacts() and compare stale count.",
        max_per_cycle=1000,
        supports_limit_flag=True,
        # enqueue-* doesn't move its own category in summary_counts.
        # The reprocess drainer (separate process) is what eventually
        # moves the outcome metrics. Don't penalize this action when
        # the category doesn't shrink in the same cycle.
        delta_semantics="redirect",
    ),
    "low_quality_parses": CorrectiveAction(
        finding_category="low_quality_parses",
        label="Low Quality Parses",
        cli_command="reprocess enqueue-parser-improvement-nc",
        args=["--execute", "--limit", "50"],
        estimated_impact="Re-enqueues documents where extraction produced zero or few charges.",
        risk="low",
        measurement="Re-run find_low_quality_parses() and compare low-quality count.",
        max_per_cycle=500,
        supports_limit_flag=True,
        delta_semantics="redirect",  # see note on stale_artifacts
    ),
    "unknown_documents": CorrectiveAction(
        finding_category="unknown_documents",
        label="Unknown Documents",
        cli_command="doc-intel adjudicate-classifications",
        # doc-intel adjudicate-classifications has NO --execute flag; it
        # operates in live mode by default (only --dry-run inverts it).
        args=["--limit", "50"],
        estimated_impact="Re-runs classification on UNKNOWN documents to assign family and type.",
        risk="medium",
        measurement="Re-run find_unknown_documents() and compare unknown count.",
        max_per_cycle=200,
        supports_limit_flag=True,
    ),
    "family_lineage_gaps": CorrectiveAction(
        finding_category="family_lineage_gaps",
        label="Family Lineage Gaps",
        cli_command="bootstrap-missing-versions-nc",
        # See note on missing_versions above; no --execute flag here.
        args=["--limit", "100"],
        estimated_impact="Fills version-timeline gaps by importing missing documents.",
        risk="medium",
        measurement="Re-run find_family_lineage_gaps() and compare gap count.",
        max_per_cycle=500,
        supports_limit_flag=True,
    ),
    "docket_coverage": CorrectiveAction(
        finding_category="docket_coverage",
        label="Docket Coverage",
        cli_command="recommend-missing-dockets-nc",
        args=["--json"],
        estimated_impact="Identifies highest-value dockets to fetch next. Read-only — no writes.",
        risk="low",
        measurement="Re-run find_docket_coverage_summary() and compare unique docket count.",
        max_per_cycle=0,  # read-only
        requires_execute=False,
        delta_semantics="neutral",  # read-only: doesn't change state
    ),
}

# Severity-to-threshold mapping: only act when finding count >= threshold
SEVERITY_THRESHOLDS: dict[str, int] = {
    "critical": 1,  # always act
    "high": 5,
    "medium": 10,
    "low": 25,
}


# ---------------------------------------------------------------------------
# Decision layer
# ---------------------------------------------------------------------------


@dataclass
class RecommendedAction:
    action: CorrectiveAction
    finding_count: int
    severity: str  # highest severity in that category
    priority: int  # lower = more urgent
    rationale: str = ""


def decide_actions(
    report: dict[str, Any],
    *,
    action_registry: dict[str, CorrectiveAction] | None = None,
    max_actions: int = 3,
    cooldown_categories: set[str] | None = None,
) -> list[RecommendedAction]:
    """Map a database intelligence report to a ranked list of corrective actions.

    Parameters
    ----------
    report:
        Output of ``build_database_intelligence_report()``.
    action_registry:
        Registry of corrective actions. Defaults to ``ACTION_REGISTRY``.
    max_actions:
        Maximum number of actions to recommend in one cycle.
    cooldown_categories:
        Categories the loop has decided to skip this cycle (because
        prior runs against the same backlog produced no measurable
        improvement). The action is still listed in the returned set
        with priority demoted, so the caller can see it was considered.

    Returns
    -------
    List of ``RecommendedAction`` sorted by priority (lowest first).
    """
    registry = action_registry or ACTION_REGISTRY
    cooldown = cooldown_categories or set()
    summary_counts: dict[str, int] = report.get("summary_counts", {})
    recommendations: list[RecommendedAction] = []

    for category, action in registry.items():
        count = summary_counts.get(category, 0)
        if count == 0:
            continue

        # Determine highest severity for this category
        section = report.get(category, {})
        severity = _highest_severity(section)

        # Check threshold
        threshold = SEVERITY_THRESHOLDS.get(severity, 10)
        if count < threshold:
            continue

        # Categories on cooldown are silently dropped. The loop logs
        # the cooldown set separately so the operator can see why.
        if category in cooldown:
            continue

        priority = _compute_priority(count, severity)
        recommendations.append(
            RecommendedAction(
                action=action,
                finding_count=count,
                severity=severity,
                priority=priority,
                rationale=(
                    f"{count} finding(s) at {severity} severity "
                    f"(threshold: {threshold}). "
                    f"Action: {action.cli_command}."
                ),
            )
        )

    recommendations.sort(key=lambda r: r.priority)

    # Coalesce recommendations that would invoke the same CLI command
    # with the same args. ``missing_versions`` and ``family_lineage_gaps``
    # both map to ``bootstrap-missing-versions-nc``, and previously the
    # loop would run that command twice in a row against the same
    # backlog — wasted work, plus the second run looks like "stuck" to
    # the cooldown logic. Keep the first (best-priority) entry; merge
    # subsequent same-command entries into its rationale so we still
    # report that the duplicate category was considered.
    coalesced: list[RecommendedAction] = []
    seen: dict[tuple[str, tuple[str, ...]], RecommendedAction] = {}
    for r in recommendations:
        key = (r.action.cli_command, tuple(r.action.args))
        if key in seen:
            primary = seen[key]
            primary.rationale += (
                f" Also covers {r.action.finding_category} "
                f"({r.finding_count} findings)."
            )
            primary.finding_count = max(primary.finding_count, r.finding_count)
        else:
            seen[key] = r
            coalesced.append(r)

    return coalesced[:max_actions]


def _highest_severity(section: dict[str, Any]) -> str:
    rows = section.get("rows", [])
    severities = [r.get("severity", "low") for r in rows if isinstance(r, dict)]
    for s in ("critical", "high", "medium", "low"):
        if s in severities:
            return s
    return "low"


def _compute_priority(count: int, severity: str) -> int:
    """Lower priority value = run sooner.

    Old formula was ``severity_weight * 1000 - min(count, 999)``, which
    made a single critical finding outrank 999 low-severity findings —
    even when the low-severity backlog was 3,400+. For autonomous
    drain workloads we want backlog size to matter more than severity:
    severity is a tie-breaker among similar-sized backlogs, not the
    primary sort key.

    New formula uses log-scaled count so going from 50 -> 5000 findings
    moves priority by ~6 (one severity tier), but going from 5,000 ->
    50,000 moves it by another ~2.3. Severity contributes 100 per tier,
    log-count contributes ~33 per decade.
    """
    import math

    severity_weight = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sev = severity_weight.get(severity, 3)
    # log10(1)=0, log10(10)=1, log10(100)=2, log10(1000)=3, log10(10000)=4
    log_count = math.log10(max(1, count))
    # Scale: severity adds 100 per tier; log_count subtracts ~33 per decade.
    # A "low" with 3,400 findings (log10≈3.5) -> 3*100 - 3.5*33 ≈ 184.5
    # A "medium" with 11 findings (log10≈1.0) -> 2*100 - 1.0*33 ≈ 167
    # so medium-tiny still beats low-huge slightly, but a low at 50,000
    # (log10≈4.7) ≈ 145 beats medium-tiny.
    return int(round(sev * 100 - log_count * 33))


# ---------------------------------------------------------------------------
# Cycle runner (safe by default)
# ---------------------------------------------------------------------------


@dataclass
class ActionOutcome:
    """Structured record of one corrective-action invocation."""
    category: str
    cli_command: str
    args: list[str]
    finding_count: int  # backlog size at decision time
    return_code: int | None  # None when not executed (dry-run / skipped)
    success: bool  # return_code == 0 and not skipped
    duration_ms: int
    stderr_tail: str = ""  # last ~500 chars of stderr on failure
    error: str | None = None  # python-side exception (timeout etc.)
    delta_semantics: str = "drain"  # see CorrectiveAction.delta_semantics


@dataclass
class CycleResult:
    actions_taken: list[str]
    actions_skipped: list[str]
    before_counts: dict[str, int]
    after_counts: dict[str, int] | None = None
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0
    outcomes: list[ActionOutcome] = field(default_factory=list)


def run_cycle(
    database_path: str,
    *,
    limit: int = 50,
    max_actions: int = 2,
    dry_run: bool = True,
    action_batch_limit: int | None = None,
    cooldown_categories: set[str] | None = None,
) -> CycleResult:
    """Run one autonomous loop cycle: detect -> decide -> act -> measure.

    When *dry_run* is True (the default), decisions are printed but no
    corrective commands are executed. This is the safe default for
    unattended operation.

    *action_batch_limit* (when provided) is passed to each action's
    ``--limit`` flag, capped by the action's ``max_per_cycle``. This
    lets the loop drain large backlogs in larger chunks per cycle
    instead of always-50. ``None`` keeps the registry-default args.
    """
    import subprocess
    import sys
    import time

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.database_reports import (
        build_database_intelligence_report,
    )

    t0 = time.perf_counter()

    # 1. DETECT
    conn = connect(database_path)
    try:
        report = build_database_intelligence_report(conn, limit=limit)
    finally:
        conn.close()

    before_counts = dict(report.get("summary_counts", {}))

    # 2. DECIDE
    recommendations = decide_actions(
        report,
        max_actions=max_actions,
        cooldown_categories=cooldown_categories,
    )

    # 3. ACT (or preview)
    actions_taken: list[str] = []
    actions_skipped: list[str] = []
    errors: list[str] = []
    outcomes: list[ActionOutcome] = []

    for rec in recommendations:
        cmd = rec.action.cli_command
        args = rec.action.build_args(batch_limit=action_batch_limit)

        if dry_run or not rec.action.requires_execute:
            actions_skipped.append(
                f"{cmd} {' '.join(args)} ({rec.finding_count} findings, {rec.severity})"
            )
            outcomes.append(ActionOutcome(
                category=rec.action.finding_category,
                cli_command=cmd,
                args=args,
                finding_count=rec.finding_count,
                return_code=None,
                success=False,
                duration_ms=0,
                delta_semantics=rec.action.delta_semantics,
            ))
            continue

        action_t0 = time.perf_counter()
        outcome = ActionOutcome(
            category=rec.action.finding_category,
            cli_command=cmd,
            args=args,
            finding_count=rec.finding_count,
            return_code=None,
            success=False,
            duration_ms=0,
            delta_semantics=rec.action.delta_semantics,
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "duke_rates", *cmd.split(), *args],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            outcome.return_code = proc.returncode
            outcome.success = proc.returncode == 0
            if proc.returncode != 0:
                outcome.stderr_tail = (proc.stderr or "")[-500:]
                errors.append(
                    f"{cmd} exited {proc.returncode}: {outcome.stderr_tail[:200]}"
                )
            actions_taken.append(
                f"{cmd} {' '.join(args)} "
                f"(targeted {rec.finding_count} findings, rc={proc.returncode})"
            )
        except subprocess.TimeoutExpired:
            outcome.error = "timeout (600s)"
            errors.append(f"{cmd}: timeout")
        except Exception as exc:
            outcome.error = str(exc)
            errors.append(f"{cmd}: {exc}")
        finally:
            outcome.duration_ms = int((time.perf_counter() - action_t0) * 1000)
            outcomes.append(outcome)

    # 4. MEASURE
    after_counts: dict[str, int] | None = None
    if actions_taken:
        conn = connect(database_path)
        try:
            after_report = build_database_intelligence_report(conn, limit=limit)
            after_counts = dict(after_report.get("summary_counts", {}))
        finally:
            conn.close()

    duration_ms = int((time.perf_counter() - t0) * 1000)

    return CycleResult(
        actions_taken=actions_taken,
        actions_skipped=actions_skipped,
        before_counts=before_counts,
        after_counts=after_counts,
        errors=errors,
        duration_ms=duration_ms,
        outcomes=outcomes,
    )
