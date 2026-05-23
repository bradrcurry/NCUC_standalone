"""Missing-doc workflow sub-app: search, run, promote, report, triage,
plan, execute, and remediate the NC missing-document remediation pipeline.

Wired into the main CLI as `duke-rates workflow <command>`.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from duke_rates.cli_commands._cli_utils import _bootstrap, _safe_cli_text


workflow_app = typer.Typer(help="NC missing-document workflow: search, fetch, import, bootstrap, reprocess, validate.")


def _stage_order_index(stage_name: str) -> int:
    stage_order = {
        "search": 0,
        "fetch": 1,
        "import": 2,
        "bootstrap_versions": 3,
        "queue_reprocess": 4,
        "process_reprocess": 5,
        "validate": 6,
    }
    return stage_order[stage_name]


@workflow_app.command("search-nc-missing-clean-docs")
def search_nc_missing_clean_docs_cmd(
    limit: int = typer.Option(20, "--limit", help="Maximum audit leads to search."),
    min_priority: str = typer.Option(
        "medium",
        "--min-priority",
        help="Minimum audit priority band to include: low, medium, high.",
    ),
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict to one family key.",
    ),
    structured_max: int = typer.Option(
        50,
        "--structured-max",
        help="Maximum structured portal documents per lead query.",
    ),
    keyword_max: int = typer.Option(
        20,
        "--keyword-max",
        help="Maximum keyword-discovery results per lead query.",
    ),
    max_candidates_per_family: int = typer.Option(
        12,
        "--max-candidates-per-family",
        help="Maximum persisted candidate rows per family after scoring.",
    ),
    enrich_details: bool = typer.Option(
        True,
        "--enrich-details/--no-enrich-details",
        help="Fetch detail-page ViewFile links for structured portal results.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run searches and emit a manifest without persisting DB records.",
    ),
    no_manifest: bool = typer.Option(
        False,
        "--no-manifest",
        help="Skip saving the combined search manifest under data/manifests/search_pipeline.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Search NCUC for likely missing clean historical documents and persist candidates for fetch/import."""
    from duke_rates.historical.ncuc.missing_clean_doc_search import (
        search_nc_missing_clean_documents,
    )

    settings, repository = _bootstrap()
    report = search_nc_missing_clean_documents(
        settings,
        repository,
        database_path=database,
        limit=limit,
        min_priority=min_priority,
        family_key=family_key,
        structured_max_results=structured_max,
        keyword_max_results=keyword_max,
        max_candidates_per_family=max_candidates_per_family,
        enrich_portal_details=enrich_details,
        persist=not dry_run,
        save_manifest=not no_manifest,
    )

    typer.echo(
        "NC missing clean doc search: "
        f"lead_count={report['lead_count']} "
        f"persisted_discovery={report['persisted_discovery_count']} "
        f"persisted_historical_leads={report['persisted_historical_lead_count']} "
        f"persisted_docket_leads={report['persisted_docket_lead_count']}"
    )
    if report.get("harvest_path"):
        typer.echo(f"  manifest: {report['harvest_path']}")
    for row in report["rows"][:10]:
        typer.echo(
            "  "
            f"{row['family_key']} "
            f"{row['missing_kind']} "
            f"priority={row['priority_band']}({row['priority_score']}) "
            f"candidates={row['candidate_count']}"
        )
        for candidate in row["top_candidates"][:3]:
            typer.echo(
                "    "
                f"[{candidate['source_type']}] "
                f"score={candidate['score']:.2f} "
                f"docket={candidate['docket_number'] or '-'} "
                f"date={candidate['filing_date'] or '-'} "
                f"title={candidate['title'] or '(untitled)'}"
            )


