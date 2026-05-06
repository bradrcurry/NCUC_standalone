"""
Overnight parse-improvement loop (Phase 5.6).

Integrates parse failure diagnosis, regex suggestion generation, deterministic
validation, and schema-guided extraction into a single resumable batch that can
run unattended overnight. Follows the same safety pattern as Phase 5.5's
``run-overnight-doc-intelligence-nc``.

Safety guarantees:
- No destructive overwrites — only INSERTs new rows
- Bounded by wall-clock cap even with unlimited --max-documents
- Resumable — skips completed (subject, stage, model, prompt_version) tuples
- Stops cleanly on: max docs, max runtime, consecutive failures, health probe
  degradation, SIGINT/SIGTERM
"""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.ollama_orchestrator import (
    OllamaOrchestrator,
    OllamaRunResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task kinds
# ---------------------------------------------------------------------------

VALID_TASK_KINDS = frozenset({"diagnose", "suggest", "validate", "shadow_test", "profile_consensus", "extract"})

# ---------------------------------------------------------------------------
# Report schema
# ---------------------------------------------------------------------------


@dataclass
class ParseImprovementReport:
    """Morning report produced by the overnight parse-improvement loop."""

    run_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    stop_reason: str = "completed"
    documents_analyzed: int = 0
    parse_failures_by_type: dict[str, int] = field(default_factory=dict)
    regex_suggestions_created: int = 0
    normalization_suggestions_created: int = 0
    schema_extractions_attempted: int = 0
    schema_extractions_validated: int = 0
    human_review_candidates: list[dict[str, Any]] = field(default_factory=list)
    highest_value_next_actions: list[dict[str, Any]] = field(default_factory=list)
    task_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    roles_used: dict[str, str] = field(default_factory=dict)
    runtime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "stop_reason": self.stop_reason,
            "documents_analyzed": self.documents_analyzed,
            "parse_failures_by_type": self.parse_failures_by_type,
            "regex_suggestions_created": self.regex_suggestions_created,
            "normalization_suggestions_created": self.normalization_suggestions_created,
            "schema_extractions_attempted": self.schema_extractions_attempted,
            "schema_extractions_validated": self.schema_extractions_validated,
            "human_review_candidates": self.human_review_candidates,
            "highest_value_next_actions": self.highest_value_next_actions,
            "task_stats": self.task_stats,
            "roles_used": self.roles_used,
            "runtime_seconds": self.runtime_seconds,
            "idle": self.is_idle(),
        }

    def is_idle(self) -> bool:
        """True if this run did no productive work (no docs, no suggestions, no validations)."""
        if self.documents_analyzed > 0:
            return False
        for stats in self.task_stats.values():
            if stats.get("ok", 0) > 0 or stats.get("fail", 0) > 0:
                return False
        return True


# ---------------------------------------------------------------------------
# Loop orchestrator
# ---------------------------------------------------------------------------


