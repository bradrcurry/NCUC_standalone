"""Reprocess sub-app: queue management, stale recovery, parser-impact enqueue, and queue drain.

Wired into the main CLI as `duke-rates reprocess <command>`.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import typer

from duke_rates.db.artifact_cache import load_page_artifacts, save_page_artifacts, save_span_artifacts
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect as connect_sqlite
from duke_rates.historical.ncuc.pipeline.ocr import (
    extract_ocr_document_pages,
    load_ocr_sidecar_payload,
    summarize_ocr_payload,
)
from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
)
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf
from duke_rates.models.pipeline import PipelineRoute

from duke_rates.cli_commands._cli_utils import _bootstrap, _safe_cli_text


logger = logging.getLogger(__name__)

reprocess_app = typer.Typer(help="Historical reprocess queue management, stale recovery, and queue drain.")


# ---------------------------------------------------------------------------
# Private helpers — only the reprocess block uses these. The cross-cutting
# helpers (_ensure_historical_tariff_version, _build_parser_improvement_candidates_nc_report)
# stay in cli.py and are lazy-imported below.
# ---------------------------------------------------------------------------


def _refresh_historical_artifacts_for_reprocess(
    database_path: str | Path,
    *,
    source_pdf: str,
    file_hash: str | None,
    stale_reasons: list[str] | None = None,
) -> dict[str, bool]:
    """Refresh cached page/span artifacts when a stale reprocess item requires it."""
    stale_reason_set = set(stale_reasons or [])
    needs_page_refresh = any(
        reason in stale_reason_set
        for reason in (
            "page_artifact_missing",
            "page_artifact_version",
            "ocr_backend_version",
            "ocr_normalization_version",
        )
    )
    needs_span_refresh = needs_page_refresh or any(
        reason in stale_reason_set
        for reason in ("span_artifact_missing", "span_artifact_version")
    )
    if not needs_page_refresh and not needs_span_refresh:
        return {"page_refreshed": False, "span_refreshed": False}

    pages = []
    page_metadata: dict[str, object] = {}
    triage = None
    if needs_page_refresh:
        triage = triage_pdf(source_pdf)
        if triage.route_recommendation == PipelineRoute.OCR_REQUIRED:
            pages = extract_ocr_document_pages(source_pdf)
            ocr_summary = summarize_ocr_payload(load_ocr_sidecar_payload(source_pdf))
            page_metadata = {
                "artifact_source": "ocr",
                "triage_confidence_score": getattr(triage, "confidence_score", None),
                "ocr_confidence_score": getattr(triage, "ocr_confidence_score", None),
                "native_text_quality_score": getattr(triage, "native_text_quality_score", None),
                "reading_order_risk_score": getattr(triage, "reading_order_risk_score", None),
                "gpu_ocr_candidate": getattr(triage, "gpu_ocr_candidate", False),
                "table_mode_candidate": getattr(triage, "table_mode_candidate", None),
                "document_archetype_candidate": getattr(triage, "document_archetype_candidate", None),
                "native_text_backend": getattr(triage, "native_text_backend", None),
                "ocr_backend_version": OCR_BACKEND_VERSION,
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                **ocr_summary,
            }
        else:
            pages = mine_document_pages(source_pdf)
            page_metadata = {
                "artifact_source": "native_text",
                "route_recommendation": triage.route_recommendation,
                "triage_confidence_score": getattr(triage, "confidence_score", None),
                "ocr_confidence_score": getattr(triage, "ocr_confidence_score", None),
                "native_text_quality_score": getattr(triage, "native_text_quality_score", None),
                "reading_order_risk_score": getattr(triage, "reading_order_risk_score", None),
                "table_mode_candidate": getattr(triage, "table_mode_candidate", None),
                "document_archetype_candidate": getattr(triage, "document_archetype_candidate", None),
                "native_text_backend": getattr(triage, "native_text_backend", None),
            }
    else:
        conn = connect_sqlite(database_path)
        try:
            pages = load_page_artifacts(
                conn,
                source_pdf=source_pdf,
                file_hash=file_hash,
            )
        finally:
            conn.close()

    if not pages:
        return {"page_refreshed": False, "span_refreshed": False}

    conn = connect_sqlite(database_path)
    try:
        if needs_page_refresh:
            save_page_artifacts(
                conn,
                discovery_record_id=None,
                source_pdf=source_pdf,
                file_hash=file_hash,
                pages=pages,
                metadata=page_metadata,
            )
        if needs_span_refresh:
            spans = segment_document(pages, parent_discovery_id=None)
            if spans:
                # Classify spans against families so evidence_score_breakdown
                # is populated before saving.  This allows the downstream
                # populate_evidence_json_for_document() call to succeed.
                try:
                    from duke_rates.historical.ncuc.pipeline.family_matcher import (
                        classify_span_against_families,
                    )

                    family_rows = conn.execute(
                        "SELECT family_key, schedule_code FROM tariff_families "
                        "WHERE family_key LIKE 'nc-progress-%'"
                        "   OR family_key LIKE 'nc-carolinas-%'"
                    ).fetchall()

                    supported_families = []
                    for fr in family_rows:
                        fk, code = fr
                        parts = fk.split("-")
                        leaf = parts[-1] if parts else ""
                        supported_families.append({
                            "family_id": fk,
                            "aliases": [],
                            "leaf_no": leaf,
                            "code": code or "",
                        })

                    if supported_families:
                        for span in spans:
                            classify_span_against_families(span, supported_families)
                except Exception:
                    logger.debug(
                        "Span classification during reprocess failed",
                        exc_info=True,
                    )

                save_span_artifacts(
                    conn,
                    discovery_record_id=None,
                    source_pdf=source_pdf,
                    file_hash=file_hash,
                    spans=spans,
                    metadata={
                        "refresh_source": "historical_reprocess_queue",
                        "triage_confidence_score": getattr(triage, "confidence_score", None),
                        "table_mode_candidate": getattr(triage, "table_mode_candidate", None),
                        "document_archetype_candidate": getattr(triage, "document_archetype_candidate", None),
                    },
                )
        conn.commit()
    finally:
        conn.close()

    return {
        "page_refreshed": needs_page_refresh,
        "span_refreshed": needs_span_refresh,
    }


def _process_single_reprocess_queue_item(
    database_path: str | Path,
    force_clear: bool = False,
) -> dict[str, int | bool]:
    from duke_rates.cli import _ensure_historical_tariff_version
    from duke_rates.db.reprocess import (
        claim_next_historical_reprocess,
        complete_historical_reprocess,
        latest_processing_run_for_document,
    )
    from duke_rates.db.sqlite import connect
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    db_path = Path(database_path)
    repository = Repository(str(db_path))
    extractor = BulkExtractor(str(db_path))

    conn = connect(db_path)
    try:
        item = claim_next_historical_reprocess(conn)
        if not item:
            conn.commit()
            return {"processed": False, "completed": 0, "failed": 0}
        conn.commit()
    finally:
        conn.close()

    queue_id = int(item["id"])
    historical_document_id = int(item["historical_document_id"])
    queue_metadata = json.loads(item.get("metadata_json") or "{}")
    doc = extractor.get_document_for_extraction(historical_document_id)
    if not doc:
        conn = connect(db_path)
        try:
            complete_historical_reprocess(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=f"Historical document {historical_document_id} not found.",
            )
            conn.commit()
        finally:
            conn.close()
        return {"processed": True, "completed": 0, "failed": 1}

    artifact_refresh = _refresh_historical_artifacts_for_reprocess(
        str(db_path),
        source_pdf=str(doc.get("local_path") or item.get("source_pdf") or ""),
        file_hash=doc.get("content_hash"),
        stale_reasons=list(queue_metadata.get("stale_reasons") or []),
    )

    version_bootstrapped = False
    version_id = extractor.get_tariff_version_for_document(historical_document_id)
    if version_id is None:
        family_key = doc.get("family_key")
        if not family_key:
            conn = connect(db_path)
            try:
                complete_historical_reprocess(
                    conn,
                    queue_id=queue_id,
                    status="failed",
                    error_message=f"Historical document {historical_document_id} has no family_key.",
                )
                conn.commit()
            finally:
                conn.close()
            return {"processed": True, "completed": 0, "failed": 1}
        version_id = _ensure_historical_tariff_version(
            repository,
            historical_document_id=historical_document_id,
            family_key=str(family_key),
            effective_start=doc.get("effective_start"),
        )
        doc["version_id"] = version_id
        version_bootstrapped = True

    try:
        process_result = extractor.process_document(doc, force_clear=force_clear)
        _, family_key, inserted = process_result[:3]
        conn = connect(db_path)
        try:
            latest_run = latest_processing_run_for_document(
                conn,
                historical_document_id=historical_document_id,
            )
            complete_historical_reprocess(
                conn,
                queue_id=queue_id,
                status="completed",
                latest_run_id=latest_run["id"] if latest_run else None,
                metadata={
                    "charges_inserted": inserted,
                    "family_key": family_key,
                    **artifact_refresh,
                    "version_bootstrapped": version_bootstrapped,
                    "version_id": doc.get("version_id") or version_id,
                },
            )
            conn.commit()

            # Populate evidence_json from freshly-created span artifacts
            try:
                repository.populate_evidence_json_for_document(historical_document_id)
            except Exception:
                logger.debug(
                    "Failed to populate evidence_json for hd:%d",
                    historical_document_id,
                    exc_info=True,
                )
        finally:
            conn.close()
        return {"processed": True, "completed": 1, "failed": 0}
    except Exception as exc:
        conn = connect(db_path)
        try:
            complete_historical_reprocess(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=str(exc),
            )
            conn.commit()
        finally:
            conn.close()
        return {"processed": True, "completed": 0, "failed": 1}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@reprocess_app.command("enqueue-nc")
def enqueue_reprocess_nc(
    from_needs_review: bool = typer.Option(
        False,
        "--from-needs-review/--no-from-needs-review",
        help="Queue historical docs whose latest parse review still needs review.",
    ),
    hd_id: list[int] | None = typer.Option(None, "--hd-id", help="Queue a specific historical document id directly. Repeat for multiple docs."),
    family_key: str | None = typer.Option(None, help="Optional family key filter."),
    parser_profile: str | None = typer.Option(None, help="Optional parser profile filter."),
    source_pdf: str | None = typer.Option(None, help="Optional source PDF filter."),
    limit: int = typer.Option(100, "--limit", help="Max candidate parse attempts to inspect."),
    priority: int = typer.Option(70, "--priority", help="Base queue priority."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label."),
) -> None:
    """Enqueue targeted historical documents for reparsing."""
    from duke_rates.db.reprocess import (
        enqueue_reprocess_candidates_from_review_queue,
        enqueue_specific_historical_documents,
    )
    from duke_rates.db.sqlite import connect

    direct_ids = [int(item) for item in (hd_id or [])]
    if not from_needs_review and not direct_ids:
        raise typer.BadParameter("No enqueue source selected. Use --from-needs-review and/or --hd-id.")

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        inserted = 0
        skipped = 0
        missing_ids: list[int] = []
        if from_needs_review:
            report = enqueue_reprocess_candidates_from_review_queue(
                conn,
                limit=limit,
                priority=priority,
                requested_by=requested_by,
                family_key=family_key,
                parser_profile=parser_profile,
                source_pdf=source_pdf,
            )
            inserted += int(report["inserted"])
            skipped += int(report["skipped"])
        if direct_ids:
            report = enqueue_specific_historical_documents(
                conn,
                historical_document_ids=direct_ids,
                priority=priority,
                requested_by=requested_by,
                queue_reason="manual_requeue",
            )
            inserted += int(report["inserted"])
            skipped += int(report["skipped"])
            missing_ids.extend(int(item) for item in report["missing_ids"])
        conn.commit()
    finally:
        conn.close()

    typer.echo(f"Historical reprocess queue: inserted={inserted} skipped={skipped}")
    if missing_ids:
        typer.echo("Missing historical docs: " + ",".join(str(item) for item in missing_ids))


@reprocess_app.command("enqueue-parser-improvement-nc")
def enqueue_parser_improvement_reprocess_nc(
    limit: int = typer.Option(500, "--limit", help="Max parser-improvement candidate families to inspect."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    priority: int = typer.Option(70, "--priority", help="Reprocess queue priority for enqueued docs."),
    requested_by: str = typer.Option("parser_improvement_workflow", "--requested-by", help="Queue requester label."),
    process: bool = typer.Option(False, "--process", help="Drain the reprocess queue immediately after enqueueing."),
    workers: int = typer.Option(4, "--workers", min=1, help="Workers for the optional drain step."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview by default; use --execute to enqueue."),
) -> None:
    """Enqueue the easy-win parser-improvement cohort (recommended_action=enqueue_reprocess).

    These are documents flagged by show-parser-improvement-candidates-nc as having
    usable text plus a working parser profile, but no latest pipeline run. No parser
    work needed — they just need to be reprocessed.
    """
    from duke_rates.cli import _build_parser_improvement_candidates_nc_report
    from duke_rates.db.reprocess import enqueue_specific_historical_documents

    settings, repository = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_parser_improvement_candidates_nc_report(
            repository,
            conn,
            limit=limit,
            company=company,
        )
    finally:
        conn.close()

    hd_ids: list[int] = []
    family_summary: list[tuple[str, int]] = []
    for row in report["rows"]:
        if row.get("recommended_action") != "enqueue_reprocess":
            continue
        action_ids = list(row.get("action_historical_document_ids") or [])
        if not action_ids:
            continue
        family_summary.append((row.get("family_key") or "?", len(action_ids)))
        hd_ids.extend(int(x) for x in action_ids)

    mode = "dry_run" if dry_run else "execute"
    typer.echo(
        f"Parser-improvement reprocess ({mode}): families={len(family_summary)} hd_ids={len(hd_ids)}"
    )
    for family_key, count in family_summary[:15]:
        typer.echo(f"  family={family_key} hd_count={count}")

    if dry_run:
        typer.echo("  (dry-run — pass --execute to enqueue)")
        return

    if not hd_ids:
        typer.echo("  no actionable hd_ids; nothing to enqueue")
        return

    conn = connect_sqlite(settings.database_path)
    try:
        result = enqueue_specific_historical_documents(
            conn,
            historical_document_ids=hd_ids,
            priority=priority,
            requested_by=requested_by,
            queue_reason="parser_improvement_reprocess",
        )
        conn.commit()
    finally:
        conn.close()

    typer.echo(
        f"  enqueued inserted={result['inserted']} skipped={result['skipped']}"
    )

    if process:
        typer.echo("")
        typer.echo("=== Draining reprocess queue ===")
        process_reprocess_queue_nc(
            limit=500,
            workers=workers,
            until_empty=True,
        )


@reprocess_app.command("show-queue-nc")
def show_reprocess_queue_nc(
    status: str | None = typer.Option("pending", "--status", help="pending | running | completed | failed | all"),
    limit: int = typer.Option(50, "--limit", help="Max queue rows to display."),
) -> None:
    """Show the targeted historical reprocess queue."""
    from duke_rates.db.reprocess import list_historical_reprocess_queue
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = list_historical_reprocess_queue(conn, status=None if status == "all" else status, limit=limit)
    finally:
        conn.close()

    for row in rows:
        typer.echo(
            "\t".join(
                [
                    _safe_cli_text(row["id"]),
                    _safe_cli_text(row["status"]),
                    _safe_cli_text(row["priority"]),
                    _safe_cli_text(row.get("family_key") or "-"),
                    _safe_cli_text(row.get("queue_reason") or "-"),
                    _safe_cli_text(row.get("source_pdf") or "-"),
                ]
            )
        )


@reprocess_app.command("show-stale-nc")
def show_stale_reprocess_nc(
    older_than_minutes: int = typer.Option(240, "--older-than-minutes", help="Minimum age for a running row to be considered stale."),
    limit: int = typer.Option(50, "--limit", help="Max stale running rows to display."),
) -> None:
    """Show running historical reprocess queue items that appear stale."""
    from duke_rates.db.reprocess import find_stale_running_historical_reprocess_queue
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = find_stale_running_historical_reprocess_queue(
            conn,
            older_than_minutes=older_than_minutes,
            limit=limit,
        )
    finally:
        conn.close()

    for row in rows:
        typer.echo(
            "\t".join(
                [
                    _safe_cli_text(row["queue_id"]),
                    _safe_cli_text(row["historical_document_id"]),
                    _safe_cli_text(row["status"]),
                    _safe_cli_text(row["priority"]),
                    _safe_cli_text(row.get("family_key") or "-"),
                    _safe_cli_text(row.get("age_minutes") if row.get("age_minutes") is not None else "-"),
                    _safe_cli_text(",".join(row.get("reasons") or [])),
                    _safe_cli_text(row.get("queue_reason") or "-"),
                ]
            )
        )


@reprocess_app.command("recover-stale-nc")
def recover_stale_reprocess_nc(
    older_than_minutes: int = typer.Option(240, "--older-than-minutes", help="Minimum age for a running row to be considered stale."),
    limit: int = typer.Option(50, "--limit", help="Max stale running rows to inspect or recover."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue recovery label."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview the recovery without committing. Defaults to dry-run."),
) -> None:
    """Recover stale running reprocess rows by returning them to pending."""
    from duke_rates.db.reprocess import (
        find_stale_running_historical_reprocess_queue,
        recover_stale_running_historical_reprocess_queue,
    )
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        preview_rows = find_stale_running_historical_reprocess_queue(
            conn,
            older_than_minutes=older_than_minutes,
            limit=limit,
        )
        if dry_run:
            conn.rollback()
            report = {
                "scanned": len(preview_rows),
                "recovered": 0,
                "queue_ids": [row["queue_id"] for row in preview_rows],
                "rows": preview_rows,
            }
        else:
            report = recover_stale_running_historical_reprocess_queue(
                conn,
                older_than_minutes=older_than_minutes,
                limit=limit,
                requested_by=requested_by,
            )
            conn.commit()
    finally:
        conn.close()

    mode = "dry_run" if dry_run else "execute"
    typer.echo(
        f"Stale running reprocess recovery ({mode}): scanned={report['scanned']} recovered={report['recovered']}"
    )
    for row in report["rows"]:
        typer.echo(
            "\t".join(
                [
                    _safe_cli_text(row["queue_id"]),
                    _safe_cli_text(row["historical_document_id"]),
                    _safe_cli_text(row["priority"]),
                    _safe_cli_text(row.get("family_key") or "-"),
                    _safe_cli_text(row.get("age_minutes") if row.get("age_minutes") is not None else "-"),
                    _safe_cli_text(",".join(row.get("reasons") or [])),
                    _safe_cli_text(row.get("queue_reason") or "-"),
                ]
            )
        )


@reprocess_app.command("show-priority-nc")
def show_reprocess_priority_nc(
    status: str | None = typer.Option("pending", "--status", help="pending | running | completed | failed | all"),
    limit: int = typer.Option(50, "--limit", help="Max ranked queue rows to display."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show ranked historical reprocess priorities with impact explanations."""
    from duke_rates.db.sqlite import connect
    from duke_rates.historical.ncuc.reprocess_priority import build_reprocess_priority_report

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = build_reprocess_priority_report(
            conn,
            status=None if status == "all" else status,
            limit=limit,
        )
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Reprocess Priority (NC)")
    typer.echo(
        "  "
        f"queue_rows={summary['queue_row_count']}  "
        f"visible={summary['visible_row_count']}"
    )
    if summary["category_counts"]:
        ordered_counts = ", ".join(
            f"{key}={value}"
            for key, value in sorted(
                summary["category_counts"].items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        typer.echo(f"  categories={ordered_counts}")

    for row in report["rows"]:
        typer.echo(
            "  "
            f"rank={row['rank_score']} queue_id={row['queue_id']} "
            f"category={row['priority_category']} stored_priority={row['stored_priority']} "
            f"family={row['family_key'] or '-'}"
        )
        typer.echo(f"    note={row['priority_note']}")
        typer.echo(
            "    "
            f"queue_reason={row['queue_reason'] or '-'} "
            f"latest_outcome={row['latest_outcome_quality'] or '-'} "
            f"latest_profile={row['latest_parser_profile'] or '-'}"
        )
        if row["impact_summary"]:
            typer.echo(f"    impact={'; '.join(row['impact_summary'])}")


@reprocess_app.command("show-stale-historical-nc")
def show_stale_historical_nc(
    limit: int = typer.Option(50, "--limit", help="Max stale historical docs to display."),
    family_key: str | None = typer.Option(None, help="Optional family key filter."),
) -> None:
    """Show historical documents whose cached stages are stale vs current versions."""
    from duke_rates.db.reprocess import find_stale_historical_documents
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = find_stale_historical_documents(conn, limit=limit, family_key=family_key)
    finally:
        conn.close()

    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row.get("family_key") or "-",
                    str(row.get("priority") or 0),
                    ",".join(row.get("reasons") or []),
                    row.get("source_pdf") or "-",
                ]
            )
        )