@workflow_app.command("run-nc-missing-doc")
def run_nc_missing_doc_workflow_cmd(
    from_stage: str = typer.Option(
        "search",
        "--from-stage",
        help="Workflow start stage: search, fetch, import, bootstrap_versions, queue_reprocess, process_reprocess, validate.",
    ),
    to_stage: str = typer.Option(
        "queue_reprocess",
        "--to-stage",
        help="Workflow end stage: search, fetch, import, bootstrap_versions, queue_reprocess, process_reprocess, validate.",
    ),
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict the workflow to one family key.",
    ),
    record_id: list[int] | None = typer.Option(
        None,
        "--record-id",
        help="Specific discovery record id(s) to resume from. Repeat for multiple.",
    ),
    historical_document_id: list[int] | None = typer.Option(
        None,
        "--historical-document-id",
        help="Specific historical document id(s) to resume from. Repeat for multiple.",
    ),
    limit: int = typer.Option(20, "--limit", help="Max items to process at each bounded stage."),
    min_priority: str = typer.Option(
        "medium",
        "--min-priority",
        help="Minimum missing-doc audit priority when starting at search.",
    ),
    structured_max: int = typer.Option(50, "--structured-max", help="Max structured portal results per lead."),
    keyword_max: int = typer.Option(20, "--keyword-max", help="Max keyword-search results per lead."),
    max_candidates_per_family: int = typer.Option(
        12,
        "--max-candidates-per-family",
        help="Max persisted search candidates per family.",
    ),
    auto_promote: bool = typer.Option(
        True,
        "--auto-promote/--no-auto-promote",
        help="Only auto-advance search hits that meet promotion thresholds.",
    ),
    promotion_min_ideality: str = typer.Option(
        "probable",
        "--promotion-min-ideality",
        help="Minimum search ideality for auto-promotion: possible, probable, ideal.",
    ),
    promotion_min_confidence: float = typer.Option(
        45.0,
        "--promotion-min-confidence",
        help="Minimum search confidence score for auto-promotion.",
    ),
    auto_promote_imported: bool = typer.Option(
        True,
        "--auto-promote-imported/--no-auto-promote-imported",
        help="Only queue imported historical documents that meet import-stage promotion thresholds.",
    ),
    import_promotion_min_family_score: float = typer.Option(
        24.0,
        "--import-promotion-min-family-score",
        help="Minimum importer family-match score for auto-queueing imported historical documents.",
    ),
    retry_failed_fetch: bool = typer.Option(
        False,
        "--retry-failed-fetch",
        help="Include failed discovery rows in fetch-stage retries.",
    ),
    reprocess_limit: int = typer.Option(
        25,
        "--reprocess-limit",
        help="Queue-processing batch size when running the process_reprocess stage.",
    ),
    requested_by: str = typer.Option(
        "workflow",
        "--requested-by",
        help="Requester label stored on queued work items.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Do not persist search results or queue mutations.",
    ),
) -> None:
    """Run the resumable NC missing-document workflow from discovery through queueing and parsing."""
    from duke_rates.historical.ncuc.missing_doc_workflow import (
        WORKFLOW_STAGES,
        run_nc_missing_doc_workflow,
    )

    if from_stage not in WORKFLOW_STAGES:
        raise typer.BadParameter(f"Unknown --from-stage {from_stage}. Expected one of: {', '.join(WORKFLOW_STAGES)}")
    if to_stage not in WORKFLOW_STAGES:
        raise typer.BadParameter(f"Unknown --to-stage {to_stage}. Expected one of: {', '.join(WORKFLOW_STAGES)}")

    settings, repository = _bootstrap()
    core_to_stage = to_stage
    if to_stage in {"process_reprocess", "validate"}:
        core_to_stage = "queue_reprocess"

    report = run_nc_missing_doc_workflow(
        settings,
        repository,
        from_stage=from_stage,
        to_stage=core_to_stage,
        family_key=family_key,
        discovery_record_ids=[int(item) for item in (record_id or [])],
        historical_document_ids=[int(item) for item in (historical_document_id or [])],
        limit=limit,
        min_priority=min_priority,
        structured_max_results=structured_max,
        keyword_max_results=keyword_max,
        max_candidates_per_family=max_candidates_per_family,
        persist_search=not dry_run,
        save_manifest=True,
        auto_promote_search_hits=auto_promote,
        promotion_min_ideality=promotion_min_ideality,
        promotion_min_confidence=promotion_min_confidence,
        auto_promote_imported_docs=auto_promote_imported,
        import_promotion_min_family_score=import_promotion_min_family_score,
        fetch_retry_failed=retry_failed_fetch,
        requested_by=requested_by,
    )

    typer.echo(
        "NC missing-doc workflow: "
        f"from={report['from_stage']} to={report['to_stage']} "
        f"discovery_ids={len(report['discovery_record_ids'])} "
        f"historical_ids={len(report['historical_document_ids'])}"
    )
    for stage_name, stage_report in report["stages"].items():
        typer.echo(f"  stage={stage_name} {json.dumps(stage_report, sort_keys=True, default=str)}")

    if dry_run:
        return

    if _stage_order_index(to_stage) >= _stage_order_index("process_reprocess"):
        from duke_rates.cli_commands.reprocess import process_reprocess_queue_nc
        process_reprocess_queue_nc(limit=reprocess_limit)
    if _stage_order_index(to_stage) >= _stage_order_index("validate"):
        from duke_rates.cli import validate_extraction_nc
        validate_extraction_nc()


