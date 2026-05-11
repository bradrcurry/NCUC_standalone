from __future__ import annotations

import json
from typing import Any

import typer

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.logging_config import configure_logging


def _bootstrap():
    settings = get_settings()
    configure_logging(settings.log_level)
    return settings, Repository(settings.database_path)

# =============================================================================
# Phase 5.6 — LLM-assisted parse diagnosis and regex improvement loop
# =============================================================================


def analyze_parse_failures_nc(
    limit: int = typer.Option(25, "--limit", help="Max parse attempts to analyze."),
    profile: str | None = typer.Option(None, "--profile", help="Optional parser profile filter."),
    family: str | None = typer.Option(None, "--family", help="Optional family_key filter."),
    since: str = typer.Option("", "--since", help="ISO8601 datetime — only parse attempts after this."),
    rediagnose_unknown: bool = typer.Option(
        False,
        "--rediagnose-unknown",
        help="Re-run prior unknown/0.0-confidence diagnostics instead of selecting fresh attempts.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate candidates without calling the LLM."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Analyze weak/empty parse attempts with LLM root-cause diagnosis.

    Queries ``parse_attempt_logs`` for weak/empty parses, sends structured
    context to an LLM (``parse_failure_triage`` role), and persists a
    root-cause diagnosis with evidence and recommended actions to
    ``llm_parse_diagnostics``.

    Low-confidence diagnoses are escalated to the ``hard_parse_diagnosis`` role.
    No parser code is modified.
    """
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.parse_diagnosis import ParseFailureDiagnoser

    settings, _ = _bootstrap()
    db_path = _Path(settings.database_path)

    # Candidate selection only needs DB — no Ollama call
    orch = OllamaOrchestrator(db_path=settings.database_path)
    diagnoser = ParseFailureDiagnoser(orch, db_path)

    if rediagnose_unknown:
        candidates = diagnoser.select_rediagnosis_candidates(
            limit=limit,
            profile=profile,
            family=family,
            since=since or None,
        )
    else:
        candidates = diagnoser.select_candidates(
            limit=limit,
            profile=profile,
            family=family,
            since=since or None,
        )

    typer.echo(f"Candidates: {len(candidates)}")

    if dry_run:
        typer.echo("\n--- Dry Run Candidates ---")
        for c in candidates[:10]:
            typer.echo(
                f"  id={c.get('parse_attempt_id')} "
                f"profile={c.get('parser_profile')} "
                f"family={c.get('family_key')} "
                f"charges={c.get('charge_count')}"
            )
        typer.echo(f"  ... and {max(0, len(candidates) - 10)} more")
        return

    # Health probe only for live runs
    ok, err = orch.health_probe("parse_failure_triage")
    if not ok:
        typer.echo(f"ERROR: parse_failure_triage health check failed: {err}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"  parse_failure_triage -> {orch.roles['parse_failure_triage'].primary} (OK)")

    results = diagnoser.diagnose_batch(candidates, limit=limit)

    # Summary
    by_type: dict[str, int] = {}
    for r in results:
        ft = r.failure_type
        by_type[ft] = by_type.get(ft, 0) + 1

    if as_json:
        typer.echo(json.dumps({
            "candidates": len(candidates),
            "diagnosed": len(results),
            "rediagnose_unknown": rediagnose_unknown,
            "failures_by_type": by_type,
            "details": [
                {
                    "failure_type": r.failure_type,
                    "confidence": r.confidence,
                    "recommended_action": r.recommended_action,
                    "notes": r.notes,
                }
                for r in results
            ],
        }, indent=2, default=str))
        return

    typer.echo("\n--- Diagnosis Summary ---")
    typer.echo(f"  Candidates: {len(candidates)}")
    typer.echo(f"  Diagnosed:  {len(results)}")
    typer.echo("  By failure type:")
    for ft, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
        typer.echo(f"    {ft}: {cnt}")

    for r in results[:5]:
        typer.echo(
            f"  [{r.failure_type}] action={r.recommended_action} "
            f"conf={r.confidence:.2f} — {r.notes[:120]}"
        )


def suggest_regex_fixes_nc(
    limit: int = typer.Option(10, "--limit", help="Max suggestions to generate."),
    diagnosis_id: int | None = typer.Option(None, "--diagnosis-id", help="Target a specific diagnosis."),
    profile: str | None = typer.Option(None, "--profile", help="Optional parser profile filter."),
    failure_type: str | None = typer.Option(None, "--failure-type", help="Filter by failure_type (regex_gap, normalization_gap, ocr_noise)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate candidates without calling the LLM."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Generate regex/normalization suggestions for diagnosed parse failures.

    Queries ``llm_parse_diagnostics`` for failures classified as
    ``regex_gap``, ``normalization_gap``, or ``ocr_noise``, then asks an
    LLM (``regex_suggestion`` role) to propose candidate regex patterns or
    normalization rules.

    Suggestions are stored as review artifacts in
    ``docs/reports/regex_suggestions/`` and are NEVER auto-applied to parser code.
    """
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.regex_suggestions import RegexSuggestionGenerator

    settings, _ = _bootstrap()
    db_path = _Path(settings.database_path)

    orch = OllamaOrchestrator(db_path=settings.database_path)
    generator = RegexSuggestionGenerator(orch, db_path)

    candidates = generator.select_diagnoses_for_suggestion(
        limit=limit,
        diagnosis_id=diagnosis_id,
        profile=profile,
        failure_type=failure_type,
    )

    typer.echo(f"Candidates: {len(candidates)}")

    if dry_run:
        typer.echo("\n--- Dry Run Candidates ---")
        for c in candidates[:10]:
            typer.echo(
                f"  diagnosis_id={c.get('diagnosis_id')} "
                f"failure_type={c.get('failure_type')} "
                f"profile={c.get('parser_profile')}"
            )
        return

    ok, err = orch.health_probe("regex_suggestion")
    if not ok:
        typer.echo(f"ERROR: regex_suggestion health check failed: {err}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"  regex_suggestion -> {orch.roles['regex_suggestion'].primary} (OK)")

    results = generator.generate_batch(candidates, limit=limit)

    if as_json:
        typer.echo(json.dumps({
            "candidates": len(candidates),
            "suggestions_generated": len(results),
            "details": [
                {
                    "suggestion_type": r.suggestion_type,
                    "target_profile": r.target_profile,
                    "target_field": r.target_field,
                    "confidence": r.confidence,
                    "risk": r.risk,
                }
                for r in results
            ],
        }, indent=2, default=str))
        return

    typer.echo("\n--- Suggestion Summary ---")
    typer.echo(f"  Candidates:          {len(candidates)}")
    typer.echo(f"  Suggestions created:  {len(results)}")
    for r in results:
        typer.echo(
            f"  [{r.suggestion_type}] profile={r.target_profile} "
            f"field={r.target_field or '(any)'} risk={r.risk} conf={r.confidence:.2f}"
        )
    typer.echo(f"  Review artifacts: docs/reports/regex_suggestions/")


def validate_regex_suggestions_nc(
    limit: int = typer.Option(10, "--limit", help="Max suggestions to validate."),
    suggestion_id: int | None = typer.Option(None, "--suggestion-id", help="Validate a specific suggestion."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate pending suggestions without running validation."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Deterministically validate pending regex/normalization suggestions.

    Tests candidate regexes against known-good documents (regression),
    known-failed documents (improvement), and unrelated document types
    (false-positive check). Marks each suggestion as accepted, rejected,
    or needing human review.

    Does NOT modify parser code — regexes are tested against extracted text only.
    """
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.regex_validation import RegexValidationHarness

    settings, _ = _bootstrap()

    harness = RegexValidationHarness(_Path(settings.database_path))

    if dry_run:
        pending = harness.select_pending_suggestions(limit=limit, suggestion_id=suggestion_id)
        typer.echo(f"Pending suggestions: {len(pending)}")
        for p in pending:
            typer.echo(
                f"  id={p.get('id')} type={p.get('suggestion_type')} "
                f"profile={p.get('target_profile')} confidence={p.get('confidence')}"
            )
        return

    suggestions = harness.select_pending_suggestions(limit=limit, suggestion_id=suggestion_id)
    typer.echo(f"Pending suggestions: {len(suggestions)}")

    results = harness.validate_all_pending(limit=limit)

    if as_json:
        typer.echo(json.dumps({
            "pending": len(suggestions),
            "validated": len(results),
            "results": [r.model_dump() for r in results],
        }, indent=2, default=str))
        return

    typer.echo("\n--- Validation Summary ---")
    accepted = sum(1 for r in results if r.status == "accepted_candidate")
    rejected_fp = sum(1 for r in results if r.status == "rejected_false_positive")
    rejected_ng = sum(1 for r in results if r.status == "rejected_no_gain")
    needs_review = sum(1 for r in results if r.status == "needs_human_review")

    typer.echo(f"  Validated:          {len(results)}")
    typer.echo(f"  Accepted:           {accepted}")
    typer.echo(f"  Rejected (FP):      {rejected_fp}")
    typer.echo(f"  Rejected (no gain): {rejected_ng}")
    typer.echo(f"  Needs human review: {needs_review}")

    for r in results:
        typer.echo(
            f"  [{r.status}] suggestion_id={r.suggestion_id} "
            f"before={r.before_charge_count} after={r.after_charge_count} "
            f"regressions={len(r.regression_failures)}"
        )


def run_llm_parse_fallback_nc(
    limit: int = typer.Option(10, "--limit", help="Max documents to attempt LLM extraction on."),
    historical_document_id: int | None = typer.Option(None, "--historical-document-id", help="Target a specific historical document."),
    profile: str | None = typer.Option(None, "--profile", help="Optional parser profile filter."),
    family: str | None = typer.Option(None, "--family", help="Optional family_key filter."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate candidates without calling the LLM."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Run schema-guided LLM fallback extraction on weak/empty parses.

    For documents where deterministic parsing failed but text quality is
    adequate, uses the ``structured_rate_extraction`` role to extract
    candidate rate rows as a fallback.

    Extracted rows are stored as CANDIDATES in ``llm_candidate_rate_extractions``.
    They are NEVER merged into production ``tariff_charges`` without validation.
    """
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.schema_extraction import SchemaGuidedExtractor

    settings, _ = _bootstrap()
    db_path = _Path(settings.database_path)

    orch = OllamaOrchestrator(db_path=settings.database_path)
    extractor = SchemaGuidedExtractor(orch, db_path)

    candidates = extractor.select_extraction_candidates(
        limit=limit,
        historical_document_id=historical_document_id,
        profile=profile,
        family=family,
    )

    typer.echo(f"Candidates: {len(candidates)}")

    if dry_run:
        typer.echo("\n--- Dry Run Candidates ---")
        for c in candidates[:10]:
            typer.echo(
                f"  parse_attempt_id={c.get('parse_attempt_id')} "
                f"profile={c.get('parser_profile')} "
                f"family={c.get('family_key')} "
                f"charges={c.get('charge_count')}"
            )
        return

    ok, err = orch.health_probe("structured_rate_extraction")
    if not ok:
        typer.echo(f"ERROR: structured_rate_extraction health check failed: {err}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"  structured_rate_extraction -> {orch.roles['structured_rate_extraction'].primary} (OK)")

    results = extractor.extract_batch(candidates, limit=limit)

    total_rows = sum(len(r.rate_rows) for r in results)

    if as_json:
        typer.echo(json.dumps({
            "candidates": len(candidates),
            "extractions": len(results),
            "total_candidate_rows": total_rows,
            "details": [
                {
                    "source_pdf": c.get("source_pdf", ""),
                    "family_key": c.get("family_key", ""),
                    "row_count": len(r.rate_rows),
                    "confidence": r.extraction_confidence,
                    "warnings": r.warnings,
                }
                for c, r in zip(candidates, results)
            ],
        }, indent=2, default=str))
        return

    typer.echo("\n--- Extraction Summary ---")
    typer.echo(f"  Candidates:           {len(candidates)}")
    typer.echo(f"  Extractions attempted: {len(results)}")
    typer.echo(f"  Total candidate rows:  {total_rows}")
    for i, r in enumerate(results):
        typer.echo(
            f"  [{i+1}] {len(r.rate_rows)} rows, "
            f"confidence={r.extraction_confidence:.2f}, "
            f"warnings={len(r.warnings)}"
        )
    typer.echo("  CAUTION: These are CANDIDATE rows only. Review before use.")


def run_overnight_parse_improvement_nc(
    max_documents: int = typer.Option(0, "--max-documents", help="Max documents to process (0 = unlimited)."),
    max_runtime_minutes: int = typer.Option(0, "--max-runtime-minutes", help="Hard wall-clock cap in minutes (0 = unlimited)."),
    max_consecutive_failures: int = typer.Option(5, "--max-consecutive-failures", help="Abort after N consecutive model call failures."),
    task_kind: str = typer.Option(
        "diagnose",
        "--task-kind",
        help=(
            "Comma-separated tasks: diagnose, suggest, validate, revalidate, "
            "shadow_test, profile_consensus, extract, extract_staged, "
            "populate_identity, populate_routing_tier, bind_tier1, "
            "generate_per_doc_rules, detect_rule_promotions, "
            "populate_sections, analyze_document_structure. "
            "Use extract_staged for higher-accuracy per-line classification."
        ),
    ),
    profile: str | None = typer.Option(None, "--profile", help="Optional parser profile filter."),
    family: str | None = typer.Option(None, "--family", help="Optional family_key filter."),
    since: str = typer.Option("", "--since", help="ISO8601 datetime — only parse attempts after this."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate work set and exit without model calls or DB writes."),
    resume: bool = typer.Option(False, "--resume", help="Skip subjects already covered at current prompt_version + model."),
    rediagnose_unknown: bool = typer.Option(
        False,
        "--rediagnose-unknown",
        help="For diagnose stage, re-run prior unknown/0.0-confidence diagnostics instead of fresh attempts.",
    ),
    limit: int = typer.Option(25, "--limit", help="Max parse attempts per task kind."),
    exit_when_idle: bool = typer.Option(
        False,
        "--exit-when-idle",
        help="Exit with code 42 if the run finds no work (lets wrapper loops break cleanly).",
    ),
    self_consistency_votes: int = typer.Option(
        1,
        "--self-consistency-votes",
        help="For diagnose: total triage calls per case (1 = off; 3 recommended). "
             "Extra votes only fire when first-call confidence is in the uncertain band.",
    ),
    auto_rediagnose_unknown: bool = typer.Option(
        False,
        "--auto-rediagnose-unknown",
        help="If a fresh-diagnose pass finds no candidates, automatically retry with "
             "rediagnose-unknown so the loop has work to do instead of exiting idle.",
    ),
) -> None:
    """Run parse-improvement tasks as a resumable overnight batch.

    Processes weak/empty parse attempts in sequential stages:
      1. **diagnose** — classify the failure root cause (LLM)
      2. **suggest** — generate regex/normalization candidates (LLM)
      3. **validate** — deterministic validation of pending suggestions (local)
      4. **extract** — schema-guided LLM fallback extraction (LLM)
      5. **populate_identity / populate_routing_tier / bind_tier1** — Phase 1-3 routing refresh (local)
      6. **generate_per_doc_rules / detect_rule_promotions** — Phase 4 per-doc rule path (LLM + local)

    Recommended overnight pattern (two-phase):
      Seed (once):  --task-kind diagnose,populate_identity,populate_routing_tier,bind_tier1 --limit 500
      Loop (repeat): --task-kind diagnose,suggest,validate,generate_per_doc_rules,detect_rule_promotions
                     --max-runtime-minutes 55 --limit 10

    The seed phase refreshes identity/tier routing and runs a fresh diagnosis
    pass. The loop alternates between the legacy suggest pipeline (for Tier 1/2
    docs with diagnosed failures) and the per-doc rule path (for Tier 3 docs).

    Safety guarantees:
      - No destructive overwrites — only INSERTs new rows
      - Bounded by wall-clock cap even with unlimited --max-documents
      - Resumable — --resume skips completed (subject, stage, model, prompt_version) tuples
      - Stops cleanly on: max docs, max runtime, consecutive failures, health probe degradation, SIGINT/SIGTERM

    End-of-run JSON report written to docs/reports/overnight_parse_improvement/<timestamp>.json.
    Idle runs (no work done) go under ``idle/`` instead. With --exit-when-idle, exit code 42
    signals an idle run so wrapper loops can break.
    """
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.parse_improvement_loop import (
        ParseImprovementLoop,
        VALID_TASK_KINDS,
    )

    settings, _ = _bootstrap()

    tasks = [t.strip() for t in task_kind.split(",") if t.strip()]
    valid_tasks = set(VALID_TASK_KINDS)
    for t in tasks:
        if t not in valid_tasks:
            typer.echo(f"Unknown task kind {t!r}. Valid: {', '.join(sorted(valid_tasks))}", err=True)
            raise typer.Exit(code=1)

    typer.echo(f"Tasks: {tasks}")
    typer.echo(f"Max documents: {max_documents or 'unlimited'}")
    typer.echo(f"Max runtime:   {max_runtime_minutes or 'unlimited'} minutes")
    if rediagnose_unknown:
        typer.echo("Mode:          re-diagnose prior unknown/0.0 diagnostics")

    orch = OllamaOrchestrator(db_path=settings.database_path)
    loop = ParseImprovementLoop(orch, _Path(settings.database_path))

    report = loop.run(
        task_kinds=tasks,
        max_documents=max_documents,
        max_runtime_minutes=max_runtime_minutes,
        max_consecutive_failures=max_consecutive_failures,
        profile=profile,
        family=family,
        since=since or None,
        dry_run=dry_run,
        resume=resume,
        rediagnose_unknown=rediagnose_unknown,
        limit=limit,
        self_consistency_votes=self_consistency_votes,
        auto_rediagnose_unknown=auto_rediagnose_unknown,
    )

    report_dict = report.to_dict()

    if dry_run:
        typer.echo("\n--- Dry Run Work Set ---")
        for task, stats in report_dict.get("task_stats", {}).items():
            typer.echo(f"  {task}: {stats.get('candidates', 0)} candidates")
        return

    typer.echo(f"\n--- Overnight Parse Improvement Complete ---")
    typer.echo(f"  stop reason:     {report.stop_reason}")
    typer.echo(f"  runtime:         {report.runtime_seconds:.1f}s")
    typer.echo(f"  docs analyzed:   {report.documents_analyzed}")
    for task, stats in report_dict.get("task_stats", {}).items():
        parts = ", ".join(
            f"{k}={v}" for k, v in stats.items()
            if (isinstance(v, int) and v > 0) or (isinstance(v, list) and len(v) > 0)
        )
        typer.echo(f"  {task}: {parts}")
    typer.echo(f"  failures by type: {report.parse_failures_by_type}")
    idle = report.is_idle()
    sub = "idle/" if idle else ""
    typer.echo(f"  report:          docs/reports/overnight_parse_improvement/{sub}{report.run_id}.json")
    if idle:
        typer.echo(f"  idle:            true (no work found)")
        if exit_when_idle:
            raise typer.Exit(code=42)


def report_wrong_profile_diagnostics_nc(
    limit: int = typer.Option(40, "--limit", help="Max clusters to show in stdout output."),
    sample_pdfs: int = typer.Option(2, "--sample-pdfs", help="Example PDFs to show per cluster."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full grouped report as JSON to stdout."),
    write_report: bool = typer.Option(
        True,
        "--write-report/--no-write-report",
        help="Write a JSON report to docs/reports/wrong_profile_diagnostics/<timestamp>.json.",
    ),
) -> None:
    """Group wrong_profile parse-failure diagnoses by current parser_profile + source directory.

    The overnight parse-improvement loop labels failures as ``wrong_profile`` when the
    parser ran but produced no usable rate rows. These cases are NOT regex gaps — feeding
    them to the regex-suggestion LLM is wasted effort. Instead this report groups them
    so you can spot patterns: e.g. ``progress_single_value_rider`` failing on N specific
    rider PDFs, suggesting a profile bug or a need to split the profile into sub-variants.

    For each cluster, the report shows:
      - the current (failing) parser_profile
      - the source directory the docs came from
      - the diagnostic's recommended_action distribution (retry_profile vs suggest_regex etc.)
      - up to N example PDFs

    Use this output to decide whether to fix the existing profile, route the docs to a
    different profile, or build a new profile.
    """
    import json as _json
    import os as _os
    from collections import Counter, defaultdict
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path

    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT ld.id          AS diagnosis_id,
                   ld.recommended_action,
                   ld.confidence   AS diagnosis_confidence,
                   pal.source_pdf,
                   pal.parser_profile
            FROM llm_parse_diagnostics ld
            LEFT JOIN parse_attempt_logs pal ON pal.id = ld.parse_attempt_id
            WHERE ld.failure_type = 'wrong_profile'
            ORDER BY pal.parser_profile, pal.source_pdf
            """
        ).fetchall()
    finally:
        conn.close()

    total = len(rows)
    clusters: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"members": 0, "actions": Counter(), "examples": []}
    )
    for r in rows:
        pdf = r["source_pdf"] or ""
        profile = r["parser_profile"] or "unknown"
        src_dir = _os.path.dirname(pdf) if pdf else "?"
        key = (profile, src_dir)
        c = clusters[key]
        c["members"] = int(c["members"]) + 1  # type: ignore[operator]
        c["actions"][r["recommended_action"] or "?"] += 1  # type: ignore[index]
        if len(c["examples"]) < sample_pdfs:  # type: ignore[arg-type]
            c["examples"].append(_os.path.basename(pdf))  # type: ignore[union-attr]

    sorted_clusters = sorted(
        clusters.items(), key=lambda kv: kv[1]["members"], reverse=True  # type: ignore[arg-type]
    )

    payload = {
        "generated_at": _dt.now(_tz.utc).isoformat(),
        "total_wrong_profile_diagnoses": total,
        "cluster_count": len(clusters),
        "clusters": [
            {
                "parser_profile": k[0],
                "source_dir": k[1],
                "members": v["members"],
                "recommended_actions": dict(v["actions"]),  # type: ignore[arg-type]
                "examples": v["examples"],
            }
            for k, v in sorted_clusters
        ],
    }

    if write_report:
        report_dir = _Path("docs/reports/wrong_profile_diagnostics")
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        path = report_dir / f"{ts}.json"
        path.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
        typer.echo(f"Report written: {path}")

    if as_json:
        typer.echo(_json.dumps(payload, indent=2))
        return

    typer.echo(f"\nwrong_profile diagnoses: {total} total across {len(clusters)} clusters")
    typer.echo(f"{'profile':<42} {'members':>7}  source_dir / examples")
    typer.echo("-" * 100)
    for (profile, src_dir), v in sorted_clusters[:limit]:
        actions = ", ".join(f"{a}={n}" for a, n in v["actions"].most_common())  # type: ignore[union-attr]
        typer.echo(f"{profile:<42} {v['members']:>7}  {src_dir}")
        typer.echo(f"{'':>42}          actions: {actions}")
        for ex in v["examples"]:  # type: ignore[union-attr]
            typer.echo(f"{'':>42}          - {ex}")
    if len(sorted_clusters) > limit:
        typer.echo(f"... ({len(sorted_clusters) - limit} more clusters omitted; use --json for full list)")


def report_profile_recommendations_nc(
    limit: int = typer.Option(40, "--limit", help="Max rows to show in stdout output."),
    status: str = typer.Option(
        "recommended",
        "--status",
        help="Filter by status: recommended | failing_already_best | no_recommendation | all.",
    ),
    min_confidence: float = typer.Option(
        0.0, "--min-confidence", help="Hide rows below this confidence (0.0-1.0)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit full grouped report as JSON."),
    write_report: bool = typer.Option(
        True,
        "--write-report/--no-write-report",
        help="Write JSON to docs/reports/profile_recommendations/<timestamp>.json.",
    ),
) -> None:
    """List parser-profile reassignment recommendations from the consensus engine.

    The consensus engine (``--task-kind profile_consensus``) writes one row to
    ``parser_profile_recommendations`` per ``wrong_profile`` diagnosis. This
    command summarizes those rows and surfaces the top failing→recommended
    flips so you can decide whether to bulk-reassign.

    Statuses:
      - **recommended**          — top profile beats failing profile by both
        confidence and margin thresholds; safe to act on.
      - **failing_already_best** — engine agrees with the current assignment,
        so the doc isn't really mis-routed — the parser is just failing to
        extract. These should move to the regex/extraction fix path.
      - **no_recommendation**    — too few signals to be confident.
    """
    import json as _json
    from collections import Counter
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path

    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        if status == "all":
            where = "WHERE confidence >= ?"
            params: tuple[Any, ...] = (min_confidence,)
        else:
            where = "WHERE status = ? AND confidence >= ?"
            params = (status, min_confidence)
        rows = conn.execute(
            f"""
            SELECT id, parse_attempt_id, source_pdf, failing_profile,
                   recommended_profile, confidence, margin, status,
                   votes_json, evidence_json
            FROM parser_profile_recommendations
            {where}
            ORDER BY confidence DESC, margin DESC, id DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    rows_dicts = [dict(r) for r in rows]
    flip_counts = Counter(
        (r["failing_profile"], r["recommended_profile"]) for r in rows_dicts
    )

    payload = {
        "generated_at": _dt.now(_tz.utc).isoformat(),
        "filter_status": status,
        "min_confidence": min_confidence,
        "total": len(rows_dicts),
        "top_flips": [
            {"failing": k[0], "recommended": k[1], "count": n}
            for k, n in flip_counts.most_common(20)
        ],
        "rows": rows_dicts[:limit],
    }

    if write_report:
        report_dir = _Path("docs/reports/profile_recommendations")
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        path = report_dir / f"{ts}.json"
        # Don't dump giant rows json blobs; persist a compact form.
        compact = dict(payload)
        compact["rows"] = [
            {k: v for k, v in r.items() if k not in ("votes_json", "evidence_json")}
            for r in rows_dicts
        ]
        path.write_text(_json.dumps(compact, indent=2), encoding="utf-8")
        typer.echo(f"Report written: {path}")

    if as_json:
        typer.echo(_json.dumps(payload, indent=2, default=str))
        return

    typer.echo(f"\nProfile recommendations: status={status} count={len(rows_dicts)}")
    typer.echo(f"\nTop failing -> recommended flips:")
    typer.echo(f"  {'failing':<42} {'recommended':<42} {'count':>5}")
    typer.echo("-" * 100)
    for (failing, rec), n in flip_counts.most_common(15):
        typer.echo(f"  {failing:<42} {rec:<42} {n:>5}")
    typer.echo()
    typer.echo(f"Top {min(limit, len(rows_dicts))} rows by confidence:")
    typer.echo(f"  {'conf':>4} {'margin':>6}  {'failing':<35} {'recommended':<35}")
    typer.echo("-" * 100)
    for r in rows_dicts[:limit]:
        typer.echo(
            f"  {r['confidence']:>4.2f} {r['margin']:>6.2f}  "
            f"{(r['failing_profile'] or '?'):<35} {(r['recommended_profile'] or '?'):<35}"
        )


# ---------------------------------------------------------------------------
# Phase 1 — document_identity layer
# Plan ref: docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md §4
# ---------------------------------------------------------------------------


def populate_document_identity_nc(
    limit: int = typer.Option(
        0, "--limit", help="Process at most N source_pdfs (0 = unlimited)."
    ),
) -> None:
    """Aggregate evidence from existing tables into ``document_identity``.

    Reads from ``document_fingerprints_v2``, ``document_classifications``,
    and ``parser_profile_recommendations``, applies filename heuristics,
    scores overall identity confidence, and upserts one row per source_pdf.

    This is the Phase 1 foundation pass for the parsing-architecture
    refactor. It does NOT change extraction behavior — it produces the
    identity bundles that future routing layers consume.
    """
    from duke_rates.document_intelligence.document_identity import (
        DocumentIdentityAggregator,
    )

    settings, _ = _bootstrap()
    agg = DocumentIdentityAggregator(settings.database_path)
    n = agg.populate_all(limit=limit if limit > 0 else None)
    typer.echo(f"document_identity: populated/refreshed {n} rows")


def report_document_identity_nc(
    source_pdf: str = typer.Option(
        ..., "--source-pdf", help="Path of the source PDF to inspect."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw bundle as JSON."),
) -> None:
    """Print the persisted identity bundle for one document.

    Use this to debug routing decisions and to tune the confidence weights.
    """
    import json as _json

    from duke_rates.document_intelligence.document_identity import (
        DocumentIdentityAggregator, fetch_identity,
    )

    settings, _ = _bootstrap()
    bundle = fetch_identity(settings.database_path, source_pdf)
    if not bundle:
        # Allow inspection even when not yet persisted — build live.
        agg = DocumentIdentityAggregator(settings.database_path)
        live = agg.build_bundle(source_pdf)
        bundle = {
            "source_pdf": live.source_pdf,
            "schedule_codes_strong_json": _json.dumps(live.schedule_codes_strong),
            "rider_codes_strong_json": _json.dumps(live.rider_codes_strong),
            "leaf_numbers_json": _json.dumps(live.leaf_numbers),
            "detected_titles_json": _json.dumps(live.detected_titles),
            "filename_signals_json": _json.dumps(live.filename_signals),
            "classifier_label": live.classifier_label,
            "classifier_confidence": live.classifier_confidence,
            "profile_consensus_top": live.profile_consensus_top,
            "profile_consensus_confidence": live.profile_consensus_confidence,
            "profile_consensus_margin": live.profile_consensus_margin,
            "overall_confidence": live.overall_confidence,
            "evidence_log_json": _json.dumps(live.evidence_log, default=str),
            "_persisted": False,
        }

    if as_json:
        typer.echo(_json.dumps(bundle, indent=2, default=str))
        return

    typer.echo(f"\n=== Document Identity Bundle ===")
    typer.echo(f"PDF: {bundle['source_pdf']}")
    if not bundle.get("_persisted", True):
        typer.echo("(not yet persisted — built live; run populate-document-identity-nc to save)")
    typer.echo(f"\nOverall confidence: {bundle['overall_confidence']:.3f}")
    typer.echo()
    typer.echo("Strong schedule codes:")
    typer.echo(f"  {_json.loads(bundle['schedule_codes_strong_json'] or '[]')}")
    typer.echo("Strong rider codes:")
    typer.echo(f"  {_json.loads(bundle['rider_codes_strong_json'] or '[]')}")
    typer.echo("Leaf numbers:")
    typer.echo(f"  {_json.loads(bundle['leaf_numbers_json'] or '[]')}")
    titles = _json.loads(bundle['detected_titles_json'] or '[]')
    typer.echo(f"Distinctive titles ({len(titles)}):")
    for t in titles[:8]:
        typer.echo(f"  - {t}")
    typer.echo("Filename signals:")
    typer.echo(f"  {_json.loads(bundle['filename_signals_json'] or '[]')}")
    typer.echo()
    typer.echo(f"Classifier: {bundle.get('classifier_label')} (conf={bundle.get('classifier_confidence')})")
    typer.echo(f"Profile consensus: {bundle.get('profile_consensus_top')} "
               f"(conf={bundle.get('profile_consensus_confidence')}, "
               f"margin={bundle.get('profile_consensus_margin')})")
    typer.echo()
    typer.echo("Evidence log:")
    for entry in _json.loads(bundle['evidence_log_json'] or '[]'):
        typer.echo(f"  - {entry}")


def report_document_identity_summary_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Print confidence distribution and signal coverage across all identity bundles.

    Used by the Phase 1D quality assessment.
    """
    import json as _json

    from duke_rates.document_intelligence.document_identity import (
        fetch_identity_summary,
    )

    settings, _ = _bootstrap()
    summary = fetch_identity_summary(settings.database_path)

    if as_json:
        typer.echo(_json.dumps(summary, indent=2))
        return

    typer.echo(f"\n=== Document Identity Summary ===")
    typer.echo(f"Total identity rows: {summary['total']}")
    typer.echo()
    typer.echo("Confidence distribution:")
    for bucket, cnt in summary["confidence_buckets"].items():
        bar = "#" * int(cnt / max(1, summary["total"]) * 40)
        typer.echo(f"  {bucket:<15} {cnt:>5}  {bar}")
    typer.echo()
    typer.echo("Signal coverage:")
    for sig, cnt in summary["coverage"].items():
        pct = (cnt / max(1, summary["total"])) * 100
        typer.echo(f"  {sig:<32} {cnt:>5}  ({pct:.1f}%)")


def report_document_identity_quality_nc(
    write_report: bool = typer.Option(
        True,
        "--write-report/--no-write-report",
        help="Write JSON to docs/reports/document_identity_quality/<timestamp>.json.",
    ),
) -> None:
    """Cross-check identity confidence against actual parse outcomes (Phase 1D).

    Validates the weights chosen in document_identity.py by comparing:
      - High-confidence docs vs parse_attempt success rate
      - Low-confidence docs vs wrong_profile/unknown diagnoses
      - High-confidence docs where the profile_consensus disagrees with the
        currently-assigned parser_profile (highest-value reassignment leads)

    Writes a JSON report and prints a console summary.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path

    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        # Bucket -> (parsed_with_charges, total)
        outcome_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN di.overall_confidence >= 0.85 THEN 'high (>=0.85)'
                    WHEN di.overall_confidence >= 0.5  THEN 'mid (0.5-0.85)'
                    ELSE 'low (<0.5)'
                END AS bucket,
                pal.status,
                COUNT(*) AS cnt
            FROM document_identity di
            LEFT JOIN parse_attempt_logs pal ON pal.source_pdf = di.source_pdf
            GROUP BY 1, 2
            """
        ).fetchall()
        # Bucket -> failure_type counts (via diagnoses)
        diag_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN di.overall_confidence >= 0.85 THEN 'high (>=0.85)'
                    WHEN di.overall_confidence >= 0.5  THEN 'mid (0.5-0.85)'
                    ELSE 'low (<0.5)'
                END AS bucket,
                ld.failure_type,
                COUNT(*) AS cnt
            FROM document_identity di
            JOIN parse_attempt_logs pal ON pal.source_pdf = di.source_pdf
            JOIN llm_parse_diagnostics ld ON ld.parse_attempt_id = pal.id
            GROUP BY 1, 2
            """
        ).fetchall()
        # High-confidence routing disagreements (highest-value reassignment)
        disagreement_rows = conn.execute(
            """
            SELECT di.source_pdf,
                   di.overall_confidence,
                   di.profile_consensus_top,
                   pal.parser_profile AS current_profile
            FROM document_identity di
            JOIN parse_attempt_logs pal ON pal.source_pdf = di.source_pdf
            WHERE di.overall_confidence >= 0.85
              AND di.profile_consensus_top IS NOT NULL
              AND di.profile_consensus_top != COALESCE(pal.parser_profile, '')
            ORDER BY di.overall_confidence DESC
            LIMIT 50
            """
        ).fetchall()
    finally:
        conn.close()

    # Group outcomes by bucket
    outcomes: dict[str, dict[str, int]] = {}
    for r in outcome_rows:
        outcomes.setdefault(r["bucket"], {})[r["status"] or "no_attempt"] = r["cnt"]
    diagnoses: dict[str, dict[str, int]] = {}
    for r in diag_rows:
        diagnoses.setdefault(r["bucket"], {})[r["failure_type"]] = r["cnt"]
    disagreements = [dict(r) for r in disagreement_rows]

    payload = {
        "generated_at": _dt.now(_tz.utc).isoformat(),
        "outcomes_by_bucket": outcomes,
        "diagnoses_by_bucket": diagnoses,
        "high_confidence_routing_disagreements": disagreements,
        "high_confidence_disagreement_count": len(disagreements),
    }

    if write_report:
        report_dir = _Path("docs/reports/document_identity_quality")
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        path = report_dir / f"{ts}.json"
        path.write_text(_json.dumps(payload, indent=2, default=str), encoding="utf-8")
        typer.echo(f"Report written: {path}")

    typer.echo("\n=== Identity Confidence vs Parse Outcomes ===")
    for bucket in ("high (>=0.85)", "mid (0.5-0.85)", "low (<0.5)"):
        statuses = outcomes.get(bucket, {})
        total = sum(statuses.values())
        parsed = statuses.get("parsed", 0)
        rate = (parsed / total * 100) if total else 0.0
        typer.echo(f"  {bucket:<18} total={total:>5} parsed={parsed:>5} ({rate:.1f}%)")

    typer.echo("\n=== Identity Confidence vs Diagnosed Failures ===")
    for bucket in ("high (>=0.85)", "mid (0.5-0.85)", "low (<0.5)"):
        d = diagnoses.get(bucket, {})
        if not d:
            typer.echo(f"  {bucket}: (no diagnosed failures)")
            continue
        top = sorted(d.items(), key=lambda kv: kv[1], reverse=True)
        snip = ", ".join(f"{k}={v}" for k, v in top[:5])
        typer.echo(f"  {bucket:<18} {snip}")

    typer.echo(f"\n=== High-Confidence Routing Disagreements: {len(disagreements)} ===")
    for d in disagreements[:10]:
        typer.echo(
            f"  conf={d['overall_confidence']:.2f} "
            f"current={d['current_profile']!r:<35} "
            f"recommended={d['profile_consensus_top']!r}"
        )


def report_document_fingerprint_clusters_nc(
    limit: int = typer.Option(40, "--limit", help="Max clusters to show."),
    min_size: int = typer.Option(2, "--min-size", help="Hide clusters with fewer than N members."),
    sample_pdfs: int = typer.Option(2, "--sample-pdfs", help="Show up to N example PDFs per cluster."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
) -> None:
    """Group fingerprinted documents by their coarse cluster signature.

    Surfaces document types we encountered across the corpus — including
    types we don't yet have classifiers for. Each row shows a cluster
    signature (e.g. ``DOCKET_HEADER|pages=51-150|vocab=tariff,schedule,docket|tables=0``)
    plus the count of members and a couple of example PDF paths.

    Use this to spot new document types that deserve a classifier or a
    parser path. Clusters with size >= N but no associated extractions
    are particularly interesting — that's content we're seeing but not
    using.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT cluster_signature_v1, COUNT(*) AS members,
                   AVG(page_count) AS avg_pages,
                   AVG(text_chars) AS avg_chars,
                   SUM(has_tables) AS table_members,
                   SUM(has_scanned_pages) AS scanned_members
            FROM document_fingerprints_v2
            WHERE cluster_signature_v1 IS NOT NULL
            GROUP BY cluster_signature_v1
            HAVING members >= ?
            ORDER BY members DESC
            """,
            (min_size,),
        ).fetchall()
        clusters = []
        for row in rows[:limit]:
            samples = conn.execute(
                """
                SELECT source_pdf, page_count, leaf_numbers_json, schedule_codes_json
                FROM document_fingerprints_v2
                WHERE cluster_signature_v1 = ?
                LIMIT ?
                """,
                (row["cluster_signature_v1"], sample_pdfs),
            ).fetchall()
            clusters.append(
                {
                    "signature": row["cluster_signature_v1"],
                    "members": row["members"],
                    "avg_pages": round(row["avg_pages"] or 0, 1),
                    "avg_chars": int(row["avg_chars"] or 0),
                    "table_members": row["table_members"] or 0,
                    "scanned_members": row["scanned_members"] or 0,
                    "samples": [
                        {
                            "pdf": s["source_pdf"],
                            "pages": s["page_count"],
                        }
                        for s in samples
                    ],
                }
            )
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM document_fingerprints_v2"
        ).fetchone()["n"]
    finally:
        conn.close()

    report = {
        "total_fingerprints": total,
        "clusters_shown": len(clusters),
        "clusters": clusters,
    }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(f"total_fingerprints={total}")
    typer.echo(f"clusters shown (size >= {min_size}): {len(clusters)}")
    typer.echo("")
    for c in clusters:
        typer.echo(
            f"  [{c['members']:4d}] {c['signature']}"
            f"  (avg_pages={c['avg_pages']}, tables={c['table_members']}, scanned={c['scanned_members']})"
        )
        for s in c["samples"]:
            typer.echo(f"        sample: pages={s['pages']:>4} {s['pdf']}")




# =============================================================================
# Phase 2 — Routing tier system
# Plan ref: docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md §5
# (commands were dropped during the cli-split refactor; restored here)
# =============================================================================


def populate_routing_tier_nc(
    limit: int = typer.Option(
        0, "--limit", help="Process at most N identity rows (0 = unlimited).",
    ),
) -> None:
    """Label every ``document_identity`` row with a routing tier."""
    from duke_rates.document_intelligence.routing_tier import TierAggregator

    settings, _ = _bootstrap()
    agg = TierAggregator(settings.database_path)
    n = agg.label_all(limit=limit if limit > 0 else None)
    typer.echo(f"document_routing_tier: labeled/refreshed {n} rows")


def report_routing_tier_nc(
    sample_per_tier: int = typer.Option(
        5, "--sample-per-tier", help="Show N rationale samples per tier.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Show tier distribution and a few example rationales per tier."""
    import sqlite3 as _sql

    from duke_rates.document_intelligence.routing_tier import (
        fetch_tier_distribution,
    )

    settings, _ = _bootstrap()
    dist = fetch_tier_distribution(settings.database_path)
    total = sum(dist.values())

    samples: dict[int, list[dict[str, Any]]] = {}
    conn = _sql.connect(settings.database_path)
    conn.row_factory = _sql.Row
    try:
        for tier in (1, 2, 3):
            rows = conn.execute(
                """
                SELECT source_pdf, overall_confidence, profile_consensus_top,
                       profile_consensus_margin, rationale
                FROM document_routing_tier
                WHERE tier = ?
                ORDER BY overall_confidence DESC LIMIT ?
                """,
                (tier, sample_per_tier),
            ).fetchall()
            samples[tier] = [dict(r) for r in rows]
    finally:
        conn.close()

    payload = {"distribution": dist, "total": total, "samples": samples}
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    typer.echo(f"\n=== Routing Tier Distribution ===")
    typer.echo(f"Total docs labeled: {total}")
    for tier in (1, 2, 3):
        cnt = dist.get(tier, 0)
        pct = (cnt / total * 100) if total else 0.0
        bar = "#" * int(cnt / max(1, total) * 40)
        typer.echo(f"  TIER_{tier}: {cnt:>5}  ({pct:5.1f}%)  {bar}")
    typer.echo()
    for tier in (1, 2, 3):
        typer.echo(f"--- TIER_{tier} samples ---")
        if not samples[tier]:
            typer.echo("  (no rows)")
            continue
        for s in samples[tier]:
            typer.echo(
                f"  conf={s['overall_confidence']:.2f} "
                f"top={(s['profile_consensus_top'] or '?'):<30} "
                f"margin={s['profile_consensus_margin']}"
            )
            typer.echo(f"    {s['rationale']}")


def report_routing_tier_validation_nc(
    write_report: bool = typer.Option(
        True, "--write-report/--no-write-report",
        help="Write JSON to docs/reports/routing_tier_validation/<timestamp>.json.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit full report as JSON."),
) -> None:
    """Cross-check tier predictions against actual parse outcomes."""
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.routing_tier import (
        build_tier_validation_report,
    )

    settings, _ = _bootstrap()
    report = build_tier_validation_report(settings.database_path)
    report["generated_at"] = _dt.now(_tz.utc).isoformat()

    if write_report:
        report_dir = _Path("docs/reports/routing_tier_validation")
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        path = report_dir / f"{ts}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        typer.echo(f"Report written: {path}")

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    s = report["summary"]
    typer.echo(f"\n=== Routing Tier Validation ===")
    typer.echo(
        f"Tier 1: {s['tier1_count']:>6} attempts  parsed-with-charges {s['tier1_parsed_rate']:>5.1%}  "
        f"template_bugs={s['tier1_extraction_failure_count']}"
    )
    typer.echo(f"Tier 2: {s['tier2_count']:>6} attempts")
    typer.echo(
        f"Tier 3: {s['tier3_count']:>6} attempts  parsed-with-charges {s['tier3_parsed_rate']:>5.1%}  "
        f"unexpected_successes={s['tier3_unexpected_success_count']}"
    )

    typer.echo("\n--- Per-Tier Status Distribution (top 4) ---")
    for tier, bucket in sorted(report["tier_outcomes"].items()):
        top = sorted(bucket["by_status"].items(), key=lambda kv: -kv[1])[:4]
        snip = ", ".join(f"{k}={v}" for k, v in top)
        typer.echo(f"  TIER_{tier}: {snip}")

    typer.echo("\n--- Per-Tier Diagnosed Failure Types ---")
    for tier, fts in sorted(report["tier_diagnoses"].items()):
        if not fts:
            typer.echo(f"  TIER_{tier}: (none)")
            continue
        top = sorted(fts.items(), key=lambda kv: -kv[1])[:5]
        snip = ", ".join(f"{k}={v}" for k, v in top)
        typer.echo(f"  TIER_{tier}: {snip}")


# =============================================================================
# Phase 3 — Tier 1 binder (dry-run) + comparison
# Plan ref: docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md §6
# (commands were dropped during the cli-split refactor; restored here)
# =============================================================================


def bind_tier1_proposals_nc(
    limit: int = typer.Option(
        0, "--limit", help="Process at most N Tier 1 docs (0 = unlimited).",
    ),
) -> None:
    """Record Tier 1 binding proposals (Phase 3B, dry-run)."""
    from duke_rates.document_intelligence.tier1_binder import Tier1Binder

    settings, _ = _bootstrap()
    binder = Tier1Binder(settings.database_path)
    counts = binder.bind_all(limit=limit if limit > 0 else None)
    nonzero = {k: v for k, v in counts.items() if v}
    typer.echo(f"tier1_binding: {nonzero}")


def report_tier1_binding_nc(
    sample: int = typer.Option(15, "--sample", help="Disagreement samples to print."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Print Tier 1 binding summary."""
    from duke_rates.document_intelligence.tier1_binder import (
        fetch_binding_summary,
    )

    settings, _ = _bootstrap()
    summary = fetch_binding_summary(settings.database_path)

    if as_json:
        typer.echo(json.dumps(summary, indent=2, default=str))
        return

    typer.echo("\n=== Tier 1 Binding Summary ===")
    typer.echo("Status counts:")
    for k, v in summary["status_counts"].items():
        typer.echo(f"  {k:<15} {v}")
    typer.echo("\nBy proposed profile:")
    for k, v in summary["by_profile"].items():
        typer.echo(f"  {k:<40} {v}")
    typer.echo("\nAgreement with current parser_profile:")
    for k, v in summary["agreement"].items():
        typer.echo(f"  {k:<10} {v}")
    typer.echo(f"\n--- Disagreement samples (top {sample} by confidence) ---")
    for r in summary["disagreement_samples"][:sample]:
        typer.echo(
            f"  conf={r['overall_confidence']:.2f}  "
            f"current={(r['current_parser_profile'] or '?'):<28} "
            f"-> proposed={(r['proposed_profile'] or '?')}"
        )


def report_tier1_binding_comparison_nc(
    write_report: bool = typer.Option(
        True, "--write-report/--no-write-report",
        help="Write JSON to docs/reports/tier1_binding_comparison/<timestamp>.json.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON to stdout."),
) -> None:
    """Run the Phase 3C comparison report."""
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.tier1_binder import (
        build_comparison_report,
    )

    settings, _ = _bootstrap()
    report = build_comparison_report(settings.database_path)
    report["generated_at"] = _dt.now(_tz.utc).isoformat()

    if write_report:
        report_dir = _Path("docs/reports/tier1_binding_comparison")
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        path = report_dir / f"{ts}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        typer.echo(f"Report written: {path}")

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("\n=== Tier 1 Binding Comparison (Phase 3C) ===")
    t = report["totals"]
    typer.echo(
        f"Tier 1 docs total: {t['tier1_total']}  "
        f"proposed={t.get('proposed', 0)}  refused={t.get('refused', 0)}  "
        f"no_consensus={t.get('no_consensus', 0)}"
    )
    typer.echo(
        f"Agreement: {report['agreement_count']}  "
        f"Disagreement: {report['disagreement_count']}  "
        f"No current attempt: {report['no_current_count']}"
    )
    typer.echo(f"Agreement rate:    {report['agreement_rate']:.1%}")
    typer.echo(
        f"Disagreement rate: {report['disagreement_rate']:.1%}  "
        f"(plan threshold: <5%)"
    )
    typer.echo("\nDisagreements by kind:")
    for k, v in report["disagreements_by_kind"].items():
        typer.echo(f"  {k:<25} {v}")
    typer.echo("\nTop flips (current -> proposed):")
    for f in report["top_flips"][:10]:
        typer.echo(
            f"  {(f['current'] or '?'):<30} -> {(f['proposed'] or '?'):<30} {f['count']}"
        )
    typer.echo("\nWhen binder disagrees, current parse outcome is:")
    for k, v in report["parsed_outcome_when_disagree"].items():
        typer.echo(f"  {k:<22} {v}")


# =============================================================================
# Phase 4 — Per-document rules (Tier 3 path)
# Plan ref: docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md §7
# =============================================================================


def generate_per_doc_rules_nc(
    limit: int = typer.Option(5, "--limit", help="Max Tier 3 docs to process this run."),
) -> None:
    """Generate and validate per-document rules for Tier 3 docs (Phase 4B).

    Picks Tier 3 docs whose ``document_identity`` row has signal evidence
    and no accepted/pending rule yet. Asks the LLM for a doc-scoped regex,
    validates against the target doc + 5 closest siblings (Jaccard on
    schedule/rider/filename signals), and persists accepted rules to
    ``document_specific_rules``.

    Skips the corpus-wide false-positive check entirely — per-doc rules
    can't leak.
    """
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.per_doc_rule_generator import (
        PerDocRuleGenerator,
    )

    settings, _ = _bootstrap()
    orch = OllamaOrchestrator(db_path=settings.database_path)
    gen = PerDocRuleGenerator(orch, settings.database_path)
    outcomes = gen.generate_batch(limit=limit)

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    typer.echo(f"per-doc rule generation: processed {len(outcomes)}; status counts: {counts}")
    typer.echo("\nPer-doc outcomes:")
    for o in outcomes:
        s = o.to_summary()
        typer.echo(
            f"  doc={s['document_identity_id']:>4}  status={s['status']:<10} "
            f"rule_id={s['rule_id']}  target_matches={s['target_matches']}  "
            f"siblings={s['siblings_tested']}  oor={s['sibling_out_of_range']}"
        )
        if s["reason"]:
            typer.echo(f"    {s['reason']}")


def report_per_doc_rules_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Status distribution for ``document_specific_rules``."""
    from duke_rates.document_intelligence.document_specific_rules import (
        fetch_status_summary,
    )

    settings, _ = _bootstrap()
    summary = fetch_status_summary(settings.database_path)

    if as_json:
        typer.echo(json.dumps(summary, indent=2, default=str))
        return

    typer.echo(f"\n=== Per-Doc Rules Summary ===")
    typer.echo(f"Total rules: {summary['total']}")
    typer.echo(f"Promotion candidates (accepted, >=3 siblings agree): {summary['promotion_candidates']}")
    typer.echo("\nBy status:")
    for k, v in summary["by_status"].items():
        typer.echo(f"  {k:<12} {v}")
    if summary["sample_pending"]:
        typer.echo("\nRecent pending samples:")
        for r in summary["sample_pending"]:
            typer.echo(
                f"  id={r['id']:>4}  doc_id={r['document_identity_id']:>4}  "
                f"target_field={(r['target_field'] or '-'):<14} "
                f"regex={r['regex_preview']!r}"
            )


def detect_rule_promotions_nc() -> None:
    """Run the Phase 4C promotion-candidate detector.

    Scans accepted ``document_specific_rules`` for clusters of regexes
    that appear in 3+ documents in the same family/template. Records
    promotion candidates in ``template_promotion_candidates`` for human
    review. Idempotent — running twice doesn't create duplicates.

    This command does NOT modify ``profile_templates.yaml`` or any
    parser code. It surfaces candidates for manual approval.
    """
    from duke_rates.document_intelligence.rule_promotion import PromotionDetector

    settings, _ = _bootstrap()
    detector = PromotionDetector(settings.database_path)
    candidates = detector.detect_all()
    typer.echo(f"detected {len(candidates)} promotion candidate(s)")
    for c in candidates[:20]:
        typer.echo(
            f"  template={c.target_template:<35} field={(c.target_field or '-'):<18} "
            f"size={c.cluster_size}  regex={c.suggested_regex[:60]!r}"
        )


def report_rule_promotions_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Show promotion-candidate summary for human review."""
    from duke_rates.document_intelligence.rule_promotion import (
        fetch_promotion_summary,
    )

    settings, _ = _bootstrap()
    summary = fetch_promotion_summary(settings.database_path)

    if as_json:
        typer.echo(json.dumps(summary, indent=2, default=str))
        return

    typer.echo(f"\n=== Template Promotion Candidates (Phase 4C) ===")
    typer.echo(f"Total candidates: {summary['total']}")
    typer.echo("\nBy status:")
    for k, v in summary["by_status"].items():
        typer.echo(f"  {k:<12} {v}")
    typer.echo("\nBy target template:")
    for k, v in summary["by_template"].items():
        typer.echo(f"  {k:<40} {v}")
    typer.echo("\nTop clusters by size:")
    for r in summary["top_clusters"]:
        typer.echo(
            f"  size={r['cluster_size']:>3}  "
            f"template={(r['target_template'] or '?'):<35} "
            f"field={(r['target_field'] or '-'):<14} "
            f"regex={r['regex_preview']!r}"
        )


# =============================================================================
# Phase 6 — sub-document section intelligence
# =============================================================================


def populate_document_sections_nc(
    limit: int = typer.Option(
        0, "--limit", help="Process at most N source_pdfs (0 = unlimited).",
    ),
) -> None:
    """Build sub-document section bundles from span + page-level evidence (Phase 6AB).

    Reads from ``ncuc_span_artifacts`` and ``ncuc_page_artifacts``, seeds
    section boundaries, refines via page-level signals, classifies section
    types, and persists the result to ``document_sections``.

    Deterministic and idempotent — running multiple times is safe.
    """
    from duke_rates.document_intelligence.section_aggregator import (
        DocumentSectionAggregator,
    )

    settings = get_settings()
    configure_logging(settings.log_level)
    agg = DocumentSectionAggregator(settings.database_path)
    n = agg.populate_all(limit=limit if limit > 0 else None)
    typer.echo(f"document_sections: populated/refreshed {n} documents")


def report_document_sections_nc(
    source_pdf: str = typer.Option(
        ..., "--source-pdf", help="Path of the source PDF to inspect.",
    ),
    min_confidence: float = typer.Option(
        0.0, "--min-confidence", help="Hide sections below this confidence (0.0-1.0).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit data as JSON."),
) -> None:
    """Print the persisted section bundles for one document.

    Shows section boundaries, types, codes, and confidence scores
    for every section in the document.
    """
    from duke_rates.document_intelligence.document_sections import (
        fetch_sections,
    )

    settings = get_settings()
    configure_logging(settings.log_level)
    sections = fetch_sections(
        settings.database_path, source_pdf,
        min_confidence=min_confidence if min_confidence > 0 else None,
    )

    if as_json:
        typer.echo(json.dumps(
            [s.__dict__ for s in sections] if sections else [],
            indent=2, default=str,
        ))
        return

    typer.echo(f"\n=== Document Sections ===")
    typer.echo(f"PDF: {source_pdf}")
    typer.echo(f"Sections: {len(sections)}")
    for s in sections:
        typer.echo(
            f"  [{s.section_index}] pages {s.start_page}-{s.end_page} "
            f"type={s.section_type.value} conf={s.overall_confidence:.2f}"
        )
        if s.schedule_codes:
            typer.echo(f"       schedule_codes={s.schedule_codes}")
        if s.rider_codes:
            typer.echo(f"       rider_codes={s.rider_codes}")
        if s.leaf_numbers:
            typer.echo(f"       leaf_numbers={s.leaf_numbers}")


def report_document_sections_summary_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Print aggregate statistics for the document_sections table.

    Shows total sections, total documents with sections, type distribution,
    and confidence histogram.
    """
    from duke_rates.document_intelligence.document_sections import (
        fetch_sections_summary,
    )

    settings = get_settings()
    configure_logging(settings.log_level)
    summary = fetch_sections_summary(settings.database_path)

    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        return

    typer.echo(f"\n=== Document Sections Summary ===")
    typer.echo(f"Total sections:     {summary['total_sections']}")
    typer.echo(f"Total documents:    {summary['total_documents']}")
    typer.echo()
    typer.echo("By section type:")
    total = max(1, summary["total_sections"])
    for t, n in summary["type_distribution"].items():
        bar = "#" * int(n / total * 40)
        typer.echo(f"  {t:<20} {n:>5}  {bar}")
    typer.echo()
    typer.echo("Confidence distribution:")
    for bucket, n in summary["confidence_histogram"].items():
        bar = "#" * int(n / total * 40) if total else ""
        typer.echo(f"  {bucket:<10} {n:>5}  {bar}")


def analyze_document_structure_nc(
    limit: int = typer.Option(
        5, "--limit", help="Max candidate documents to analyze this run.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List candidates without calling the LLM.",
    ),
    max_pages: int = typer.Option(
        40, "--max-pages", help="Max pages per document to include in the LLM prompt.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit results as JSON."),
) -> None:
    """Use a local LLM to review and refine deterministic section boundaries (Phase 6C).

    Analyzes documents where sections have low confidence (<0.5) or where
    over-splitting (>8 sections) is suspected. The LLM reviews page-level
    signals and proposes boundary refinements. Agreements boost confidence;
    disagreements flag sections as ``needs_review``.

    Idempotent — re-running re-evaluates all candidates.
    """
    from pathlib import Path as _Path

    from duke_rates.document_intelligence.document_structure_analyst import (
        DocumentStructureAnalyst,
    )
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )

    settings, _ = _bootstrap()
    db_path = _Path(settings.database_path)

    orch = OllamaOrchestrator(db_path=settings.database_path)
    analyst = DocumentStructureAnalyst(
        orch, db_path, max_pages=max_pages,
    )

    if dry_run:
        candidates = analyst.select_candidates(limit=limit)
        typer.echo(f"Candidates: {len(candidates)}")
        for pdf in candidates[:10]:
            typer.echo(f"  {pdf}")
        return

    ok, err = orch.health_probe("document_structure_analyst")
    if not ok:
        typer.echo(
            f"ERROR: document_structure_analyst health check failed: {err}",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"  document_structure_analyst -> "
        f"{orch.roles['document_structure_analyst'].primary} (OK)"
    )

    results = analyst.analyze_batch(limit=limit)

    if as_json:
        typer.echo(json.dumps(results, indent=2, default=str))
        return

    typer.echo(f"\n=== Document Structure Analysis ===")
    typer.echo(f"Candidates: {results['candidates']}")
    typer.echo(f"Analyzed:   {results['analyzed']}")
    typer.echo(f"Merged:     {results['merged']} total section updates")
    typer.echo(f"Failed:     {results['failed']}")
    typer.echo()
    for pdf, info in results["per_document"].items():
        status = info["status"]
        if status == "ok":
            typer.echo(
                f"  {pdf}: {info['sections_proposed']} sections proposed, "
                f"{info['sections_updated']} updated, "
                f"quality={info['overall_quality']:.2f}"
            )
        else:
            typer.echo(f"  {pdf}: {status}")


def aggregate_overnight_reports_nc(
    glob_pattern: str = typer.Option(
        "docs/reports/overnight_parse_improvement/*.json",
        "--glob",
        help="Glob pattern for report JSON files to aggregate.",
    ),
    since: str = typer.Option(
        "", "--since",
        help="Only include reports with started_at >= this ISO8601 datetime.",
    ),
    top_n: int = typer.Option(5, "--top", help="Top-N models / failure types to display."),
) -> None:
    """Roll up overnight-loop JSON reports into a single deltas summary.

    Reads every report matching --glob (after the optional --since cutoff),
    sums task throughput, surfaces failure-type movement, and lists the models
    that actually got used. Useful for "what did the loop do last night?"
    without having to open 30 individual JSON files.
    """
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    from glob import glob as _glob

    paths = sorted(_glob(glob_pattern))
    if not paths:
        typer.echo(f"No reports found matching: {glob_pattern}", err=True)
        raise typer.Exit(code=1)

    from datetime import timezone as _tz
    cutoff: _dt | None = None
    if since:
        try:
            cutoff = _dt.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            typer.echo(f"Invalid --since ISO8601: {since!r}", err=True)
            raise typer.Exit(code=1)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=_tz.utc)

    total = {
        "runs": 0,
        "runtime_seconds": 0.0,
        "documents_analyzed": 0,
        "regex_suggestions_created": 0,
        "normalization_suggestions_created": 0,
        "schema_extractions_attempted": 0,
        "schema_extractions_validated": 0,
    }
    task_totals: dict[str, dict[str, int]] = {}
    failures_by_type: dict[str, int] = {}
    roles_seen: dict[str, dict[str, int]] = {}
    dsr_by_status: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    first_run: str | None = None
    last_run: str | None = None

    for path in paths:
        try:
            data = json.loads(_Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue

        started = data.get("started_at") or ""
        if cutoff and started:
            try:
                started_dt = _dt.fromisoformat(started.replace("Z", "+00:00"))
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=_tz.utc)
                if started_dt < cutoff:
                    continue
            except ValueError:
                pass

        total["runs"] += 1
        total["runtime_seconds"] += float(data.get("runtime_seconds") or 0)
        total["documents_analyzed"] += int(data.get("documents_analyzed") or 0)
        total["regex_suggestions_created"] += int(data.get("regex_suggestions_created") or 0)
        total["normalization_suggestions_created"] += int(data.get("normalization_suggestions_created") or 0)
        total["schema_extractions_attempted"] += int(data.get("schema_extractions_attempted") or 0)
        total["schema_extractions_validated"] += int(data.get("schema_extractions_validated") or 0)

        for task, stats in (data.get("task_stats") or {}).items():
            tt = task_totals.setdefault(task, {})
            for k, v in stats.items():
                if isinstance(v, int):
                    tt[k] = tt.get(k, 0) + v

        for failure, cnt in (data.get("parse_failures_by_type") or {}).items():
            failures_by_type[failure] = failures_by_type.get(failure, 0) + int(cnt)

        for role, model in (data.get("roles_used") or {}).items():
            r = roles_seen.setdefault(role, {})
            r[model] = r.get(model, 0) + 1

        for status, cnt in (data.get("document_specific_rules_by_status") or {}).items():
            dsr_by_status[status] = max(dsr_by_status.get(status, 0), int(cnt))

        reason = data.get("stop_reason") or "unknown"
        stop_reasons[reason] = stop_reasons.get(reason, 0) + 1

        if first_run is None or started < first_run:
            first_run = started
        if last_run is None or started > last_run:
            last_run = started

    if total["runs"] == 0:
        typer.echo(f"No reports passed the --since filter.", err=True)
        raise typer.Exit(code=1)

    typer.echo("=" * 70)
    typer.echo(f"Overnight Reports Summary - {total['runs']} run(s)")
    typer.echo(f"  window: {first_run} -> {last_run}")
    typer.echo(f"  total runtime: {total['runtime_seconds'] / 3600:.2f} h "
               f"({total['runtime_seconds']:.0f}s)")
    typer.echo("=" * 70)
    typer.echo("")
    typer.echo("Throughput:")
    typer.echo(f"  documents analyzed:       {total['documents_analyzed']}")
    typer.echo(f"  regex suggestions:        {total['regex_suggestions_created']}")
    typer.echo(f"  normalization rules:      {total['normalization_suggestions_created']}")
    typer.echo(f"  schema extractions:       {total['schema_extractions_attempted']} "
               f"(validated: {total['schema_extractions_validated']})")
    typer.echo("")

    typer.echo("Per-task totals:")
    for task in sorted(task_totals):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(task_totals[task].items()) if v)
        typer.echo(f"  {task}: {parts or '(no activity)'}")
    typer.echo("")

    typer.echo("Stop reasons:")
    for reason, cnt in sorted(stop_reasons.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {reason}: {cnt}")
    typer.echo("")

    typer.echo(f"Top {top_n} failure types (summed across runs):")
    sorted_failures = sorted(failures_by_type.items(), key=lambda kv: -kv[1])
    for failure, cnt in sorted_failures[:top_n]:
        typer.echo(f"  {failure}: {cnt}")
    typer.echo("")

    typer.echo("Models used (per role):")
    for role, models in sorted(roles_seen.items()):
        for model, cnt in sorted(models.items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {role}: {model} ({cnt} run(s))")
    typer.echo("")

    if dsr_by_status:
        typer.echo("Document-specific rules (max snapshot across runs):")
        for status, cnt in sorted(dsr_by_status.items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {status}: {cnt}")


def register_parse_refactor_commands(app: typer.Typer) -> None:
    """Register parsing-refactor and document-identity command group."""
    app.command("analyze-parse-failures-nc")(analyze_parse_failures_nc)
    app.command("suggest-regex-fixes-nc")(suggest_regex_fixes_nc)
    app.command("validate-regex-suggestions-nc")(validate_regex_suggestions_nc)
    app.command("run-llm-parse-fallback-nc")(run_llm_parse_fallback_nc)
    app.command("run-overnight-parse-improvement-nc")(run_overnight_parse_improvement_nc)
    app.command("aggregate-overnight-reports-nc")(aggregate_overnight_reports_nc)
    app.command("report-wrong-profile-diagnostics-nc")(report_wrong_profile_diagnostics_nc)
    app.command("report-profile-recommendations-nc")(report_profile_recommendations_nc)
    app.command("populate-document-identity-nc")(populate_document_identity_nc)
    app.command("report-document-identity-nc")(report_document_identity_nc)
    app.command("report-document-identity-summary-nc")(report_document_identity_summary_nc)
    app.command("report-document-identity-quality-nc")(report_document_identity_quality_nc)
    app.command("report-document-fingerprint-clusters-nc")(report_document_fingerprint_clusters_nc)
    # Phase 2 routing tier
    app.command("populate-routing-tier-nc")(populate_routing_tier_nc)
    app.command("report-routing-tier-nc")(report_routing_tier_nc)
    app.command("report-routing-tier-validation-nc")(report_routing_tier_validation_nc)
    # Phase 3 tier1 binder
    app.command("bind-tier1-proposals-nc")(bind_tier1_proposals_nc)
    app.command("report-tier1-binding-nc")(report_tier1_binding_nc)
    app.command("report-tier1-binding-comparison-nc")(report_tier1_binding_comparison_nc)
    # Phase 4 per-doc rules
    app.command("generate-per-doc-rules-nc")(generate_per_doc_rules_nc)
    app.command("report-per-doc-rules-nc")(report_per_doc_rules_nc)
    app.command("detect-rule-promotions-nc")(detect_rule_promotions_nc)
    app.command("report-rule-promotions-nc")(report_rule_promotions_nc)
    # Phase 6 sub-document sections
    app.command("populate-document-sections-nc")(populate_document_sections_nc)
    app.command("report-document-sections-nc")(report_document_sections_nc)
    app.command("report-document-sections-summary-nc")(report_document_sections_summary_nc)
    app.command("analyze-document-structure-nc")(analyze_document_structure_nc)