@reprocess_app.command("enqueue-stale-nc")
def enqueue_stale_reprocess_nc(
    limit: int = typer.Option(500, "--limit", help="Max stale historical docs to inspect."),
    family_key: str | None = typer.Option(None, help="Optional family key filter."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label."),
    dry_run: bool = typer.Option(False, "--dry-run/--execute", help="Preview the enqueue without committing. Defaults to execute for backward compatibility."),
) -> None:
    """Queue historical documents whose cached stages are stale."""
    from duke_rates.db.reprocess import enqueue_stale_historical_documents
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = enqueue_stale_historical_documents(
            conn,
            limit=limit,
            requested_by=requested_by,
            family_key=family_key,
        )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    mode = "dry_run" if dry_run else "execute"
    typer.echo(
        f"Stale historical reprocess queue ({mode}): inserted={report['inserted']} skipped={report['skipped']}"
    )


@reprocess_app.command("show-profile-impact-nc")
def show_profile_impact_nc(
    parser_profile: str = typer.Option(..., "--parser-profile", help="Changed parser profile name."),
    limit: int = typer.Option(50, "--limit", help="Max impacted historical docs to display."),
    family_key: str | None = typer.Option(None, help="Optional family key filter."),
) -> None:
    """Show historical documents targeted by parser-profile dependency rules."""
    from duke_rates.db.reprocess import find_profile_impacted_historical_documents
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = find_profile_impacted_historical_documents(
            conn,
            parser_profile=parser_profile,
            limit=limit,
            family_key=family_key,
        )
    except ValueError as exc:
        conn.close()
        raise typer.BadParameter(str(exc)) from exc
    finally:
        if conn:
            conn.close()

    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row.get("family_key") or "-",
                    str(row.get("priority") or 0),
                    ",".join(row.get("reasons") or []),
                    row.get("source_pdf") or "-",
                ]
            )
        )