@workflow_app.command("promote-nc-missing-doc-targets")
def promote_nc_missing_doc_targets_cmd(
    scope: str = typer.Option(
        "search_hits",
        "--scope",
        help="Promotion scope: search_hits or imported_docs.",
    ),
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict promotion to one family key.",
    ),
    record_id: list[int] | None = typer.Option(
        None,
        "--record-id",
        help="Specific discovery record id(s) to re-evaluate. Repeat for multiple.",
    ),
    historical_document_id: list[int] | None = typer.Option(
        None,
        "--historical-document-id",
        help="Specific historical document id(s) to re-evaluate. Repeat for multiple.",
    ),
    limit: int = typer.Option(20, "--limit", help="Max items to process."),
    auto_promote: bool = typer.Option(
        True,
        "--auto-promote/--no-auto-promote",
        help="Apply discovery-hit promotion gates instead of advancing everything.",
    ),
    promotion_min_ideality: str = typer.Option(
        "probable",
        "--promotion-min-ideality",
        help="Minimum search ideality for discovery-hit promotion: possible, probable, ideal.",
    ),
    promotion_min_confidence: float = typer.Option(
        45.0,
        "--promotion-min-confidence",
        help="Minimum search confidence score for discovery-hit promotion.",
    ),
    auto_promote_imported: bool = typer.Option(
        True,
        "--auto-promote-imported/--no-auto-promote-imported",
        help="Apply import-stage promotion gates for historical documents.",
    ),
    import_promotion_min_family_score: float = typer.Option(
        24.0,
        "--import-promotion-min-family-score",
        help="Minimum importer family-match score for imported-document promotion.",
    ),
    retry_failed_fetch: bool = typer.Option(
        False,
        "--retry-failed-fetch",
        help="Include failed discovery rows when re-promoting search hits.",
    ),
    requested_by: str = typer.Option(
        "workflow",
        "--requested-by",
        help="Requester label stored on queued work items.",
    ),
) -> None:
    """Re-evaluate already found missing-doc targets and advance only the items that now qualify."""
    from duke_rates.historical.ncuc.missing_doc_workflow import (
        PROMOTION_SCOPES,
        promote_nc_missing_doc_targets,
    )

    if scope not in PROMOTION_SCOPES:
        raise typer.BadParameter(f"Unknown --scope {scope}. Expected one of: {', '.join(PROMOTION_SCOPES)}")

    settings, repository = _bootstrap()
    report = promote_nc_missing_doc_targets(
        settings,
        repository,
        scope=scope,
        family_key=family_key,
        discovery_record_ids=[int(item) for item in (record_id or [])],
        historical_document_ids=[int(item) for item in (historical_document_id or [])],
        limit=limit,
        auto_promote_search_hits=auto_promote,
        promotion_min_ideality=promotion_min_ideality,
        promotion_min_confidence=promotion_min_confidence,
        auto_promote_imported_docs=auto_promote_imported,
        import_promotion_min_family_score=import_promotion_min_family_score,
        fetch_retry_failed=retry_failed_fetch,
        requested_by=requested_by,
    )

    typer.echo(
        "NC missing-doc promotion: "
        f"scope={scope} "
        f"discovery_ids={len(report['discovery_record_ids'])} "
        f"historical_ids={len(report['historical_document_ids'])}"
    )
    for stage_name, stage_report in report["stages"].items():
        typer.echo(f"  stage={stage_name} {json.dumps(stage_report, sort_keys=True, default=str)}")


