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

VALID_TASK_KINDS = frozenset({
    "diagnose", "suggest", "validate", "revalidate",
    "shadow_test", "profile_consensus", "extract",
    # Phase 6E: staged iterative extraction (filter → find-lines → classify)
    "extract_staged",
    "populate_identity", "populate_routing_tier", "bind_tier1",
    "generate_per_doc_rules", "detect_rule_promotions",
    # Phase 6F: extraction-grounded rule generation
    "generate_grounded_rules",
    # Phase 6 sub-document sections
    "populate_sections", "analyze_document_structure",
})

# Per-stage minimum runtime budget in seconds. When the wall-clock budget
# remaining is below a stage's minimum, the stage is skipped (with a stat
# entry "skipped_insufficient_budget") so the loop doesn't burn the whole
# remaining budget on one expensive stage and leave deterministic ones
# unable to run. Tuned for typical Ollama call latency (20–60s per call).
STAGE_MIN_BUDGET_SECONDS: dict[str, int] = {
    # LLM-bound stages need enough time for a typical batch (limit × ~30s).
    "diagnose":          90,   # one diagnose call is ~30s; allow at least 3
    "suggest":           90,
    "extract":           90,
    "extract_staged":   180,   # 1 find-lines call + N classify calls per doc
    # Deterministic stages are fast.
    "validate":          15,
    "revalidate":        15,
    "shadow_test":       30,   # corpus sweep can take seconds per suggestion
    "profile_consensus": 15,
    "populate_identity": 15,
    "populate_routing_tier": 15,
    "bind_tier1": 15,
    "generate_per_doc_rules": 90,
    "detect_rule_promotions": 15,
    "generate_grounded_rules": 60,
    # Phase 6 sub-document sections
    "populate_sections": 15,
    "analyze_document_structure": 120,
}

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
    document_specific_rules_by_status: dict[str, int] = field(default_factory=dict)
    promotion_candidates_by_status: dict[str, int] = field(default_factory=dict)
    human_review_candidates: list[dict[str, Any]] = field(default_factory=list)
    highest_value_next_actions: list[dict[str, Any]] = field(default_factory=list)
    task_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    roles_used: dict[str, str] = field(default_factory=dict)
    runtime_seconds: float = 0.0
    # Phase 6 section statistics
    sections_total: int = 0
    sections_by_type: dict[str, int] = field(default_factory=dict)

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
            "document_specific_rules_by_status": self.document_specific_rules_by_status,
            "promotion_candidates_by_status": self.promotion_candidates_by_status,
            "human_review_candidates": self.human_review_candidates,
            "highest_value_next_actions": self.highest_value_next_actions,
            "task_stats": self.task_stats,
            "roles_used": self.roles_used,
            "runtime_seconds": self.runtime_seconds,
            "sections_total": self.sections_total,
            "sections_by_type": self.sections_by_type,
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
        self._identity_agg: Any = None
        self._tier_agg: Any = None
        self._tier1_binder: Any = None
        self._per_doc_generator: Any = None
        self._grounded_generator: Any = None
        self._promotion_detector: Any = None
        self._sections_populator: Any = None
        self._structure_analyst: Any = None

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
        self_consistency_votes: int = 1,
        auto_rediagnose_unknown: bool = False,
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

        # Lazy-init components (self-consistency setting affects diagnoser only)
        self._sc_votes = max(1, int(self_consistency_votes))
        self._auto_rediagnose = bool(auto_rediagnose_unknown)
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
        if "extract_staged" in tasks:
            roles_needed.add("structured_rate_extraction")
            roles_needed.add("structured_rate_classify")
        if "generate_per_doc_rules" in tasks:
            roles_needed.add("regex_suggestion")
        if "generate_grounded_rules" in tasks:
            roles_needed.add("regex_suggestion")
        if "analyze_document_structure" in tasks:
            roles_needed.add("document_structure_analyst")
        # validate, populate_sections tasks don't need LLM

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

            # Per-stage budget guard (Phase 0A). When the remaining wall-clock
            # budget is below this stage's minimum, skip the stage and let
            # deterministic stages run instead of burning the tail on one
            # expensive LLM batch.
            stage_min = STAGE_MIN_BUDGET_SECONDS.get(task, 0)
            if stage_min > 0 and wall_deadline != float("inf"):
                remaining_seconds = wall_deadline - time.monotonic()
                if remaining_seconds < stage_min:
                    report.task_stats[task] = {
                        "ok": 0, "skip": 0, "fail": 0,
                        "skipped_insufficient_budget": 1,
                        "remaining_seconds": int(remaining_seconds),
                        "stage_min_seconds": stage_min,
                    }
                    logger.info(
                        "Skipping task %s: %.0fs remaining, %ds minimum required",
                        task, remaining_seconds, stage_min,
                    )
                    continue

            remaining = max_documents - total_processed if max_documents > 0 else limit
            task_limit = min(remaining, limit) if max_documents > 0 else limit

            try:
                task_deadline = (
                    wall_deadline if wall_deadline != float("inf") else None
                )
                task_result = self._run_task(
                    task,
                    task_limit,
                    profile,
                    family,
                    since,
                    resume,
                    rediagnose_unknown,
                    wall_deadline=task_deadline,
                )
                report.task_stats[task] = task_result
                total_processed += self._document_count_for_task(task, task_result)
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
            self._diagnoser = ParseFailureDiagnoser(
                self._orch,
                self._db_path,
                self_consistency_votes=getattr(self, "_sc_votes", 1),
            )

        if "suggest" in tasks and self._suggestion_gen is None:
            from duke_rates.document_intelligence.regex_suggestions import RegexSuggestionGenerator
            self._suggestion_gen = RegexSuggestionGenerator(self._orch, self._db_path)

        if ("validate" in tasks or "revalidate" in tasks) and self._validator is None:
            from duke_rates.document_intelligence.regex_validation import RegexValidationHarness
            self._validator = RegexValidationHarness(self._db_path)

        if "shadow_test" in tasks and self._shadow is None:
            from duke_rates.document_intelligence.regex_shadow_test import RegexShadowHarness
            self._shadow = RegexShadowHarness(self._db_path)

        if "profile_consensus" in tasks and self._consensus is None:
            from duke_rates.document_intelligence.profile_consensus import ProfileConsensusEngine
            self._consensus = ProfileConsensusEngine(self._db_path)

        if (
            "extract" in tasks or "extract_staged" in tasks
        ) and self._extractor is None:
            from duke_rates.document_intelligence.schema_extraction import SchemaGuidedExtractor
            self._extractor = SchemaGuidedExtractor(self._orch, self._db_path)

        if "populate_identity" in tasks and self._identity_agg is None:
            from duke_rates.document_intelligence.document_identity import DocumentIdentityAggregator
            self._identity_agg = DocumentIdentityAggregator(self._db_path)

        if "populate_routing_tier" in tasks and self._tier_agg is None:
            from duke_rates.document_intelligence.routing_tier import TierAggregator
            self._tier_agg = TierAggregator(self._db_path)

        if "bind_tier1" in tasks and self._tier1_binder is None:
            from duke_rates.document_intelligence.tier1_binder import Tier1Binder
            self._tier1_binder = Tier1Binder(self._db_path)

        if "generate_per_doc_rules" in tasks and self._per_doc_generator is None:
            from duke_rates.document_intelligence.per_doc_rule_generator import PerDocRuleGenerator
            self._per_doc_generator = PerDocRuleGenerator(self._orch, self._db_path)

        if "generate_grounded_rules" in tasks and self._grounded_generator is None:
            from duke_rates.document_intelligence.extraction_grounded_rules import (
                ExtractionGroundedRuleGenerator,
            )
            self._grounded_generator = ExtractionGroundedRuleGenerator(
                self._orch, self._db_path,
            )

        if "detect_rule_promotions" in tasks and self._promotion_detector is None:
            from duke_rates.document_intelligence.rule_promotion import PromotionDetector
            self._promotion_detector = PromotionDetector(self._db_path)

        # Phase 6 sub-document sections
        if "populate_sections" in tasks and self._sections_populator is None:
            from duke_rates.document_intelligence.section_aggregator import (
                DocumentSectionAggregator,
            )
            self._sections_populator = DocumentSectionAggregator(self._db_path)

        if "analyze_document_structure" in tasks and self._structure_analyst is None:
            from duke_rates.document_intelligence.document_structure_analyst import (
                DocumentStructureAnalyst,
            )
            self._structure_analyst = DocumentStructureAnalyst(
                self._orch, self._db_path,
            )

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
        wall_deadline: float | None = None,
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
            # Auto-fallback: if a fresh-diagnose pass finds nothing AND we weren't
            # already in rediagnose mode, fall through to rediagnose-unknown so
            # the loop has work to do instead of exiting idle. Controlled by the
            # _auto_rediagnose flag wired in from the run() call.
            if (
                not candidates
                and not rediagnose_unknown
                and getattr(self, "_auto_rediagnose", False)
            ):
                candidates = self._diagnoser.select_rediagnosis_candidates(
                    limit=limit, profile=profile, family=family, since=since
                )
                if candidates:
                    stats["fallback_to_rediagnose"] = len(candidates)
                    rediagnose_unknown = True  # affect resume filter below
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

        elif task == "revalidate":
            # Move stuck needs_human_review rows back into pending and run
            # validate against them with the current thresholds.
            if self._validator is None:
                stats["skip"] = limit
            else:
                reset = self._validator.reset_human_review_for_revalidation(limit=limit)
                stats["reset"] = reset
                pending = self._validator.select_pending_suggestions(limit=limit)
                stats["candidates"] = len(pending)
                for row in pending:
                    try:
                        self._validator.validate_suggestion(row)
                        stats["ok"] += 1
                    except Exception as exc:
                        logger.warning(
                            "revalidate failed for id=%s: %s",
                            row.get("id"), exc, exc_info=True,
                        )
                        stats["fail"] += 1

        elif task == "shadow_test":
            if self._shadow is None:
                stats["skip"] = limit
            else:
                # First, drain the strict pool (accepted_synthetic). Then, if
                # there's budget left, top up with needs_human_review rows so
                # the corpus-wide signal can rescue stuck cases too.
                primary = self._shadow.select_pending_shadow_tests(limit=limit)
                remaining = max(0, limit - len(primary))
                if remaining > 0:
                    secondary = self._shadow.select_pending_shadow_tests(
                        limit=remaining, include_human_review=True
                    )
                    # de-dup any overlap (primary already covers accepted_synthetic)
                    primary_ids = {r.get("id") for r in primary}
                    rescue = [r for r in secondary if r.get("id") not in primary_ids]
                    if rescue:
                        stats["human_review_rescue"] = len(rescue)
                    pending = primary + rescue
                else:
                    pending = primary
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
                # Reclassify diagnoses where consensus said the existing profile
                # IS the best fit — those are extraction failures, not routing
                # failures, and should flow to the suggest stage.
                reclassified = self._consensus.reclassify_failing_already_best()
                if reclassified:
                    stats["reclassified_to_regex_gap"] = reclassified
                    logger.info(
                        "profile_consensus reclassified %s diagnoses to regex_gap",
                        reclassified,
                    )

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

        elif task == "extract_staged":
            candidates = self._extractor.select_extraction_candidates(
                limit=limit, profile=profile, family=family
            )
            stats["filtered_at_stage_1"] = 0
            stats["rate_rows_total"] = 0
            for candidate in candidates:
                if wall_deadline is not None:
                    import time as _time
                    if _time.monotonic() >= wall_deadline:
                        stats["skip"] += 1
                        stats.setdefault("stopped_at_deadline", 1)
                        continue
                try:
                    extraction = self._extractor.extract_candidate_staged(candidate)
                except Exception:
                    stats["fail"] += 1
                    continue
                if extraction is None:
                    stats["fail"] += 1
                    continue
                # Stage-1 deterministic filter outcomes have very low conf.
                if extraction.extraction_confidence <= 0.1 and not extraction.rate_rows:
                    stats["filtered_at_stage_1"] += 1
                    stats["skip"] += 1
                elif extraction.rate_rows:
                    stats["ok"] += 1
                    stats["rate_rows_total"] += len(extraction.rate_rows)
                else:
                    stats["skip"] += 1

        elif task == "populate_identity":
            processed = self._identity_agg.populate_all(limit=limit if limit > 0 else None)
            stats["ok"] = processed

        elif task == "populate_routing_tier":
            processed = self._tier_agg.label_all(limit=limit if limit > 0 else None)
            stats["ok"] = processed

        elif task == "bind_tier1":
            counts = self._tier1_binder.bind_all(limit=limit if limit > 0 else None)
            stats.update({str(k): int(v) for k, v in counts.items()})
            stats["ok"] = sum(int(v) for v in counts.values())

        elif task == "generate_per_doc_rules":
            outcomes = self._per_doc_generator.generate_batch(limit=limit)
            status_counts: dict[str, int] = {}
            rejection_reasons: list[str] = []
            for outcome in outcomes:
                status_counts[outcome.status] = status_counts.get(outcome.status, 0) + 1
                if outcome.status == "rejected" and outcome.validation and outcome.validation.reason:
                    rejection_reasons.append(outcome.validation.reason)
                elif outcome.status == "error" and outcome.error:
                    rejection_reasons.append(f"error: {outcome.error}")
            stats.update(status_counts)
            stats["candidates"] = len(outcomes)
            stats["ok"] = status_counts.get("accepted", 0)
            stats["skip"] = status_counts.get("skipped", 0)
            stats["fail"] = status_counts.get("rejected", 0) + status_counts.get("error", 0)
            if rejection_reasons:
                stats["rejection_reasons"] = rejection_reasons[:20]

        elif task == "generate_grounded_rules":
            # Extraction-grounded rule generation (Phase 6F). Each candidate
            # is one high-confidence rate row from llm_candidate_rate_extractions
            # — the LLM produces a regex anchored on the doc's schedule code
            # that captures the row's known value.
            outcomes = self._grounded_generator.generate_batch(limit=limit)
            g_status_counts: dict[str, int] = {}
            g_rejection_reasons: list[str] = []
            for outcome in outcomes:
                g_status_counts[outcome.status] = g_status_counts.get(outcome.status, 0) + 1
                if outcome.status == "rejected" and outcome.validation and outcome.validation.reason:
                    g_rejection_reasons.append(outcome.validation.reason)
                elif outcome.status == "skipped" and outcome.error:
                    g_rejection_reasons.append(f"skipped: {outcome.error}")
                elif outcome.status == "error" and outcome.error:
                    g_rejection_reasons.append(f"error: {outcome.error}")
            stats.update(g_status_counts)
            stats["candidates"] = len(outcomes)
            stats["ok"] = g_status_counts.get("accepted", 0)
            stats["skip"] = g_status_counts.get("skipped", 0)
            stats["fail"] = g_status_counts.get("rejected", 0) + g_status_counts.get("error", 0)
            if g_rejection_reasons:
                stats["rejection_reasons"] = g_rejection_reasons[:10]

        elif task == "detect_rule_promotions":
            # Skip the clustering pass when there aren't enough accepted rules
            # to form a cluster (MIN_CLUSTER_SIZE=3 in rule_promotion.py).
            accepted_count = self._count_where(
                "document_specific_rules", "status = 'accepted'", limit=1000,
            )
            if accepted_count < 3:
                stats["ok"] = 0
                stats["skip"] = 0
                stats["fail"] = 0
                stats["skipped_few_accepted"] = accepted_count
            else:
                candidates = self._promotion_detector.detect_all()
                stats["ok"] = len(candidates)
                stats["candidates"] = len(candidates)

        # Phase 6 sub-document sections
        elif task == "populate_sections":
            n = self._sections_populator.populate_all(limit=limit)
            stats["ok"] = n
            stats["documents_populated"] = n

        elif task == "analyze_document_structure":
            # Short-circuit if there's no work — avoids burning the wall-clock
            # budget on empty queues when populate_sections hasn't run yet.
            small_candidates = self._structure_analyst.select_candidates(limit=1)
            large_count = self._structure_analyst._count_large_section_docs(limit=1)
            if not small_candidates and large_count == 0:
                stats["ok"] = 0
                stats["skip"] = 1
                stats["candidates"] = 0
                stats["skipped_no_work"] = 1
                return stats
            results = self._structure_analyst.analyze_batch(
                limit=limit, deadline=wall_deadline,
            )
            stats["ok"] = results["analyzed"]
            stats["fail"] = results["failed"]
            stats["skip"] = results.get("skipped", 0)
            stats["candidates"] = results["candidates"]
            stats["sections_updated"] = results["merged"]
            # Per-page boundary classifier for large sections
            ls = results.get("large_sections", {})
            if ls:
                stats["large_section_docs"] = ls.get("docs_analyzed", 0)
                stats["large_section_boundaries"] = ls.get("boundaries_found", 0)
                stats["large_section_splits"] = ls.get("sections_split", 0)

        return stats

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _document_count_for_task(task: str, stats: dict[str, int]) -> int:
        """Count document-like LLM work, not deterministic refresh rows.

        ``candidates`` is the queue size discovered before processing — it must
        NOT be used to bump ``total_processed`` because that prematurely trips
        ``--max-documents``. Always count actual outcomes (ok+fail+skip).
        """
        if task in {"populate_identity", "populate_routing_tier", "bind_tier1",
                     "detect_rule_promotions", "populate_sections"}:
            return 0
        return (
            int(stats.get("ok", 0) or 0)
            + int(stats.get("fail", 0) or 0)
            + int(stats.get("skip", 0) or 0)
        )

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
            elif task == "extract_staged":
                candidates = self._extractor.select_extraction_candidates(
                    limit=limit, profile=profile, family=family
                )
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": len(candidates)}
            elif task == "populate_identity":
                candidates = self._count_rows("document_fingerprints_v2", "source_pdf", limit=limit)
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": candidates}
            elif task == "populate_routing_tier":
                candidates = self._count_rows("document_identity", "source_pdf", limit=limit)
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": candidates}
            elif task == "bind_tier1":
                candidates = self._count_where("document_routing_tier", "tier = 1", limit=limit)
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": candidates}
            elif task == "generate_per_doc_rules":
                candidates = self._per_doc_generator.select_candidates(limit=limit)
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": len(candidates)}
            elif task == "generate_grounded_rules":
                candidates = self._grounded_generator.select_candidates(limit=limit)
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": len(candidates)}
            elif task == "detect_rule_promotions":
                candidates = self._count_where("document_specific_rules", "status = 'accepted'", limit=limit)
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": candidates}
            # Phase 6 sub-document sections
            elif task == "populate_sections":
                candidates = len(self._sections_populator.select_candidates(limit=limit))
                report.task_stats[task] = {"ok": 0, "skip": 0, "fail": 0, "candidates": candidates}
            elif task == "analyze_document_structure":
                candidates = len(self._structure_analyst.select_candidates(limit=limit))
                large_docs = self._structure_analyst._count_large_section_docs(limit=limit)
                report.task_stats[task] = {
                    "ok": 0, "skip": 0, "fail": 0,
                    "candidates": candidates,
                    "large_section_docs": large_docs,
                }

        report.completed_at = datetime.now(timezone.utc).isoformat()
        return report

    def _count_rows(self, table: str, distinct_column: str, *, limit: int) -> int:
        return self._count_sql(
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {distinct_column} FROM {table} LIMIT ?)",
            (limit,),
        )

    def _count_where(self, table: str, where: str, *, limit: int) -> int:
        return self._count_sql(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} WHERE {where} LIMIT ?)",
            (limit,),
        )

    def _count_sql(self, sql: str, params: tuple[Any, ...]) -> int:
        import sqlite3
        try:
            conn = sqlite3.connect(str(self._db_path))
            row = conn.execute(sql, params).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0

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

            dsr_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM document_specific_rules GROUP BY status"
            ).fetchall()
            report.document_specific_rules_by_status = {
                r["status"]: r["cnt"] for r in dsr_rows
            }

            promo_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM template_promotion_candidates GROUP BY status"
            ).fetchall()
            report.promotion_candidates_by_status = {
                r["status"]: r["cnt"] for r in promo_rows
            }

            # Phase 6 section statistics
            try:
                sec_total = conn.execute(
                    "SELECT COUNT(*) FROM document_sections"
                ).fetchone()
                report.sections_total = sec_total[0] if sec_total else 0
                sec_types = conn.execute(
                    "SELECT section_type, COUNT(*) as cnt FROM document_sections GROUP BY section_type"
                ).fetchall()
                report.sections_by_type = {r[0]: r[1] for r in sec_types}
            except Exception:
                report.sections_total = 0
                report.sections_by_type = {}

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