@reprocess_app.command("enqueue-profile-impact-nc")
def enqueue_profile_impact_nc(
    parser_profile: str = typer.Option(..., "--parser-profile", help="Changed parser profile name."),
    limit: int = typer.Option(100, "--limit", help="Max impacted historical docs to inspect."),
    family_key: str | None = typer.Option(None, help="Optional family key filter."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label."),
) -> None:
    """Queue historical documents targeted by parser-profile dependency rules."""
    from duke_rates.db.reprocess import enqueue_profile_impacted_historical_documents
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = enqueue_profile_impacted_historical_documents(
            conn,
            parser_profile=parser_profile,
            limit=limit,
            requested_by=requested_by,
            family_key=family_key,
        )
        conn.commit()
    except ValueError as exc:
        conn.close()
        raise typer.BadParameter(str(exc)) from exc
    finally:
        if conn:
            conn.close()

    typer.echo(
        f"Profile-impact reprocess queue: inserted={report['inserted']} skipped={report['skipped']}"
    )


@reprocess_app.command("process-queue-nc")
def process_reprocess_queue_nc(
    limit: int = typer.Option(500, "--limit", help="Max queue items to process per invocation."),
    workers: int = typer.Option(1, "--workers", min=1, help="Parallel workers for local reprocess queue items."),
    until_empty: bool = typer.Option(False, "--until-empty", help="Keep processing until the queue is empty (overrides --limit)."),
    enforce_cleanup: bool = typer.Option(
        False,
        "--enforce-cleanup",
        help=(
            "Delete stale tariff_charges for the version even when this reprocess "
            "extracts 0 charges. Use after tightening routing/guard logic so "
            "now-refused old extractions get cleared instead of surviving silently."
        ),
    ),
) -> None:
    """Process pending historical reparse queue items."""
    settings, _ = _bootstrap()
    if not isinstance(workers, int):
        workers = 1
    if until_empty:
        limit = 1_000_000
    processed = 0
    completed = 0
    failed = 0
    if workers == 1:
        while processed < limit:
            result = _process_single_reprocess_queue_item(
                settings.database_path, force_clear=enforce_cleanup
            )
            if not result["processed"]:
                break
            processed += 1
            completed += int(result["completed"])
            failed += int(result["failed"])
    else:
        target = max(0, limit)
        max_workers = min(workers, target) if target else workers
        submitted = 0
        futures = set()
        exhausted = False
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while submitted < target and len(futures) < max_workers:
                futures.add(
                    executor.submit(
                        _process_single_reprocess_queue_item,
                        settings.database_path,
                        force_clear=enforce_cleanup,
                    )
                )
                submitted += 1
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    if not result["processed"]:
                        exhausted = True
                        continue
                    processed += 1
                    completed += int(result["completed"])
                    failed += int(result["failed"])
                    if not exhausted and submitted < target:
                        futures.add(
                            executor.submit(
                                _process_single_reprocess_queue_item,
                                settings.database_path,
                                force_clear=enforce_cleanup,
                            )
                        )
                        submitted += 1

    typer.echo(
        f"Historical reprocess queue processed={processed} completed={completed} failed={failed} workers={workers}"
        + (" enforce_cleanup=True" if enforce_cleanup else "")
    )