class ParseImprovementLoop:
    """Overnight parse-improvement loop.

    Parameters
    ----------
    orchestrator : OllamaOrchestrator
        Phase 2.5 orchestrator with DB persistence.
    db_path : Path
        Path to the SQLite database.
    report_dir : Path
        Directory for end-of-run JSON reports.
    """

    def __init__(
        self,
        orchestrator: OllamaOrchestrator,
        db_path: Path,
        report_dir: Path | None = None,
    ) -> None:
        self._orch = orchestrator
        self._db_path = db_path
        self._report_dir = report_dir or Path("docs/reports/overnight_parse_improvement")

        # Lazy-initialized components
        self._diagnoser: Any = None
        self._suggestion_gen: Any = None
        self._validator: Any = None
        self._shadow: Any = None
        self._consensus: Any = None
        self._extractor: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        task_kinds: list[str] | None = None,
        max_documents: int = 0,
        max_runtime_minutes: int = 0,
        max_consecutive_failures: int = 5,
        profile: str | None = None,
        family: str | None = None,
        since: str | None = None,
        dry_run: bool = False,
        resume: bool = False,
        rediagnose_unknown: bool = False,
        limit: int = 25,
    ) -> ParseImprovementReport:
        """Run the parse-improvement loop.

        Returns a ``ParseImprovementReport`` with full statistics.
        """
        tasks = task_kinds or ["diagnose"]
        for t in tasks:
            if t not in VALID_TASK_KINDS:
                raise ValueError(f"Unknown task kind {t!r}. Valid: {sorted(VALID_TASK_KINDS)}")

        report = ParseImprovementReport(
            run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            started_at=datetime.now(timezone.utc).isoformat(),
            task_stats={t: {"ok": 0, "skip": 0, "fail": 0} for t in tasks},
        )

        # Lazy-init components
        self._init_components(tasks)

        # --- Dry run --- (before health probes — no LLM needed)
        if dry_run:
            return self._dry_run(
                tasks, profile, family, since, limit, report, rediagnose_unknown
            )

        # Health probes
        roles_needed: set[str] = set()
        if "diagnose" in tasks:
            roles_needed.add("parse_failure_triage")
        if "suggest" in tasks:
            roles_needed.add("regex_suggestion")
        if "extract" in tasks:
            roles_needed.add("structured_rate_extraction")
        # validate task doesn't need LLM

        for role in roles_needed:
            ok, err = self._orch.health_probe(role)
            if not ok:
                report.stop_reason = f"health_probe_failed:{role}"
                report.notes = err or "unknown error"
                return report
            cfg = self._orch.roles.get(role)
            if cfg:
                report.roles_used[role] = cfg.primary

        # --- Main loop ---
        start_time = time.monotonic()
        wall_deadline = (
            start_time + (max_runtime_minutes * 60)
            if max_runtime_minutes > 0
            else float("inf")
        )

        # Signal handling
        abort_flag = {"value": False}

        def _handle_signal(signum: int, frame: Any) -> None:
            abort_flag["value"] = True

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        consecutive_failures = 0
        total_processed = 0

        # Execute tasks in order
        for task in tasks:
            if abort_flag["value"]:
                report.stop_reason = "interrupted"
                break
            if time.monotonic() >= wall_deadline:
                report.stop_reason = "max_runtime"
                break
            if max_documents > 0 and total_processed >= max_documents:
                report.stop_reason = "max_documents"
                break
            if consecutive_failures >= max_consecutive_failures:
                report.stop_reason = "max_consecutive_failures"
                break

            remaining = max_documents - total_processed if max_documents > 0 else limit
            task_limit = min(remaining, limit) if max_documents > 0 else limit

            try:
                task_result = self._run_task(
                    task,
                    task_limit,
                    profile,
                    family,
                    since,
                    resume,
                    rediagnose_unknown,
                )
                report.task_stats[task] = task_result
                total_processed += task_result.get("ok", 0) + task_result.get("fail", 0)
                report.documents_analyzed = total_processed
            except Exception as exc:
                report.task_stats[task]["fail"] += 1
                logger.warning("Task %s failed: %s", task, exc, exc_info=True)

        report.completed_at = datetime.now(timezone.utc).isoformat()
        report.runtime_seconds = round(time.monotonic() - start_time, 1)

        # Collect summary statistics
        self._collect_summary_stats(report)

        # Write end-of-run report
        self._write_report(report)

        return report

    # ------------------------------------------------------------------
    # Internal — component init
    # ------------------------------------------------------------------

    def _init_components(self, tasks: list[str]) -> None:
        if "diagnose" in tasks and self._diagnoser is None:
            from duke_rates.document_intelligence.parse_diagnosis import ParseFailureDiagnoser
            self._diagnoser = ParseFailureDiagnoser(self._orch, self._db_path)

        if "suggest" in tasks and self._suggestion_gen is None:
            from duke_rates.document_intelligence.regex_suggestions import RegexSuggestionGenerator
            self._suggestion_gen = RegexSuggestionGenerator(self._orch, self._db_path)

        if "validate" in tasks and self._validator is None:
            from duke_rates.document_intelligence.regex_validation import RegexValidationHarness
            self._validator = RegexValidationHarness(self._db_path)

        if "shadow_test" in tasks and self._shadow is None:
            from duke_rates.document_intelligence.regex_shadow_test import RegexShadowHarness
            self._shadow = RegexShadowHarness(self._db_path)

        if "profile_consensus" in tasks and self._consensus is None:
            from duke_rates.document_intelligence.profile_consensus import ProfileConsensusEngine
            self._consensus = ProfileConsensusEngine(self._db_path)

        if "extract" in tasks and self._extractor is None:
            from duke_rates.document_intelligence.schema_extraction import SchemaGuidedExtractor
            self._extractor = SchemaGuidedExtractor(self._orch, self._db_path)

    # ------------------------------------------------------------------
    # Internal — task execution
    # ------------------------------------------------------------------

    def _run_task(
        self,
        task: str,
        limit: int,
        profile: str | None,
        family: str | None,
        since: str | None,
        resume: bool,
        rediagnose_unknown: bool,
    ) -> dict[str, int]:
        stats: dict[str, int] = {"ok": 0, "skip": 0, "fail": 0}

        if task == "diagnose":
            if rediagnose_unknown:
                candidates = self._diagnoser.select_rediagnosis_candidates(
                    limit=limit, profile=profile, family=family, since=since
                )
            else:
                candidates = self._diagnoser.select_candidates(
                    limit=limit, profile=profile, family=family, since=since
                )
            if resume and not rediagnose_unknown:
                # Filter out already-diagnosed (shouldn't happen with select_candidates,
                # but double-check for resume safety)
                candidates = [
                    c for c in candidates
                    if not self._is_already_done("parse_attempt", str(c.get("parse_attempt_id", 0)), "parse_diagnosis")
                ]
            for candidate in candidates:
                try:
                    result = self._diagnoser.diagnose(candidate)
                    if result.failure_type != "unknown":
                        stats["ok"] += 1
                    else:
                        stats["fail"] += 1
                except Exception:
                    stats["fail"] += 1

        elif task == "suggest":
            diagnosis_rows = self._suggestion_gen.select_diagnoses_for_suggestion(
                limit=limit, profile=profile, failure_type=None
            )
            for row in diagnosis_rows:
                try:
                    suggestion = self._suggestion_gen.generate_suggestion(row)
                    if suggestion:
                        stats["ok"] += 1
                    else:
                        stats["fail"] += 1
                except Exception:
                    stats["fail"] += 1

        elif task == "validate":
            if self._validator is None:
                stats["skip"] = limit
            else:
                pending = self._validator.select_pending_suggestions(limit=limit)
                stats["candidates"] = len(pending)
                for row in pending:
                    try:
                        self._validator.validate_suggestion(row)
                        stats["ok"] += 1
                    except Exception as exc:
                        logger.warning(
                            "validate_suggestion failed for id=%s: %s",
                            row.get("id"), exc, exc_info=True,
                        )
                        stats["fail"] += 1

        elif task == "shadow_test":
            if self._shadow is None:
                stats["skip"] = limit
            else:
                pending = self._shadow.select_pending_shadow_tests(limit=limit)
                stats["candidates"] = len(pending)
                for row in pending:
                    try:
                        self._shadow.shadow_test_suggestion(row)
                        stats["ok"] += 1
                    except Exception as exc:
                        logger.warning(
                            "shadow_test_suggestion failed for id=%s: %s",
                            row.get("id"), exc, exc_info=True,
                        )
                        stats["fail"] += 1

        elif task == "profile_consensus":
            if self._consensus is None:
                stats["skip"] = limit
            else:
                pending = self._consensus.select_pending_diagnoses(limit=limit)
                stats["candidates"] = len(pending)
                self._consensus._build_lookups()  # one-time per batch
                for row in pending:
                    try:
                        rec = self._consensus.recommend_for_diagnosis(row)
                        self._consensus._persist_recommendation(row.get("diagnosis_id"), rec)
                        if rec.status == "recommended":
                            stats["ok"] += 1
                        else:
                            stats["skip"] += 1
                    except Exception as exc:
                        logger.warning(
                            "profile_consensus failed for parse_attempt=%s: %s",
                            row.get("parse_attempt_id"), exc, exc_info=True,
                        )
                        stats["fail"] += 1

        elif task == "extract":
            candidates = self._extractor.select_extraction_candidates(
                limit=limit, profile=profile, family=family
            )
            for candidate in candidates:
                try:
                    extraction = self._extractor.extract_candidate(candidate)
                    if extraction and extraction.rate_rows:
                        stats["ok"] += 1
                    elif extraction:
                        stats["skip"] += 1  # extracted but empty
                    else:
                        stats["fail"] += 1
                except Exception:
                    stats["fail"] += 1

        return stats

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    def _is_already_done(
        self, subject_kind: str, subject_id: str, stage: str
    ) -> bool:
        """Check if a (subject, stage) tuple is already completed."""
        import sqlite3
        try:
            conn = sqlite3.connect(str(self._db_path))
            row = conn.execute(
                """
                SELECT id FROM ollama_model_runs
                WHERE subject_kind = ? AND subject_id = ? AND stage = ? AND status = 'ok'
                LIMIT 1
                """,
                (subject_kind, subject_id, stage),
            ).fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def _dry_run(
        self,
        tasks: list[str],
        profile: str | None,
        family: str | None,
        since: str | None,
        limit: int,
        report: ParseImprovementReport,
        rediagnose_unknown: bool = False,
    ) -> ParseImprovementReport:
        """Enumerate the work set without making any model calls."""
        report.stop_reason = "dry_run"

        self._init_components(tasks)

        for task in tasks:
            if task == "diagnose":
                if rediagnose_unknown:
                    candidates = self._diagnoser.select_rediagnosis_candidates(
                        limit=limit, profile=profile, family=family, since=since
                    )
                else:
                    candidates = self._diagnoser.select_candidates(
                        limit=limit, profile=profile, family=family, since=since
                    )
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": len(candidates)}
                report.documents_analyzed = len(candidates)
            elif task == "suggest":
                rows = self._suggestion_gen.select_diagnoses_for_suggestion(
                    limit=limit, profile=profile
                )
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": len(rows)}
            elif task == "validate":
                if self._validator:
                    pending = self._validator.select_pending_suggestions(limit=limit)
                    report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": len(pending)}
                else:
                    report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": 0}
            elif task == "extract":
                candidates = self._extractor.select_extraction_candidates(
                    limit=limit, profile=profile, family=family
                )
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": len(candidates)}

        report.completed_at = datetime.now(timezone.utc).isoformat()
        return report

    def _collect_summary_stats(self, report: ParseImprovementReport) -> None:
        """Collect aggregate statistics from the database for the report."""
        import sqlite3
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row

            # Count parse failures by type
            rows = conn.execute(
                """
                SELECT failure_type, COUNT(*) as cnt
                FROM llm_parse_diagnostics
                GROUP BY failure_type
                ORDER BY cnt DESC
                """
            ).fetchall()
            report.parse_failures_by_type = {r["failure_type"]: r["cnt"] for r in rows}

            # Count suggestions by type
            regex_count = conn.execute(
                "SELECT COUNT(*) FROM llm_regex_suggestions WHERE suggestion_type = 'regex_candidate'"
            ).fetchone()
            norm_count = conn.execute(
                "SELECT COUNT(*) FROM llm_regex_suggestions WHERE suggestion_type = 'normalization_rule'"
            ).fetchone()
            report.regex_suggestions_created = regex_count[0] if regex_count else 0
            report.normalization_suggestions_created = norm_count[0] if norm_count else 0

            # Count extractions
            ext_total = conn.execute(
                "SELECT COUNT(*) FROM llm_candidate_rate_extractions"
            ).fetchone()
            ext_validated = conn.execute(
                "SELECT COUNT(*) FROM llm_candidate_rate_extractions WHERE status = 'validated'"
            ).fetchone()
            report.schema_extractions_attempted = ext_total[0] if ext_total else 0
            report.schema_extractions_validated = ext_validated[0] if ext_validated else 0

            conn.close()
        except Exception:
            pass

    def _write_report(self, report: ParseImprovementReport) -> None:
        """Write the end-of-run JSON report.

        Idle runs (no docs analyzed, no task work) are written under ``idle/``
        to keep the main report directory readable.
        """
        try:
            target_dir = self._report_dir / "idle" if report.is_idle() else self._report_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            timestamp = report.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filepath = target_dir / f"{timestamp}.json"
            filepath.write_text(
                json.dumps(report.to_dict(), indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to write parse-improvement report", exc_info=True)