@workflow_app.command("show-nc-missing-doc-status")
def show_nc_missing_doc_status_cmd(
    family_key: str | None = typer.Option(None, "--family-key", help="Explain workflow state for one family key."),
    record_id: int | None = typer.Option(None, "--record-id", help="Explain workflow state for one discovery record."),
    historical_document_id: int | None = typer.Option(
        None,
        "--historical-document-id",
        help="Explain workflow state for one historical document.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show workflow status and provenance for one missing-document target."""
    from duke_rates.historical.ncuc.missing_doc_status import (
        build_nc_missing_doc_status_report,
    )

    if not any([family_key, record_id is not None, historical_document_id is not None]):
        raise typer.BadParameter("Provide --family-key, --record-id, or --historical-document-id.")

    _, repository = _bootstrap()
    report = build_nc_missing_doc_status_report(
        repository,
        family_key=family_key,
        discovery_record_id=record_id,
        historical_document_id=historical_document_id,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("NC Missing Document Status")
    typer.echo(
        "  "
        f"family={summary['family_key'] or '-'} "
        f"discovery={summary['discovery_record_count']} "
        f"historical_leads={summary['historical_lead_count']} "
        f"docket_leads={summary['docket_lead_count']} "
        f"historical_docs={summary['historical_document_count']} "
        f"versions={summary['tariff_version_count']}"
    )
    typer.echo(
        "  "
        f"fetched_success={summary['fetched_success_count']} "
        f"queued_reprocess={summary['queued_reprocess_count']} "
        f"needs_review={summary['needs_review_count']} "
        f"versions_linked={summary['versions_with_historical_link_count']}"
    )

    if report["discovery_records"]:
        typer.echo("Discovery Records")
        for row in report["discovery_records"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} status={row['fetch_status']} "
                f"docket={row['docket_number'] or '-'} "
                f"date={row['filing_date'] or '-'} "
                f"title={_safe_cli_text(row['filing_title'] or '(untitled)')}"
            )

    if report["historical_documents"]:
        typer.echo("Historical Documents")
        for row in report["historical_documents"][:10]:
            latest_run = row.get("latest_processing_run") or {}
            latest_review = row.get("latest_review") or {}
            latest_queue = row.get("latest_reprocess_queue") or {}
            typer.echo(
                "  "
                f"id={row['id']} stage={row['current_stage']} "
                f"eff={row['effective_start'] or '-'} "
                f"pages={row['start_page']}-{row['end_page'] or row['start_page']} "
                f"profile={latest_run.get('parser_profile') or '-'} "
                f"run_status={latest_run.get('status') or '-'} "
                f"review={latest_review.get('outcome') or '-'} "
                f"queue={latest_queue.get('status') or '-'}"
            )


@workflow_app.command("report-nc-missing-doc-deferred")
def report_nc_missing_doc_deferred_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict the deferred report to one family key.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        help="Maximum deferred discovery rows and historical docs to include.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Report deferred missing-doc targets and group them by reason."""
    from duke_rates.historical.ncuc.missing_doc_deferred_report import (
        build_nc_missing_doc_deferred_report,
    )

    _, repository = _bootstrap()
    report = build_nc_missing_doc_deferred_report(
        repository,
        family_key=family_key,
        limit=limit,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("NC Missing Doc Deferred Report")
    typer.echo(
        "  "
        f"family={report['family_key'] or '-'} "
        f"deferred_discovery={summary['deferred_discovery_count']} "
        f"deferred_historical={summary['deferred_historical_count']}"
    )

    if summary["combined_reason_summary"]:
        typer.echo("Top Reasons")
        for row in summary["combined_reason_summary"][:10]:
            typer.echo(
                "  "
                f"{row['reason']} "
                f"count={row['count']} "
                f"discovery={row['discovery_count']} "
                f"historical={row['historical_count']}"
            )

    if report["deferred_discovery_records"]:
        typer.echo("Deferred Discovery Records")
        for row in report["deferred_discovery_records"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} "
                f"docket={row['docket_number'] or '-'} "
                f"date={row['filing_date'] or '-'} "
                f"ideality={row['search_ideality'] or '-'} "
                f"confidence={row['search_confidence_score'] if row['search_confidence_score'] is not None else '-'} "
                f"reasons={','.join(row['reasons']) or '-'}"
            )

    if report["deferred_historical_documents"]:
        typer.echo("Deferred Historical Documents")
        for row in report["deferred_historical_documents"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} "
                f"family={row['family_key'] or '-'} "
                f"eff={row['effective_start'] or '-'} "
                f"family_score={row['family_match_score'] if row['family_match_score'] is not None else '-'} "
                f"reasons={','.join(row['reasons']) or '-'}"
            )


@workflow_app.command("report-nc-missing-doc-triage")
def report_nc_missing_doc_triage_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict the triage report to one family key.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        help="Maximum triaged discovery rows and historical docs to include.",
    ),
    top: int | None = typer.Option(
        None,
        "--top",
        help="Only show the top ranked triage targets.",
    ),
    actionable_only: bool = typer.Option(
        False,
        "--actionable-only",
        help="Only include targets whose next action still requires intervention.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Report persisted missing-doc triage guidance for resumable agent work."""
    from duke_rates.historical.ncuc.missing_doc_triage_report import (
        build_nc_missing_doc_triage_report,
    )

    _, repository = _bootstrap()
    report = build_nc_missing_doc_triage_report(
        repository,
        family_key=family_key,
        limit=limit,
        actionable_only=actionable_only,
        top=top,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("NC Missing Doc Triage Report")
    typer.echo(
        "  "
        f"family={report['family_key'] or '-'} "
        f"discovery={summary['discovery_triage_count']} "
        f"historical={summary['historical_triage_count']} "
        f"combined={summary['combined_triage_count']} "
        f"ranked={summary['ranked_target_count']}"
    )

    if summary["next_action_summary"]:
        typer.echo("Next Actions")
        for row in summary["next_action_summary"][:10]:
            typer.echo(f"  {row['next_action']} count={row['count']}")

    if summary["blocked_reason_summary"]:
        typer.echo("Blocked Reasons")
        for row in summary["blocked_reason_summary"][:10]:
            typer.echo(f"  {row['blocked_reason']} count={row['count']}")

    if report["ranked_targets"]:
        typer.echo("Targets")
        for row in report["ranked_targets"][:10]:
            typer.echo(
                "  "
                f"type={row['target_type']} "
                f"id={row['id']} "
                f"family={row.get('family_key') or '-'} "
                f"next={row.get('next_action') or '-'} "
                f"blocked={row.get('blocked_reason') or '-'} "
                f"score={row.get('priority_score') or 0}"
            )
            if row.get("suggested_command"):
                typer.echo(f"    cmd: {row['suggested_command']}")


@workflow_app.command("execute-top-nc-missing-doc-triage")
def execute_top_nc_missing_doc_triage_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict execution to one family key.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        help="Maximum triaged rows to scan before selecting the top actionable target.",
    ),
    requested_by: str = typer.Option(
        "workflow",
        "--requested-by",
        help="Requester label stored on queued work items.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Execute the top ranked actionable missing-doc triage target and report before/after state."""
    from duke_rates.historical.ncuc.missing_doc_triage_report import (
        execute_top_nc_missing_doc_triage_action,
    )

    settings, repository = _bootstrap()
    report = execute_top_nc_missing_doc_triage_action(
        settings,
        repository,
        family_key=family_key,
        limit=limit,
        requested_by=requested_by,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    if not report["executed"]:
        typer.echo("NC missing-doc triage execution: no actionable ranked targets available.")
        return

    selected = report["selected_target"] or {}
    before_count = int((report["before_report"].get("summary") or {}).get("ranked_target_count") or 0)
    after_count = int((report["after_report"].get("summary") or {}).get("ranked_target_count") or 0)
    typer.echo(
        "NC missing-doc triage execution: "
        f"family={report['family_key'] or '-'} "
        f"type={selected.get('target_type') or '-'} "
        f"id={selected.get('id') or '-'} "
        f"next={selected.get('next_action') or '-'} "
        f"before_ranked={before_count} "
        f"after_ranked={after_count}"
    )
    if selected.get("suggested_command"):
        typer.echo(f"  cmd: {selected['suggested_command']}")


@workflow_app.command("execute-batch-nc-missing-doc-triage")
def execute_batch_nc_missing_doc_triage_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict batch execution to one family key.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        help="Maximum triaged rows to scan before each step selection.",
    ),
    max_actions: int = typer.Option(
        5,
        "--max-actions",
        help="Maximum actionable triage steps to execute in this batch.",
    ),
    requested_by: str = typer.Option(
        "workflow",
        "--requested-by",
        help="Requester label stored on queued work items.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Execute up to N ranked actionable missing-doc triage targets with stop conditions."""
    from duke_rates.historical.ncuc.missing_doc_triage_report import (
        execute_batch_nc_missing_doc_triage_actions,
    )

    settings, repository = _bootstrap()
    report = execute_batch_nc_missing_doc_triage_actions(
        settings,
        repository,
        family_key=family_key,
        limit=limit,
        max_actions=max_actions,
        requested_by=requested_by,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    initial_count = int((report["initial_report"].get("summary") or {}).get("ranked_target_count") or 0)
    final_count = int((report["final_report"].get("summary") or {}).get("ranked_target_count") or 0)
    typer.echo(
        "NC missing-doc triage batch execution: "
        f"family={report['family_key'] or '-'} "
        f"executed={report['executed_count']} "
        f"max_actions={report['max_actions']} "
        f"initial_ranked={initial_count} "
        f"final_ranked={final_count} "
        f"stop_reason={report['stop_reason']}"
    )
    for step in report["steps"][:10]:
        selected = step.get("selected_target") or {}
        typer.echo(
            "  "
            f"type={selected.get('target_type') or '-'} "
            f"id={selected.get('id') or '-'} "
            f"next={selected.get('next_action') or '-'}"
        )
        if selected.get("suggested_command"):
            typer.echo(f"    cmd: {selected['suggested_command']}")


@workflow_app.command("plan-nc-missing-doc-remediation")
def plan_nc_missing_doc_remediation_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict the remediation plan to one family key.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        help="Maximum deferred rows to scan when building the plan.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Rank supported missing-doc remediations by likely payoff and frequency."""
    from duke_rates.historical.ncuc.missing_doc_deferred_report import (
        build_nc_missing_doc_remediation_plan,
    )

    _, repository = _bootstrap()
    report = build_nc_missing_doc_remediation_plan(
        repository,
        family_key=family_key,
        limit=limit,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("NC Missing Doc Remediation Plan")
    typer.echo(
        "  "
        f"family={report['family_key'] or '-'} "
        f"steps={len(report['ranked_steps'])}"
    )
    for step in report["ranked_steps"][:10]:
        typer.echo(
            "  "
            f"reason={step['reason']} "
            f"scope={step['scope']} "
            f"count={step['count']} "
            f"weighted_score={step['weighted_score']} "
            f"families={','.join(step['family_keys']) or '-'}"
        )
        if step["recommended_command"]:
            typer.echo(f"    cmd: {step['recommended_command']}")


@workflow_app.command("execute-top-nc-missing-doc-remediation")
def execute_top_nc_missing_doc_remediation_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict execution to one family key.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        help="Maximum deferred rows to scan when building before/after plans.",
    ),
    promotion_min_ideality: str = typer.Option(
        "probable",
        "--promotion-min-ideality",
        help="Minimum search ideality for re-promotion after remediation.",
    ),
    promotion_min_confidence: float = typer.Option(
        45.0,
        "--promotion-min-confidence",
        help="Minimum search confidence score for re-promotion after remediation.",
    ),
    import_promotion_min_family_score: float = typer.Option(
        24.0,
        "--import-promotion-min-family-score",
        help="Minimum importer family-match score for re-promotion after historical remediation.",
    ),
    requested_by: str = typer.Option(
        "workflow",
        "--requested-by",
        help="Requester label stored on queued work items.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Execute the top ranked supported remediation step and report before/after plan state."""
    from duke_rates.historical.ncuc.missing_doc_deferred_report import (
        execute_top_nc_missing_doc_remediation_step,
    )

    settings, repository = _bootstrap()
    report = execute_top_nc_missing_doc_remediation_step(
        settings,
        repository,
        family_key=family_key,
        limit=limit,
        promotion_min_ideality=promotion_min_ideality,
        promotion_min_confidence=promotion_min_confidence,
        import_promotion_min_family_score=import_promotion_min_family_score,
        requested_by=requested_by,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    if not report["executed"]:
        typer.echo("NC missing-doc remediation execution: no ranked supported steps available.")
        return

    selected = report["selected_step"] or {}
    before_steps = len(report["before_plan"].get("ranked_steps", []))
    after_steps = len(report["after_plan"].get("ranked_steps", []))
    typer.echo(
        "NC missing-doc remediation execution: "
        f"family={report['family_key'] or '-'} "
        f"reason={selected.get('reason') or '-'} "
        f"scope={selected.get('scope') or '-'} "
        f"before_steps={before_steps} "
        f"after_steps={after_steps}"
    )
    typer.echo(f"  selected_step={json.dumps(selected, sort_keys=True, default=str)}")
    typer.echo(f"  execution_report={json.dumps(report['execution_report'], sort_keys=True, default=str)}")


@workflow_app.command("report-nc-missing-doc-remediation-history")
def report_nc_missing_doc_remediation_history_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict the remediation history to one family key.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Maximum remediation execution rows to show.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show persisted missing-doc remediation execution history."""
    _, repository = _bootstrap()
    rows = repository.list_missing_doc_remediation_runs(
        family_key=family_key,
        limit=limit,
    )

    if json_out:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return

    typer.echo("NC Missing Doc Remediation History")
    typer.echo(
        "  "
        f"family={family_key or '-'} "
        f"rows={len(rows)}"
    )
    for row in rows[:20]:
        typer.echo(
            "  "
            f"id={row['id']} "
            f"created_at={row['created_at']} "
            f"reason={row['selected_reason'] or '-'} "
            f"scope={row['selected_scope'] or '-'} "
            f"executed={row['executed']} "
            f"before_steps={row['before_step_count']} "
            f"after_steps={row['after_step_count']} "
            f"before_discovery={row['before_deferred_discovery_count']} "
            f"after_discovery={row['after_deferred_discovery_count']} "
            f"before_historical={row['before_deferred_historical_count']} "
            f"after_historical={row['after_deferred_historical_count']}"
        )


@workflow_app.command("remediate-nc-missing-doc-no-download-url")
def remediate_nc_missing_doc_no_download_url_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict remediation to one family key.",
    ),
    record_id: list[int] | None = typer.Option(
        None,
        "--record-id",
        help="Specific deferred discovery record id(s) to remediate. Repeat for multiple.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum deferred discovery rows to remediate.",
    ),
    delay_seconds: float = typer.Option(
        0.2,
        "--delay-seconds",
        help="Delay between detail-page enrichments.",
    ),
) -> None:
    """Re-open deferred discovery rows blocked on missing download URLs and try to recover ViewFile links."""
    from duke_rates.historical.ncuc.missing_doc_remediation import (
        remediate_no_downloadable_url_discovery_records,
    )

    settings, repository = _bootstrap()
    report = remediate_no_downloadable_url_discovery_records(
        settings,
        repository,
        family_key=family_key,
        discovery_record_ids=[int(item) for item in (record_id or [])],
        limit=limit,
        delay_seconds=delay_seconds,
    )

    typer.echo(
        "NC missing-doc remediation: "
        f"selected={report['selected_count']} "
        f"resolved={report['resolved_count']} "
        f"updated={len(report['updated_record_ids'])} "
        f"unresolved={len(report['unresolved_record_ids'])}"
    )
    if report["updated_record_ids"]:
        typer.echo(f"  updated_record_ids={report['updated_record_ids']}")
    if report["unresolved_record_ids"]:
        typer.echo(f"  unresolved_record_ids={report['unresolved_record_ids']}")


@workflow_app.command("remediate-nc-missing-doc-effective-start")
def remediate_nc_missing_doc_effective_start_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict remediation to one family key.",
    ),
    historical_document_id: list[int] | None = typer.Option(
        None,
        "--historical-document-id",
        help="Specific deferred historical document id(s) to remediate. Repeat for multiple.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum deferred historical docs to remediate.",
    ),
) -> None:
    """Re-open deferred imported historical docs blocked on missing effective_start and try to recover footer dates."""
    from duke_rates.historical.ncuc.missing_doc_remediation import (
        remediate_missing_effective_start_historical_documents,
    )

    _, repository = _bootstrap()
    report = remediate_missing_effective_start_historical_documents(
        repository,
        family_key=family_key,
        historical_document_ids=[int(item) for item in (historical_document_id or [])],
        limit=limit,
    )

    typer.echo(
        "NC missing-doc effective-start remediation: "
        f"selected={report['selected_count']} "
        f"resolved={report['resolved_count']} "
        f"updated={len(report['updated_historical_document_ids'])} "
        f"unresolved={len(report['unresolved_historical_document_ids'])}"
    )
    if report["updated_historical_document_ids"]:
        typer.echo(f"  updated_historical_document_ids={report['updated_historical_document_ids']}")
    if report["unresolved_historical_document_ids"]:
        typer.echo(f"  unresolved_historical_document_ids={report['unresolved_historical_document_ids']}")


@workflow_app.command("remediate-nc-missing-doc-confidence")
def remediate_nc_missing_doc_confidence_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict remediation to one family key.",
    ),
    record_id: list[int] | None = typer.Option(
        None,
        "--record-id",
        help="Specific deferred discovery record id(s) to remediate. Repeat for multiple.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum deferred discovery rows to remediate.",
    ),
    structured_max_results: int = typer.Option(
        100,
        "--structured-max-results",
        help="Structured portal result cap for family re-search.",
    ),
    keyword_max_results: int = typer.Option(
        40,
        "--keyword-max-results",
        help="Keyword result cap for family re-search.",
    ),
    max_candidates_per_family: int = typer.Option(
        20,
        "--max-candidates-per-family",
        help="Maximum persisted candidates per family during remediation re-search.",
    ),
) -> None:
    """Re-run missing-doc search with broader breadth for discovery rows deferred on confidence."""
    from duke_rates.historical.ncuc.missing_doc_remediation import (
        remediate_confidence_below_threshold_discovery_records,
    )

    settings, repository = _bootstrap()
    report = remediate_confidence_below_threshold_discovery_records(
        settings,
        repository,
        family_key=family_key,
        discovery_record_ids=[int(item) for item in (record_id or [])],
        limit=limit,
        structured_max_results=structured_max_results,
        keyword_max_results=keyword_max_results,
        max_candidates_per_family=max_candidates_per_family,
    )

    typer.echo(
        "NC missing-doc confidence remediation: "
        f"selected={report['selected_count']} "
        f"families={','.join(report['rerun_family_keys']) or '-'} "
        f"updated={len(report['updated_record_ids'])} "
        f"unresolved={len(report['unresolved_record_ids'])}"
    )
    if report["updated_record_ids"]:
        typer.echo(f"  updated_record_ids={report['updated_record_ids']}")
    if report["unresolved_record_ids"]:
        typer.echo(f"  unresolved_record_ids={report['unresolved_record_ids']}")


@workflow_app.command("remediate-and-promote-nc-missing-docs")
def remediate_and_promote_nc_missing_docs_cmd(
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict remediation/promotion to one family key.",
    ),
    reason: list[str] | None = typer.Option(
        None,
        "--reason",
        help="Reason(s) to remediate: confidence_below_threshold, no_downloadable_url, missing_effective_start_for_weak_match. Repeat for multiple.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum items to remediate per supported reason.",
    ),
    delay_seconds: float = typer.Option(
        0.2,
        "--delay-seconds",
        help="Delay between detail-page enrichments for no-download-url remediation.",
    ),
    promotion_min_ideality: str = typer.Option(
        "probable",
        "--promotion-min-ideality",
        help="Minimum search ideality for re-promotion after discovery remediation.",
    ),
    promotion_min_confidence: float = typer.Option(
        45.0,
        "--promotion-min-confidence",
        help="Minimum search confidence score for re-promotion after discovery remediation.",
    ),
    import_promotion_min_family_score: float = typer.Option(
        24.0,
        "--import-promotion-min-family-score",
        help="Minimum importer family-match score for re-promotion after historical remediation.",
    ),
    requested_by: str = typer.Option(
        "workflow",
        "--requested-by",
        help="Requester label stored on queued work items.",
    ),
) -> None:
    """Run supported missing-doc remediations and immediately re-promote any rows that become eligible."""
    from duke_rates.historical.ncuc.missing_doc_remediation import (
        remediate_and_promote_missing_doc_targets,
    )

    settings, repository = _bootstrap()
    report = remediate_and_promote_missing_doc_targets(
        settings,
        repository,
        family_key=family_key,
        reasons=[str(item) for item in (reason or [])],
        limit=limit,
        delay_seconds=delay_seconds,
        promotion_min_ideality=promotion_min_ideality,
        promotion_min_confidence=promotion_min_confidence,
        import_promotion_min_family_score=import_promotion_min_family_score,
        requested_by=requested_by,
    )

    typer.echo(
        "NC missing-doc remediation+promotion: "
        f"family={report['family_key'] or '-'} "
        f"reasons={','.join(report['reasons']) or '-'}"
    )
    for reason_key, remediation in report["remediation_reports"].items():
        typer.echo(f"  remediation[{reason_key}] {json.dumps(remediation, sort_keys=True, default=str)}")
    for reason_key, promotion in report["promotion_reports"].items():
        typer.echo(f"  promotion[{reason_key}] {json.dumps(promotion, sort_keys=True, default=str)}")


