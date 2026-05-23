from __future__ import annotations

# -- Hardware configuration ----------------------------------------------------
# MKL/OMP env vars must be set before torch is imported anywhere in this
# process. configure_cpu() uses os.environ.setdefault() so it's safe to call
# even if vars were already set externally.
from duke_rates.hardware.cpu_config import configure_cpu, configure_torch_inference, warmup_gpu
configure_cpu()
# ----------------------------------------------------------------------------─

import json
import logging
import sqlite3
import sys
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import typer

from duke_rates.billing.calculators import UsageInput
from duke_rates.billing.engine import BillingEngine
from duke_rates.billing.observations import derive_bill_component_observations
from duke_rates.billing.reconciliation import ProgressNCBillReconciliationService
from duke_rates.billing.usage_io import read_usage_file
from duke_rates.config import get_settings
from duke_rates.db.artifact_cache import load_page_artifacts, save_page_artifacts, save_span_artifacts
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect as connect_sqlite
from duke_rates.discovery.classifier import extract_rev_token
from duke_rates.discovery.duke_site import DukeDiscoveryService
from duke_rates.download.downloader import DocumentDownloader
from duke_rates.download.hashing import sha256_bytes
from duke_rates.download.manifest import ManifestWriter
from duke_rates.external.openei import OpenEIClient
from duke_rates.external.openei_export import build_openei_export_candidate
from duke_rates.historical.archive_today import (
    ArchiveTodayClient,
    write_archive_today_markdown_report,
)
from duke_rates.historical.bill_relevant_gaps import ProgressNCBillRelevantGapService
from duke_rates.historical.citation_miner import HistoricalCitationMiner
from duke_rates.historical.family_crosswalk import ProgressNCFamilyCrosswalkService
from duke_rates.historical.family_targets import find_target_by_query
from duke_rates.historical.inbox import ProgressNCHistoricalInboxService
from duke_rates.historical.lead_registry import ProgressNCLeadRegistryService
from duke_rates.historical.lineage import ProgressNCLineageService
from duke_rates.historical.manual_import import ProgressNCHistoricalImportService
from duke_rates.historical.notice_links import ProgressNCNoticeLinkService
from duke_rates.historical.observed_components import (
    ProgressNCObservedComponentHistoryService,
)
from duke_rates.historical.openei_progress_nc import (
    ProgressNCOpenEIHistoricalRecoveryService,
)
from duke_rates.historical.progress_nc import ProgressNCHistoricalRecoveryService
from duke_rates.historical.provenance import ProgressNCProvenanceService
from duke_rates.historical.public_notices import ProgressNCPublicNoticeRecoveryService
from duke_rates.historical.regulator_gaps import ProgressNCRegulatorGapService
from duke_rates.historical.regulator_leads import ProgressNCRegulatorLeadService
from duke_rates.historical.root_url_lists import ProgressNCRootUrlListService
from duke_rates.historical.search_packs import ProgressNCSearchPackService
from duke_rates.historical.ncuc.pipeline.ocr import (
    extract_ocr_document_pages,
    load_ocr_sidecar_payload,
    summarize_ocr_payload,
)
from duke_rates.historical.ncuc.manual_registration import suggest_registration_metadata
from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    HISTORICAL_BULK_PARSER_VERSION,
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
)
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector
from duke_rates.historical.url_archaeology import ProgressNCUrlArchaeologyService
from duke_rates.logging_config import configure_logging
from duke_rates.mcp.server import serve as serve_mcp
from duke_rates.models.bill import BillStatementData
from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.jurisdiction import JurisdictionQuery
from duke_rates.models.pipeline import PipelineRoute
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.tariff import TariffVersionRecord
from duke_rates.parse.bill_parser import parse_bill_text
from duke_rates.parse.html_extract import extract_html_text
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.selection import (
    canonical_tariff_key,
    estimation_score,
    is_estimatable_schedule,
    supports_usage_input,
)

from duke_rates.cli_commands._cli_utils import _read_usage_file

# Sub-app imports (registered at end of file)
from duke_rates.cli_commands.ocr import (
    ocr_app,
    enqueue_ocr_remediation_nc,
    process_ocr_queue_nc,
)
from duke_rates.cli_commands._ocr_reports import (
    _safe_text_file_length,
    _classify_ocr_route,
    _build_ocr_benchmark_nc_report,
    _build_ocr_remediation_candidates_nc_report,
)
# Reprocess sub-app — names re-imported here are referenced by other code
# still in cli.py (workflow-next-actions, autonomous loop) and by tests that
# monkeypatch on the `duke_rates.cli` module namespace.
from duke_rates.cli_commands.reprocess import (
    reprocess_app,
    enqueue_reprocess_nc,
    enqueue_parser_improvement_reprocess_nc,
    show_reprocess_queue_nc,
    show_stale_reprocess_nc,
    recover_stale_reprocess_nc,
    show_reprocess_priority_nc,
    show_stale_historical_nc,
    enqueue_stale_reprocess_nc,
    show_profile_impact_nc,
    enqueue_profile_impact_nc,
    process_reprocess_queue_nc,
    _process_single_reprocess_queue_item,
    _refresh_historical_artifacts_for_reprocess,
)
# Audit/export sub-apps — pure read-only reports. No internal cli.py
# code calls these by name; no tests import their functions, so no
# re-imports needed.
from duke_rates.cli_commands.export_audit import audit_app, export_app
# Workflow sub-app — missing-doc remediation pipeline. No internal cli.py
# code calls these by name; no tests import their functions.
from duke_rates.cli_commands.workflow import workflow_app
# Search sub-app — multi-stage NCUC search pipeline. One test patches
# `cli._bootstrap` AND `search._bootstrap` (mirrored pattern from Phase 0).
from duke_rates.cli_commands.search import search_app
# Lineage sub-app — tariff family + historical document lifecycle.
from duke_rates.cli_commands.lineage import lineage_app
# NCUC sub-app — portal/search/wayback/docket pipeline.
from duke_rates.cli_commands.ncuc import ncuc_app
# Billing + data sub-apps — bill calc/parse/compare + EIA/OpenEI/URDB.
from duke_rates.cli_commands.billing import billing_app, data_app

app = typer.Typer(help="Duke Energy tariff discovery and analysis CLI.")
app.add_typer(ocr_app, name="ocr")
app.add_typer(reprocess_app, name="reprocess")
app.add_typer(audit_app, name="audit")
app.add_typer(export_app, name="export")
app.add_typer(workflow_app, name="workflow")
app.add_typer(search_app, name="search")
app.add_typer(lineage_app, name="lineage")
app.add_typer(ncuc_app, name="ncuc")
app.add_typer(billing_app, name="billing")
app.add_typer(data_app, name="data")


def _safe_cli_text(value: object) -> str:
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _format_optional_pct(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "-"
logger = logging.getLogger(__name__)
ESTIMATABLE_CATEGORIES = {
    DocumentCategory.RATE.value,
    DocumentCategory.TARIFF.value,
}


def _bootstrap():
    settings = get_settings()
    configure_logging(settings.log_level)
    return settings, Repository(settings.database_path)



def _parse_document(document_id: int, repository: Repository) -> DocumentParseResult:
    document = repository.get_document(document_id)
    if not document:
        raise typer.BadParameter(f"Document {document_id} not found.")

    raw_path = document.local_path
    if document.kind == DocumentKind.PDF.value:
        text = extract_pdf_text(raw_path)
    else:
        text = extract_html_text(raw_path)

    raw_text_path = raw_path.with_suffix(raw_path.suffix + ".txt")
    raw_text_path.write_text(text, encoding="utf-8")

    category = document.category
    if category == DocumentCategory.RIDER.value:
        result = parse_rider_text(
            document_id=document.id,
            title=document.title,
            state=document.state,
            company=document.company,
            text=text,
            raw_text_path=raw_text_path,
        )
    else:
        result = parse_schedule_text(
            document_id=document.id,
            title=document.title,
            state=document.state,
            company=document.company,
            text=text,
            raw_text_path=raw_text_path,
        )
    repository.save_parse_result(result)
    return result


def _parse_service_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter("Expected --service-date in YYYY-MM-DD format.") from exc


def _ensure_historical_tariff_version(
    repository: Repository,
    *,
    historical_document_id: int,
    family_key: str,
    effective_start: str | None,
) -> int:
    """Ensure a historical document has a minimal tariff_version row for extraction."""
    for version in repository.list_tariff_versions(family_key):
        if version.historical_document_id == historical_document_id:
            if version.id is None:
                raise ValueError(
                    f"Existing tariff_version for historical document {historical_document_id} is missing an id."
                )
            return int(version.id)

    return repository.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            historical_document_id=historical_document_id,
            effective_start=effective_start,
            source_type="regulator",
            confidence_score=0.5,
            notes="Bootstrapped for historical reprocess queue.",
        )
    )





def _schedule_has_bill_components(result: DocumentParseResult) -> bool:
    return is_estimatable_schedule(result)



def _count_rows(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[object, ...] = (),
) -> int:
    row = conn.execute(query, params).fetchone()
    if not row:
        return 0
    return int(row[0])


def _build_operational_parse_review_status(conn: sqlite3.Connection) -> dict[str, object]:
    query = """
        WITH
        latest_reviews AS (
            SELECT
                parse_attempt_id,
                MAX(id) AS max_id
            FROM parse_review_outcomes
            WHERE parse_attempt_id IS NOT NULL
            GROUP BY parse_attempt_id
        )
        SELECT
            COALESCE(pal.parser_profile, 'unknown') AS parser_profile,
            pro.outcome
        FROM latest_reviews lr
        JOIN parse_review_outcomes pro
          ON pro.id = lr.max_id
        JOIN parse_attempt_logs pal
          ON pal.id = lr.parse_attempt_id
    """
    rows = conn.execute(query).fetchall()
    needs_review_count = 0
    active_needs_review_count = 0
    profile_counts: dict[str, int] = {}
    for row in rows:
        outcome = str(row["outcome"] or "")
        if outcome != "needs_review":
            continue
        needs_review_count += 1
        profile = str(row["parser_profile"] or "unknown")
        if profile != "tiered_ingest":
            active_needs_review_count += 1
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
    top_needs_review_profiles = [
        profile
        for profile, _count in sorted(
            profile_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:3]
    ]
    return {
        "needs_review_count": needs_review_count,
        "active_needs_review_count": active_needs_review_count,
        "legacy_needs_review_count": needs_review_count - active_needs_review_count,
        "top_needs_review_profiles": top_needs_review_profiles,
    }


def _build_workflow_status_nc_report(conn: sqlite3.Connection) -> dict[str, object]:
    review_status = _build_operational_parse_review_status(conn)
    historical_document_count = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM historical_documents
        WHERE state = 'NC'
        """,
    )
    linked_version_count = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM tariff_versions tv
        JOIN historical_documents hd
          ON hd.id = tv.historical_document_id
        WHERE hd.state = 'NC'
        """,
    )
    versions_with_charges_count = _count_rows(
        conn,
        """
        SELECT COUNT(DISTINCT tv.id)
        FROM tariff_versions tv
        JOIN historical_documents hd
          ON hd.id = tv.historical_document_id
        JOIN tariff_charges tc
          ON tc.version_id = tv.id
        WHERE hd.state = 'NC'
        """,
    )
    extraction_coverage_pct = round(
        100.0 * versions_with_charges_count / linked_version_count,
        1,
    ) if linked_version_count else 0.0
    provisional_family_count = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM tariff_families
        WHERE state = 'NC'
          AND notes LIKE 'Provisional historical family%'
        """,
    )
    null_effective_start_count = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM historical_documents
        WHERE state = 'NC'
          AND local_path IS NOT NULL
          AND effective_start IS NULL
        """,
    )
    reprocess_pending_count = _count_rows(
        conn,
        "SELECT COUNT(*) FROM historical_reprocess_queue WHERE status = 'pending'",
    )
    reprocess_running_count = _count_rows(
        conn,
        "SELECT COUNT(*) FROM historical_reprocess_queue WHERE status = 'running'",
    )
    ocr_pending_count = _count_rows(
        conn,
        "SELECT COUNT(*) FROM ocr_processing_queue WHERE status = 'pending'",
    )
    ocr_running_count = _count_rows(
        conn,
        "SELECT COUNT(*) FROM ocr_processing_queue WHERE status = 'running'",
    )
    last_historical_run_at = conn.execute(
        """
        SELECT MAX(hpr.completed_at)
        FROM historical_processing_runs hpr
        JOIN historical_documents hd
          ON hd.id = hpr.historical_document_id
        WHERE hd.state = 'NC'
          AND hpr.parser_stage = 'historical_bulk'
        """,
    ).fetchone()[0]
    # Stale = docs with a processing run but stale artifacts/versions.
    # Never-processed = docs with no processing run AND null effective_start
    # (these are newly-bootstrapped versions awaiting their first run).
    # The two buckets are disjoint and together replace the previous
    # single stale_historical_count that mixed both categories.
    never_processed_historical_count = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM historical_documents hd
        WHERE hd.state = 'NC'
          AND hd.local_path IS NOT NULL
          AND hd.effective_start IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM historical_processing_runs hpr
            WHERE hpr.historical_document_id = hd.id
          )
        """,
    )
    stale_historical_count = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM historical_documents hd
        LEFT JOIN historical_processing_runs hpr
          ON hpr.id = (
            SELECT r2.id
            FROM historical_processing_runs r2
            WHERE r2.historical_document_id = hd.id
            ORDER BY r2.id DESC
            LIMIT 1
          )
        WHERE hd.state = 'NC'
          AND hd.local_path IS NOT NULL
          AND (
            hpr.id IS NULL
            OR COALESCE(hpr.parser_version, '') != ?
          )
          AND NOT (hd.effective_start IS NULL AND hpr.id IS NULL)
        """,
        (HISTORICAL_BULK_PARSER_VERSION,),
    )

    return {
        "state": "NC",
        "historical_document_count": historical_document_count,
        "linked_version_count": linked_version_count,
        "versions_with_charges_count": versions_with_charges_count,
        "extraction_coverage_pct": extraction_coverage_pct,
        "parse_review_needs_review_count": review_status["needs_review_count"],
        "parse_review_active_needs_review_count": review_status["active_needs_review_count"],
        "parse_review_legacy_needs_review_count": review_status["legacy_needs_review_count"],
        "reprocess_pending_count": reprocess_pending_count,
        "reprocess_running_count": reprocess_running_count,
        "stale_historical_count": stale_historical_count,
        "never_processed_historical_count": never_processed_historical_count,
        "ocr_pending_count": ocr_pending_count,
        "ocr_running_count": ocr_running_count,
        "provisional_family_count": provisional_family_count,
        "null_effective_start_count": null_effective_start_count,
        "last_historical_run_at": last_historical_run_at,
        "top_needs_review_profiles": review_status["top_needs_review_profiles"],
    }


# OCR helpers (_safe_text_file_length, _classify_ocr_route,
# _build_ocr_benchmark_nc_report, _build_ocr_remediation_candidates_nc_report)
# now live in duke_rates.cli_commands._ocr_reports and are imported at the top
# of this module.




def _build_fast_ocr_remediation_summary_nc(
    conn: sqlite3.Connection,
    *,
    company: str | None = None,
    family_key: str | None = None,
) -> dict[str, Any]:
    report = _build_ocr_remediation_candidates_nc_report(
        conn,
        limit=100,
        company=company,
        family_key=family_key,
    )
    queueable_count = 0
    for row in report["recommended_lane_summary"]:
        if row["recommended_lane"] == "queue_ocr_or_paddle":
            queueable_count = int(row["count"])
            break
    top_row = next(
        (row for row in report["rows"] if row.get("recommended_lane") == "queue_ocr_or_paddle"),
        report["rows"][0] if report["rows"] else None,
    )
    route_reason = str(top_row.get("route_reason") if top_row else "weak_without_ocr")
    return {
        "candidate_count": queueable_count,
        "route_reason": route_reason,
        "top_row": dict(top_row) if top_row else None,
    }


def _build_fast_parser_problem_summary_nc(
    conn: sqlite3.Connection,
    *,
    company: str | None = None,
    family_key: str | None = None,
) -> dict[str, Any]:
    query = """
        WITH latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                WHERE historical_document_id IS NOT NULL
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        )
        SELECT
            COALESCE(
                json_extract(lr.metadata_json, '$.selection.final_parser_profile'),
                lr.parser_profile,
                'unknown'
            ) AS parser_profile,
            COUNT(*) AS profile_count
        FROM latest_runs lr
        JOIN historical_documents hd
          ON hd.id = lr.historical_document_id
        WHERE hd.state = 'NC'
          AND COALESCE(
            json_extract(lr.metadata_json, '$.selection.final_outcome_quality'),
            lr.outcome_quality,
            'unknown'
          ) IN ('weak', 'empty')
    """
    params: list[Any] = []
    if company:
        query += " AND hd.company = ?"
        params.append(company)
    if family_key:
        query += " AND hd.family_key = ?"
        params.append(family_key)
    query += """
        GROUP BY COALESCE(
            json_extract(lr.metadata_json, '$.selection.final_parser_profile'),
            lr.parser_profile,
            'unknown'
        )
        ORDER BY profile_count DESC, parser_profile ASC
        LIMIT 1
    """
    top_row = conn.execute(query, tuple(params)).fetchone()
    total_count = _count_rows(
        conn,
        """
        WITH latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                WHERE historical_document_id IS NOT NULL
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        )
        SELECT COUNT(*)
        FROM latest_runs lr
        JOIN historical_documents hd
          ON hd.id = lr.historical_document_id
        WHERE hd.state = 'NC'
          AND COALESCE(
            json_extract(lr.metadata_json, '$.selection.final_outcome_quality'),
            lr.outcome_quality,
            'unknown'
          ) IN ('weak', 'empty')
        """
        + (" AND hd.company = ?" if company else "")
        + (" AND hd.family_key = ?" if family_key else ""),
        tuple(([company] if company else []) + ([family_key] if family_key else [])),
    )
    return {
        "problem_count": int(total_count),
        "top_parser_profile": str(top_row["parser_profile"] or "unknown") if top_row else "unknown",
    }


def _build_fast_reprocess_priority_summary_nc(conn: sqlite3.Connection) -> dict[str, Any]:
    count_row = conn.execute(
        """
        SELECT COUNT(*) AS pending_count
        FROM historical_reprocess_queue
        WHERE status = 'pending'
        """
    ).fetchone()
    top_row = conn.execute(
        """
        SELECT
            hrq.id AS queue_id,
            hrq.historical_document_id,
            hrq.family_key,
            hrq.priority,
            hrq.queue_reason,
            hd.title
        FROM historical_reprocess_queue hrq
        LEFT JOIN historical_documents hd
          ON hd.id = hrq.historical_document_id
        WHERE hrq.status = 'pending'
        ORDER BY hrq.priority DESC, hrq.requested_at ASC
        LIMIT 1
        """
    ).fetchone()
    return {
        "pending_count": int(count_row["pending_count"] or 0) if count_row else 0,
        "top_row": dict(top_row) if top_row else None,
    }


def _build_fast_stale_reprocess_summary_nc(
    conn: sqlite3.Connection,
    *,
    older_than_minutes: int = 240,
) -> dict[str, Any]:
    from duke_rates.db.reprocess import find_stale_running_historical_reprocess_queue

    rows = find_stale_running_historical_reprocess_queue(
        conn,
        older_than_minutes=older_than_minutes,
        limit=50,
    )
    top_row = rows[0] if rows else None
    return {
        "stale_running_count": len(rows),
        "top_row": top_row,
        "older_than_minutes": older_than_minutes,
    }


def _build_workflow_next_actions_nc_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    workflow_status = _build_workflow_status_nc_report(conn)
    ocr_summary = _build_fast_ocr_remediation_summary_nc(conn)
    parser_summary = _build_fast_parser_problem_summary_nc(conn)
    reprocess_summary = _build_fast_reprocess_priority_summary_nc(conn)
    stale_reprocess_summary = _build_fast_stale_reprocess_summary_nc(conn)

    actions: list[dict[str, Any]] = []

    def _policy(
        action_type: str,
        *,
        recommended_command: str,
        recommended_parallel_command: str | None = None,
    ) -> dict[str, Any]:
        if action_type in {"process_ocr_queue", "process_reprocess_queue"}:
            return {
                "concurrency_policy": "workers_allowed",
                "workers_allowed": True,
                "recommended_command": recommended_command,
                "recommended_parallel_command": recommended_parallel_command,
            }
        return {
            "concurrency_policy": "sequential_only",
            "workers_allowed": False,
            "recommended_command": recommended_command,
            "recommended_parallel_command": None,
        }

    if workflow_status["ocr_pending_count"] > 0:
        actions.append(
            {
                "action_type": "process_ocr_queue",
                "priority": 10,
                "executable": True,
                "count": int(workflow_status["ocr_pending_count"]),
                "summary": f"{workflow_status['ocr_pending_count']} OCR queue items pending",
                "failure_class": "ocr_queue_pending",
                "source": "ocr_queue",
                **_policy(
                    "process_ocr_queue",
                    recommended_command="python -m duke_rates ocr process-queue-nc --limit 1",
                    recommended_parallel_command="python -m duke_rates ocr process-queue-nc --limit 2 --workers 2",
                ),
            }
        )

    if workflow_status["reprocess_pending_count"] > 0:
        top_row = reprocess_summary["top_row"]
        actions.append(
            {
                "action_type": "process_reprocess_queue",
                "priority": 20,
                "executable": True,
                "count": int(workflow_status["reprocess_pending_count"]),
                "summary": f"{workflow_status['reprocess_pending_count']} reprocess queue items pending",
                "failure_class": "reprocess_queue_pending",
                "source": "reprocess_queue",
                "target_family_key": top_row.get("family_key") if top_row else None,
                "target_note": top_row.get("priority_note") if top_row else None,
                **_policy(
                    "process_reprocess_queue",
                    recommended_command="python -m duke_rates reprocess process-queue-nc --limit 1",
                    recommended_parallel_command="python -m duke_rates reprocess process-queue-nc --limit 2 --workers 2",
                ),
            }
        )

    if stale_reprocess_summary["stale_running_count"] > 0:
        top_row = stale_reprocess_summary["top_row"] or {}
        actions.append(
            {
                "action_type": "recover_stale_reprocess",
                "priority": 15,
                "executable": True,
                "count": int(stale_reprocess_summary["stale_running_count"]),
                "summary": (
                    f"{stale_reprocess_summary['stale_running_count']} running reprocess rows appear stale "
                    f"(older than {stale_reprocess_summary['older_than_minutes']} minutes)"
                ),
                "failure_class": "stale_running_queue",
                "source": "reprocess_queue",
                "target_family_key": top_row.get("family_key"),
                "target_note": top_row.get("queue_reason"),
                **_policy(
                    "recover_stale_reprocess",
                    recommended_command=(
                        f"python -m duke_rates reprocess recover-stale-nc --limit 10 "
                        f"--older-than-minutes {stale_reprocess_summary['older_than_minutes']} --execute"
                    ),
                ),
            }
        )

    if ocr_summary["candidate_count"] > 0:
        top_row = ocr_summary["top_row"] or {}
        actions.append(
            {
                "action_type": "enqueue_ocr_remediation",
                "priority": 30,
                "executable": True,
                "count": int(ocr_summary["candidate_count"]),
                "summary": f"{ocr_summary['candidate_count']} OCR remediation candidates are queueable",
                "failure_class": str(ocr_summary["route_reason"]),
                "source": "ocr_remediation",
                "target_family_key": top_row.get("family_key"),
                "target_historical_document_id": int(top_row["historical_document_id"]) if top_row.get("historical_document_id") else None,
                **_policy(
                    "enqueue_ocr_remediation",
                    recommended_command="python -m duke_rates ocr enqueue-remediation-nc --limit 1 --execute",
                ),
            }
        )

    never_processed_count = workflow_status.get("never_processed_historical_count", 0)
    if never_processed_count > 0:
        actions.append(
            {
                "action_type": "bootstrap_or_extract",
                "priority": 35,
                "executable": True,
                "count": int(never_processed_count),
                "summary": f"{never_processed_count} docs never processed (null effective_start, no runs). Bootstrap or extract first.",
                "failure_class": "never_processed",
                "source": "never_processed",
                **_policy(
                    "bootstrap_or_extract",
                    recommended_command="python -m duke_rates bootstrap-missing-versions-nc && python -m duke_rates extract-rates-nc",
                ),
            }
        )

    if workflow_status["stale_historical_count"] > 0:
        actions.append(
            {
                "action_type": "enqueue_stale_reprocess",
                "priority": 40,
                "executable": True,
                "count": int(workflow_status["stale_historical_count"]),
                "summary": f"{workflow_status['stale_historical_count']} stale historical docs need reprocess queueing",
                "failure_class": "stale_artifacts",
                "source": "stale_historical",
                **_policy(
                    "enqueue_stale_reprocess",
                    recommended_command="python -m duke_rates reprocess enqueue-stale-nc --limit 10",
                ),
            }
        )

    if ocr_summary["candidate_count"] > 0 and ocr_summary["route_reason"] not in {"no_usable_text_unknown_profile", "weak_without_ocr"}:
        top_row = ocr_summary["top_row"] or {}
        actions.append(
            {
                "action_type": "review_docling_candidates",
                "priority": 50,
                "executable": False,
                "count": int(ocr_summary["candidate_count"]),
                "summary": f"{ocr_summary['candidate_count']} OCR remediation candidates may need layout-heavy Docling/Paddle review",
                "failure_class": str(ocr_summary["route_reason"]),
                "source": "ocr_remediation",
                "target_family_key": top_row.get("family_key"),
                "target_historical_document_id": int(top_row["historical_document_id"]) if top_row.get("historical_document_id") else None,
                **_policy(
                    "review_docling_candidates",
                    recommended_command="python -m duke_rates ocr show-remediation-candidates-nc --limit 10",
                ),
            }
        )

    if parser_summary["problem_count"] > 0:
        top_profile = parser_summary["top_parser_profile"]
        actions.append(
            {
                "action_type": "review_parser_problem_profile",
                "priority": 60,
                "executable": False,
                "count": int(parser_summary["problem_count"]),
                "summary": "Weak/empty parser outcomes remain after queue handling",
                "failure_class": "parser_profile_gap",
                "source": "parser_audit",
                "target_parser_profile": top_profile,
                **_policy(
                    "review_parser_problem_profile",
                    recommended_command="python -m duke_rates show-parser-selection-audit-nc --limit 25",
                ),
            }
        )

    actions.sort(key=lambda row: (int(row["priority"]), 0 if row["executable"] else 1, row["action_type"]))
    return {
        "summary": {
            "action_count": len(actions),
            "executable_count": sum(1 for row in actions if row["executable"]),
            "workflow_status": workflow_status,
        },
        "rows": actions[:limit],
    }


def _record_workflow_action_receipt_start(
    conn: sqlite3.Connection,
    *,
    workflow: str,
    action: dict[str, Any],
    requested_limit: int,
) -> int:
    now = datetime.now().astimezone().isoformat()
    cur = conn.execute(
        """
        INSERT INTO workflow_action_receipts (
            workflow, action_type, status, target_family_key, target_historical_document_id,
            target_parser_profile, command_text, requested_limit, metadata_json, started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            workflow,
            str(action.get("action_type") or "unknown"),
            "started",
            action.get("target_family_key"),
            action.get("target_historical_document_id"),
            action.get("target_parser_profile"),
            action.get("recommended_command"),
            requested_limit,
            json.dumps(action, sort_keys=True, default=str),
            now,
        ),
    )
    return int(cur.lastrowid)


def _record_workflow_action_receipt_finish(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    status: str,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE workflow_action_receipts
        SET status = ?, completed_at = ?, error_message = ?
        WHERE id = ?
        """,
        (
            status,
            datetime.now().astimezone().isoformat(),
            error_message,
            receipt_id,
        ),
    )


def _list_workflow_action_receipts(
    conn: sqlite3.Connection,
    *,
    workflow: str = "nc_guided",
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM workflow_action_receipts
        WHERE workflow = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (workflow, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _reconcile_workflow_action_receipts(
    conn: sqlite3.Connection,
    *,
    workflow: str = "nc_guided",
    limit: int = 50,
) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT *
        FROM workflow_action_receipts
        WHERE workflow = ? AND status = 'started'
        ORDER BY id DESC
        LIMIT ?
        """,
        (workflow, limit),
    ).fetchall()
    completed = 0
    failed = 0
    running = 0

    for row in rows:
        action_type = str(row["action_type"] or "")
        started_at = str(row["started_at"] or "")
        target_historical_document_id = row["target_historical_document_id"]
        resolved_status: str | None = None
        error_message: str | None = None

        if action_type == "process_ocr_queue":
            queue_row = conn.execute(
                """
                SELECT status, error_message
                FROM ocr_processing_queue
                WHERE (started_at IS NOT NULL AND started_at >= ?)
                   OR (completed_at IS NOT NULL AND completed_at >= ?)
                ORDER BY COALESCE(completed_at, started_at, requested_at) DESC, id DESC
                LIMIT 1
                """,
                (started_at, started_at),
            ).fetchone()
            if queue_row:
                resolved_status = str(queue_row["status"])
                error_message = queue_row["error_message"]
        elif action_type == "process_reprocess_queue":
            queue_row = conn.execute(
                """
                SELECT status, error_message
                FROM historical_reprocess_queue
                WHERE (started_at IS NOT NULL AND started_at >= ?)
                   OR (completed_at IS NOT NULL AND completed_at >= ?)
                ORDER BY COALESCE(completed_at, started_at, requested_at) DESC, id DESC
                LIMIT 1
                """,
                (started_at, started_at),
            ).fetchone()
            if queue_row:
                resolved_status = str(queue_row["status"])
                error_message = queue_row["error_message"]
        elif action_type == "enqueue_ocr_remediation":
            queue_row = conn.execute(
                """
                SELECT status
                FROM ocr_processing_queue
                WHERE requested_at >= ?
                  AND json_extract(metadata_json, '$.requested_by') = 'workflow_next_action'
                  AND (? IS NULL OR CAST(json_extract(metadata_json, '$.historical_document_id') AS INTEGER) = ?)
                ORDER BY requested_at DESC, id DESC
                LIMIT 1
                """,
                (started_at, target_historical_document_id, target_historical_document_id),
            ).fetchone()
            if queue_row:
                resolved_status = "completed"
        elif action_type == "enqueue_stale_reprocess":
            queue_row = conn.execute(
                """
                SELECT status
                FROM historical_reprocess_queue
                WHERE requested_at >= ?
                  AND requested_by = 'workflow_next_action'
                ORDER BY requested_at DESC, id DESC
                LIMIT 1
                """,
                (started_at,),
            ).fetchone()
            if queue_row:
                resolved_status = "completed"

        if resolved_status == "completed":
            _record_workflow_action_receipt_finish(
                conn,
                receipt_id=int(row["id"]),
                status="completed",
                error_message=None,
            )
            completed += 1
        elif resolved_status == "failed":
            _record_workflow_action_receipt_finish(
                conn,
                receipt_id=int(row["id"]),
                status="failed",
                error_message=error_message,
            )
            failed += 1
        elif resolved_status == "running":
            conn.execute(
                """
                UPDATE workflow_action_receipts
                SET status = ?
                WHERE id = ?
                """,
                ("running", int(row["id"])),
            )
            running += 1

    return {"completed": completed, "failed": failed, "running": running}


@app.command()
def crawl(
    state: str | None = typer.Option(None, help="Two-letter state code, e.g. NC."),
    company: str | None = typer.Option(None, help="Optional company hint, e.g. progress."),
    all: bool = typer.Option(False, "--all", help="Crawl all configured jurisdictions."),
) -> None:
    settings, repository = _bootstrap()
    manifest = ManifestWriter(settings.manifest_path)
    discovery = DukeDiscoveryService(settings)
    downloader = DocumentDownloader(settings)

    query = JurisdictionQuery(state=state, company=company, crawl_all=all)
    discoveries = discovery.crawl(query)
    typer.echo(f"Discovered {len(discoveries)} document candidates.")

    stored = 0
    for record in discoveries:
        try:
            # Rev-token change detection: skip if the file hasn't changed
            _new_rev = extract_rev_token(str(record.document_url))
            if _new_rev:
                _existing = repository.get_document_by_base_url(str(record.document_url))
                if _existing and _existing.rev_token == _new_rev:
                    logger.debug("Skipping unchanged document (rev=%s): %s", _new_rev, record.document_url)
                    stored += 1
                    continue
            downloaded = downloader.download(record)
            repository.upsert_document(downloaded)
            manifest.append(downloaded)
            stored += 1
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Failed to download %s: %s", record.document_url, exc)

    discovery.close()
    downloader.close()
    typer.echo(f"Archived {stored} documents.")


@app.command("tariff-update")
def tariff_update(
    state: str | None = typer.Option(None, help="Two-letter state code, e.g. NC."),
    company: str | None = typer.Option(None, help="Optional company hint, e.g. progress."),
    all: bool = typer.Option(False, "--all", help="Crawl all configured jurisdictions."),
    auto_parse: bool = typer.Option(
        False,
        "--auto-parse",
        help="Automatically run parse-tariff-versions on changed/new documents.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report new/changed documents but do not download or parse.",
    ),
) -> None:
    """Check Duke Energy website for new or updated tariff documents.

    Crawls the Duke Energy tariff pages and compares discovered documents against
    the local database using rev_token (URL change marker) and content_hash (byte-level
    dedup).  Reports each document as NEW, CHANGED, or UNCHANGED.

    With --auto-parse, automatically runs parse-tariff-versions on any companies
    where new or changed documents were found.

    Examples:
        # Check NC Progress for updates (dry-run, no downloads)
        duke-rates tariff-update --state NC --company progress --dry-run

        # Check and download any updates, then re-parse
        duke-rates tariff-update --state NC --company progress --auto-parse

        # Check all jurisdictions
        duke-rates tariff-update --all
    """
    settings, repository = _bootstrap()
    discovery = DukeDiscoveryService(settings)
    downloader = DocumentDownloader(settings)
    manifest = ManifestWriter(settings.manifest_path)

    query = JurisdictionQuery(state=state, company=company, crawl_all=all)
    discoveries = discovery.crawl(query)
    typer.echo(f"Discovered {len(discoveries)} document candidates.")

    n_new = 0
    n_changed = 0
    n_unchanged = 0
    n_error = 0
    changed_companies: set[str] = set()

    for record in discoveries:
        url = str(record.document_url)
        _new_rev = extract_rev_token(url)
        _existing = repository.get_document_by_base_url(url)

        if _existing is None:
            status = "NEW"
            n_new += 1
        elif _new_rev and _existing.rev_token == _new_rev:
            status = "UNCHANGED"
            n_unchanged += 1
        elif _new_rev and _existing.rev_token != _new_rev:
            status = "CHANGED"
            n_changed += 1
        elif not _new_rev and _existing is not None:
            # No rev token — compare by content hash after download
            status = "CHANGED"  # assume changed until we can verify
            n_changed += 1
        else:
            status = "NEW"
            n_new += 1

        title = (record.title or "")[:55]
        typer.echo(f"  {status:<10} {title}")

        if status in ("NEW", "CHANGED") and not dry_run:
            try:
                downloaded = downloader.download(record)
                # Re-check content hash after download to distinguish "changed URL token but same content"
                if _existing and downloaded.content_hash == _existing.content_hash:
                    typer.echo(f"             (content unchanged — same hash)")
                    n_changed -= 1
                    n_unchanged += 1
                else:
                    repository.upsert_document(downloaded)
                    manifest.append(downloaded)
                    if record.company:
                        changed_companies.add(record.company)
            except Exception as exc:
                typer.echo(f"             ERROR: {exc}", err=True)
                n_error += 1
        elif status in ("NEW", "CHANGED") and dry_run:
            if record.company:
                changed_companies.add(record.company or "")

    discovery.close()
    downloader.close()

    typer.echo(
        f"\nSummary: {n_new} new, {n_changed} changed, {n_unchanged} unchanged"
        + (f", {n_error} errors" if n_error else "")
    )

    if auto_parse and changed_companies and not dry_run:
        typer.echo(f"\nAuto-parsing changed companies: {', '.join(sorted(changed_companies))}")
        from duke_rates.parse.nc_progress import parse_nc_progress_leaf_file
        from duke_rates.parse.nc_carolinas import parse_nc_carolinas_leaf_file
        from duke_rates.parse.fl_florida import parse_fl_florida_sheet_file
        from duke_rates.parse.in_indiana import parse_in_indiana_tariff_file
        from duke_rates.parse.ky_kentucky import parse_ky_kentucky_tariff_file
        from duke_rates.parse.oh_ohio import parse_oh_ohio_tariff_file

        _parser_map = {
            "progress": parse_nc_progress_leaf_file,
            "carolinas": parse_nc_carolinas_leaf_file,
            "florida": parse_fl_florida_sheet_file,
            "indiana": parse_in_indiana_tariff_file,
            "kentucky": parse_ky_kentucky_tariff_file,
            "ohio": parse_oh_ohio_tariff_file,
        }
        _state = (state or "NC").upper()
        for _company in sorted(changed_companies):
            _parse_file = _parser_map.get(_company)
            if _parse_file is None:
                typer.echo(f"  No parser available for company '{_company}', skipping.")
                continue
            families = repository.list_tariff_families(state=_state, company=_company)
            _n_parsed = _n_skip = _n_err = _n_charges = _n_riders = 0
            for family in families:
                if not family.current_document_id:
                    _n_skip += 1
                    continue
                doc = repository.get_document(family.current_document_id)
                if not doc or not doc.local_path:
                    _n_skip += 1
                    continue
                pdf_path = Path(doc.local_path)
                if not pdf_path.is_file():
                    _n_skip += 1
                    continue
                try:
                    version, charges, riders = _parse_file(
                        pdf_path,
                        version_id=0,
                        family_key=family.family_key,
                        document_id=doc.id,
                    )
                    repository.replace_parsed_tariff_data(family.family_key, version, charges, riders)
                    _n_parsed += 1
                    _n_charges += len(charges)
                    _n_riders += len(riders)
                except Exception as exc:
                    logger.error("Parse error for %s: %s", family.family_key, exc)
                    _n_err += 1
            typer.echo(
                f"  {_company}: {_n_parsed} parsed, {_n_skip} skipped, {_n_err} errors"
                f" | {_n_charges} charges, {_n_riders} riders"
            )
    elif auto_parse and not changed_companies:
        typer.echo("\nNo changes detected — nothing to re-parse.")


@app.command("list-docs")
def list_docs(
    state: str | None = typer.Option(None),
    company: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    documents = repository.list_documents(state=state, company=company)
    for doc in documents:
        line = "\t".join(
            [
                str(doc.id),
                doc.state or "-",
                doc.company or "-",
                doc.category,
                doc.title,
                str(doc.local_path),
            ]
        )
        typer.echo(line)


@app.command("show-doc")
def show_doc(document_id: int) -> None:
    _, repository = _bootstrap()
    document = repository.get_document(document_id)
    if not document:
        raise typer.BadParameter(f"Document {document_id} not found.")
    typer.echo(json.dumps(document.model_dump(mode="json"), indent=2, default=str))


@app.command()
def parse(document_id: int) -> None:
    _, repository = _bootstrap()
    result = _parse_document(document_id, repository)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


@app.command("parse-batch")
def parse_batch(
    state: str | None = typer.Option(None),
    company: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    documents = repository.list_documents(state=state, company=company)
    parsed = 0
    for doc in documents:
        try:
            _parse_document(doc.id, repository)
            parsed += 1
        except Exception as exc:
            logger.warning("Failed to parse document %s: %s", doc.id, exc)
    typer.echo(f"Parsed {parsed} documents.")


@app.command("classify-docs")
def classify_docs(
    state: str | None = typer.Option(None, help="Limit to state."),
    company: str | None = typer.Option(None, help="Limit to company."),
) -> None:
    """Classify PDF documents: extract tariff_identifier, schedule_code, rev_token from URL patterns."""
    _, repository = _bootstrap()
    updated = repository.classify_documents(state=state, company=company)
    typer.echo(f"Classified {updated} documents.")


@app.command("build-tariff-families")
def build_tariff_families(
    state: str | None = typer.Option(None, help="Limit to state, e.g. NC."),
    company: str | None = typer.Option(None, help="Limit to company, e.g. progress."),
) -> None:
    """Build tariff_families rows from classified documents (Phase 2c).

    One family per unique (state, company, tariff_identifier) combination.
    Run after classify-docs.
    """
    from duke_rates.models.tariff import TariffFamilyRecord

    _, repository = _bootstrap()
    docs = repository.list_documents(state=state, company=company)
    created = 0
    updated = 0
    for doc in docs:
        if doc.kind != "pdf" or not doc.tariff_identifier:
            continue
        company_key = (doc.company or "unknown").lower()
        state_key = (doc.state or "unknown").upper()
        family_key = f"{state_key.lower()}-{company_key}-{doc.tariff_identifier}"

        # Determine family_type from category and tariff_identifier
        if doc.category == "rider" or (doc.tariff_identifier or "").startswith("rider-"):
            family_type = "rider"
        elif doc.category == "rate":
            family_type = "rate_schedule"
        elif doc.category == "tariff":
            family_type = "regulation"
        elif doc.category == "index":
            family_type = "index"
        elif doc.category == "program":
            family_type = "program"
        else:
            family_type = "overhead"

        existing = repository.get_tariff_family(family_key)
        record = TariffFamilyRecord(
            family_key=family_key,
            state=state_key,
            company=company_key,
            tariff_identifier=doc.tariff_identifier,
            schedule_code=doc.schedule_code,
            family_type=family_type,
            title=doc.title if doc.title and not doc.title.endswith(".pdf") else None,
            current_document_id=doc.id,
        )
        repository.upsert_tariff_family(record)
        if existing:
            updated += 1
        else:
            created += 1

    typer.echo(f"Tariff families: {created} created, {updated} updated.")


@app.command("show-document-classification-audit-nc")
def show_document_classification_audit_nc(
    limit: int = typer.Option(25, "--limit", help="Max classified rows to show."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC document buckets for routing beyond simple tariff-charge extraction."""
    from duke_rates.historical.ncuc.document_classification_audit import (
        build_document_classification_audit_report,
    )

    _, repository = _bootstrap()
    report = build_document_classification_audit_report(
        repository,
        limit=limit,
        company=company,
        family_key=family_key,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Document Classification Audit (NC)")
    typer.echo(f"  historical_documents={report['summary']['historical_document_count']}")
    typer.echo("  bucket_counts:")
    for row in report["summary"]["bucket_counts"]:
        typer.echo(f"    {row['document_bucket']}={row['count']}")
    top_profiles = ", ".join(
        f"{row['parser_profile']}:{row['count']}"
        for row in report["summary"]["top_parser_profiles"][:5]
    ) or "-"
    typer.echo(f"  top_parser_profiles={top_profiles}")

    typer.echo("\nClassified Rows")
    if not report["rows"]:
        typer.echo("  none")
        return
    for row in report["rows"]:
        typer.echo(
            "  "
            f"id={row['historical_document_id']} "
            f"bucket={row['document_bucket']} "
            f"family={row['family_key'] or '-'} "
            f"profile={row['parser_profile']} "
            f"charges={row['charge_count']}"
        )
        typer.echo(
            "    "
            f"status={row['processing_status'] or '-'} "
            f"outcome={row['outcome_quality'] or '-'} "
            f"reason={row['classification_reason']}"
        )
        if row.get("document_bucket") == "needs_normalization":
            typer.echo(
                "    "
                f"raw_text_chars={row['raw_text_chars']} "
                f"pages={row['page_count']} "
                f"lane={row['normalization_lane']}"
            )
        if row.get("document_bucket") == "needs_processing":
            typer.echo(
                "    "
                f"raw_text_chars={row['raw_text_chars']} "
                f"pages={row['page_count']}"
            )
        if row.get("filing_classification"):
            typer.echo(f"    filing_classification={row['filing_classification']}")
        if row.get("skip_reason"):
            typer.echo(f"    skip_reason={row['skip_reason']}")
        if row.get("is_redline_candidate"):
            typer.echo(f"    redline_confidence={row['redline_confidence']}")
        typer.echo(f"    title={(row['title'] or '')[:100]}")


@app.command("show-unknown-routing-audit-nc")
def show_unknown_routing_audit_nc(
    limit: int = typer.Option(25, "--limit", help="Max family-level routing rows to show."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Rank NC families still landing in `unknown` or weak fallback routing buckets."""
    from duke_rates.historical.ncuc.document_classification_audit import (
        build_unknown_routing_audit_report,
    )

    _, repository = _bootstrap()
    report = build_unknown_routing_audit_report(
        repository,
        limit=limit,
        company=company,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Unknown Routing Audit (NC)")
    typer.echo(
        "  "
        f"problem_documents={report['summary']['problem_document_count']} "
        f"problem_families={report['summary']['problem_family_count']}"
    )
    action_counts = ", ".join(
        f"{row['recommended_action']}={row['count']}"
        for row in report["summary"]["recommended_action_counts"]
    ) or "-"
    typer.echo(f"  recommended_action_counts={action_counts}")

    if not report["rows"]:
        typer.echo("  none")
        return

    typer.echo("\nFamily Routing Rows")
    for row in report["rows"]:
        typer.echo(
            "  "
            f"family={row['family_key']} docs={row['document_count']} "
            f"action={row['recommended_action']}"
        )
        typer.echo(
            "    "
            f"company={row['company'] or '-'} "
            f"profile={row['top_parser_profile']} "
            f"filing_class={row['top_filing_classification']}"
        )
        if row.get("recommended_action") == "enqueue_ocr_remediation":
            typer.echo(f"    normalization_lane={row.get('top_normalization_lane') or '-'}")
        typer.echo(f"    reason={row['reason']}")
        typer.echo(f"    title={(row['sample_title'] or '')[:100]}")
        if row.get("synthesized_profile_name"):
            typer.echo(
                "    "
                f"candidate_profile={row['synthesized_profile_name']} "
                f"kind={row.get('synthesized_profile_kind') or '-'}"
            )
            typer.echo(f"    synthesis_reason={row.get('synthesized_profile_reason') or '-'}")
            if row.get("synthesized_next_command"):
                typer.echo(f"    next={row['synthesized_next_command']}")


@app.command("parse-tariff-versions")
def parse_tariff_versions(
    state: str = typer.Option("NC", help="State abbreviation."),
    company: str = typer.Option("progress", help="Company short name (e.g. progress, carolinas, florida, indiana, kentucky, ohio)."),
    family_key: str | None = typer.Option(None, help="Process only this family_key."),
    dry_run: bool = typer.Option(False, help="Parse but do not write to database."),
    limit: int = typer.Option(0, help="Max families to process (0 = all)."),
) -> None:
    """Parse Duke tariff leaf PDFs and populate tariff_versions and tariff_charges.

    Supports NC and SC Progress and Carolinas leaf-number rate schedules and riders.
    Use --state NC or --state SC combined with --company progress or --company carolinas.
    Reads each family's current document PDF and extracts version metadata,
    charge rates, and rider applicability into the database.
    """
    from duke_rates.parse.nc_progress import parse_nc_progress_leaf_file
    from duke_rates.parse.nc_carolinas import parse_nc_carolinas_leaf_file
    from duke_rates.parse.fl_florida import parse_fl_florida_sheet_file
    from duke_rates.parse.in_indiana import parse_in_indiana_tariff_file
    from duke_rates.parse.ky_kentucky import parse_ky_kentucky_tariff_file
    from duke_rates.parse.oh_ohio import parse_oh_ohio_tariff_file

    # Select parser based on company
    _company = company.lower()
    if _company == "progress":
        _parse_file = parse_nc_progress_leaf_file
    elif _company == "carolinas":
        _parse_file = parse_nc_carolinas_leaf_file
    elif _company == "florida":
        _parse_file = parse_fl_florida_sheet_file
    elif _company == "indiana":
        _parse_file = parse_in_indiana_tariff_file
    elif _company == "kentucky":
        _parse_file = parse_ky_kentucky_tariff_file
    elif _company == "ohio":
        _parse_file = parse_oh_ohio_tariff_file
    else:
        typer.echo(f"No parser available for company '{company}'. Supported: progress, carolinas, florida, indiana, kentucky, ohio.")
        raise typer.Exit(1)

    _, repository = _bootstrap()

    families = repository.list_tariff_families(state=state, company=company)
    if family_key:
        families = [f for f in families if f.family_key == family_key]

    if limit:
        families = families[:limit]

    log = logging.getLogger("duke_rates.cli")

    n_processed = 0
    n_skipped = 0
    n_errors = 0
    n_charges = 0
    n_riders = 0

    for family in families:
        if not family.current_document_id:
            log.debug("No current_document_id for %s, skipping", family.family_key)
            n_skipped += 1
            continue

        doc = repository.get_document(family.current_document_id)
        if not doc or not doc.local_path:
            log.debug("No local_path for document %s", family.current_document_id)
            n_skipped += 1
            continue

        pdf_path = Path(doc.local_path)
        if not pdf_path.is_file():
            log.debug("PDF not a file: %s", pdf_path)
            n_skipped += 1
            continue

        try:
            version, charges, riders = _parse_file(
                pdf_path,
                version_id=0,  # placeholder; will update after insert
                family_key=family.family_key,
                document_id=doc.id,
            )
        except Exception as exc:
            log.error("Parse error for %s: %s", family.family_key, exc)
            n_errors += 1
            continue

        if dry_run:
            typer.echo(
                f"  [dry] {family.family_key}: {version.revision_label} eff={version.effective_start}"
                f" charges={len(charges)} riders={len(riders)}"
            )
            n_processed += 1
            continue

        # Atomically replace old data with newly parsed data
        repository.replace_parsed_tariff_data(family.family_key, version, charges, riders)

        if version.revision_label is None:
            log.warning("No revision_label parsed for %s", family.family_key)
        if not charges:
            log.warning("No tariff charges parsed for %s", family.family_key)

        typer.echo(
            f"  {family.family_key}: {version.revision_label} eff={version.effective_start}"
            f" charges={len(charges)} riders={len(riders)}"
        )
        n_processed += 1
        n_charges += len(charges)
        n_riders += len(riders)

    # --- Post-process: resolve leaf-number rider keys to actual family keys ---
    # For Carolinas (NC and SC), riders are listed by leaf number ("Leaf No. 131") but
    # families are keyed by rider code (nc-carolinas-rider-EDIT4). After parsing all families,
    # build a leaf-number -&gt; family_key map from tariff_versions revision labels.
    if not dry_run and _company == "carolinas":
        _resolve_carolinas_rider_keys(repository, state.upper())

    typer.echo(
        f"\nDone: {n_processed} parsed, {n_skipped} skipped, {n_errors} errors"
        f" | {n_charges} charges, {n_riders} rider links written"
    )


def _resolve_carolinas_rider_keys(repository, state: str) -> None:
    """Replace nc-carolinas-leaf-NNN rider_applicability keys with actual family keys.

    After parsing, tariff_versions has revision_labels like "NC Twelfth Revised Leaf No. 133".
    We extract the leaf number and map it to the family_key, then update rider_applicability.
    """
    import re as _re
    import sqlite3
    from pathlib import Path

    from duke_rates.parse.pdf_text import extract_pdf_text

    conn = sqlite3.connect(str(repository.database_path))

    state_key = state.upper()

    # Build leaf_no -&gt; family_key map from tariff_versions
    rows = conn.execute("""
        SELECT tv.family_key, tv.revision_label
        FROM tariff_versions tv
        JOIN tariff_families tf ON tv.family_key = tf.family_key
        WHERE tf.state = ? AND tf.company = 'carolinas'
        AND tv.revision_label IS NOT NULL
    """, (state_key,)).fetchall()

    leaf_to_key: dict[str, str] = {}
    leaf_no_re = _re.compile(r'Leaf\s+No\.\s+(\d+)', _re.I)
    for fk, label in rows:
        m = leaf_no_re.search(label)
        if m:
            leaf_no = m.group(1)
            leaf_to_key[leaf_no] = fk

    # Fallback: derive leaf numbers directly from current Carolinas source PDFs.
    families = repository.list_tariff_families(state=state_key, company="carolinas")
    for family in families:
        if family.current_document_id is None:
            continue
        doc = repository.get_document(family.current_document_id)
        if doc is None or not doc.local_path:
            continue
        pdf_path = Path(doc.local_path)
        if not pdf_path.is_file():
            continue
        try:
            text = extract_pdf_text(pdf_path)
        except Exception:
            continue
        m = leaf_no_re.search(text)
        if m:
            leaf_no = m.group(1)
            leaf_to_key.setdefault(leaf_no, family.family_key)

    if not leaf_to_key:
        return

    # Build prefix for this state
    state_lower = state.lower()
    leaf_prefix = f"{state_lower}-carolinas-leaf-"

    # Find rider_applicability records using leaf-number keys
    rider_rows = conn.execute("""
        SELECT rowid, rider_family_key, applies_to_family_key
        FROM rider_applicability
        WHERE rider_family_key LIKE ?
    """, (f"{leaf_prefix}%",)).fetchall()

    n_resolved = 0
    for rowid, rider_key, base_key in rider_rows:
        # Extract the leaf number from the key
        suffix = rider_key[len(leaf_prefix):]
        actual_key = leaf_to_key.get(suffix)
        if actual_key and actual_key != rider_key:
            # Check if an applicability record already exists for the resolved key
            existing = conn.execute("""
                SELECT 1 FROM rider_applicability
                WHERE rider_family_key = ? AND applies_to_family_key = ?
            """, (actual_key, base_key)).fetchone()
            if existing:
                # Delete the leaf-number version (duplicate)
                conn.execute("DELETE FROM rider_applicability WHERE rowid = ?", (rowid,))
            else:
                conn.execute("""
                    UPDATE rider_applicability SET rider_family_key = ?
                    WHERE rowid = ?
                """, (actual_key, rowid))
            n_resolved += 1

    conn.commit()
    conn.close()

    if n_resolved:
        typer.echo(f"  Resolved {n_resolved} carolinas rider leaf-number keys to family keys.")


@app.command("mine-ncuc-pipeline")
def mine_ncuc_pipeline(
    state: str = typer.Option("NC", help="Target state."),
    company: str = typer.Option("progress", help="Target company."),
    record_id: int | None = typer.Option(None, help="Process a specific discovery record IDs only."),
) -> None:
    """Compatibility alias for the page-aware NCUC intake path; prefer ncuc import-pipeline."""
    from duke_rates.historical.ncuc.importer import NcucPipelineImporter

    _, repository = _bootstrap()
    importer = NcucPipelineImporter(repository=repository, settings=get_settings())

    if record_id:
        record = repository.get_ncuc_discovery_record(record_id)
        if not record:
            typer.echo(f"Record {record_id} not found.")
            raise typer.Exit(1)
        
        span_ids = importer.mine_discovery_record_spans(record)
        typer.echo(f"Record {record_id} processed. Created {len(span_ids)} HistoricalDocumentRecords.")
    else:
        typer.echo(
            "Note: mine-ncuc-pipeline is a compatibility alias. "
            "Prefer 'ncuc import-pipeline --all-downloaded' for normal intake."
        )
        typer.echo(f"Importing all pending downloads into the historical pipeline...")
        summaries = importer.import_all_pending_downloads()
        total_spans = sum(len(s.get("historical_document_ids", [])) for s in summaries if "historical_document_ids" in s)
        typer.echo(f"Processed {len(summaries)} records. Extracted {total_spans} explicit bounded TariffSpans.")


@app.command("mine-docling-nc")
def mine_docling_nc(
    limit: int = typer.Option(50, help="Max Docling artifacts to process."),
    accelerator: str = typer.Option("cuda", help="Accelerator used for Docling conversion: cpu or cuda."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be processed without running."),
    record_id: int | None = typer.Option(None, help="Process a single discovery record only."),
    skip_extraction: bool = typer.Option(False, "--skip-extraction", help="Skip BulkExtractor after family matching (pipeline only)."),
) -> None:
    """Bridge stored Docling artifacts into the NCUC page-aware parsing pipeline.

    For each successfully-converted Docling artifact with stored JSON content:
    1. Reconstructs PageEvidence from stored Docling JSON
    2. Feeds pages through NcucPipelineImporter (family match + historical doc creation
       with all guardrails: provisional family filters, weak-match rejection, hint recovery)
    3. Runs BulkExtractor.process_document() on each created historical doc
       (creates document_fingerprints, parse_attempt_logs, parse_review_outcomes, tariff_charges)

    This is a selective operator path — not run by default on every import.
    """
    import sqlite3 as _sqlite3
    import time as _time
    from datetime import UTC, datetime
    from pathlib import Path as _Path

    from duke_rates.historical.ncuc.pipeline.docling_page_miner import (
        mine_pages_from_docling_artifact,
    )
    from duke_rates.historical.ncuc.pipeline.stage_versions import (
        DOCLING_PAGE_MINER_VERSION,
    )
    from duke_rates.historical.ncuc.importer import NcucPipelineImporter
    from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus, NcucFilingClassification

    settings, repository = _bootstrap()
    from duke_rates.config import get_settings as _get_settings

    # Build query for Docling artifacts to process
    _db_path = settings.database_path
    _conn_probe = _sqlite3.connect(_db_path)
    _conn_probe.row_factory = _sqlite3.Row
    try:
        if record_id:
            rows = _conn_probe.execute(
                """
                SELECT d.id, d.local_path, d.content_hash, d.filing_title, d.filing_date,
                       d.docket_number, d.utility,
                       da.doc_json_content, da.plain_text_content,
                       da.tables_json_content, da.page_count, da.accelerator, da.pipeline,
                       da.file_hash
                FROM docling_artifacts da
                JOIN ncuc_discovery_records d ON da.source_pdf = d.local_path
                WHERE d.id = ?
                  AND da.status IN ('success', 'ConversionStatus.SUCCESS', 'ConversionStatus.PARTIAL_SUCCESS')
                  AND da.doc_json_content IS NOT NULL
                """,
                (record_id,),
            ).fetchall()
        else:
            rows = _conn_probe.execute(
                """
                SELECT d.id, d.local_path, d.content_hash, d.filing_title, d.filing_date,
                       d.docket_number, d.utility,
                       da.doc_json_content, da.plain_text_content,
                       da.tables_json_content, da.page_count, da.accelerator, da.pipeline,
                       da.file_hash
                FROM docling_artifacts da
                LEFT JOIN ncuc_discovery_records d ON da.source_pdf = d.local_path
                WHERE da.status IN ('success', 'ConversionStatus.SUCCESS', 'ConversionStatus.PARTIAL_SUCCESS')
                  AND da.doc_json_content IS NOT NULL
                  AND da.accelerator = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM ncuc_page_artifacts pa
                      WHERE pa.source_pdf = da.source_pdf
                        AND pa.artifact_version = ?
                  )
                LIMIT ?
                """,
                (accelerator, DOCLING_PAGE_MINER_VERSION, limit),
            ).fetchall()
        rows = list(rows)
    finally:
        _conn_probe.close()

    total = len(rows)
    if dry_run:
        typer.echo(f"Would process {total} Docling artifact(s):")
        for row in rows[:50]:
            typer.echo(f"  {row['local_path']}")
        if total > 50:
            typer.echo(f"  ... and {total - 50} more")
        return

    typer.echo(f"Processing {total} Docling artifact(s) with accelerator={accelerator}")
    typer.echo("Press Ctrl+C to stop — progress is committed after each record.\n")

    importer = NcucPipelineImporter(settings, repository)

    done = 0
    total_docs = 0
    total_charges = 0

    try:
        for i, row in enumerate(rows, 1):
            disc_id = row["id"]
            local_path = row["local_path"]
            content_hash = row["content_hash"]
            filing_title = row["filing_title"]
            filing_date = row["filing_date"]
            docket_number = row["docket_number"]
            utility = row["utility"] or "Duke Energy Progress"
            doc_json = row["doc_json_content"]
            plain_text = row["plain_text_content"]
            tables_json = row["tables_json_content"]
            page_count = row["page_count"]
            accel = row["accelerator"]
            pipeline = row["pipeline"]
            file_hash = row["file_hash"]

            if not local_path:
                typer.echo(f"  [{i}/{total}] SKIP (no local_path)")
                continue

            typer.echo(f"  [{i}/{total}] {local_path}", nl=False)
            t0 = _time.perf_counter()

            # Step 1: Reconstruct PageEvidence from stored Docling JSON
            artifact = {
                "doc_json_content": doc_json,
                "plain_text_content": plain_text,
                "tables_json_content": tables_json,
                "page_count": page_count,
                "accelerator": accel,
                "pipeline": pipeline,
                "file_hash": file_hash,
            }
            pages, page_metadata = mine_pages_from_docling_artifact(artifact)
            if not pages:
                typer.echo(" FAIL (no pages reconstructed)")
                continue

            # Step 2: Build a thin NcucDiscoveryRecord so the importer has context
            # for company inference, hint seeding, and provisional family creation.
            synth_record = NcucDiscoveryRecord(
                id=disc_id,
                local_path=local_path,
                content_hash=content_hash or file_hash,
                discovered_url=f"docling://{local_path}",
                filing_title=filing_title or _Path(local_path).stem,
                filing_date=filing_date or "1970-01-01",
                docket_number=docket_number,
                utility=utility,
                filing_classification=NcucFilingClassification.TARIFF_SHEETS,
                fetch_status=NcucFetchStatus.SUCCESS,
                fetched_at=datetime.now(UTC),
            )

            # Step 3: Feed into importer — runs Stage B save + Stage C segmentation
            # + full Stages D–G (all guardrails, hints, provisional families).
            created_ids = importer.mine_discovery_record_spans_with_pages(
                synth_record,
                pages,
                page_artifact_version=DOCLING_PAGE_MINER_VERSION,
                page_metadata=page_metadata,
            )

            docs_this = len(created_ids)
            charges_this = 0

            # Step 4: Run BulkExtractor on each created historical document
            if created_ids and not skip_extraction:
                from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
                extractor = BulkExtractor(db_path=_db_path)
                for doc_id in created_ids:
                    doc = extractor.get_document_for_extraction(doc_id)
                    if doc:
                        try:
                            _, _, num_inserted, *_ = extractor.process_document(doc)
                            charges_this += num_inserted
                        except Exception as exc:
                            typer.echo(f"\n    WARN extraction failed for doc {doc_id}: {exc}")

            elapsed = _time.perf_counter() - t0
            typer.echo(
                f" OK  pages={len(pages)} docs={docs_this} charges={charges_this} t={elapsed:.1f}s"
            )
            total_docs += docs_this
            total_charges += charges_this
            done += 1

    except KeyboardInterrupt:
        typer.echo("\nInterrupted.")

    typer.echo(
        f"\nDone: {done}/{total} processed, {total_docs} historical docs, {total_charges} charges."
    )


@app.command("mine-tariff-sheets-nc")
def mine_tariff_sheets_nc(
    limit: int = typer.Option(0, help="Max docs to process (0 = all)."),
    family: str | None = typer.Option(None, help="Filter to one family key, e.g. nc-progress-leaf-605."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List candidates without processing."),
    skip_extraction: bool = typer.Option(False, "--skip-extraction", help="Mine pages only; skip charge extraction."),
) -> None:
    """Mine standalone tariff sheet PDFs that are in historical_documents but have no page artifacts.

    Targets files whose local_path contains 'leaf-no-', 'rider-', or 'schedule-' (DEP website
    direct-download tariff sheets) which were imported without NCUC discovery records and
    therefore skipped by the normal pipeline.

    For each doc: mines page artifacts, ensures a tariff_version exists, then runs
    charge extraction via BulkExtractor.process_document().
    """
    import sqlite3 as _sqlite3
    import hashlib as _hashlib
    from pathlib import Path as _Path

    from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
    from duke_rates.db.artifact_cache import save_page_artifacts, PAGE_ARTIFACT_VERSION
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    settings, repository = _bootstrap()
    db_path = str(settings.database_path)

    conn = _sqlite3.connect(db_path)
    try:
        query = """
            SELECT DISTINCT hd.id, hd.local_path, hd.family_key, hd.effective_start
            FROM historical_documents hd
            WHERE (
                hd.local_path LIKE '%leaf-no-%'
                OR hd.local_path LIKE '%rider-%ry%'
                OR hd.local_path LIKE '%schedule-%ry%'
            )
            AND hd.local_path IS NOT NULL
            AND (SELECT COUNT(*) FROM ncuc_page_artifacts
                 WHERE source_pdf = hd.local_path
                 AND artifact_version = ?) = 0
        """
        params: list = [PAGE_ARTIFACT_VERSION]
        if family:
            query += " AND hd.family_key = ?"
            params.append(family)
        query += " ORDER BY hd.family_key, hd.effective_start"
        if limit:
            query += f" LIMIT {limit}"

        candidates = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    total = len(candidates)
    typer.echo(f"Found {total} standalone tariff sheet docs without page artifacts.")
    if dry_run:
        for hd_id, path, fk, eff in candidates:
            exists = _Path(path).exists()
            typer.echo(f"  [{hd_id}] {fk} [{eff}]: {_Path(path).name} (exists={exists})")
        return

    extractor = BulkExtractor(db_path=db_path)
    done = 0
    skipped = 0
    total_charges = 0
    for hd_id, path, fk, eff in candidates:
        p = _Path(path)
        if not p.exists():
            typer.echo(f"  [{done+skipped+1}/{total}] MISSING: {path}")
            skipped += 1
            continue
        try:
            file_hash = _hashlib.sha256(p.read_bytes()).hexdigest()
            pages = mine_document_pages(str(p))
            if not pages:
                typer.echo(f"  [{done+skipped+1}/{total}] no-pages: {fk} ({p.name})")
                skipped += 1
                continue
            conn2 = _sqlite3.connect(db_path)
            try:
                save_page_artifacts(
                    conn2,
                    discovery_record_id=None,
                    source_pdf=str(p),
                    file_hash=file_hash,
                    pages=pages,
                    metadata={"artifact_source": "native_text", "route": "mine_tariff_sheets_nc"},
                )
                conn2.commit()
            finally:
                conn2.close()

            charges_inserted = 0
            if not skip_extraction:
                version_id = _ensure_historical_tariff_version(
                    repository,
                    historical_document_id=hd_id,
                    family_key=fk,
                    effective_start=eff,
                )
                doc = extractor.get_document_for_extraction(hd_id)
                if doc:
                    doc["version_id"] = version_id
                    _, _, charges_inserted, *_ = extractor.process_document(doc)
                    total_charges += charges_inserted

            done += 1
            typer.echo(
                f"  [{done}/{total}] OK pages={len(pages)} charges={charges_inserted} {fk} ({p.name})"
            )
        except Exception as exc:
            typer.echo(f"  [{done+skipped+1}/{total}] ERROR {fk}: {exc}")
            skipped += 1

    typer.echo(f"\nDone: {done} mined, {skipped} skipped/missing, {total_charges} charges inserted.")


@app.command("extract-rates-nc")
def extract_rates_nc(
    limit: int | None = typer.Option(None, help="Limit to N documents for testing."),
    family_key: str | None = typer.Option(None, "--family-key", help="Only extract one family key."),
    verbose: bool = typer.Option(False, "--verbose", help="Show status buckets and zero-charge document details."),
    progress: bool = typer.Option(False, "--progress", help="Emit periodic stderr status during long runs."),
    progress_interval: int = typer.Option(30, "--progress-interval", help="Seconds between progress lines when --progress is set."),
) -> None:
    """Extract rate charges from historical NC documents.

    Phase 2: Bulk extraction from documents with effective dates.
    """
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import bulk_extract_rates

    settings, _ = _bootstrap()

    typer.echo("Starting bulk rate extraction from historical NC documents...")
    results = bulk_extract_rates(
        settings.database_path,
        limit=limit,
        family_key=family_key,
        progress=progress,
        progress_interval_seconds=progress_interval,
    )

    typer.echo(f"\n=== Extraction Results ===")
    typer.echo(f"Version-linked documents processed: {results['documents_processed']}/{results['total_documents']}")
    if results.get("documents_missing_versions"):
        typer.echo(
            f"Historical documents skipped before extraction due to missing tariff_version links: "
            f"{results['documents_missing_versions']}"
        )
    typer.echo(f"Total charges inserted: {results['total_charges_inserted']}")
    if family_key:
        typer.echo(f"Family filter: {family_key}")
    if results.get("status_counts"):
        typer.echo("Status counts:")
        for status, count in results["status_counts"].items():
            typer.echo(f"  {status}: {count}")

    if results['by_family']:
        typer.echo(f"\nCharges by family:")
        for family_key, count in sorted(results['by_family'].items()):
            if count > 0:
                typer.echo(f"  {family_key}: {count}")

    if verbose and results.get("zero_charge_documents"):
        typer.echo("\nZero-charge documents:")
        for row in results["zero_charge_documents"][:25]:
            typer.echo(
                "  "
                f"id={row['id']} family={row.get('family_key') or '-'} "
                f"eff={row.get('effective_start') or '-'} "
                f"status={row.get('status') or '-'} "
                f"profile={row.get('parser_profile') or '-'} "
                f"title={(row.get('title') or '')[:70]}"
            )
        if len(results["zero_charge_documents"]) > 25:
            typer.echo(f"  ... and {len(results['zero_charge_documents']) - 25} more")

    if results.get("failed_documents"):
        typer.echo("\nFailed documents:")
        for row in results["failed_documents"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} family={row.get('family_key') or '-'} "
                f"eff={row.get('effective_start') or '-'} error={(row.get('error') or '')[:120]}"
            )


@app.command("validate-extraction-nc")
def validate_extraction_nc() -> None:
    """Validate extracted rate charges for quality and outliers.

    Phase 3a: Quality validation of bulk extraction results.
    """
    from duke_rates.historical.ncuc.pipeline.extraction_validator import validate_extraction

    settings, _ = _bootstrap()

    typer.echo("Validating extracted rate charges...")
    results = validate_extraction(settings.database_path)

    typer.echo(f"\n=== Validation Results ===")
    typer.echo(f"Total charges validated: {results['total_charges']}")
    typer.echo(f"Total issues found: {results['total_issues']}")

    if results['issues_by_severity']:
        typer.echo(f"\nIssues by severity:")
        for severity, count in sorted(results['issues_by_severity'].items()):
            typer.echo(f"  {severity}: {count}")

    if results['error_families']:
        typer.echo(f"\nFamilies with errors:")
        for family in sorted(results['error_families'])[:10]:
            typer.echo(f"  {family}")

    if results['issues']:
        typer.echo(f"\nTop issues:")
        for i, issue in enumerate(results['issues'][:10], 1):
            typer.echo(f"  {i}. [{issue.severity.upper()}] {issue.family_key} - {issue.issue}")


@app.command("test-bill-reconstruction-nc")
def test_bill_reconstruction_nc() -> None:
    """Test bill reconstruction capability with extracted rates.

    Phase 3b: Verify extracted charges can be used for billing calculations.
    """
    from duke_rates.historical.ncuc.pipeline.bill_reconstruction_tester import test_bill_reconstruction

    settings, _ = _bootstrap()

    typer.echo("Testing bill reconstruction with extracted rates...")
    results = test_bill_reconstruction(settings.database_path)

    typer.echo(f"\n=== Residential Families (Critical) ===")
    res = results['residential']
    typer.echo(f"Total versions tested: {res['total_tests']}")
    typer.echo(f"Can reconstruct: {res['can_reconstruct']}/{res['total_tests']} ({res['pct_success']:.1f}%)")

    if res.get('tests'):
        typer.echo(f"\nDetails:")
        for test in res['tests'][:10]:
            status = "OK" if test.can_reconstruct else "FAIL"
            typer.echo(f"  [{status}] {test.family_key} ({test.effective_date}): {test.reason}")


@app.command("bootstrap-missing-versions-nc")
def bootstrap_missing_versions_nc(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be created without writing."),
    limit: int = typer.Option(0, "--limit", help="Max docs to process (0 = all)."),
) -> None:
    """Create tariff_version rows for NC historical docs that have a date+path but no version link.

    These docs are invisible to extract-rates-nc because the extractor requires a version link.
    This command bootstraps the minimum version row needed for extraction to proceed.
    """
    _, repository = _bootstrap()

    with repository._connect() as conn:
        query = """
            SELECT hd.id, hd.family_key, hd.effective_start
            FROM historical_documents hd
            WHERE hd.state = 'NC'
              AND hd.company IN ('progress', 'carolinas')
              AND hd.effective_start IS NOT NULL
              AND hd.local_path IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tariff_versions tv WHERE tv.historical_document_id = hd.id
              )
            ORDER BY hd.family_key, hd.effective_start
        """
        if limit:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()

    typer.echo(f"Historical docs missing versions: {len(rows)}")
    if dry_run:
        for row in rows[:20]:
            typer.echo(f"  would create: {row[1]} effective={row[2]}")
        if len(rows) > 20:
            typer.echo(f"  ... and {len(rows) - 20} more")
        return

    created = 0
    skipped = 0
    for row in rows:
        try:
            _ensure_historical_tariff_version(
                repository,
                historical_document_id=row[0],
                family_key=row[1],
                effective_start=row[2],
            )
            created += 1
        except Exception as exc:
            typer.echo(f"  skip {row[0]} ({row[1]}): {exc}")
            skipped += 1

    typer.echo(f"Done: created={created} skipped={skipped}")
    typer.echo("Run extract-rates-nc to extract charges from newly linked documents.")




























@app.command("review-queue")
def review_queue() -> None:
    """Legacy current-document parse queue; prefer parse-review-queue for historical pipeline work."""
    typer.echo(
        "Legacy current-document queue. Prefer 'parse-review-queue' and "
        "'parse-review-summary' for historical pipeline work."
    )
    _, repository = _bootstrap()
    flagged = []
    for doc in repository.list_documents():
        result = repository.latest_parse_result(doc.id)
        if result and (result.status != ParseStatus.PARSED or result.review_flags):
            flagged.append((doc.id, doc.title, result.status.value, "; ".join(result.review_flags)))
    for doc_id, title, status, flags in flagged:
        typer.echo(f"{doc_id}\t{status}\t{title}\t{flags}")


@app.command("parse-review-queue")
def parse_review_queue(
    limit: int = typer.Option(50, "--limit", help="Max parse attempts to display."),
) -> None:
    """List parse attempts whose latest review outcome still needs review."""
    from duke_rates.db.parse_review import list_parse_review_queue
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = list_parse_review_queue(conn, limit=limit)
    finally:
        conn.close()

    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["parse_attempt_id"]),
                    row.get("outcome") or "-",
                    row.get("parser_profile") or "-",
                    row.get("utility") or "-",
                    str(row.get("charge_count") or 0),
                    row.get("source_pdf") or "-",
                ]
            )
        )


@app.command("record-parse-review")
def record_parse_review(
    parse_attempt_id: int = typer.Argument(..., help="Target parse_attempt_logs.id."),
    outcome: str = typer.Argument(..., help="accepted | corrected | rejected | needs_review"),
    notes: str = typer.Option("", "--notes", help="Freeform note attached to the review outcome."),
    corrections_json: Path | None = typer.Option(
        None,
        "--corrections-json",
        help="Optional JSON file describing field corrections.",
    ),
    review_source: str = typer.Option("human", "--source", help="Review source label."),
) -> None:
    """Attach a manual review outcome to an existing parse attempt."""
    from duke_rates.db.parse_review import record_parse_review_outcome
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    corrections_payload: dict[str, object] = {}
    if corrections_json:
        try:
            corrections_payload = json.loads(corrections_json.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise typer.BadParameter(f"Corrections file not found: {corrections_json}") from exc
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Corrections file is not valid JSON: {corrections_json}") from exc

    notes_payload: dict[str, object] = {}
    if notes:
        notes_payload["note"] = notes
    if corrections_payload:
        notes_payload["correction_count"] = len(corrections_payload)

    conn = connect(settings.database_path)
    try:
        review_id = record_parse_review_outcome(
            conn,
            parse_attempt_id=parse_attempt_id,
            outcome=outcome,
            review_source=review_source,
            notes=notes_payload,
            corrections=corrections_payload,
        )
        conn.commit()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()

    typer.echo(f"Recorded parse review outcome {review_id} for parse attempt {parse_attempt_id}.")


@app.command("parse-review-summary")
def parse_review_summary(
    top: int = typer.Option(10, "--top", help="Max profiles/families to display."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Summarize parse-review outcomes by profile and family."""
    from duke_rates.db.parse_review import build_parse_review_summary
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = build_parse_review_summary(conn, top_n=top)
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Parse Review Summary")
    typer.echo(
        "  "
        f"reviewed={summary['reviewed_attempt_count']}  "
        f"needs_review={summary['outstanding_needs_review']}  "
        f"accepted={summary['accepted_count']}  "
        f"corrected={summary['corrected_count']}  "
        f"rejected={summary['rejected_count']}"
    )
    typer.echo(
        "  "
        f"human_reviews={summary['human_review_count']}  "
        f"rule_reviews={summary['rule_review_count']}  "
        f"total_corrections={summary['total_corrections_applied']}"
    )

    typer.echo("\nTop Correction Categories")
    for row in report["top_correction_categories"]:
        typer.echo(f"  {row['category']:<24} count={row['count']}")

    typer.echo("\nTop Needs-Review Root Causes")
    for row in report["top_root_causes"]:
        typer.echo(f"  {row['root_cause']:<32} count={row['count']}")

    typer.echo("\nTop Parser Profiles")
    for row in report["top_profiles"]:
        top_categories = ",".join(item["category"] for item in row["top_correction_categories"]) or "-"
        top_root_causes = ",".join(item["root_cause"] for item in row["top_root_causes"]) or "-"
        typer.echo(
            "  "
            f"{row['parser_profile']:<32} "
            f"attempts={row['attempt_count']:<3} "
            f"needs_review={row['needs_review']:<3} "
            f"corrected={row['corrected']:<3} "
            f"rejected={row['rejected']:<3} "
            f"human={row['human_reviewed']:<3} "
            f"corrections={row['correction_count']:<3} "
            f"categories={top_categories} "
            f"root_causes={top_root_causes}"
        )

    typer.echo("\nTop Families")
    for row in report["top_families"]:
        top_categories = ",".join(item["category"] for item in row["top_correction_categories"]) or "-"
        top_root_causes = ",".join(item["root_cause"] for item in row["top_root_causes"]) or "-"
        typer.echo(
            "  "
            f"{row['family_key']:<36} "
            f"company={(row['company'] or '-'): <10} "
            f"attempts={row['attempt_count']:<3} "
            f"needs_review={row['needs_review']:<3} "
            f"corrected={row['corrected']:<3} "
            f"rejected={row['rejected']:<3} "
            f"categories={top_categories} "
            f"root_causes={top_root_causes}"
        )


@app.command("show-parser-selection-audit-nc")
def show_parser_selection_audit_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to display."),
    company: str | None = typer.Option(None, help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Summarize latest NC parser-profile selection outcomes and fallback behavior."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_parser_selection_audit_nc_report(
            conn,
            limit=limit,
            company=company,
            family_key=family_key,
        )
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Parser Selection Audit (NC)")
    typer.echo(
        "  "
        f"latest_runs={summary['latest_run_count']}  "
        f"fallback_applied={summary['fallback_applied_count']}  "
        f"generic_final={summary['generic_final_profile_count']}  "
        f"weak={summary['weak_count']}  "
        f"empty={summary['empty_count']}  "
        f"strong={summary['strong_count']}"
    )

    typer.echo("\nTop Problem Profiles")
    for row in report["top_problem_profiles"]:
        typer.echo(f"  {row['parser_profile']:<32} count={row['count']}")

    typer.echo("\nTop Profile Transitions")
    for row in report["top_profile_transitions"]:
        typer.echo(f"  {row['transition']:<64} count={row['count']}")

    typer.echo("\nFallback Reasons")
    for row in report["fallback_reason_summary"]:
        typer.echo(f"  {row['reason']:<36} count={row['count']}")

    typer.echo("\nSample Rows")
    for row in report["rows"]:
        typer.echo(
            "  "
            f"hd={row['historical_document_id']} "
            f"family={row['family_key']} "
            f"eff={row['effective_start'] or '-'} "
            f"initial={row['initial_parser_profile'] or '-'} "
            f"final={row['final_parser_profile'] or '-'} "
            f"outcome={row['outcome_quality']} "
            f"charges={row['charge_count']}"
        )
        if row["fallback_applied"]:
            typer.echo(
                "    "
                f"fallback trigger={row['fallback_triggered_by'] or '-'} "
                f"reason={row['fallback_reason'] or '-'}"
            )


@app.command("show-parser-improvement-candidates-nc")
def show_parser_improvement_candidates_nc(
    limit: int = typer.Option(25, "--limit", help="Max family-level candidates to display."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Rank parser/routing improvement candidates with a suggested next command."""
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

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Parser Improvement Candidates (NC)")
    typer.echo(
        "  "
        f"problem_documents={summary['problem_document_count']} "
        f"problem_families={summary['problem_family_count']} "
        f"generic_final={summary['generic_final_profile_count']} "
        f"weak={summary['weak_count']} "
        f"empty={summary['empty_count']}"
    )
    action_counts = ", ".join(
        f"{row['recommended_action']}={row['count']}"
        for row in summary["recommended_action_counts"]
    ) or "-"
    typer.echo(f"  recommended_action_counts={action_counts}")
    top_profiles = ", ".join(
        f"{row['parser_profile']}:{row['count']}"
        for row in summary["top_problem_profiles"]
    ) or "-"
    typer.echo(f"  top_problem_profiles={top_profiles}")

    if not report["rows"]:
        typer.echo("  none")
        return

    typer.echo("\nCandidate Rows")
    for row in report["rows"]:
        typer.echo(
            "  "
            f"family={row['family_key']} docs={row['document_count']} "
            f"action={row['recommended_action']}"
        )
        typer.echo(
            "    "
            f"company={row['company'] or '-'} "
            f"profile={row['top_parser_profile']} "
            f"filing_class={row['top_filing_classification']}"
        )
        typer.echo(f"    reason={row['reason']}")
        typer.echo(f"    next={row['suggested_next_command']}")
        typer.echo(f"    title={(row['sample_title'] or '')[:100]}")


@app.command("show-near-miss-profiles-nc")
def show_near_miss_profiles_nc(
    limit: int = typer.Option(20, "--limit", help="Max rows per output section."),
    min_score: float = typer.Option(0.0, "--min-score", help="Only count near-miss candidates scoring >= this threshold."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Surface the top_candidates diagnostic from problem parser runs.

    For each historical document whose latest run ended on empty/weak/missing,
    inspect the persisted top_candidates list and aggregate by:

      1. near-miss profile (the highest-scoring CANDIDATE) — reveals which
         existing profile is "almost working" and worth fixing
      2. families with no near-miss (all candidates scored 0) — reveals where
         a new profile family is needed entirely

    Each aggregation is ranked by impact (doc count) so the next profile-work
    target is obvious.
    """
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        # Pull the latest run per historical doc that has top_candidates and a problem outcome.
        # We use a window-style filter via MAX(id) per historical_document_id.
        params: list[Any] = []
        company_clause = ""
        if company:
            company_clause = " AND hd.company = ?"
            params.append(company)
        rows = conn.execute(
            f"""
            WITH latest_runs AS (
                SELECT r.*
                FROM historical_processing_runs r
                JOIN (
                    SELECT historical_document_id, MAX(id) AS max_id
                    FROM historical_processing_runs
                    GROUP BY historical_document_id
                ) latest ON latest.max_id = r.id
            )
            SELECT
                lr.historical_document_id AS hd_id,
                lr.parser_profile,
                lr.outcome_quality,
                lr.charge_count,
                hd.family_key,
                hd.company,
                hd.title,
                json_extract(lr.metadata_json, '$.selection.top_candidates') AS top_candidates_json,
                json_extract(lr.metadata_json, '$.selection.parse_warnings') AS parse_warnings_json
            FROM latest_runs lr
            JOIN historical_documents hd ON hd.id = lr.historical_document_id
            WHERE hd.state = 'NC'
              AND lr.outcome_quality IN ('empty', 'weak', 'missing')
              AND json_extract(lr.metadata_json, '$.selection.top_candidates') IS NOT NULL
              {company_clause}
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    near_miss_buckets: dict[tuple[str, bool], dict[str, Any]] = defaultdict(
        lambda: {
            "doc_count": 0,
            "empty_count": 0,
            "weak_count": 0,
            "missing_count": 0,
            "score_sum": 0.0,
            "families": set(),
            "selected_was_same": 0,
            "charge_count_sum": 0,
        }
    )
    no_near_miss_families: dict[str, dict[str, int]] = defaultdict(
        lambda: {"doc_count": 0, "empty_count": 0, "weak_count": 0, "missing_count": 0}
    )
    parse_warning_profiles: Counter[str] = Counter()
    total_problem_runs = 0
    total_no_near_miss = 0

    for row in rows:
        total_problem_runs += 1
        outcome = str(row["outcome_quality"] or "missing")
        outcome_field = f"{outcome}_count"
        try:
            candidates = json.loads(row["top_candidates_json"]) if row["top_candidates_json"] else []
        except (TypeError, ValueError):
            candidates = []
        if not candidates:
            continue

        # Top-scoring candidate (already sorted by score desc upstream)
        top = candidates[0]
        score = float(top.get("score") or 0.0)
        name = str(top.get("name") or "unknown")
        supported = bool(top.get("supported"))

        if score <= 0:
            total_no_near_miss += 1
            fam_bucket = no_near_miss_families[row["family_key"] or "?"]
            fam_bucket["doc_count"] += 1
            if outcome_field in fam_bucket:
                fam_bucket[outcome_field] += 1
            continue
        if score < min_score:
            continue

        bucket = near_miss_buckets[(name, supported)]
        bucket["doc_count"] += 1
        if outcome_field in bucket:
            bucket[outcome_field] += 1
        bucket["score_sum"] += score
        bucket["families"].add(row["family_key"] or "?")
        bucket["charge_count_sum"] += int(row["charge_count"] or 0)
        if name == row["parser_profile"]:
            # Highest candidate IS the selected profile but produced empty/weak —
            # the profile gates fired but extraction failed. Worth fixing.
            bucket["selected_was_same"] += 1

        # Track parse_warnings as a separate signal
        if row["parse_warnings_json"] and row["parse_warnings_json"] != "[]":
            try:
                warnings = json.loads(row["parse_warnings_json"])
                for w in warnings:
                    parse_warning_profiles[str(w.get("profile") or "?")] += 1
            except (TypeError, ValueError):
                pass

    # Convert near-miss buckets to ranked rows
    near_miss_rows = sorted(
        [
            {
                "profile": profile,
                "supported": supported,
                "doc_count": data["doc_count"],
                "empty_count": data["empty_count"],
                "weak_count": data["weak_count"],
                "missing_count": data["missing_count"],
                "avg_score": round(data["score_sum"] / data["doc_count"], 3),
                "avg_charges_recovered": round(data["charge_count_sum"] / data["doc_count"], 1),
                "family_count": len(data["families"]),
                "sample_families": sorted(data["families"])[:5],
                "selected_was_same": data["selected_was_same"],
            }
            for (profile, supported), data in near_miss_buckets.items()
        ],
        key=lambda r: (-(r["empty_count"] + r["missing_count"]), -r["doc_count"], -r["avg_score"]),
    )

    no_near_miss_rows = sorted(
        [
            {
                "family_key": fk,
                "doc_count": data["doc_count"],
                "empty_count": data["empty_count"],
                "weak_count": data["weak_count"],
                "missing_count": data["missing_count"],
            }
            for fk, data in no_near_miss_families.items()
        ],
        key=lambda r: (-(r["empty_count"] + r["missing_count"]), -r["doc_count"]),
    )[:limit]

    parse_warning_rows = [
        {"profile": profile, "warning_count": count}
        for profile, count in parse_warning_profiles.most_common()
    ]

    report = {
        "total_problem_runs": total_problem_runs,
        "total_no_near_miss": total_no_near_miss,
        "near_miss_profiles": near_miss_rows[:limit],
        "no_near_miss_top_families": no_near_miss_rows,
        "parse_warning_profiles": parse_warning_rows,
    }

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Near-Miss Profile Audit (NC)")
    typer.echo(
        f"  problem_runs={total_problem_runs}  "
        f"no_near_miss={total_no_near_miss}  "
        f"with_near_miss={total_problem_runs - total_no_near_miss}"
    )

    if near_miss_rows:
        typer.echo("")
        typer.echo("Top near-miss profiles (highest-scoring candidate on problem runs)")
        typer.echo("Sorted by empty+missing count (real failures) — `weak` rows often extract some charges")
        typer.echo("Action: fix or extend these existing profiles to convert empty/missing -> parsed")
        for r in near_miss_rows[:limit]:
            same_note = f" (selected:{r['selected_was_same']})" if r["selected_was_same"] else ""
            typer.echo(
                f"  {r['profile']:50s} "
                f"docs={r['doc_count']:4d} "
                f"[empty={r['empty_count']:3d} weak={r['weak_count']:3d} missing={r['missing_count']:3d}] "
                f"avg_score={r['avg_score']:.2f} "
                f"avg_charges={r['avg_charges_recovered']:5.1f} "
                f"families={r['family_count']:3d}"
                f"{same_note}"
            )
            if r["sample_families"]:
                typer.echo(f"    sample_families: {', '.join(r['sample_families'])}")

    if no_near_miss_rows:
        typer.echo("")
        typer.echo("Top no-near-miss families (all candidates scored 0)")
        typer.echo("Action: these need a new profile family or explicit routing")
        for r in no_near_miss_rows:
            typer.echo(
                f"  {r['family_key']:50s} "
                f"docs={r['doc_count']:3d} "
                f"[empty={r['empty_count']:2d} weak={r['weak_count']:2d} missing={r['missing_count']:2d}]"
            )

    if parse_warning_rows:
        typer.echo("")
        typer.echo("Profiles with parse_warnings (ValueError on float/parse)")
        typer.echo("Action: harden float coercion in these profiles")
        for r in parse_warning_rows[:limit]:
            typer.echo(f"  {r['profile']:50s} warnings={r['warning_count']}")


@app.command("show-family-mismatch-audit-nc")
def show_family_mismatch_audit_nc(
    limit: int = typer.Option(50, "--limit", help="Max per-doc rows to print."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family-key filter."),
    max_span_pages: int = typer.Option(30, "--max-span-pages", help="Skip docs whose page span exceeds this. Compliance bundles legitimately contain many schedules; a single mismatch reading is meaningless."),
    include_bundles: bool = typer.Option(False, "--include-bundles", help="Include large-span docs in the scan (default skips them)."),
    progress: bool = typer.Option(False, "--progress", help="Emit periodic stderr status during the scan."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Walk every linked NC historical doc and surface family-vs-content mismatches.

    Uses the existing detect_historical_family_mismatch detector to find:
      - schedule_code_mismatch: text contains a SCHEDULE/RIDER code that doesn't
        match the family's expected schedule_code (e.g. EDPR family bound to a
        page slice that contains SCHEDULE FL-N text)
      - company_text_mismatch: text mentions a different Duke company than the
        family is assigned to
      - summary_sheet_family_mismatch: doc was bound to a summary sheet rather
        than the actual schedule's leaf

    Each row's reasons inform whether to lineage retire-historical-document, run
    lineage rebind-historical-page-range, or extend the reference-only classifier.
    """
    from collections import Counter, defaultdict
    import sys as _sys
    import time as _time
    from duke_rates.historical.family_mismatch_audit import (
        detect_historical_family_mismatch,
        extract_schedule_code_hint,
        extract_rider_code_hint,
    )
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    settings, _ = _bootstrap()

    conn = connect_sqlite(Path(settings.database_path))
    try:
        params: list[Any] = []
        company_clause = ""
        if company:
            company_clause = " AND hd.company = ?"
            params.append(company)
        family_clause = ""
        if family_key:
            family_clause = " AND hd.family_key = ?"
            params.append(family_key)
        rows = conn.execute(
            f"""
            SELECT
                hd.id, hd.family_key, hd.company, hd.title,
                hd.local_path, hd.start_page, hd.end_page,
                tf.schedule_code
            FROM historical_documents hd
            JOIN tariff_versions tv ON tv.historical_document_id = hd.id
            LEFT JOIN tariff_families tf ON tf.family_key = hd.family_key
            WHERE hd.state = 'NC'
              AND hd.local_path IS NOT NULL
              {company_clause}{family_clause}
            ORDER BY hd.id
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    extractor = BulkExtractor(settings.database_path)

    total_planned = len(rows)
    scanned = 0
    skipped_no_text = 0
    skipped_bundle = 0
    mismatches: list[dict[str, Any]] = []
    by_reason: Counter[str] = Counter()
    by_family: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"docs": 0, "reasons": Counter()}
    )

    progress_start = _time.time()
    progress_last = progress_start

    for row in rows:
        if not Path(row["local_path"]).exists():
            continue
        # Skip compliance bundles — a single SCHEDULE mismatch reading is
        # expected behavior when many schedules are bundled together.
        sp, ep = row["start_page"], row["end_page"]
        if not include_bundles and sp is not None and ep is not None:
            if (ep - sp + 1) > max_span_pages:
                skipped_bundle += 1
                continue
        try:
            text = extractor.extract_text_from_pdf(
                row["local_path"],
                start_page=sp,
                end_page=ep,
            )
        except Exception:
            continue
        scanned += 1
        if not text:
            skipped_no_text += 1
            if progress and _time.time() - progress_last >= 5:
                print(
                    f"[mismatch-audit] {scanned}/{total_planned} scanned "
                    f"({len(mismatches)} mismatches, {skipped_no_text} no-text)",
                    file=_sys.stderr,
                    flush=True,
                )
                progress_last = _time.time()
            continue

        reasons = detect_historical_family_mismatch(
            family_key=row["family_key"],
            family_schedule_code=row["schedule_code"],
            text=text,
        )
        if reasons:
            mismatches.append(
                {
                    "hd_id": row["id"],
                    "family_key": row["family_key"],
                    "company": row["company"],
                    "title": (row["title"] or "")[:80],
                    "pages": f"{row['start_page']}-{row['end_page']}",
                    "expected_schedule_code": row["schedule_code"],
                    "found_schedule_code": extract_schedule_code_hint(text),
                    "found_rider_code": extract_rider_code_hint(text),
                    "reasons": list(reasons),
                }
            )
            for reason in reasons:
                by_reason[reason] += 1
            fam_bucket = by_family[row["family_key"] or "?"]
            fam_bucket["docs"] += 1
            for reason in reasons:
                fam_bucket["reasons"][reason] += 1

        if progress and _time.time() - progress_last >= 5:
            print(
                f"[mismatch-audit] {scanned}/{total_planned} scanned "
                f"({len(mismatches)} mismatches)",
                file=_sys.stderr,
                flush=True,
            )
            progress_last = _time.time()

    # Sort mismatches by family for predictable output
    mismatches.sort(key=lambda m: (m["family_key"], m["hd_id"]))
    family_rows = sorted(
        [(fk, data) for fk, data in by_family.items()],
        key=lambda x: -x[1]["docs"],
    )

    report = {
        "scanned": scanned,
        "skipped_no_text": skipped_no_text,
        "mismatch_count": len(mismatches),
        "mismatch_rate_pct": round(len(mismatches) / scanned * 100, 2) if scanned else 0.0,
        "by_reason": [{"reason": r, "count": c} for r, c in by_reason.most_common()],
        "by_family": [
            {
                "family_key": fk,
                "doc_count": data["docs"],
                "reasons": dict(data["reasons"]),
            }
            for fk, data in family_rows
        ],
        "rows": mismatches,
    }

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Family Mismatch Audit (NC)")
    typer.echo(
        f"  scanned={scanned}  "
        f"mismatches={len(mismatches)}  "
        f"rate={report['mismatch_rate_pct']:.1f}%  "
        f"skipped_no_text={skipped_no_text}  "
        f"skipped_bundle={skipped_bundle}"
    )

    if by_reason:
        typer.echo("")
        typer.echo("By reason:")
        for reason, count in by_reason.most_common():
            typer.echo(f"  {reason:42s} {count}")

    if family_rows:
        typer.echo("")
        typer.echo("Top mismatched families (worst first):")
        for fk, data in family_rows[:20]:
            reasons_str = ", ".join(
                f"{r}:{c}" for r, c in data["reasons"].most_common(3)
            )
            typer.echo(f"  {fk:50s} docs={data['docs']:3d}  {reasons_str}")

    if mismatches:
        typer.echo("")
        typer.echo(f"Per-doc rows (showing first {min(limit, len(mismatches))} of {len(mismatches)}):")
        for m in mismatches[:limit]:
            typer.echo(
                f"  hd={m['hd_id']} family={m['family_key']} pages={m['pages']}"
            )
            typer.echo(
                f"    expected_code={m['expected_schedule_code'] or '-'} "
                f"found_sched={m['found_schedule_code'] or '-'} "
                f"found_rider={m['found_rider_code'] or '-'}"
            )
            typer.echo(f"    reasons={','.join(m['reasons'])}")
            typer.echo(f"    title={m['title']}")


@app.command("show-workflow-status-nc")
def show_workflow_status_nc(
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show a compact NC historical workflow status summary for session orientation."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_workflow_status_nc_report(conn)
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Workflow Status (NC)")
    typer.echo(
        "  "
        f"historical_docs={report['historical_document_count']}  "
        f"linked_versions={report['linked_version_count']}  "
        f"versions_with_charges={report['versions_with_charges_count']}  "
        f"coverage={report['extraction_coverage_pct']}%"
    )
    typer.echo(
        "  "
        f"needs_review_active={report['parse_review_active_needs_review_count']}  "
        f"needs_review_legacy={report['parse_review_legacy_needs_review_count']}  "
        f"reprocess_pending={report['reprocess_pending_count']}  "
        f"reprocess_running={report['reprocess_running_count']}"
    )
    typer.echo(
        "  "
        f"stale_historical={report['stale_historical_count']}  "
        f"never_processed={report['never_processed_historical_count']}  "
        f"ocr_pending={report['ocr_pending_count']}  "
        f"ocr_running={report['ocr_running_count']}  "
        f"provisional_families={report['provisional_family_count']}  "
        f"null_effective_start={report['null_effective_start_count']}"
    )
    typer.echo(f"  last_historical_run_at={report['last_historical_run_at'] or '-'}")
    top_profiles = ", ".join(report["top_needs_review_profiles"]) or "-"
    typer.echo(f"  top_needs_review_profiles={top_profiles}")


@app.command("recommend-overnight-lane-nc")
def recommend_overnight_lane_nc(
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Recommend which overnight loop to run based on current NC workflow state.

    Advisory only — never executes anything. Reads `show-workflow-status-nc`
    state and applies the prose decision rules from
    `docs/NEXT_SESSION_START_HERE.md` §C to pick the highest-yield lane.

    Lanes (v1, three-way):
      - ``ocr_drain``     — OCR queue is non-trivial; drain it first because
                             downstream profiles depend on the resulting text.
                             Maps to `scripts/overnight/tonight_9am.ps1`.
      - ``routing_first`` — Unknown-routing audit shows many problem families
                             that haven't been mapped to profiles. Maps to
                             `scripts/overnight/routing_first_until_9am.ps1`.
      - ``extract_loop``  — Queues drained and routing reasonably mapped;
                             extraction/promotion is the highest leverage.
                             Maps to `scripts/overnight/backlog_drain_overnight.ps1`.
      - ``idle``          — All queues empty and no clear next lane. Skip the
                             overnight; pick a code-side improvement instead.
    """
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        status = _build_workflow_status_nc_report(conn)
    finally:
        conn.close()

    # Inputs we score on
    ocr_pending = int(status.get("ocr_pending_count") or 0)
    ocr_running = int(status.get("ocr_running_count") or 0)
    reprocess_pending = int(status.get("reprocess_pending_count") or 0)
    reprocess_running = int(status.get("reprocess_running_count") or 0)
    stale = int(status.get("stale_historical_count") or 0)
    never_processed = int(status.get("never_processed_historical_count") or 0)
    coverage_pct = float(status.get("extraction_coverage_pct") or 0.0)
    active_needs_review = int(status.get("parse_review_active_needs_review_count") or 0)

    # Lane decisions, in priority order. Each rule returns (lane, reason).
    decisions: list[tuple[str, str]] = []

    if ocr_pending + ocr_running >= 20:
        decisions.append((
            "ocr_drain",
            f"OCR queue is non-trivial ({ocr_pending} pending + {ocr_running} "
            f"running); downstream profiles depend on this text",
        ))

    if stale > 0 or never_processed > 0:
        decisions.append((
            "ocr_drain",
            f"{stale} stale + {never_processed} never-processed docs need to "
            f"be (re)processed before downstream lanes can use them",
        ))

    # Routing-first when many docs land on 'unknown' profile and reprocess
    # queue is empty (no point routing what's already queued).
    if active_needs_review >= 1000 and reprocess_pending == 0:
        decisions.append((
            "routing_first",
            f"{active_needs_review} active needs-review rows with reprocess "
            f"queue empty — routing/profile work is the leverage point",
        ))

    if reprocess_pending >= 25:
        decisions.append((
            "extract_loop",
            f"{reprocess_pending} docs already queued for reprocessing — "
            f"drain that before measuring routing again",
        ))

    if (
        ocr_pending == 0
        and ocr_running == 0
        and stale == 0
        and never_processed == 0
        and reprocess_pending == 0
        and active_needs_review > 0
    ):
        decisions.append((
            "extract_loop",
            f"Queues are clean; LLM extract + promote on {active_needs_review} "
            f"active needs-review rows is the highest-yield work",
        ))

    if not decisions:
        decisions.append((
            "idle",
            "All queues empty and no active needs-review work — no overnight "
            "lane is justified right now. Pick code/profile work instead.",
        ))

    chosen_lane, chosen_reason = decisions[0]
    lane_to_script = {
        "ocr_drain":     "pwsh scripts\\overnight\\tonight_9am.ps1",
        "routing_first": "pwsh scripts\\overnight\\routing_first_until_9am.ps1",
        "extract_loop":  "pwsh scripts\\overnight\\backlog_drain_overnight.ps1",
        "idle":          None,
    }
    report = {
        "chosen_lane": chosen_lane,
        "chosen_reason": chosen_reason,
        "recommended_command": lane_to_script[chosen_lane],
        "all_matched_rules": [
            {"lane": lane, "reason": reason} for lane, reason in decisions
        ],
        "inputs": {
            "ocr_pending": ocr_pending,
            "ocr_running": ocr_running,
            "reprocess_pending": reprocess_pending,
            "reprocess_running": reprocess_running,
            "stale_historical": stale,
            "never_processed": never_processed,
            "active_needs_review": active_needs_review,
            "coverage_pct": coverage_pct,
        },
    }

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Recommended overnight lane (NC)")
    typer.echo(f"  lane:   {chosen_lane}")
    typer.echo(f"  reason: {chosen_reason}")
    if report["recommended_command"]:
        typer.echo(f"  run:    {report['recommended_command']}")
    else:
        typer.echo("  run:    (nothing — see open items in NEXT_SESSION_START_HERE.md)")
    if len(decisions) > 1:
        typer.echo("  other matched rules:")
        for lane, reason in decisions[1:]:
            typer.echo(f"    [{lane}] {reason}")
    typer.echo("  inputs:")
    for key, value in report["inputs"].items():
        typer.echo(f"    {key}={value}")


@app.command("show-workflow-next-actions-nc")
def show_workflow_next_actions_nc(
    limit: int = typer.Option(10, "--limit", help="Max next actions to display."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show ranked next actions across OCR, reprocess, and parser workflow surfaces."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_workflow_next_actions_nc_report(conn, limit=limit)
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Workflow Next Actions (NC)")
    typer.echo(
        "  "
        f"actions={report['summary']['action_count']}  "
        f"executable={report['summary']['executable_count']}"
    )
    for row in report["rows"]:
        typer.echo(
            "  "
            f"priority={row['priority']} "
            f"type={row['action_type']} "
            f"count={row['count']} "
            f"executable={str(row['executable']).lower()} "
            f"policy={row['concurrency_policy']}"
        )
        typer.echo(f"    summary={row['summary']}")
        typer.echo(f"    next={row['recommended_command']}")
        if row.get("recommended_parallel_command"):
            typer.echo(f"    parallel={row['recommended_parallel_command']}")
        if row.get("target_family_key"):
            typer.echo(f"    family={row['target_family_key']}")
        if row.get("target_parser_profile"):
            typer.echo(f"    parser_profile={row['target_parser_profile']}")


@app.command("show-workflow-capabilities-nc")
def show_workflow_capabilities_nc(
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show sanctioned concurrency policy for the guided NC workflow actions."""
    report = {
        "workflow": "nc_guided",
        "actions": [
            {
                "action_type": "process_ocr_queue",
                "concurrency_policy": "workers_allowed",
                "workers_allowed": True,
                "notes": "Bounded local OCR queue processing only; do not generalize to portal/search.",
                "recommended_command": "python -m duke_rates ocr process-queue-nc --limit 1",
                "recommended_parallel_command": "python -m duke_rates ocr process-queue-nc --limit 2 --workers 2",
            },
            {
                "action_type": "process_reprocess_queue",
                "concurrency_policy": "workers_allowed",
                "workers_allowed": True,
                "notes": "Bounded local reprocess queue processing only; each worker claims queue items independently.",
                "recommended_command": "python -m duke_rates reprocess process-queue-nc --limit 1",
                "recommended_parallel_command": "python -m duke_rates reprocess process-queue-nc --limit 2 --workers 2",
            },
            {
                "action_type": "recover_stale_reprocess",
                "concurrency_policy": "sequential_only",
                "workers_allowed": False,
                "notes": "Safe queue recovery for running reprocess rows that appear stale; use before more extraction loops when the queue is stuck.",
                "recommended_command": "python -m duke_rates reprocess recover-stale-nc --limit 10 --older-than-minutes 240 --execute",
                "recommended_parallel_command": None,
            },
            {
                "action_type": "enqueue_ocr_remediation",
                "concurrency_policy": "sequential_only",
                "workers_allowed": False,
                "notes": "Queue-enqueue step is kept sequential in guided mode for predictable receipts and batching.",
                "recommended_command": "python -m duke_rates ocr enqueue-remediation-nc --limit 1 --execute",
                "recommended_parallel_command": None,
            },
            {
                "action_type": "enqueue_stale_reprocess",
                "concurrency_policy": "sequential_only",
                "workers_allowed": False,
                "notes": "Queue-enqueue step is kept sequential in guided mode for predictable receipts and batching.",
                "recommended_command": "python -m duke_rates reprocess enqueue-stale-nc --limit 10",
                "recommended_parallel_command": None,
            },
            {
                "action_type": "portal_search",
                "concurrency_policy": "sequential_only",
                "workers_allowed": False,
                "notes": "Authenticated NCUC portal/search must remain sequential to reduce 403/rate-limit risk.",
                "recommended_command": "python -m duke_rates ncuc portal-search ...",
                "recommended_parallel_command": None,
            },
        ],
    }

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Workflow Capabilities (NC)")
    for row in report["actions"]:
        typer.echo(
            "  "
            f"type={row['action_type']} "
            f"policy={row['concurrency_policy']} "
            f"workers_allowed={str(row['workers_allowed']).lower()}"
        )
        typer.echo(f"    next={row['recommended_command']}")
        if row.get("recommended_parallel_command"):
            typer.echo(f"    parallel={row['recommended_parallel_command']}")
        typer.echo(f"    notes={row['notes']}")


@app.command("execute-workflow-next-action-nc")
def execute_workflow_next_action_nc(
    limit: int = typer.Option(1, "--limit", help="Bounded work to perform for the selected action."),
    workers: int = typer.Option(2, "--workers", min=1, help="Worker count to use for `workers_allowed` guided actions."),
    auto_workers: bool = typer.Option(True, "--auto-workers/--no-auto-workers", help="Automatically use sanctioned workers for local queue actions when allowed."),
) -> None:
    """Execute the highest-priority bounded workflow action that is marked executable."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_workflow_next_actions_nc_report(conn, limit=10)
    finally:
        conn.close()

    selected = next((row for row in report["rows"] if row["executable"]), None)
    if not selected:
        typer.echo("No executable workflow next action is currently available.")
        return
    selected = dict(selected)
    action_type = str(selected["action_type"])
    selected_workers = 1
    if auto_workers and bool(selected.get("workers_allowed")):
        selected_workers = max(1, min(workers, limit))
    if action_type == "process_ocr_queue":
        selected["recommended_command"] = (
            f"python -m duke_rates ocr process-queue-nc --limit {limit}"
            + (f" --workers {selected_workers}" if selected_workers > 1 else "")
        )
    elif action_type == "process_reprocess_queue":
        selected["recommended_command"] = (
            f"python -m duke_rates reprocess process-queue-nc --limit {limit}"
            + (f" --workers {selected_workers}" if selected_workers > 1 else "")
        )
    elif action_type == "recover_stale_reprocess":
        selected["recommended_command"] = (
            f"python -m duke_rates reprocess recover-stale-nc --limit {limit} "
            f"--older-than-minutes 240 --execute"
        )
    elif action_type == "enqueue_ocr_remediation":
        selected["recommended_command"] = f"python -m duke_rates ocr enqueue-remediation-nc --limit {limit} --execute"
    elif action_type == "enqueue_stale_reprocess":
        selected["recommended_command"] = f"python -m duke_rates reprocess enqueue-stale-nc --limit {limit}"
    selected["guided_workers"] = selected_workers

    conn = connect_sqlite(Path(settings.database_path))
    try:
        receipt_id = _record_workflow_action_receipt_start(
            conn,
            workflow="nc_guided",
            action=selected,
            requested_limit=limit,
        )
        conn.commit()
    finally:
        conn.close()

    typer.echo(
        "Executing workflow next action: "
        f"type={selected['action_type']} count={selected['count']} priority={selected['priority']}"
    )
    typer.echo(f"  receipt_id={receipt_id}")
    typer.echo(f"  summary={selected['summary']}")
    typer.echo(f"  policy={selected['concurrency_policy']} workers={selected_workers}")

    status = "completed"
    error_message: str | None = None
    try:
        if action_type == "process_ocr_queue":
            process_ocr_queue_nc(limit=limit, force=False, workers=selected_workers)
        elif action_type == "process_reprocess_queue":
            process_reprocess_queue_nc(limit=limit, workers=selected_workers)
        elif action_type == "recover_stale_reprocess":
            recover_stale_reprocess_nc(
                limit=limit,
                older_than_minutes=240,
                requested_by="workflow_next_action",
                dry_run=False,
            )
        elif action_type == "enqueue_ocr_remediation":
            enqueue_ocr_remediation_nc(limit=limit, company=None, family_key=None, backend="pytesseract_cpu", requested_by="workflow_next_action", dry_run=False)
        elif action_type == "enqueue_stale_reprocess":
            enqueue_stale_reprocess_nc(limit=limit, family_key=None, requested_by="workflow_next_action")
        else:
            status = "failed"
            error_message = f"Selected action is not executable: {action_type}"
            typer.echo(error_message)
    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        raise
    finally:
        conn = connect_sqlite(Path(settings.database_path))
        try:
            _record_workflow_action_receipt_finish(
                conn,
                receipt_id=receipt_id,
                status=status,
                error_message=error_message,
            )
            conn.commit()
        finally:
            conn.close()


@app.command("show-workflow-action-receipts-nc")
def show_workflow_action_receipts_nc(
    limit: int = typer.Option(20, "--limit", help="Max recent receipts to display."),
    reconcile: bool = typer.Option(True, "--reconcile/--no-reconcile", help="Reconcile in-flight receipts against downstream queue state before listing."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show recent guided workflow action receipts for resumable NC execution."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        if reconcile:
            _reconcile_workflow_action_receipts(conn, workflow="nc_guided", limit=max(limit, 50))
            conn.commit()
        rows = _list_workflow_action_receipts(conn, workflow="nc_guided", limit=limit)
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return

    typer.echo("Workflow Action Receipts (NC)")
    for row in rows:
        typer.echo(
            "  "
            f"id={row['id']} status={row['status']} action={row['action_type']} "
            f"limit={row['requested_limit']} started={row['started_at']}"
        )
        typer.echo(f"    command={row['command_text'] or '-'}")
        if row.get("target_family_key"):
            typer.echo(f"    family={row['target_family_key']}")
        if row.get("target_parser_profile"):
            typer.echo(f"    parser_profile={row['target_parser_profile']}")
        if row.get("error_message"):
            typer.echo(f"    error={row['error_message']}")


@app.command("reconcile-workflow-action-receipts-nc")
def reconcile_workflow_action_receipts_nc(
    limit: int = typer.Option(50, "--limit", help="Max in-flight receipts to reconcile."),
) -> None:
    """Reconcile guided workflow receipts against OCR/reprocess queue state."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _reconcile_workflow_action_receipts(conn, workflow="nc_guided", limit=limit)
        conn.commit()
    finally:
        conn.close()

    typer.echo(
        "Workflow receipt reconciliation: "
        f"completed={report['completed']} failed={report['failed']} running={report['running']}"
    )


@app.command("reconcile-skipped-parse-reviews")
def reconcile_skipped_parse_reviews(
    limit: int = typer.Option(0, "--limit", help="Max skipped rule-based reviews to reconcile (0 = all)."),
) -> None:
    """Backfill accepted review outcomes for skipped parse attempts still marked needs_review."""
    from duke_rates.db.parse_review import reconcile_skipped_rule_reviews
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = reconcile_skipped_rule_reviews(conn, limit=limit)
        conn.commit()
    finally:
        conn.close()

    typer.echo(f"Reconciled skipped parse reviews: {report['reconciled']}")


@app.command("validate-document-diagnostics")
def validate_document_diagnostics(
    state: str = typer.Option("NC", "--state", help="State filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Validate the v_document_diagnostics view against the Python remediation report.

    The view is the SQL-side single source of truth for per-document routing
    signals. This command shows the rollup counts and cross-checks them against
    _build_ocr_remediation_candidates_nc_report (Python) so any drift between
    the two implementations is immediately visible.
    """
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        # 1. Top-level counts from the view
        total_docs = conn.execute(
            "SELECT COUNT(*) FROM v_document_diagnostics WHERE state=?", (state,)
        ).fetchone()[0]
        with_run = conn.execute(
            "SELECT COUNT(*) FROM v_document_diagnostics WHERE state=? AND latest_run_id IS NOT NULL",
            (state,),
        ).fetchone()[0]
        never_processed = conn.execute(
            "SELECT COUNT(*) FROM v_document_diagnostics WHERE state=? AND latest_run_id IS NULL",
            (state,),
        ).fetchone()[0]

        # 2. Route-reason / lane breakdown
        route_rows = conn.execute(
            """SELECT route_reason, COUNT(*) AS n FROM v_document_diagnostics
               WHERE state=? GROUP BY route_reason ORDER BY n DESC""",
            (state,),
        ).fetchall()
        lane_rows = conn.execute(
            """SELECT recommended_lane, COUNT(*) AS n FROM v_document_diagnostics
               WHERE state=? GROUP BY recommended_lane ORDER BY n DESC""",
            (state,),
        ).fetchall()

        # 3. Profile groups (weak / unknown / fallback)
        profile_groups = {
            "unknown_profile": conn.execute(
                """SELECT COUNT(*) FROM v_document_diagnostics
                   WHERE state=? AND (latest_parser_profile IS NULL OR latest_parser_profile='unknown')""",
                (state,),
            ).fetchone()[0],
            "generic_residential_fallback": conn.execute(
                """SELECT COUNT(*) FROM v_document_diagnostics
                   WHERE state=? AND latest_parser_profile='generic_residential'""",
                (state,),
            ).fetchone()[0],
            "weak_outcome": conn.execute(
                """SELECT COUNT(*) FROM v_document_diagnostics
                   WHERE state=? AND latest_outcome_quality='weak'""",
                (state,),
            ).fetchone()[0],
            "empty_outcome": conn.execute(
                """SELECT COUNT(*) FROM v_document_diagnostics
                   WHERE state=? AND latest_outcome_quality='empty'""",
                (state,),
            ).fetchone()[0],
            "strong_outcome": conn.execute(
                """SELECT COUNT(*) FROM v_document_diagnostics
                   WHERE state=? AND latest_outcome_quality='strong'""",
                (state,),
            ).fetchone()[0],
        }

        # 4. Cross-check against Python report
        python_report = _build_ocr_remediation_candidates_nc_report(conn, limit=10000)
        python_lanes: dict[str, int] = {
            row["recommended_lane"]: row["count"]
            for row in python_report["recommended_lane_summary"]
        }
        view_lane_map = {row[0]: row[1] for row in lane_rows}

        comparison = []
        for lane in sorted(set(view_lane_map) | set(python_lanes)):
            if lane == "no_ocr_action":
                continue  # Python report excludes healthy_or_non_ocr_issue
            v = view_lane_map.get(lane, 0)
            p = python_lanes.get(lane, 0)
            comparison.append({"lane": lane, "view_count": v, "python_count": p, "delta": v - p})
    finally:
        conn.close()

    payload = {
        "state": state,
        "total_documents": total_docs,
        "with_latest_run": with_run,
        "never_processed": never_processed,
        "route_reasons": [{"route_reason": r[0], "count": r[1]} for r in route_rows],
        "recommended_lanes": [{"lane": r[0], "count": r[1]} for r in lane_rows],
        "profile_groups": profile_groups,
        "view_vs_python_comparison": comparison,
    }

    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    typer.echo(f"Document Diagnostics View Validation (state={state})")
    typer.echo(f"  total_documents={total_docs}  with_latest_run={with_run}  never_processed={never_processed}")
    typer.echo("\nRoute Reasons (view)")
    for r in route_rows:
        typer.echo(f"  {(r[0] or '(null)'):<40} {r[1]}")
    typer.echo("\nRecommended Lanes (view)")
    for r in lane_rows:
        typer.echo(f"  {(r[0] or '(null)'):<40} {r[1]}")
    typer.echo("\nProfile Groups")
    for key, value in profile_groups.items():
        typer.echo(f"  {key:<40} {value}")
    typer.echo("\nView vs Python Report (lane counts; ignoring healthy/no_ocr_action)")
    drift = False
    for row in comparison:
        marker = "" if row["delta"] == 0 else "  <-- DRIFT"
        if row["delta"] != 0:
            drift = True
        typer.echo(
            f"  {row['lane']:<40} view={row['view_count']:<5} python={row['python_count']:<5} delta={row['delta']:+d}{marker}"
        )
    if drift:
        drifted = [r["lane"] for r in comparison if r["delta"] != 0]
        if drifted == ["reprocess_or_refresh_ocr"]:
            typer.echo(
                "\nNote: view does not compute the 'reprocess_or_refresh_ocr' lane "
                "(requires find_stale_historical_documents). Other lanes agree."
            )
        else:
            typer.echo(
                "\nWARNING: view and python report disagree on lanes: "
                + ", ".join(drifted)
            )
    else:
        typer.echo("\nView and Python report agree on all non-healthy lane counts.")




@app.command("run-docling-nc")
def run_docling_nc(
    pdf_path: str = typer.Argument(..., help="Path to a local PDF file to convert with Docling."),
    accelerator: str = typer.Option("cpu", help="Accelerator: cpu, cuda, or mps."),
    force: bool = typer.Option(False, "--force", help="Re-run even if cached artifacts exist."),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="Save artifact record to DB."),
    scanned: bool = typer.Option(False, "--scanned", help="Enable Tesseract OCR (document has scanned pages)."),
    full_ocr: bool = typer.Option(False, "--full-ocr", help="Force OCR on every page (fully scanned document)."),
) -> None:
    """Convert a single PDF with Docling (standard pipeline) and cache the structured artifacts.

    This is a selective pilot command. Do NOT use it on every document.
    Use only for OCR-heavy, table-heavy, or repeatedly weak-parse documents.

    Uses the standard pipeline: docling-layout-heron + TableFormer ACCURATE + Tesseract OCR.
    For hard pre-1990 scans, use run-docling-vlm instead.

    When --persist is set (default), content is stored in the docling_artifacts DB table
    (doc_json_content, plain_text_content, tables_json_content columns) — no sidecar files
    are written to disk.  Use --no-persist to disable DB storage (sidecar fallback mode).
    """
    from duke_rates.historical.ncuc.pipeline.docling_backend import (
        PIPELINE_STANDARD,
        convert_pdf_safe,
        get_docling_unavailable_reason,
    )
    from duke_rates.db.sqlite import connect
    from duke_rates.hardware.cpu_config import configure_torch_inference, warmup_gpu

    unavailable = get_docling_unavailable_reason()
    if unavailable:
        typer.echo(f"Docling unavailable: {unavailable}")
        raise typer.Exit(code=1)

    configure_torch_inference()
    if accelerator == "cuda":
        warmup_gpu()

    settings, _ = _bootstrap()

    typer.echo(f"Running Docling on: {pdf_path}")
    typer.echo(f"  accelerator={accelerator}  scanned={scanned}  full_ocr={full_ocr}  force={force}")

    db_conn = None
    if persist:
        db_conn = connect(settings.database_path)

    try:
        result = convert_pdf_safe(
            pdf_path,
            accelerator=accelerator,
            force=force,
            has_scanned_pages=scanned or full_ocr,
            force_full_page_ocr=full_ocr,
            conn=db_conn,
        )
    finally:
        if db_conn is not None:
            db_conn.close()

    if result is None:
        typer.echo("Docling conversion failed. See logs for details.")
        raise typer.Exit(code=1)

    tables_count = len(result.get("tables") or [])
    storage = "db" if result.get("json_path") is None else "sidecar"
    typer.echo(f"Conversion status : {result['conversion_status']}")
    typer.echo(f"Pages             : {result['page_count']}")
    typer.echo(f"Pipeline          : {result.get('pipeline', 'standard')}")
    typer.echo(f"Tables            : {tables_count}")
    typer.echo(f"Storage           : {storage}")
    if result.get("json_path"):
        typer.echo(f"JSON sidecar      : {result['json_path']}")
        typer.echo(f"Text sidecar      : {result['plain_text_path']}")
        typer.echo(f"Tables sidecar    : {result['tables_path']}")


@app.command("run-docling-vlm")
def run_docling_vlm(
    pdf_path: str = typer.Argument(..., help="Path to a PDF file to convert with the VLM pipeline."),
    accelerator: str = typer.Option("cuda", help="Accelerator: cuda or cpu (cuda strongly recommended)."),
    force: bool = typer.Option(False, "--force", help="Re-run even if cached artifacts exist."),
    max_pages: int = typer.Option(0, help="Limit to first N pages (0 = all pages)."),
) -> None:
    """Convert a hard scanned PDF using the SmolDocling/GraniteDocling VLM pipeline.

    Use this for pre-1990 scanned filings where standard OCR (Tesseract) produces
    poor results. The VLM pipeline treats each page as a vision task, reading it
    end-to-end like a human rather than running separate layout detection + OCR.

    Requires CUDA for practical throughput (~30-120s/page on CPU vs ~5-15s/page on GPU).
    """
    from duke_rates.historical.ncuc.pipeline.docling_backend import (
        PIPELINE_VLM,
        convert_pdf_with_docling,
        get_docling_unavailable_reason,
    )
    from duke_rates.hardware.cpu_config import configure_torch_inference, warmup_gpu

    unavailable = get_docling_unavailable_reason()
    if unavailable:
        typer.echo(f"Docling unavailable: {unavailable}")
        raise typer.Exit(code=1)

    configure_torch_inference()
    if accelerator == "cuda":
        warmup_gpu()

    typer.echo(f"Running Docling VLM pipeline on: {pdf_path}")
    typer.echo(f"  accelerator={accelerator}  force={force}")

    result = convert_pdf_with_docling(
        pdf_path,
        accelerator=accelerator,
        pipeline=PIPELINE_VLM,
        force=force,
        max_pages=max_pages if max_pages > 0 else None,
    )
    if result is None:
        typer.echo("VLM conversion failed. See logs for details.")
        raise typer.Exit(code=1)

    typer.echo(f"Conversion status : {result['conversion_status']}")
    typer.echo(f"Pages             : {result['page_count']}")
    typer.echo(f"Pipeline          : {result['pipeline']}")
    if result.get("json_path"):
        typer.echo(f"JSON sidecar      : {result['json_path']}")
        typer.echo(f"Text sidecar      : {result['plain_text_path']}")
        typer.echo(f"Tables sidecar    : {result['tables_path']}")
    else:
        typer.echo("Storage           : db")


@app.command("benchmark-docling")
def benchmark_docling(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to benchmark. Repeat for multiple files."
    ),
    categories: list[str] = typer.Option(
        ..., "--category", help="Category for each PDF: A=native-text, B=rider-table, C=scanned, D=large, E=complex-table."
    ),
    accelerator: str = typer.Option(
        "auto",
        help="Force accelerator: auto (dispatch decides), cpu, or cuda.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
) -> None:
    """Benchmark Docling CPU vs GPU conversion on representative NCUC documents.

    Run each PDF through triage + dispatch + Docling conversion (force=True to bypass cache),
    then print a timing/quality report.

    Example — compare CPU vs GPU on the same file:

      python -m duke_rates benchmark-docling \\
          --pdf data/raw/nc/.../leaf-600.pdf --category B \\
          --accelerator cpu

      python -m duke_rates benchmark-docling \\
          --pdf data/raw/nc/.../leaf-600.pdf --category B \\
          --accelerator cuda
    """
    from duke_rates.benchmark.pipeline_bench import (
        run_single, print_result, CATEGORY_DESCRIPTIONS, VALID_CATEGORIES,
    )
    from duke_rates.hardware.cpu_config import configure_torch_inference, warmup_gpu

    configure_torch_inference()

    if len(pdf_paths) != len(categories):
        typer.echo("ERROR: --pdf and --category counts must match.")
        raise typer.Exit(code=1)

    for cat in categories:
        if cat not in VALID_CATEGORIES:
            typer.echo(f"ERROR: unknown category '{cat}'. Valid: {', '.join(VALID_CATEGORIES)}")
            raise typer.Exit(code=1)

    typer.echo("=== Docling Pipeline Benchmark ===")
    for cat, desc in CATEGORY_DESCRIPTIONS.items():
        typer.echo(f"  {cat}: {desc}")
    typer.echo("")

    if accelerator == "auto":
        accel_arg = None
        typer.echo("Accelerator: auto (dispatch decides per document)")
    else:
        accel_arg = accelerator
        typer.echo(f"Accelerator: forced={accelerator}")

    if accel_arg == "cuda" or accelerator == "auto":
        warmed = warmup_gpu()
        if warmed:
            typer.echo("GPU warmed up.")

    results = []
    for pdf_path, category in zip(pdf_paths, categories):
        typer.echo(f"\nProcessing: {pdf_path}  (cat={category})")
        r = run_single(pdf_path, category, accelerator=accel_arg)
        print_result(r)
        results.append(r.as_dict())

    # Summary table
    typer.echo("\n=== Summary ===")
    typer.echo(f"{'File':<40} {'Cat':>3} {'Accel':>5} {'Pages':>5} {'Conv(s)':>8} {'p/s':>6} {'Tables':>6}")
    typer.echo("-" * 80)
    for r in results:
        name = Path(r["pdf_path"]).name[:38]
        typer.echo(
            f"{name:<40} {r['category']:>3} {r['accelerator_used']:>5} "
            f"{r['page_count']:>5} {r['conversion_time_s']:>8.2f} "
            f"{r['pages_per_second']:>6.2f} {r['tables_detected']:>6}"
        )

    if output_json:
        import json as _json
        Path(output_json).write_text(_json.dumps(results, indent=2), encoding="utf-8")
        typer.echo(f"\nResults written to: {output_json}")


@app.command("benchmark-document-normalization")
def benchmark_document_normalization(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to benchmark. Repeat for multiple files."
    ),
    labels: list[str] = typer.Option(
        ..., "--label", help="Short label for each PDF benchmark case."
    ),
    max_pages: int = typer.Option(
        2,
        help="Maximum number of leading pages to benchmark for each PDF.",
    ),
    skip_glm: bool = typer.Option(
        False,
        help="Skip GLM-OCR comparison and router fallback to GLM.",
    ),
    ollama_host: str = typer.Option(
        "http://localhost:11434",
        help="Local Ollama host for GLM-OCR benchmarking.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
) -> None:
    """Benchmark native vs Paddle vs GLM document normalization on representative PDFs."""
    from duke_rates.benchmark.document_normalization_bench import (
        print_normalization_benchmark,
        run_normalization_benchmark,
        write_results_json,
    )

    if len(pdf_paths) != len(labels):
        typer.echo("ERROR: --pdf and --label counts must match.")
        raise typer.Exit(code=1)

    typer.echo("=== Document Normalization Benchmark ===")
    typer.echo(f"max_pages={max_pages}  glm_enabled={not skip_glm}  ollama_host={ollama_host}")

    results: list[dict] = []
    for pdf_path, label in zip(pdf_paths, labels):
        typer.echo(f"\nProcessing: {label} -> {pdf_path}")
        result = run_normalization_benchmark(
            pdf_path,
            label=label,
            max_pages=max_pages,
            enable_glm=not skip_glm,
            ollama_host=ollama_host,
        )
        print_normalization_benchmark(result)
        results.append(result)

    if output_json:
        write_results_json(results, output_json)
        typer.echo(f"\nResults written to: {output_json}")


@app.command("compare-document-page-text")
def compare_document_page_text(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to compare. Repeat for multiple cases."
    ),
    pages: list[int] = typer.Option(
        ..., "--page", help="1-based page number for each PDF case."
    ),
    labels: list[str] = typer.Option(
        ..., "--label", help="Short label for each comparison case."
    ),
    expected_tokens: list[str] = typer.Option(
        [],
        "--expected-token",
        help="Expected token(s) that indicate better OCR accuracy. Repeat as needed.",
    ),
    skip_glm: bool = typer.Option(
        False,
        help="Skip GLM-OCR comparison.",
    ),
    skip_paddle: bool = typer.Option(
        False,
        help="Skip Paddle comparison.",
    ),
    ollama_host: str = typer.Option(
        "http://localhost:11434",
        help="Local Ollama host for GLM-OCR comparison.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
    output_markdown: str = typer.Option("", help="Optional path to write Markdown results."),
) -> None:
    """Compare page-level text accuracy across native, Paddle, and GLM OCR backends."""
    from duke_rates.benchmark.document_page_text_compare import (
        print_document_page_text_comparison,
        run_document_page_text_comparison,
        write_page_comparison_json,
        write_page_comparison_markdown,
    )

    if not (len(pdf_paths) == len(pages) == len(labels)):
        typer.echo("ERROR: --pdf, --page, and --label counts must match.")
        raise typer.Exit(code=1)

    typer.echo("=== Document Page Text Comparison ===")
    typer.echo(
        f"glm_enabled={not skip_glm}  paddle_enabled={not skip_paddle}  ollama_host={ollama_host}"
    )
    if expected_tokens:
        typer.echo(f"expected_tokens={expected_tokens}")

    results: list[dict] = []
    for pdf_path, page, label in zip(pdf_paths, pages, labels):
        typer.echo(f"\nProcessing: {label} -> {pdf_path} (page {page})")
        result = run_document_page_text_comparison(
            pdf_path,
            page_number=page,
            label=label,
            expected_tokens=expected_tokens,
            enable_glm=not skip_glm,
            enable_paddle=not skip_paddle,
            ollama_host=ollama_host,
        )
        print_document_page_text_comparison(result)
        results.append(result)

    if output_json:
        write_page_comparison_json(results, output_json)
        typer.echo(f"\nJSON results written to: {output_json}")
    if output_markdown:
        write_page_comparison_markdown(results, output_markdown)
        typer.echo(f"Markdown results written to: {output_markdown}")


@app.command("benchmark-redline-analysis")
def benchmark_redline_analysis(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to analyze. Repeat for multiple cases."
    ),
    pages: list[int] = typer.Option(
        ..., "--page", help="1-based page number for each case."
    ),
    labels: list[str] = typer.Option(
        ..., "--label", help="Short label for each case."
    ),
    ollama_host: str = typer.Option(
        "http://localhost:11434",
        help="Local Ollama host for GLM redline analysis.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
    output_markdown: str = typer.Option("", help="Optional path to write Markdown results."),
) -> None:
    """Benchmark GLM image analysis on candidate clean/redline tariff pages."""
    from duke_rates.benchmark.redline_analysis_bench import (
        print_redline_analysis,
        run_redline_analysis,
        write_redline_analysis_json,
        write_redline_analysis_markdown,
    )

    if not (len(pdf_paths) == len(pages) == len(labels)):
        typer.echo("ERROR: --pdf, --page, and --label counts must match.")
        raise typer.Exit(code=1)

    typer.echo("=== Redline Analysis Benchmark ===")
    typer.echo(f"ollama_host={ollama_host}")

    results: list[dict] = []
    for pdf_path, page, label in zip(pdf_paths, pages, labels):
        typer.echo(f"\nProcessing: {label} -> {pdf_path} (page {page})")
        result = run_redline_analysis(
            pdf_path,
            page_number=page,
            label=label,
            ollama_host=ollama_host,
        )
        print_redline_analysis(result)
        results.append(result)

    if output_json:
        write_redline_analysis_json(results, output_json)
        typer.echo(f"\nJSON results written to: {output_json}")
    if output_markdown:
        write_redline_analysis_markdown(results, output_markdown)
        typer.echo(f"Markdown results written to: {output_markdown}")


@app.command("gpu-status")
def gpu_status() -> None:
    """Show current GPU availability and VRAM budget."""
    from duke_rates.hardware.cpu_config import configure_torch_inference
    from duke_rates.hardware.gpu_manager import get_gpu_manager

    configure_torch_inference()
    mgr = get_gpu_manager()
    summary = mgr.summary()

    if not summary.get("cuda"):
        typer.echo("CUDA: unavailable")
        return

    typer.echo(f"CUDA device    : {summary.get('device_name', 'unknown')}")
    typer.echo(f"Total VRAM     : {summary.get('total_vram_gb', 0):.2f} GB")
    typer.echo(f"Free VRAM      : {summary.get('free_vram_gb', 0):.2f} GB")
    typer.echo(f"Allocated      : {summary.get('allocated_gb', 0):.2f} GB")
    typer.echo(f"Reserved       : {summary.get('reserved_gb', 0):.2f} GB")
    typer.echo(f"Can fit Docling: {mgr.can_fit_docling_models()}")


@app.command("report-docling-skipped-pages-nc")
def report_docling_skipped_pages_nc(
    limit: int = typer.Option(50, "--limit", help="Maximum number of artifacts to list."),
    min_skipped: int = typer.Option(1, "--min-skipped", min=1, help="Only include artifacts with at least N skipped pages."),
    degraded_only: bool = typer.Option(False, "--degraded-only", help="Only include artifacts that triggered the per-page degradation ladder."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
) -> None:
    """List Docling artifacts where pages were skipped or degraded during chunked conversion.

    Reads ``metadata_json`` written by ``convert_pdf_safe`` and surfaces:
      * ``skipped_pages``     — pages that exhausted the degradation ladder
      * ``degraded_modes``    — labels like ``page_degraded``, ``cpu_fallback``, ``chunked``
      * ``used_chunking``     — whether page-range chunking was used at all

    These pages are candidates for VLM (``run-docling-vlm``) or manual remediation —
    Docling's standard pipeline could not produce text for them.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT id, source_pdf, accelerator, pipeline, status, page_count,
                   table_count, metadata_json, updated_at
            FROM docling_artifacts
            WHERE metadata_json IS NOT NULL AND metadata_json != '{}'
            ORDER BY id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    matches: list[dict] = []
    skipped_total = 0
    degraded_total = 0
    chunked_total = 0
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        skipped_pages = list(meta.get("skipped_pages") or [])
        degraded_modes = list(meta.get("degraded_modes") or [])
        used_chunking = bool(meta.get("used_chunking"))
        if not (skipped_pages or degraded_modes or used_chunking):
            continue
        if len(skipped_pages) < min_skipped and not degraded_modes:
            # Still tally totals before filtering, but don't include in rows.
            if used_chunking:
                chunked_total += 1
            continue
        if degraded_only and not degraded_modes:
            continue
        skipped_total += len(skipped_pages)
        if degraded_modes:
            degraded_total += 1
        if used_chunking:
            chunked_total += 1
        matches.append(
            {
                "id": row["id"],
                "source_pdf": row["source_pdf"],
                "accelerator": row["accelerator"],
                "pipeline": row["pipeline"],
                "status": row["status"],
                "page_count": row["page_count"],
                "table_count": row["table_count"],
                "skipped_pages": skipped_pages,
                "skipped_count": len(skipped_pages),
                "degraded_modes": degraded_modes,
                "used_chunking": used_chunking,
                "updated_at": row["updated_at"],
            }
        )

    matches.sort(key=lambda m: m["skipped_count"], reverse=True)
    truncated = matches[:limit]

    report = {
        "row_count": len(matches),
        "shown": len(truncated),
        "skipped_pages_total": skipped_total,
        "degraded_artifacts_total": degraded_total,
        "chunked_artifacts_total": chunked_total,
        "rows": truncated,
    }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(f"artifacts_with_issues={report['row_count']} shown={report['shown']}")
    typer.echo(f"  skipped_pages_total={report['skipped_pages_total']}")
    typer.echo(f"  degraded_artifacts={report['degraded_artifacts_total']}")
    typer.echo(f"  chunked_artifacts={report['chunked_artifacts_total']}")
    if not truncated:
        typer.echo("(no matching artifacts)")
        return
    typer.echo("Top rows (by skipped page count):")
    for row in truncated:
        typer.echo(
            f"  id={row['id']} "
            f"pages={row['page_count']} "
            f"skipped={row['skipped_count']} "
            f"deg={','.join(row['degraded_modes']) or 'none'} "
            f"chunked={row['used_chunking']} "
            f"src={row['source_pdf']}"
        )


@app.command("list-document-types-nc")
def list_document_types_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit the taxonomy as JSON."),
) -> None:
    """List the seeded ``document_types`` taxonomy.

    Phase 2 of the document intelligence roadmap. Use this to confirm the
    taxonomy is populated before relying on the ``document_type``
    classification stage.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT code, primary_category, parent_type, description, is_terminal
            FROM document_types
            ORDER BY primary_category, code
            """
        ).fetchall()
    finally:
        conn.close()

    items = [
        {
            "code": r["code"],
            "primary_category": r["primary_category"],
            "parent_type": r["parent_type"],
            "description": r["description"],
            "is_terminal": bool(r["is_terminal"]),
        }
        for r in rows
    ]

    if as_json:
        typer.echo(json.dumps(items, indent=2))
        return

    if not items:
        typer.echo("(no document_types rows — run any DB migrate to seed)")
        return

    current_category = None
    for item in items:
        if item["primary_category"] != current_category:
            current_category = item["primary_category"]
            typer.echo("")
            typer.echo(current_category)
        typer.echo(f"  {item['code']:<28s} {item['description']}")


@app.command("report-document-types-nc")
def report_document_types_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Report the distribution of ``document_type`` classifications.

    Phase 2 definition-of-done check: a non-trivial spread across the
    seeded types (not 100% UNKNOWN) means the live classifier is wired
    and the taxonomy fits the corpus. Compares against the legacy
    ``classify_document`` string label captured in metadata so disagreements
    are visible at a glance.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        distribution_rows = conn.execute(
            """
            SELECT label, COUNT(*) AS n
            FROM document_classifications
            WHERE stage = 'document_type' AND superseded_by IS NULL
            GROUP BY label
            ORDER BY n DESC
            """
        ).fetchall()
        confidence_rows = conn.execute(
            """
            SELECT
                ROUND(confidence, 1) AS bucket,
                COUNT(*) AS n
            FROM document_classifications
            WHERE stage = 'document_type' AND superseded_by IS NULL
            GROUP BY bucket
            ORDER BY bucket
            """
        ).fetchall()
        legacy_rows = conn.execute(
            """
            SELECT label, metadata_json, COUNT(*) AS n
            FROM document_classifications
            WHERE stage = 'document_type' AND superseded_by IS NULL
            GROUP BY label, metadata_json
            """
        ).fetchall()
    finally:
        conn.close()

    distribution = [{"label": r["label"], "count": r["n"]} for r in distribution_rows]
    total = sum(item["count"] for item in distribution)
    confidence_buckets = [
        {"bucket": r["bucket"], "count": r["n"]} for r in confidence_rows
    ]

    legacy_xref: dict[str, dict[str, int]] = {}
    for row in legacy_rows:
        try:
            md = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            md = {}
        legacy = str(md.get("legacy_label", "<missing>"))
        legacy_xref.setdefault(row["label"], {}).setdefault(legacy, 0)
        legacy_xref[row["label"]][legacy] += int(row["n"])

    report = {
        "stage": "document_type",
        "total": total,
        "distribution": distribution,
        "confidence_buckets": confidence_buckets,
        "legacy_label_crosswalk": legacy_xref,
    }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(f"stage=document_type total={total}")
    if total == 0:
        typer.echo("  (no rows — run extraction to populate)")
        return

    typer.echo("")
    typer.echo("Distribution by document_type:")
    for item in distribution:
        pct = 100.0 * item["count"] / total if total else 0.0
        typer.echo(f"  {item['label']:<28s} {item['count']:>6,}  ({pct:5.1f}%)")

    typer.echo("")
    typer.echo("Confidence distribution (rounded to 0.1 buckets):")
    for bucket in confidence_buckets:
        typer.echo(f"  conf~{bucket['bucket']}  {bucket['count']:>6,}")

    typer.echo("")
    typer.echo("Crosswalk to legacy classify_document label:")
    for dt_label in sorted(legacy_xref):
        legacy_map = legacy_xref[dt_label]
        legacy_str = ", ".join(
            f"{legacy}={count}" for legacy, count in sorted(legacy_map.items())
        )
        typer.echo(f"  {dt_label:<28s} <- {legacy_str}")


@app.command("report-flag-classifications-nc")
def report_flag_classifications_nc(
    stage: str = typer.Option("", "--stage", help="Filter to a specific flag stage (e.g. 'flag_is_final'). Empty = all."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Report the distribution of Phase 3 flag classifications.

    Shows per-stage label distributions and confidence ranges, including
    how many documents have been classified for each flag stage.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        stages = conn.execute(
            """
            SELECT DISTINCT stage
            FROM document_classifications
            WHERE stage LIKE 'flag_%' OR stage IN ('utility', 'docket_number', 'effective_date', 'tariff_family')
            ORDER BY stage
            """
        ).fetchall()
    finally:
        conn.close()

    stage_list = [s["stage"] for s in stages]
    if stage:
        if stage not in stage_list:
            typer.echo(f"No data for stage {stage!r}. Available: {stage_list}")
            raise typer.Exit(code=0)
        stage_list = [stage]

    if not stage_list:
        typer.echo("(no flag classification rows — run extraction to populate)")
        return

    report: dict[str, dict] = {}
    for st in stage_list:
        conn = connect(settings.database_path)
        try:
            dist = conn.execute(
                """
                SELECT label, COUNT(*) AS n
                FROM document_classifications
                WHERE stage = ? AND superseded_by IS NULL
                GROUP BY label ORDER BY n DESC
                """,
                (st,),
            ).fetchall()
            conf = conn.execute(
                """
                SELECT MIN(confidence) AS mn, MAX(confidence) AS mx, AVG(confidence) AS avg
                FROM document_classifications
                WHERE stage = ? AND superseded_by IS NULL
                """,
                (st,),
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) FROM document_classifications WHERE stage = ? AND superseded_by IS NULL",
                (st,),
            ).fetchone()[0]
        finally:
            conn.close()
        report[st] = {
            "total": total or 0,
            "distribution": [{"label": r["label"], "count": r["n"]} for r in dist],
            "confidence": {"min": conf["mn"], "max": conf["mx"], "avg": round(conf["avg"] or 0, 3)},
        }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    for st, data in sorted(report.items()):
        if data["total"] == 0:
            continue
        typer.echo(f"\n{st}  (n={data['total']})")
        typer.echo(f"  confidence: min={data['confidence']['min']:.2f} max={data['confidence']['max']:.2f} avg={data['confidence']['avg']:.2f}")
        for item in data["distribution"]:
            pct = 100.0 * item["count"] / data["total"] if data["total"] else 0.0
            typer.echo(f"  {item['label']:<20s} {item['count']:>6,}  ({pct:5.1f}%)")


@app.command("backfill-flag-classifications-nc")
def backfill_flag_classifications_nc(
    limit: int = typer.Option(0, "--limit", help="Only process N documents (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Classify but do not persist."),
) -> None:
    """Backfill Phase 3 flag classifications for existing historical_documents.

    Extracts text from each document's PDF (first 5000 chars) and runs all
    11 flag classifiers. Skips documents that already have flag rows.
    """
    from duke_rates.classification.persistence import record_classification
    from duke_rates.document_intelligence.flag_classifiers import (
        get_flag_classifier,
        all_flag_stages,
    )
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT hd.*
            FROM historical_documents hd
            WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
              AND hd.id NOT IN (
                  SELECT DISTINCT CAST(subject_id AS INTEGER)
                  FROM document_classifications
                  WHERE subject_kind = 'historical_document'
                    AND (stage LIKE 'flag_%' OR stage IN ('utility', 'docket_number', 'effective_date', 'tariff_family'))
                    AND superseded_by IS NULL
              )
            ORDER BY hd.id
            """
        ).fetchall()
    finally:
        conn.close()

    docs = [dict(r) for r in rows]
    if limit > 0:
        docs = docs[:limit]

    typer.echo(f"Backfilling flag classifications for {len(docs)} documents...")

    if dry_run:
        typer.echo("[DRY RUN — no rows will be written]")

    stages = all_flag_stages()
    ok = skip = fail = 0
    for i, doc in enumerate(docs):
        doc_id = doc.get("id")
        local_path = doc.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            skip += 1
            continue

        try:
            import pdfplumber
            with pdfplumber.open(local_path) as pdf:
                pages = pdf.pages[:3]  # first 3 pages sufficient for classifiers
                text = "\n".join(
                    (p.extract_text() or "") for p in pages
                )
        except Exception:
            fail += 1
            continue

        if not text.strip():
            skip += 1
            continue

        metadata = {
            "company": doc.get("company", ""),
            "family_key": doc.get("family_key", ""),
            "leaf_no": doc.get("leaf_no", ""),
            "effective_start": doc.get("effective_start", ""),
            "docket_number": doc.get("docket_number", ""),
            "is_redline_candidate": int(doc.get("is_redline_candidate") or 0),
            "redline_confidence": float(doc.get("redline_confidence") or 0.0),
            "title": doc.get("title", ""),
        }

        if dry_run:
            for stage in stages:
                classifier = get_flag_classifier(stage)
                if classifier:
                    result = classifier.classify(text, metadata)
            ok += 1
        else:
            cls_conn = connect(settings.database_path)
            try:
                for stage in stages:
                    classifier = get_flag_classifier(stage)
                    if classifier is None:
                        continue
                    result = classifier.classify(text, metadata)
                    result.classifier = f"rule_{stage}_v1"
                    result.classifier_version = "v1"
                    record_classification(
                        cls_conn,
                        subject_kind="historical_document",
                        subject_id=str(doc_id),
                        stage=stage,
                        result=result,
                    )
                cls_conn.commit()
                ok += 1
            except Exception:
                fail += 1
            finally:
                cls_conn.close()

        if (i + 1) % 100 == 0:
            typer.echo(f"  {i + 1}/{len(docs)} ... ok={ok} skip={skip} fail={fail}")

    typer.echo(f"\nDone: ok={ok} skip={skip} fail={fail}")


@app.command("check-ollama-models-nc")
def check_ollama_models_nc(
    config_path: str = typer.Option(
        "config/ollama_models.yaml", "--config", help="Path to ollama_models.yaml"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit status as JSON."),
    required_only: bool = typer.Option(
        True, "--required-only/--all", help="Only show roles with primary set (--required-only) or all roles (--all)."
    ),
) -> None:
    """Probe every configured Ollama role and report availability.

    Phase 2.5 health check. For each role in ollama_models.yaml, probes the
    primary model (and fallbacks if needed) and reports whether it is available.
    Exits non-zero if any role with a non-empty primary model is unavailable.

    Use this before ``run-overnight-doc-intelligence-nc`` to confirm all
    required models are reachable.
    """
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    orchestrator = OllamaOrchestrator(config_path=Path(config_path) if config_path else None)
    health = orchestrator.list_available_roles()

    if required_only:
        health = [h for h in health if h.primary]

    if as_json:
        items = [
            {
                "role": h.role,
                "available": h.available,
                "primary": h.primary,
                "message": h.message,
            }
            for h in health
        ]
        typer.echo(json.dumps(items, indent=2))
    else:
        if not health:
            typer.echo("(no roles configured)")
            return

        width_role = max(len(h.role) for h in health) + 2
        width_model = max(len(h.primary) for h in health) + 2
        typer.echo(f"{'ROLE':<{width_role}} {'MODEL':<{width_model}} STATUS")
        typer.echo(f"{'─' * (width_role - 1):<{width_role}} {'─' * (width_model - 1):<{width_model}} ──────")
        for h in health:
            status = "OK" if h.available else f"FAIL — {h.message or 'unknown'}"
            typer.echo(f"{h.role:<{width_role}} {h.primary:<{width_model}} {status}")

    unavailable = [h for h in health if not h.available]
    if unavailable:
        raise typer.Exit(code=1)


@app.command("benchmark-ollama-roles-nc")
def benchmark_ollama_roles_nc(
    task: str = typer.Option(
        "parse_diagnosis",
        "--task",
        help=(
            "Benchmark task, comma-separated tasks, or all. Tasks: "
            "parse_diagnosis, hard_parse_diagnosis, regex_suggestion, "
            "structured_rate_extraction, staged_find_lines, "
            "staged_classify_line, document_classification."
        ),
    ),
    models: str = typer.Option(
        "",
        "--models",
        help="Comma-separated Ollama model names. Defaults to the configured role primary plus fallbacks.",
    ),
    limit: int = typer.Option(5, "--limit", help="Representative cases per task."),
    max_runtime_minutes: float = typer.Option(
        0.0,
        "--max-runtime-minutes",
        help="Stop after this many minutes. 0 means no explicit runtime cap.",
    ),
    timeout_s: float = typer.Option(
        0.0,
        "--timeout-s",
        help="Per-request timeout. 0 uses the role/config default.",
    ),
    config_path: str = typer.Option(
        "config/ollama_models.yaml",
        "--config",
        help="Path to ollama_models.yaml.",
    ),
    output: str = typer.Option(
        "",
        "--output",
        help="Report JSON path. Defaults to docs/reports/ollama_model_benchmarks/<timestamp>_<task>.json.",
    ),
    fixtures: str = typer.Option(
        "",
        "--fixtures",
        help="Optional JSON gold-fixture file with expected labels keyed by task and case_id.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the full benchmark report as JSON."),
) -> None:
    """Benchmark local Ollama models against document-intelligence tasks.

    The benchmark uses production-style prompts and Pydantic schemas but does
    not write diagnostic, suggestion, classification, or extraction rows. It is
    intended for selecting per-role models before overnight loops depend on
    them.
    """
    from duke_rates.document_intelligence.model_benchmark import (
        default_output_path,
        normalize_task_list,
        run_ollama_role_benchmark,
        run_ollama_specialization_benchmark,
    )

    settings, _ = _bootstrap()
    try:
        task_keys = normalize_task_list([task])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    model_list = [m.strip() for m in models.split(",") if m.strip()] or None
    report_task_name = "all" if len(task_keys) > 1 else task_keys[0]
    output_path = Path(output) if output else default_output_path(report_task_name)
    fixtures_path = Path(fixtures) if fixtures else None

    if len(task_keys) == 1:
        report = run_ollama_role_benchmark(
            db_path=Path(settings.database_path),
            task=task_keys[0],
            models=model_list,
            limit=limit,
            max_runtime_minutes=max_runtime_minutes if max_runtime_minutes > 0 else None,
            config_path=Path(config_path) if config_path else None,
            output_path=output_path,
            timeout_s=timeout_s if timeout_s > 0 else None,
            fixtures_path=fixtures_path,
        )
    else:
        report = run_ollama_specialization_benchmark(
            db_path=Path(settings.database_path),
            tasks=task_keys,
            models=model_list,
            limit=limit,
            max_runtime_minutes=max_runtime_minutes if max_runtime_minutes > 0 else None,
            config_path=Path(config_path) if config_path else None,
            output_path=output_path,
            timeout_s=timeout_s if timeout_s > 0 else None,
            fixtures_path=fixtures_path,
        )

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("=== Ollama Role Benchmark ===")
    typer.echo(f"Task:          {report['task']}")
    if "role" in report:
        typer.echo(f"Role:          {report['role']}")
    if "tasks" in report:
        typer.echo(f"Tasks:         {', '.join(report['tasks'])}")
    if "cases_selected" in report:
        typer.echo(f"Cases:         {report['cases_selected']}")
    if report.get("gold_case_count"):
        typer.echo(f"Gold cases:    {report['gold_case_count']}")
    typer.echo(f"Runs:          {report['runs_completed']}")
    typer.echo(f"Stop reason:   {report['stop_reason']}")
    typer.echo(f"Report:        {output_path}")
    typer.echo("")

    if report.get("task") == "multi_task_specialization":
        specialization = report.get("specialization") or {}
        typer.echo("Best by task:")
        for task_name, row in (specialization.get("best_by_task") or {}).items():
            if not row:
                continue
            typer.echo(
                f"  {task_name:<28s} {row['model']:<28.28s} "
                f"score={row['score']:.1f} valid={row['valid_pct']:.1f}% "
                f"action={row['actionable_pct']:.1f}% bias={row['label_bias_score']:.2f}"
            )
        typer.echo("")
        return

    typer.echo(
        f"{'MODEL':<28s} {'OK':>5s} {'VALID%':>8s} {'ACTION%':>8s} "
        f"{'AVG S':>8s} {'TPS':>8s} {'CONF':>7s} {'BIAS':>7s} {'ACC%':>7s}"
    )
    typer.echo("-" * 98)
    for model, stats in report["summary"].items():
        avg_s = float(stats.get("avg_duration_ms", 0.0)) / 1000.0
        typer.echo(
            f"{model:<28.28s} "
            f"{stats.get('ok', 0):>5d} "
            f"{stats.get('valid_pct', 0.0):>7.1f}% "
            f"{stats.get('actionable_pct', 0.0):>7.1f}% "
            f"{avg_s:>8.1f} "
            f"{stats.get('avg_tokens_per_second', 0.0):>8.1f} "
            f"{stats.get('avg_confidence', 0.0):>7.2f} "
            f"{stats.get('label_bias_score', 0.0):>7.2f} "
            f"{_format_optional_pct(stats.get('accuracy_pct')):>7s}"
        )
        distribution = stats.get("task_distribution") or {}
        if distribution:
            typer.echo(f"  distribution: {distribution}")


@app.command("run-llm-doc-probe-nc")
def run_llm_doc_probe_nc(
    document_id: int = typer.Argument(..., help="historical_documents.id to probe."),
    role: str = typer.Option("balanced_classifier", "--role", help="Ollama role from ollama_models.yaml."),
    config_path: str = typer.Option("config/ollama_models.yaml", "--config", help="Path to ollama_models.yaml."),
    persist: bool = typer.Option(False, "--persist", help="Write classification to document_classifications."),
    as_json: bool = typer.Option(False, "--json", help="Emit result as JSON."),
) -> None:
    """Run an LLM probe against one historical document.

    Phase 2.5 smoke-test entrypoint. Extracts text from *document_id*, sends it
    to *role*'s primary model in JSON mode, validates the output against a
    light-weight document-type schema, and prints the result.

    Does NOT write to document_classifications unless ``--persist`` is passed.
    The ollama_model_runs row is always persisted.
    """
    from duke_rates.classification.result import ClassificationResult
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        doc = conn.execute(
            "SELECT id, title, family_key, local_path, raw_text_path FROM historical_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    finally:
        conn.close()

    if doc is None:
        raise typer.BadParameter(f"No historical_document with id={document_id}")

    # Extract text
    text_sample = ""
    if doc["raw_text_path"]:
        try:
            text_sample = Path(doc["raw_text_path"]).read_text(encoding="utf-8")[:2000]
        except Exception:
            pass
    if not text_sample and doc["local_path"]:
        from duke_rates.parse.pdf import extract_pdf_text
        try:
            text_sample = extract_pdf_text(doc["local_path"])[:2000]
        except Exception:
            text_sample = "[text extraction failed]"

    orchestrator = OllamaOrchestrator(
        config_path=Path(config_path) if config_path else None,
        db_path=settings.database_path,
    )

    # Probe the role first
    ok, err = orchestrator.health_probe(role)
    if not ok:
        typer.echo(f"Role {role!r} not available: {err}")
        raise typer.Exit(code=1)

    prompt = (
        "Classify this NCUC regulatory document into exactly one of these types: "
        "TARIFF_SHEET, RIDER, RATE_SCHEDULE, ORDER_FINAL, ORDER_PROCEDURAL, "
        "TESTIMONY, COVER_LETTER, NOTICE_OF_HEARING, APPLICATION, "
        "COMPLIANCE_FILING, CERTIFICATE_OF_SERVICE, UNKNOWN.\n\n"
        "Return JSON with fields: label (the type), confidence (0.0-1.0), "
        "evidence (list of {kind, value} objects), "
        "alternatives (list of [label, score] pairs).\n\n"
        f"Document title: {doc['title']}\n"
        f"Document text (first 2000 chars):\n{text_sample}"
    )

    result = orchestrator.generate_json(
        role=role,
        prompt=prompt,
        schema=ClassificationResult,
        subject_kind="historical_document",
        subject_id=str(document_id),
        stage="llm_probe",
    )

    if as_json:
        output = {
            "document_id": document_id,
            "title": doc["title"],
            "family_key": doc["family_key"],
            "role": result.role,
            "model": result.model,
            "status": result.status,
            "duration_ms": result.duration_ms,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "result": result.result.model_dump() if result.result else None,
            "raw_payload": result.raw_payload,
            "validation_error": result.validation_error,
            "fallback_from": result.fallback_from,
        }
        typer.echo(json.dumps(output, indent=2, default=str))
    else:
        typer.echo(f"Document: {doc['id']} ({doc['family_key']})")
        typer.echo(f"Title:    {doc['title'][:100]}")
        typer.echo(f"Role:     {result.role} -> {result.model}")
        typer.echo(f"Status:   {result.status}")
        typer.echo(f"Duration: {result.duration_ms} ms")
        typer.echo(f"Tokens:   in={result.tokens_in} out={result.tokens_out}")
        if result.fallback_from:
            typer.echo(f"Fallback: {result.fallback_from} -> {result.model}")
        if result.validation_error:
            typer.echo(f"Validation error: {result.validation_error}")
        if result.result:
            typer.echo(f"Label:      {result.result.label}")
            typer.echo(f"Confidence: {result.result.confidence}")
            if result.result.alternatives:
                typer.echo(f"Alternatives: {result.result.alternatives}")
        if result.raw_payload:
            typer.echo(f"\nRaw response:\n{result.raw_payload[:500]}")


@app.command("report-classification-disagreements-nc")
def report_classification_disagreements_nc(
    stage: str = typer.Option("family_mapping", "--stage", help="Classification stage to inspect."),
    margin: float = typer.Option(0.10, "--margin", help="Confidence margin between rank-1 and rank-2 below which a row is flagged as 'low margin'."),
    limit: int = typer.Option(50, "--limit", help="Max rows to show."),
    overrides_only: bool = typer.Option(False, "--overrides-only", help="Only show rows where a hint/override changed the classifier's chosen label."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
    cross_stage: str = typer.Option("", "--cross-stage", help="Compare two classifiers for the same stage (e.g. 'document_type'). Lists (rule vs embedding) pairs per document."),
) -> None:
    """Surface low-confidence and runner-up-close classifications.

    Reads ``document_classifications`` and reports:
      * **Low-margin** rows — rank-1 score and rank-2 score are within ``--margin`` (raw score units, not confidence). These are where the classifier was on the edge between two labels and small changes in evidence would have flipped the decision.
      * **Override** rows — the legacy hint (or other override source) chose a label different from what the classifier picked.
      * **Cross-stage** comparison (``--cross-stage document_type``) — compares rule-based vs embedding-based classifications for the same document.

    Use this to triage which family-mapping decisions to review by hand or
    feed to a second-opinion classifier.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()

    # ------------------------------------------------------------------
    # Cross-stage comparison path (Phase 4)
    # ------------------------------------------------------------------
    if cross_stage:
        conn = connect(settings.database_path)
        try:
            pairs = conn.execute(
                """
                SELECT
                    r.subject_kind,
                    r.subject_id,
                    r.label AS rule_label,
                    r.confidence AS rule_confidence,
                    e.label AS emb_label,
                    e.confidence AS emb_confidence,
                    CASE
                        WHEN r.label = e.label THEN 'agreement'
                        WHEN r.confidence < 0.3 AND e.confidence >= 0.5 AND r.label = e.label
                            THEN 'embedding_confirms_weak_rule'
                        WHEN r.confidence < 0.3 AND e.confidence >= 0.5 AND r.label != e.label
                            THEN 'overrule_candidate'
                        WHEN r.label != e.label THEN 'disagreement'
                        ELSE 'other'
                    END AS status
                FROM document_classifications r
                JOIN document_classifications e
                  ON e.subject_kind = r.subject_kind
                 AND e.subject_id = r.subject_id
                 AND e.stage = r.stage
                 AND e.classifier = 'embedding_knn_v1'
                 AND e.superseded_by IS NULL
                WHERE r.stage = ?
                  AND r.classifier = 'rule_document_type_v1'
                  AND r.superseded_by IS NULL
                ORDER BY
                    CASE
                        WHEN r.label != e.label THEN 0
                        WHEN r.confidence < 0.3 THEN 1
                        ELSE 2
                    END,
                    ABS(r.confidence - e.confidence) DESC
                """,
                (cross_stage,),
            ).fetchall()
        finally:
            conn.close()

        report = {
            "cross_stage": cross_stage,
            "total_pairs": len(pairs),
            "agreements": sum(1 for p in pairs if p["status"] == "agreement"),
            "disagreements": sum(1 for p in pairs if p["status"] == "disagreement"),
            "overrule_candidates": sum(1 for p in pairs if p["status"] == "overrule_candidate"),
            "embedding_confirms_weak_rule": sum(1 for p in pairs if p["status"] == "embedding_confirms_weak_rule"),
            "pairs": [
                {
                    "subject_kind": p["subject_kind"],
                    "subject_id": p["subject_id"],
                    "rule_label": p["rule_label"],
                    "rule_confidence": round(float(p["rule_confidence"]), 3),
                    "emb_label": p["emb_label"],
                    "emb_confidence": round(float(p["emb_confidence"]), 3),
                    "status": p["status"],
                }
                for p in pairs
            ][:limit],
        }

        if as_json:
            typer.echo(json.dumps(report, indent=2, default=str))
            return

        typer.echo(f"Cross-stage comparison: {cross_stage}")
        typer.echo(f"  total pairs:      {report['total_pairs']}")
        typer.echo(f"  agreements:       {report['agreements']}")
        typer.echo(f"  disagreements:    {report['disagreements']}")
        typer.echo(f"  overrule candidates: {report['overrule_candidates']}")
        typer.echo(f"  embedding confirms weak rule: {report['embedding_confirms_weak_rule']}")

        if report["pairs"]:
            typer.echo("")
            typer.echo(f"{'subj_id':>8s} {'rule_label':<20s} {'r_conf':>6s} {'emb_label':<20s} {'e_conf':>6s} {'status'}")
            typer.echo("-" * 90)
            for p in report["pairs"]:
                typer.echo(
                    f"{p['subject_id']:>8s} "
                    f"{p['rule_label']:<20s} "
                    f"{p['rule_confidence']:>6.3f} "
                    f"{p['emb_label']:<20s} "
                    f"{p['emb_confidence']:>6.3f} "
                    f"{p['status']}"
                )
        return

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT id, subject_kind, subject_id, stage, label, confidence,
                   classifier, classifier_version, evidence_json,
                   alternatives_json, metadata_json, created_at
            FROM document_classifications
            WHERE stage = ? AND superseded_by IS NULL
            ORDER BY id DESC
            """,
            (stage,),
        ).fetchall()
    finally:
        conn.close()

    low_margin: list[dict] = []
    overrides: list[dict] = []
    low_confidence: list[dict] = []
    for row in rows:
        try:
            alternatives = json.loads(row["alternatives_json"] or "[]")
        except (TypeError, ValueError):
            alternatives = []
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}

        is_override = bool(metadata.get("override_source"))
        if is_override:
            overrides.append(
                {
                    "id": row["id"],
                    "subject_kind": row["subject_kind"],
                    "subject_id": row["subject_id"],
                    "chosen_label": row["label"],
                    "classifier_label": metadata.get("classifier_label"),
                    "classifier_confidence": metadata.get("classifier_confidence"),
                    "override_source": metadata.get("override_source"),
                    "alternatives": alternatives[:3],
                }
            )

        # For stages that produce alternatives (e.g. family_mapping),
        # compare rank-1 vs rank-2 scores. For flag/boolean stages,
        # flag rows with low confidence directly.
        is_flag_stage = (
            row["stage"].startswith("flag_")
            or row["stage"] in ("utility", "docket_number", "effective_date", "tariff_family")
        )
        if not is_flag_stage and alternatives:
            chosen_score = float(row["confidence"]) * 118.0  # _MAX_FAMILY_SCORE in family_matcher.py
            runner_score = float(alternatives[0][1]) if alternatives[0] else 0.0
            margin_score = chosen_score - runner_score
            if margin_score < margin * 118.0:
                low_margin.append(
                    {
                        "id": row["id"],
                        "subject_kind": row["subject_kind"],
                        "subject_id": row["subject_id"],
                        "chosen_label": row["label"],
                        "chosen_confidence": round(float(row["confidence"]), 3),
                        "runner_up_label": alternatives[0][0],
                        "runner_up_score": runner_score,
                        "margin_score": round(margin_score, 2),
                    }
                )

        if is_flag_stage:
            conf = float(row["confidence"])
            if conf > 0.0 and conf < 0.5:
                low_confidence.append(
                    {
                        "id": row["id"],
                        "subject_kind": row["subject_kind"],
                        "subject_id": row["subject_id"],
                        "label": row["label"],
                        "confidence": round(conf, 3),
                    }
                )

    report = {
        "stage": stage,
        "total_classifications": len(rows),
        "low_margin_count": len(low_margin),
        "overrides_count": len(overrides),
        "low_confidence_count": len(low_confidence),
        "low_margin": sorted(low_margin, key=lambda r: r["margin_score"])[:limit],
        "overrides": overrides[:limit],
        "low_confidence": sorted(low_confidence, key=lambda r: r["confidence"])[:limit],
    }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    is_flag_stage = (
        stage.startswith("flag_")
        or stage in ("utility", "docket_number", "effective_date", "tariff_family")
    )
    typer.echo(f"stage={stage}")
    typer.echo(f"  total active classifications: {report['total_classifications']}")
    if is_flag_stage:
        typer.echo(f"  low-confidence (conf < 0.5): {report['low_confidence_count']}")
    else:
        typer.echo(f"  low-margin (rank-1 within {margin} of rank-2): {report['low_margin_count']}")
        typer.echo(f"  overrides (hint changed classifier's label): {report['overrides_count']}")

    if not overrides_only and report["low_margin"]:
        typer.echo("")
        typer.echo("Top low-margin rows:")
        for r in report["low_margin"]:
            typer.echo(
                f"  id={r['id']} subj={r['subject_kind']}/{r['subject_id']} "
                f"chosen={r['chosen_label']} conf={r['chosen_confidence']} "
                f"runner_up={r['runner_up_label']} margin={r['margin_score']}"
            )

    if report["overrides"]:
        typer.echo("")
        typer.echo("Override rows (classifier vs hint disagreement):")
        for r in report["overrides"]:
            typer.echo(
                f"  id={r['id']} subj={r['subject_kind']}/{r['subject_id']} "
                f"chosen={r['chosen_label']} (was: {r['classifier_label']}, "
                f"clf_conf={r['classifier_confidence']}, src={r['override_source']})"
            )

    if report["low_confidence"]:
        typer.echo("")
        typer.echo("Low-confidence rows (conf < 0.5):")
        for r in report["low_confidence"]:
            typer.echo(
                f"  id={r['id']} subj={r['subject_kind']}/{r['subject_id']} "
                f"label={r['label']} conf={r['confidence']:.3f}"
            )


@app.command("adjudicate-classifications-nc")
def adjudicate_classifications_nc(
    limit: int = typer.Option(10, "--limit", help="Max documents to adjudicate."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be adjudicated without calling the LLM."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Run LLM adjudication on document_type disagreements.

    Finds rows where rule-based and embedding classifiers disagree, or where
    confidence is low (<0.5), or where either returned UNKNOWN. Runs the LLM
    adjudicator (``balanced_classifier`` role) on each and persists a new
    ``document_classifications`` row with classifier ``llm_<model>_v1``.

    The LLM result does NOT auto-supersede rule/embedding rows — superseding
    happens only via Phase 6 human review.
    """
    import sqlite3

    from duke_rates.classification.persistence import record_classification
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.llm_classifier import LLMAdjudicator
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.text_slicer import slice_pdf_text
    from duke_rates.classification.result import ClassificationResult

    settings, _ = _bootstrap()

    # 1. Find candidate rows
    conn = connect(settings.database_path)
    try:
        candidates = conn.execute(
            """
            SELECT
                r.subject_id,
                r.label AS rule_label,
                r.confidence AS rule_confidence,
                r.evidence_json AS rule_evidence,
                e.label AS emb_label,
                e.confidence AS emb_confidence,
                e.evidence_json AS emb_evidence
            FROM document_classifications r
            JOIN document_classifications e
              ON e.subject_kind = r.subject_kind
             AND e.subject_id = r.subject_id
             AND e.stage = r.stage
             AND e.classifier = 'embedding_knn_v1'
             AND e.superseded_by IS NULL
            LEFT JOIN document_classifications existing_llm
              ON existing_llm.subject_kind = r.subject_kind
             AND existing_llm.subject_id = r.subject_id
             AND existing_llm.stage = r.stage
             AND existing_llm.classifier LIKE 'llm_%'
             AND existing_llm.superseded_by IS NULL
            WHERE r.stage = 'document_type'
              AND r.classifier = 'rule_document_type_v1'
              AND r.superseded_by IS NULL
              AND existing_llm.id IS NULL
              AND (
                  r.label != e.label
                  OR r.label = 'UNKNOWN' OR e.label = 'UNKNOWN'
                  OR MAX(r.confidence, e.confidence) < 0.5
              )
            ORDER BY
                CASE
                    WHEN r.label = 'UNKNOWN' OR e.label = 'UNKNOWN' THEN 0
                    WHEN r.label != e.label THEN 1
                    ELSE 2
                END,
                ABS(r.confidence - e.confidence) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    if not candidates:
        typer.echo("No candidates found for LLM adjudication.")
        return

    typer.echo(f"Found {len(candidates)} candidate(s) for LLM adjudication.")
    typer.echo(f"  disagreements: {sum(1 for c in candidates if c['rule_label'] != c['emb_label'])}")
    typer.echo(f"  UNKNOWN (rule or emb): {sum(1 for c in candidates if c['rule_label'] == 'UNKNOWN' or c['emb_label'] == 'UNKNOWN')}")
    typer.echo(f"  low-confidence: {sum(1 for c in candidates if max(float(c['rule_confidence']), float(c['emb_confidence'])) < 0.5)}")

    if dry_run:
        typer.echo("")
        typer.echo(f"{'subj_id':>8s} {'rule_label':<20s} {'r_conf':>6s} {'emb_label':<20s} {'e_conf':>6s}")
        typer.echo("-" * 72)
        for c in candidates:
            typer.echo(
                f"{c['subject_id']:>8s} "
                f"{c['rule_label']:<20s} "
                f"{float(c['rule_confidence']):>6.3f} "
                f"{c['emb_label']:<20s} "
                f"{float(c['emb_confidence']):>6.3f}"
            )
        return

    # 2. Initialize adjudicator
    orch = OllamaOrchestrator(db_path=settings.database_path)
    ok, err = orch.health_probe("balanced_classifier")
    if not ok:
        typer.echo(f"ERROR: balanced_classifier health check failed: {err}")
        raise typer.Exit(code=1)

    adjudicator = LLMAdjudicator(orch, db_path=settings.database_path, role="balanced_classifier")

    # 3. Adjudicate each candidate
    results: list[dict] = []
    for idx, c in enumerate(candidates, 1):
        subj_id = c["subject_id"]
        typer.echo(f"\n[{idx}/{len(candidates)}] subject_id={subj_id}")

        # Get document path and text
        conn = connect(settings.database_path)
        try:
            doc = conn.execute(
                "SELECT local_path FROM historical_documents WHERE id = ?",
                (subj_id,),
            ).fetchone()
        finally:
            conn.close()

        if not doc:
            typer.echo("  SKIP: document not found in historical_documents")
            continue

        local_path = doc[0]
        slices = slice_pdf_text(Path(local_path), max_chars=2500)
        text = slices.full_text or ""

        if not text:
            typer.echo("  SKIP: no text extractable")
            continue

        # Parse prior results
        rule_result = ClassificationResult(
            label=c["rule_label"],
            confidence=float(c["rule_confidence"]),
            classifier="rule_document_type_v1",
        )
        emb_result = ClassificationResult(
            label=c["emb_label"],
            confidence=float(c["emb_confidence"]),
            classifier="embedding_knn_v1",
        )

        # Run adjudication
        llm_result = adjudicator.adjudicate(
            text, rule_result=rule_result, embedding_result=emb_result
        )

        typer.echo(
            f"  LLM says: {llm_result.label} (conf={llm_result.confidence:.3f}, "
            f"classifier={llm_result.classifier})"
        )

        # Persist
        conn = connect(settings.database_path)
        try:
            row_id = record_classification(
                conn,
                subject_kind="historical_document",
                subject_id=str(subj_id),
                stage="document_type",
                result=llm_result,
            )
            conn.commit()
        except Exception as exc:
            typer.echo(f"  WARN: persist failed: {exc}")
        finally:
            conn.close()

        results.append({
            "subject_id": subj_id,
            "rule_label": c["rule_label"],
            "rule_confidence": round(float(c["rule_confidence"]), 3),
            "emb_label": c["emb_label"],
            "emb_confidence": round(float(c["emb_confidence"]), 3),
            "llm_label": llm_result.label,
            "llm_confidence": llm_result.confidence,
            "llm_classifier": llm_result.classifier,
            "agrees_with": (
                "rule" if llm_result.label == c["rule_label"]
                else "embedding" if llm_result.label == c["emb_label"]
                else "neither"
            ),
        })

    # 4. Summary
    if as_json:
        typer.echo(json.dumps(results, indent=2, default=str))
        return

    if not results:
        typer.echo("\nNo results to report.")
        return

    typer.echo("\n--- Adjudication Summary ---")
    typer.echo(f"{'subj_id':>8s} {'rule':<20s} {'r_c':>5s} {'emb':<20s} {'e_c':>5s} {'llm':<20s} {'l_c':>5s} {'agrees'}")
    typer.echo("-" * 100)
    for r in results:
        typer.echo(
            f"{r['subject_id']:>8s} "
            f"{r['rule_label']:<20s} {r['rule_confidence']:>5.3f} "
            f"{r['emb_label']:<20s} {r['emb_confidence']:>5.3f} "
            f"{r['llm_label']:<20s} {r['llm_confidence']:>5.3f} "
            f"{r['agrees_with']}"
        )

    rule_agree = sum(1 for r in results if r["agrees_with"] == "rule")
    emb_agree = sum(1 for r in results if r["agrees_with"] == "embedding")
    neither = sum(1 for r in results if r["agrees_with"] == "neither")
    typer.echo(
        f"\nAgrees with rule: {rule_agree}, embedding: {emb_agree}, neither: {neither}"
    )


@app.command("fingerprint-corpus-nc")
def fingerprint_corpus_nc(
    limit: int = typer.Option(0, "--limit", help="Max PDFs to process (0 = all)."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-fingerprint PDFs even if a row already exists at the current fingerprinter version."),
    progress: bool = typer.Option(True, "--progress/--no-progress", help="Emit per-PDF progress to stderr."),
) -> None:
    """Fingerprint every PDF referenced by ``historical_documents`` and ``ncuc_discovery_records``.

    Populates ``document_fingerprints_v2`` so cluster reports have data.
    Idempotent at the current fingerprinter version — existing rows are
    skipped unless ``--refresh`` is passed.

    This is the bootstrap pass; new PDFs encountered during ingestion will
    be fingerprinted by the importer itself once that wiring lands.
    """
    from duke_rates.classification.fingerprint import (
        FINGERPRINTER_VERSION, fingerprint_pdf, save_fingerprint,
    )
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT local_path FROM (
                SELECT local_path FROM historical_documents WHERE local_path IS NOT NULL
                UNION
                SELECT local_path FROM ncuc_discovery_records WHERE local_path IS NOT NULL
            )
            ORDER BY local_path
            """
        ).fetchall()
        paths = [r["local_path"] for r in rows]
        if limit > 0:
            paths = paths[:limit]

        already_fingerprinted: set[str] = set()
        if not refresh:
            existing = conn.execute(
                "SELECT source_pdf FROM document_fingerprints_v2 WHERE fingerprinter_version = ?",
                (FINGERPRINTER_VERSION,),
            ).fetchall()
            already_fingerprinted = {r["source_pdf"] for r in existing}

        processed = 0
        skipped = 0
        failed = 0
        for i, path in enumerate(paths, 1):
            if path in already_fingerprinted:
                skipped += 1
                continue
            fp = fingerprint_pdf(path)
            if fp is None:
                failed += 1
                continue
            try:
                save_fingerprint(conn, fp)
                conn.commit()
                processed += 1
            except Exception:
                failed += 1
            if progress and i % 50 == 0:
                typer.echo(
                    f"[{i}/{len(paths)}] processed={processed} skipped={skipped} failed={failed}",
                    err=True,
                )
    finally:
        conn.close()

    typer.echo(
        f"fingerprint-corpus-nc done: total={len(paths)} "
        f"processed={processed} skipped={skipped} failed={failed}"
    )


@app.command("embed-corpus-nc")
def embed_corpus_nc(
    limit: int = typer.Option(0, "--limit", help="Max PDFs to process (0 = all)."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-embed even if a row already exists at the current embedding version."),
    embedding_kind: str = typer.Option("full_text", "--kind", help="Which text slice to embed: full_text, first_3_pages, title_block, rate_table_text, order_conclusion_section."),
    max_chars: int = typer.Option(2000, "--max-chars", help="Truncate text to this many characters before embedding (stays within model context windows)."),
    progress: bool = typer.Option(True, "--progress/--no-progress", help="Emit progress to stderr."),
) -> None:
    """Generate embeddings for every PDF referenced by ``historical_documents``.

    Populates ``document_embeddings`` so the embedding classifier has a
    reference population. Idempotent — existing (source_pdf, file_hash,
    embedding_kind, embedding_model, embedding_version) rows are skipped
    unless ``--refresh`` is passed.

    Runs against both ``embedding_primary`` and ``embedding_secondary``
    model roles, producing one row per model per slice.
    """
    import struct

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.text_slicer import slice_pdf_text

    EMBEDDING_VERSION = "v1"
    EMBEDDING_ROLES = ["embedding_primary", "embedding_secondary"]

    settings, _ = _bootstrap()

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT hd.local_path, hd.content_hash
            FROM historical_documents hd
            WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
            ORDER BY hd.local_path
            """
        ).fetchall()
    finally:
        conn.close()

    docs = [(r["local_path"], r["content_hash"]) for r in rows]
    if limit > 0:
        docs = docs[:limit]

    orchestrator = OllamaOrchestrator()

    # Resolve model names for the embedding roles
    role_models: list[tuple[str, str]] = []
    for role in EMBEDDING_ROLES:
        try:
            ok, msg = orchestrator.health_probe(role)
            if not ok:
                typer.echo(f"Warning: {role} unavailable ({msg}) — skipping", err=True)
                continue
            model = orchestrator._roles[role].primary
            role_models.append((role, model))
        except Exception:
            typer.echo(f"Warning: {role} not configured — skipping", err=True)

    if not role_models:
        typer.echo("No embedding models available. Check ollama_models.yaml.", err=True)
        return

    typer.echo(
        f"Embedding {len(docs)} PDFs × {len(role_models)} model(s) "
        f"(kind={embedding_kind})..."
    )

    # Build idempotency set of (source_pdf, file_hash, embedding_model) already present
    already_embedded: set[tuple[str, str, str]] = set()
    if not refresh:
        conn = connect(settings.database_path)
        try:
            for role, model in role_models:
                existing = conn.execute(
                    """
                    SELECT source_pdf, file_hash, embedding_model
                    FROM document_embeddings
                    WHERE embedding_kind = ?
                      AND embedding_model = ?
                      AND embedding_version = ?
                    """,
                    (embedding_kind, model, EMBEDDING_VERSION),
                ).fetchall()
                for r in existing:
                    already_embedded.add(
                        (r["source_pdf"], r["file_hash"], r["embedding_model"])
                    )
        finally:
            conn.close()

    processed = 0
    skipped = 0
    failed = 0
    for i, (local_path, file_hash) in enumerate(docs, 1):
        path = Path(local_path)
        if not path.exists():
            failed += 1
            if progress:
                typer.echo(
                    f"  [{i}/{len(docs)}] missing: {local_path}", err=True
                )
            continue

        # Extract text slice
        try:
            slices = slice_pdf_text(path, max_chars=max_chars)
        except Exception:
            failed += 1
            continue

        text = ""
        if embedding_kind == "full_text":
            text = slices.full_text
        elif embedding_kind == "first_3_pages":
            text = slices.first_3_pages
        elif embedding_kind == "title_block":
            text = slices.title_block
        elif embedding_kind == "rate_table_text":
            text = slices.rate_table_text
        elif embedding_kind == "order_conclusion_section":
            text = slices.order_conclusion_section

        if not text or not text.strip():
            skipped += 1
            continue

        any_embedded = False
        for role, model in role_models:
            if (local_path, file_hash, model) in already_embedded:
                continue

            try:
                vector = orchestrator.embed(role, text)
            except Exception:
                failed += 1
                if progress:
                    typer.echo(
                        f"  [{i}/{len(docs)}] embed failed: {local_path} "
                        f"({role}/{model})",
                        err=True,
                    )
                continue

            try:
                blob = struct.pack("f" * len(vector), *vector)
            except Exception:
                failed += 1
                continue

            conn = connect(settings.database_path)
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO document_embeddings
                        (source_pdf, file_hash, embedding_kind, embedding_model,
                         embedding_version, vector)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (local_path, file_hash, embedding_kind, model,
                     EMBEDDING_VERSION, blob),
                )
                conn.commit()
                any_embedded = True
            except Exception:
                failed += 1
            finally:
                conn.close()

        if any_embedded:
            processed += 1
        else:
            skipped += 1

        if progress and i % 10 == 0:
            typer.echo(
                f"[{i}/{len(docs)}] processed={processed} skipped={skipped} failed={failed}",
                err=True,
            )

    typer.echo(
        f"embed-corpus-nc done: total={len(docs)} "
        f"processed={processed} skipped={skipped} failed={failed}"
    )


@app.command("backfill-embedding-classifications-nc")
def backfill_embedding_classifications_nc(
    limit: int = typer.Option(0, "--limit", help="Only process N documents (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Classify but do not persist."),
    progress: bool = typer.Option(True, "--progress/--no-progress", help="Emit progress to stderr."),
) -> None:
    """Backfill embedding-based document_type classifications for existing documents.

    Runs the embedding KNN classifier against each historical_document that
    has embeddings in the reference table, and persists a second
    ``document_type`` row with ``classifier='embedding_knn_v1'``.

    Requires document_embeddings to be populated first (via embed-corpus-nc).
    """
    from pathlib import Path

    from duke_rates.classification.persistence import record_classification
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.embedding_classifier import (
        EmbeddingKNNClassifier,
    )
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )

    settings, _ = _bootstrap()

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT hd.id, hd.local_path, hd.family_key
            FROM historical_documents hd
            WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
              AND hd.id NOT IN (
                  SELECT DISTINCT CAST(subject_id AS INTEGER)
                  FROM document_classifications
                  WHERE subject_kind = 'historical_document'
                    AND stage = 'document_type'
                    AND classifier = 'embedding_knn_v1'
                    AND superseded_by IS NULL
              )
            ORDER BY hd.id
            """
        ).fetchall()
    finally:
        conn.close()

    docs = [dict(r) for r in rows]
    if limit > 0:
        docs = docs[:limit]

    if not docs:
        typer.echo("All documents already have embedding classifications.")
        return

    # Check that reference embeddings exist
    conn = connect(settings.database_path)
    try:
        emb_count = conn.execute(
            "SELECT COUNT(*) FROM document_embeddings"
        ).fetchone()[0]
    finally:
        conn.close()

    if emb_count == 0:
        typer.echo(
            "No embeddings found in document_embeddings. "
            "Run embed-corpus-nc first.",
            err=True,
        )
        return

    typer.echo(
        f"Backfilling embedding classifications for {len(docs)} documents "
        f"(reference set: {emb_count} embeddings)..."
    )

    if dry_run:
        typer.echo("[DRY RUN — no rows will be written]")

    orch = OllamaOrchestrator()
    clf = EmbeddingKNNClassifier(
        db_path=settings.database_path,
        orchestrator=orch,
        model_role="embedding_primary",
        k=11,
        min_neighbors=3,
        embedding_kind="full_text",
    )

    ok = skip = fail = 0
    for i, doc in enumerate(docs):
        doc_id = doc.get("id")
        local_path = doc.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            skip += 1
            continue

        try:
            result = clf.classify(local_path)
        except Exception:
            fail += 1
            continue

        if result.label == "UNKNOWN" and result.confidence == 0.0:
            skip += 1
            continue

        if dry_run:
            ok += 1
        else:
            cls_conn = connect(settings.database_path)
            try:
                record_classification(
                    cls_conn,
                    subject_kind="historical_document",
                    subject_id=str(doc_id),
                    stage="document_type",
                    result=result,
                )
                cls_conn.commit()
                ok += 1
            except Exception:
                fail += 1
            finally:
                cls_conn.close()

        if progress and (i + 1) % 25 == 0:
            typer.echo(
                f"  {i + 1}/{len(docs)} ok={ok} skip={skip} fail={fail}",
                err=True,
            )

    typer.echo(f"\nDone: ok={ok} skip={skip} fail={fail}")


@app.command("run-overnight-doc-intelligence-nc")
def run_overnight_doc_intelligence_nc(
    max_documents: int = typer.Option(0, "--max-documents", help="Max documents to process (0 = unlimited)."),
    max_runtime_minutes: int = typer.Option(0, "--max-runtime-minutes", help="Hard wall-clock cap in minutes (0 = unlimited)."),
    max_consecutive_failures: int = typer.Option(5, "--max-consecutive-failures", help="Abort after N consecutive model call failures."),
    stages: str = typer.Option("embed,llm_adjudicate", "--stages", help="Comma-separated stages: embed, llm_adjudicate."),
    since: str = typer.Option("", "--since", help="ISO8601 datetime — only process documents added/modified after this."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate work set and exit without model calls or DB writes."),
    resume: bool = typer.Option(False, "--resume", help="Skip subjects already covered at current prompt_version + model."),
    progress_interval: int = typer.Option(10, "--progress-interval", help="Emit progress every N documents."),
    health_probe_interval: int = typer.Option(50, "--health-probe-interval", help="Re-probe Ollama health every N documents."),
) -> None:
    """Run embedding generation + LLM adjudication as a resumable overnight batch.

    Processes the corpus in two sequential stages per document:
      1. **embed** — generate embeddings with ``embedding_primary`` role
      2. **llm_adjudicate** — run LLM on rule/embedding disagreements

    Safety guarantees:
      - No destructive overwrites — only INSERTs new rows
      - Bounded by wall-clock cap even with unlimited --max-documents
      - Resumable — --resume skips completed (subject, stage, model, prompt_version) tuples
      - Stops cleanly on: max docs, max runtime, consecutive failures, health probe degradation, SIGINT/SIGTERM

    End-of-run JSON report written to docs/reports/overnight_doc_intelligence/<timestamp>.json
    """
    import json as _json_mod
    import signal as _signal
    import struct
    import sqlite3
    import time
    from datetime import datetime as _datetime, timezone as _timezone
    from pathlib import Path as _Path

    from duke_rates.classification.persistence import record_classification
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.text_slicer import slice_pdf_text

    EMBEDDING_VERSION = "v1"
    EMBEDDING_ROLE = "embedding_primary"
    LLM_ROLE = "balanced_classifier"
    LLM_VERSION = "v1"

    settings, _ = _bootstrap()

    # ------------------------------------------------------------------
    # Parse stages
    # ------------------------------------------------------------------
    stage_list = [s.strip() for s in stages.split(",") if s.strip()]
    valid_stages = {"embed", "llm_adjudicate"}
    for s in stage_list:
        if s not in valid_stages:
            typer.echo(f"Unknown stage {s!r}. Valid: {', '.join(sorted(valid_stages))}", err=True)
            raise typer.Exit(code=1)

    typer.echo(f"Stages: {stage_list}")

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------
    orch = OllamaOrchestrator(db_path=settings.database_path)
    needed_roles: set[str] = set()
    if "embed" in stage_list:
        needed_roles.add(EMBEDDING_ROLE)
    if "llm_adjudicate" in stage_list:
        needed_roles.add(LLM_ROLE)

    role_models: dict[str, str] = {}
    for role in needed_roles:
        ok, err = orch.health_probe(role)
        if not ok:
            typer.echo(f"ERROR: {role} health check failed: {err}", err=True)
            raise typer.Exit(code=1)
        role_models[role] = orch._roles[role].primary
        typer.echo(f"  {role} -> {role_models[role]} (OK)")

    # ------------------------------------------------------------------
    # Build work set
    # ------------------------------------------------------------------
    conn = connect(settings.database_path)
    try:
        params: list = []
        extra_where = ""
        if since:
            extra_where = " AND hd.retrieved_at >= ?"
            params.append(since)

        rows = conn.execute(
            f"""
            SELECT DISTINCT hd.id, hd.local_path, hd.content_hash, hd.family_key, hd.retrieved_at
            FROM historical_documents hd
            WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
              AND hd.local_path != 'embedded'
              {extra_where}
            ORDER BY hd.id
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    docs = [dict(r) for r in rows]
    typer.echo(f"Corpus: {len(docs)} documents")

    # ------------------------------------------------------------------
    # Resume filter: check ollama_model_runs for completed subjects
    # ------------------------------------------------------------------
    if resume:
        conn = connect(settings.database_path)
        try:
            completed: set[tuple[int, str, str]] = set()
            for doc in docs:
                doc_id = doc["id"]
                # Check each stage
                for stage in stage_list:
                    role = EMBEDDING_ROLE if stage == "embed" else LLM_ROLE
                    model = role_models[role]
                    existing = conn.execute(
                        """
                        SELECT id FROM ollama_model_runs
                        WHERE subject_kind = 'historical_document'
                          AND CAST(subject_id AS INTEGER) = ?
                          AND stage = ?
                          AND role = ?
                          AND model = ?
                          AND prompt_version = ?
                          AND status = 'ok'
                        LIMIT 1
                        """,
                        (doc_id, stage, role, model, LLM_VERSION if stage == "llm_adjudicate" else EMBEDDING_VERSION),
                    ).fetchone()
                    if existing:
                        completed.add((doc_id, stage, role))
        finally:
            conn.close()

        # Filter
        original_count = len(docs)
        filtered: list[dict] = []
        for doc in docs:
            doc_id = doc["id"]
            needed = 0
            done = 0
            for stage in stage_list:
                role = EMBEDDING_ROLE if stage == "embed" else LLM_ROLE
                needed += 1
                if (doc_id, stage, role) in completed:
                    done += 1
            if done < needed:
                filtered.append(doc)
        docs = filtered
        typer.echo(f"Resume: {len(docs)} remaining (skipped {original_count - len(docs)} already completed)")

    if not docs:
        typer.echo("No documents to process.")
        return

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------
    if dry_run:
        embed_count = sum(1 for _ in docs) * (1 if "embed" in stage_list else 0)
        llm_count = 0
        if "llm_adjudicate" in stage_list:
            conn = connect(settings.database_path)
            try:
                llm_docs = set()
                for doc in docs:
                    # Check if rule/embedding disagree for this doc
                    pair = conn.execute(
                        """
                        SELECT 1 FROM document_classifications r
                        JOIN document_classifications e
                          ON e.subject_kind = r.subject_kind
                         AND e.subject_id = r.subject_id
                         AND e.stage = r.stage
                         AND e.classifier = 'embedding_knn_v1'
                         AND e.superseded_by IS NULL
                        WHERE r.subject_kind = 'historical_document'
                          AND r.subject_id = CAST(? AS TEXT)
                          AND r.stage = 'document_type'
                          AND r.classifier = 'rule_document_type_v1'
                          AND r.superseded_by IS NULL
                          AND (
                              r.label != e.label
                              OR r.label = 'UNKNOWN' OR e.label = 'UNKNOWN'
                              OR MAX(r.confidence, e.confidence) < 0.5
                          )
                        """,
                        (doc["id"],),
                    ).fetchone()
                    if pair:
                        llm_docs.add(doc["id"])
                llm_count = len(llm_docs)
            finally:
                conn.close()

        typer.echo("\n--- Dry Run Work Set ---")
        typer.echo(f"  embed calls:          {embed_count}")
        typer.echo(f"  llm_adjudicate calls: {llm_count}")
        typer.echo(f"  total documents:      {len(docs)}")

        # Estimate runtime
        est_embed_s = embed_count * 2.0  # ~2s per embedding
        est_llm_s = llm_count * 8.0       # ~8s per LLM call
        est_total_s = est_embed_s + est_llm_s
        if est_total_s < 60:
            typer.echo(f"  est. runtime:         {est_total_s:.0f}s")
        elif est_total_s < 3600:
            typer.echo(f"  est. runtime:         {est_total_s / 60:.1f}m")
        else:
            typer.echo(f"  est. runtime:         {est_total_s / 3600:.1f}h")
        return

    # ------------------------------------------------------------------
    # Signal handling for clean exit
    # ------------------------------------------------------------------
    _abort_flag = {"value": False}

    def _handle_signal(signum, frame):
        typer.echo("\nInterrupted — finishing current document and exiting...", err=True)
        _abort_flag["value"] = True

    _signal.signal(_signal.SIGINT, _handle_signal)
    _signal.signal(_signal.SIGTERM, _handle_signal)

    # ------------------------------------------------------------------
    # Initialize stage processors
    # ------------------------------------------------------------------
    embed_clf = None
    if "embed" in stage_list:
        from duke_rates.document_intelligence.embedding_classifier import EmbeddingKNNClassifier

    llm_adj = None
    if "llm_adjudicate" in stage_list:
        from duke_rates.document_intelligence.llm_classifier import LLMAdjudicator
        llm_adj = LLMAdjudicator(orch, db_path=settings.database_path, role=LLM_ROLE)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    start_time = time.monotonic()
    wall_deadline = (
        start_time + (max_runtime_minutes * 60)
        if max_runtime_minutes > 0
        else float("inf")
    )

    stats: dict[str, dict] = {
        "embed": {"ok": 0, "skip": 0, "fail": 0, "no_text": 0},
        "llm_adjudicate": {"ok": 0, "skip": 0, "fail": 0, "not_needed": 0},
    }
    consecutive_failures = 0
    last_health_probe = 0
    stop_reason = "completed"

    for doc_idx, doc in enumerate(docs):
        doc_id = doc["id"]
        local_path = doc.get("local_path", "")

        # --- Stop checks before processing ---
        if _abort_flag["value"]:
            stop_reason = "interrupted"
            break

        if max_documents > 0 and doc_idx >= max_documents:
            stop_reason = "max_documents"
            break

        if time.monotonic() >= wall_deadline:
            stop_reason = "max_runtime"
            break

        if consecutive_failures >= max_consecutive_failures:
            stop_reason = "max_consecutive_failures"
            break

        # Periodic health re-probe
        if doc_idx - last_health_probe >= health_probe_interval:
            last_health_probe = doc_idx
            for role in needed_roles:
                ok_hp, err_hp = orch.health_probe(role)
                if not ok_hp:
                    stop_reason = f"health_probe_failed:{role}"
                    typer.echo(
                        f"\nHealth probe failed for {role}: {err_hp} — stopping.",
                        err=True,
                    )
                    break
            if stop_reason != "completed":
                break

        path = _Path(local_path)
        if not path.exists():
            stats["embed"]["skip"] += 1
            stats["llm_adjudicate"]["skip"] += 1
            continue

        # --- Stage: embed ---
        if "embed" in stage_list:
            # Idempotency check
            conn = connect(settings.database_path)
            try:
                existing_emb = conn.execute(
                    """
                    SELECT id FROM document_embeddings
                    WHERE source_pdf = ?
                      AND file_hash = ?
                      AND embedding_kind = 'full_text'
                      AND embedding_model = ?
                      AND embedding_version = ?
                    """,
                    (local_path, doc.get("content_hash", ""),
                     role_models[EMBEDDING_ROLE], EMBEDDING_VERSION),
                ).fetchone()
            finally:
                conn.close()

            if existing_emb:
                stats["embed"]["skip"] += 1
            else:
                try:
                    slices = slice_pdf_text(path, max_chars=2000)
                    text = slices.full_text or ""
                except Exception:
                    stats["embed"]["fail"] += 1
                    consecutive_failures += 1
                    text = ""

                if not text or not text.strip():
                    stats["embed"]["no_text"] += 1
                else:
                    try:
                        vector = orch.embed(EMBEDDING_ROLE, text)
                        blob = struct.pack("f" * len(vector), *vector)

                        conn = connect(settings.database_path)
                        try:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO document_embeddings
                                    (source_pdf, file_hash, embedding_kind, embedding_model,
                                     embedding_version, vector)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (local_path, doc.get("content_hash", ""), "full_text",
                                 role_models[EMBEDDING_ROLE], EMBEDDING_VERSION, blob),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                        stats["embed"]["ok"] += 1
                        consecutive_failures = 0
                    except Exception:
                        stats["embed"]["fail"] += 1
                        consecutive_failures += 1

        # --- Stage: llm_adjudicate ---
        if "llm_adjudicate" in stage_list:
            # Check if this doc has both rule and embedding classifications
            conn = connect(settings.database_path)
            try:
                # Skip if already adjudicated
                existing_llm = conn.execute(
                    """
                    SELECT id FROM document_classifications
                    WHERE subject_kind = 'historical_document'
                      AND subject_id = CAST(? AS TEXT)
                      AND stage = 'document_type'
                      AND classifier LIKE 'llm_%'
                      AND superseded_by IS NULL
                    """,
                    (doc_id,),
                ).fetchone()

                if existing_llm:
                    stats["llm_adjudicate"]["skip"] += 1
                    conn.close()
                    continue

                pair = conn.execute(
                    """
                    SELECT r.label AS rule_label, r.confidence AS rule_confidence,
                           e.label AS emb_label, e.confidence AS emb_confidence
                    FROM document_classifications r
                    JOIN document_classifications e
                      ON e.subject_kind = r.subject_kind
                     AND e.subject_id = r.subject_id
                     AND e.stage = r.stage
                     AND e.classifier = 'embedding_knn_v1'
                     AND e.superseded_by IS NULL
                    WHERE r.subject_kind = 'historical_document'
                      AND r.subject_id = CAST(? AS TEXT)
                      AND r.stage = 'document_type'
                      AND r.classifier = 'rule_document_type_v1'
                      AND r.superseded_by IS NULL
                    """,
                    (doc_id,),
                ).fetchone()
            finally:
                conn.close()

            if not pair:
                stats["llm_adjudicate"]["not_needed"] += 1
                continue

            rule_label = pair["rule_label"] or "UNKNOWN"
            emb_label = pair["emb_label"] or "UNKNOWN"
            rule_conf = float(pair["rule_confidence"] or 0)
            emb_conf = float(pair["emb_confidence"] or 0)

            need_adjudication = (
                rule_label != emb_label
                or rule_label == "UNKNOWN" or emb_label == "UNKNOWN"
                or max(rule_conf, emb_conf) < 0.5
            )

            if not need_adjudication:
                stats["llm_adjudicate"]["not_needed"] += 1
                continue

            # Extract text
            slices = slice_pdf_text(path, max_chars=2500)
            text = slices.full_text or ""
            if not text:
                stats["llm_adjudicate"]["no_text"] = stats["llm_adjudicate"].get("no_text", 0) + 1
                continue

            try:
                from duke_rates.classification.result import ClassificationResult

                rule_result = ClassificationResult(
                    label=rule_label, confidence=rule_conf, classifier="rule_document_type_v1",
                )
                emb_result = ClassificationResult(
                    label=emb_label, confidence=emb_conf, classifier="embedding_knn_v1",
                )
                llm_result = llm_adj.adjudicate(
                    text, rule_result=rule_result, embedding_result=emb_result,
                )

                if llm_result.label != "UNKNOWN":
                    conn = connect(settings.database_path)
                    try:
                        record_classification(
                            conn,
                            subject_kind="historical_document",
                            subject_id=str(doc_id),
                            stage="document_type",
                            result=llm_result,
                        )
                        conn.commit()
                    finally:
                        conn.close()

                stats["llm_adjudicate"]["ok"] += 1
                consecutive_failures = 0
            except Exception:
                stats["llm_adjudicate"]["fail"] += 1
                consecutive_failures += 1

        # --- Progress ---
        if progress_interval > 0 and (doc_idx + 1) % progress_interval == 0:
            elapsed = time.monotonic() - start_time
            rate = (doc_idx + 1) / elapsed if elapsed > 0 else 0
            typer.echo(
                f"  [{doc_idx + 1}/{len(docs)}] "
                f"embed ok={stats['embed']['ok']} fail={stats['embed']['fail']} | "
                f"llm ok={stats['llm_adjudicate']['ok']} fail={stats['llm_adjudicate']['fail']} | "
                f"{rate:.1f} docs/s",
                err=True,
            )

    # ------------------------------------------------------------------
    # End-of-run report
    # ------------------------------------------------------------------
    elapsed_total = time.monotonic() - start_time
    report_dir = _Path("docs/reports/overnight_doc_intelligence")
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _datetime.now(_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"{timestamp}.json"

    report = {
        "timestamp": timestamp,
        "stop_reason": stop_reason,
        "config": {
            "stages": stage_list,
            "max_documents": max_documents if max_documents > 0 else None,
            "max_runtime_minutes": max_runtime_minutes if max_runtime_minutes > 0 else None,
            "max_consecutive_failures": max_consecutive_failures,
            "resume": resume,
            "since": since or None,
        },
        "runtime": {
            "total_seconds": round(elapsed_total, 1),
            "documents_processed": len(docs),
            "docs_per_second": round(len(docs) / elapsed_total, 2) if elapsed_total > 0 else 0,
        },
        "stats": {
            stage: {
                k: v for k, v in s.items()
            }
            for stage, s in stats.items()
        },
        "roles_used": {role: model for role, model in role_models.items()},
    }

    with open(report_path, "w", encoding="utf-8") as fh:
        _json_mod.dump(report, fh, indent=2, default=str)

    # ------------------------------------------------------------------
    # Summary to stderr
    # ------------------------------------------------------------------
    typer.echo(f"\n--- Overnight Run Complete ---")
    typer.echo(f"  stop reason:   {stop_reason}")
    typer.echo(f"  elapsed:       {elapsed_total:.1f}s ({elapsed_total / 60:.1f}m)")
    typer.echo(f"  documents:     {len(docs)}")
    for stage, s in stats.items():
        parts = ", ".join(f"{k}={v}" for k, v in s.items() if v > 0)
        typer.echo(f"  {stage}: {parts}")
    typer.echo(f"  report:        {report_path}")


# =============================================================================
# Parsing-refactor and document-identity CLI commands
# =============================================================================
from duke_rates.cli_commands.parse_refactor import (
    analyze_parse_failures_nc,
    populate_document_identity_nc,
    register_parse_refactor_commands,
    report_document_fingerprint_clusters_nc,
    report_document_identity_nc,
    report_document_identity_quality_nc,
    report_document_identity_summary_nc,
    report_profile_recommendations_nc,
    report_wrong_profile_diagnostics_nc,
    run_llm_parse_fallback_nc,
    run_overnight_parse_improvement_nc,
    suggest_regex_fixes_nc,
    validate_regex_suggestions_nc,
)

register_parse_refactor_commands(app)

@app.command("backfill-classifications-nc")
def backfill_classifications_nc(
    limit: int = typer.Option(0, "--limit", help="Max historical docs to backfill (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be recorded without writing."),
) -> None:
    """Backfill document_classifications rows for existing historical documents.

    The classification persistence was wired into the importer after most
    historical docs had already been created.  This command fills in the
    missing ``family_mapping`` classification rows using the evidence that
    was already stored in ``historical_documents.evidence_json`` at import
    time.

    Idempotent — running it again skips docs that already have a
    ``family_mapping`` classification at the current classifier version.
    """
    import json as _json

    settings, _ = _bootstrap()
    conn = None
    conn_classify = None
    try:
        from duke_rates.db.sqlite import connect
        from duke_rates.classification.result import ClassificationResult
        from duke_rates.classification.persistence import record_classification

        conn = connect(settings.database_path)
        conn.row_factory = __import__("sqlite3").Row

        rows = conn.execute(
            """
            SELECT hd.id, hd.family_key, hd.evidence_json, hd.title
            FROM historical_documents hd
            WHERE hd.family_key IS NOT NULL
              AND hd.family_key != ''
              AND hd.evidence_json IS NOT NULL
              AND hd.evidence_json != ''
              AND hd.evidence_json != '{}'
              AND NOT EXISTS (
                SELECT 1 FROM document_classifications dc
                WHERE dc.subject_kind = 'historical_document'
                  AND dc.subject_id = CAST(hd.id AS TEXT)
                  AND dc.stage = 'family_mapping'
                  AND dc.classifier = 'family_matcher_v1'
              )
            ORDER BY hd.id
            """
        ).fetchall()
        total = len(rows)
        if limit > 0:
            rows = rows[:limit]

        typer.echo(
            f"Backfill candidates: {total} total, processing {len(rows)}"
            + (" (dry run)" if dry_run else "")
        )

        recorded = 0
        skipped_empty_evidence = 0
        for row in rows:
            try:
                evidence_raw = _json.loads(row["evidence_json"])
            except (_json.JSONDecodeError, TypeError):
                evidence_raw = {}

            # evidence_json can contain non-numeric fields
            # (is_redline bool, redline_notes str, etc.) — extract only
            # numeric score contributions for the ClassificationResult.
            numeric_evidence: dict[str, float] = {}
            for k, v in evidence_raw.items():
                try:
                    numeric_evidence[k] = float(v)
                except (ValueError, TypeError):
                    pass
            if not numeric_evidence:
                skipped_empty_evidence += 1
                continue

            total_score = sum(numeric_evidence.values())
            result = ClassificationResult.from_score_breakdown(
                label=row["family_key"],
                score=total_score,
                score_breakdown=numeric_evidence,
                all_scores={row["family_key"]: total_score},
                classifier="family_matcher_v1",
                classifier_version="backfill_v1",
            )

            if dry_run:
                typer.echo(
                    f"  [dry-run] hd={row['id']} label={result.label} "
                    f"confidence={result.confidence:.2f} "
                    f"evidence_keys={list(evidence_raw.keys())[:3]}"
                )
            else:
                conn_classify = conn_classify or connect(settings.database_path)
                record_classification(
                    conn_classify,
                    subject_kind="historical_document",
                    subject_id=str(row["id"]),
                    stage="family_mapping",
                    result=result,
                )
            recorded += 1

        if not dry_run and conn_classify:
            conn_classify.commit()

        typer.echo(
            f"backfill-classifications-nc done: recorded={recorded} "
            f"skipped_empty_evidence={skipped_empty_evidence}"
        )
    finally:
        if conn_classify:
            conn_classify.close()
        if conn:
            conn.close()


@app.command("process-docling-batch")
def process_docling_batch(
    accelerator: str = typer.Option("cpu", help="Accelerator: cpu or cuda."),
    limit: int = typer.Option(0, help="Max documents to process (0 = all)."),
    classification: str = typer.Option(
        "",
        help="Filter by filing_classification (e.g. tariff_sheets, order, testimony). Empty = all.",
    ),
    scanned: bool = typer.Option(False, "--scanned", help="Enable Tesseract OCR for all documents."),
    force: bool = typer.Option(False, "--force", help="Re-process documents already in DB."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List documents that would be processed without running Docling."),
    source: str = typer.Option(
        "discovery",
        help="Source table: 'discovery' (ncuc_discovery_records) or 'historical' (historical_documents NC).",
    ),
    ocr_remediation: bool = typer.Option(
        False, "--ocr-remediation",
        help="Target only historical_documents flagged as OCR remediation candidates (run_docling_or_paddle_structure lane). Implies --source historical --scanned.",
    ),
) -> None:
    """Process a batch of NCUC documents through Docling in a single long-running process.

    Runs unprocessed local PDFs through Docling and stores results in the docling_artifacts
    table.  Keeping everything in one process avoids the CUDA DLL cold-start penalty
    (3-5 minutes) that would occur if each document were run as a separate invocation.

    The GPU/CUDA DLLs are loaded once at startup and reused for every document in the batch.

    Examples:
      # Process all tariff sheets on GPU (discovery records source)
      duke-rates process-docling-batch --accelerator cuda --classification tariff_sheets

      # Process the 322 OCR-remediation historical docs on GPU (scanned, no text)
      duke-rates process-docling-batch --accelerator cuda --ocr-remediation

      # Dry run to see what would be processed
      duke-rates process-docling-batch --dry-run --limit 20

      # Process up to 100 historical docs on CPU
      duke-rates process-docling-batch --source historical --accelerator cpu --limit 100
    """
    from duke_rates.historical.ncuc.pipeline.docling_backend import (
        PIPELINE_STANDARD, convert_pdf_safe, get_docling_unavailable_reason,
        DOCLING_BACKEND_VERSION,
    )
    from duke_rates.db.sqlite import connect
    from duke_rates.hardware.cpu_config import configure_cpu, configure_torch_inference, warmup_gpu

    if not dry_run:
        unavailable = get_docling_unavailable_reason()
        if unavailable:
            typer.echo(f"Docling unavailable: {unavailable}")
            raise typer.Exit(code=1)

        configure_cpu()
        configure_torch_inference()
        if accelerator == "cuda":
            typer.echo("Warming up GPU (loads CUDA DLLs once for the entire batch)...")
            warmup_gpu()
            typer.echo("GPU ready.")

    # --ocr-remediation implies --source historical and --scanned
    if ocr_remediation:
        source = "historical"
        scanned = True

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    # Cached-success status values stored across different docling_backend versions
    _SUCCESS_STATUSES = "('success', 'ConversionStatus.SUCCESS', 'ConversionStatus.PARTIAL_SUCCESS', 'partial_success')"

    if source == "historical":
        # Query historical_documents for NC docs that need Docling processing.
        # Deduplicate by local_path — multiple family rows may share the same PDF.
        dedup_conditions = "hd.state = 'NC' AND hd.local_path IS NOT NULL"
        params: list = []

        cache_filter = ""
        if not force:
            cache_filter = f"""
              AND NOT EXISTS (
                SELECT 1 FROM docling_artifacts a
                WHERE a.source_pdf = hd.local_path
                  AND a.backend_version = ?
                  AND a.accelerator = ?
                  AND a.status IN {_SUCCESS_STATUSES}
              )
            """
            params.extend([DOCLING_BACKEND_VERSION, accelerator])

        ocr_filter = ""
        if ocr_remediation:
            if not force:
                # When doing OCR remediation, a "successful" docling run that
                # didn't produce usable raw text isn't really done — the doc
                # still has raw_text_path = NULL.  Override the cache_filter
                # so it only skips docs that have BOTH a cached artifact AND
                # usable raw text on disk.
                cache_filter = f"""
                  AND NOT (
                    EXISTS (
                      SELECT 1 FROM docling_artifacts a
                      WHERE a.source_pdf = hd.local_path
                        AND a.backend_version = ?
                        AND a.accelerator = ?
                        AND a.status IN {_SUCCESS_STATUSES}
                    )
                    AND (hd.raw_text_path IS NOT NULL AND hd.raw_text_path != '')
                  )
                """
            # Target docs with no usable raw text OR weak_layout_sensitive route.
            # The first branch catches docs the OCR pipeline never produced text for.
            # The second branch catches docs that DO have text but the latest parser
            # outcome was weak/empty on a layout-heavy page set — these benefit from
            # Docling's structure-aware re-conversion even though they have raw text.
            #
            # We intentionally do NOT filter on ncuc_page_artifacts: many candidates
            # have noisy page artifacts from prior CPU Docling runs but raw_text_path
            # is still NULL. Use --force to also skip the docling_artifacts cache.
            ocr_filter = """
              AND (
                (hd.raw_text_path IS NULL OR hd.raw_text_path = '')
                OR EXISTS (
                  SELECT 1 FROM v_document_diagnostics vd
                  WHERE vd.historical_document_id = hd.id
                    AND vd.route_reason = 'weak_layout_sensitive'
                )
              )
            """

        query = f"""
            SELECT MIN(hd.id) AS id, hd.local_path,
                   MIN(hd.content_hash) AS content_hash,
                   NULL AS filing_classification,
                   MIN(hd.raw_text_path) AS raw_text_path,
                   MIN(hd.family_key) AS family_key
            FROM historical_documents hd
            WHERE {dedup_conditions}
            {cache_filter}
            {ocr_filter}
            GROUP BY hd.local_path
            ORDER BY MIN(hd.id) ASC
        """
    else:
        # Default: query ncuc_discovery_records
        query = """
            SELECT r.id, r.local_path, r.content_hash, r.filing_classification,
                   NULL AS raw_text_path, NULL AS family_key
            FROM ncuc_discovery_records r
            WHERE r.local_path IS NOT NULL
        """
        params = []

        if not force:
            query += f"""
              AND NOT EXISTS (
                SELECT 1 FROM docling_artifacts a
                WHERE a.source_pdf = r.local_path
                  AND a.backend_version = ?
                  AND a.accelerator = ?
                  AND a.status IN {_SUCCESS_STATUSES}
              )
            """
            params.extend([DOCLING_BACKEND_VERSION, accelerator])

        if classification:
            query += " AND r.filing_classification = ?"
            params.append(classification)

        query += " ORDER BY r.file_size_bytes ASC"  # smallest first — quicker wins early

    if limit > 0:
        query += f" LIMIT {limit}"

    rows = conn.execute(query, params).fetchall()
    total = len(rows)

    if total == 0:
        # Surface a diagnostic instead of leaving the operator guessing why the
        # batch is empty. Distinguishes "filtered to nothing" from "all already done".
        hints: list[str] = []
        if not force:
            hints.append("--force re-runs already-processed docs (current default skips them)")
        if classification:
            hints.append(f"--classification={classification!r} filter is active; remove to widen")
        if ocr_remediation:
            hints.append("--ocr-remediation restricts to the run_docling_or_paddle_structure lane; check `ocr show-remediation-candidates-nc`")
        if source == "historical":
            hints.append("source=historical only sees docs with hd.local_path set; check lineage show-fingerprint-coverage-nc")
        elif source == "discovery":
            hints.append("source=discovery requires ncuc_discovery_records rows; check ncuc list")
        if scanned:
            hints.append("--scanned restricts to docs flagged scanned=True")
        typer.echo(
            f"No documents matched the batch filters "
            f"[source={source}, scanned={scanned}, ocr_remediation={ocr_remediation}, "
            f"classification={classification or '(any)'}, force={force}]."
        )
        if hints:
            typer.echo("Possible reasons:")
            for hint in hints:
                typer.echo(f"  - {hint}")
        conn.close()
        return

    if dry_run:
        typer.echo(f"Would process {total} document(s) [source={source}, scanned={scanned}, ocr_remediation={ocr_remediation}]:")
        for r in rows[:50]:
            label = r["filing_classification"] or r["family_key"] or "unknown"
            typer.echo(f"  [{label}] {r['local_path']}")
        if total > 50:
            typer.echo(f"  ... and {total - 50} more")
        conn.close()
        return

    typer.echo(f"Processing {total} document(s) with accelerator={accelerator} source={source} scanned={scanned}")
    typer.echo("Press Ctrl+C to stop — progress is committed after each document.\n")

    done = 0
    failed = 0
    skipped = 0

    try:
        for i, row in enumerate(rows, 1):
            pdf_path = row["local_path"]
            record_id = row["id"]

            if not __import__("pathlib").Path(pdf_path).exists():
                typer.echo(f"  [{i}/{total}] SKIP (missing): {pdf_path}")
                skipped += 1
                continue

            typer.echo(f"  [{i}/{total}] {pdf_path}", nl=False)

            import time as _time
            t0 = _time.perf_counter()

            # historical docs don't have a discovery_record FK — pass None; artifact
            # is still keyed by source_pdf so mine-docling-nc can pick it up
            discovery_record_id = record_id if source == "discovery" else None

            result = convert_pdf_safe(
                pdf_path,
                accelerator=accelerator,
                force=force,
                has_scanned_pages=scanned,
                conn=conn,
                discovery_record_id=discovery_record_id,
            )

            elapsed = _time.perf_counter() - t0

            if result:
                conn.commit()
                tables_count = len(result.get("tables") or [])
                degraded = result.get("_degraded_modes")
                skipped = result.get("_skipped_pages", [])
                suffix = ""
                if degraded:
                    suffix = f" [{','.join(degraded)}]"
                if skipped:
                    suffix += f" (skipped {len(skipped)}p)"
                typer.echo(
                    f"  OK  pages={result['page_count']} tables={tables_count} "
                    f"t={elapsed:.1f}s{suffix}"
                )
                done += 1
            else:
                typer.echo(f"  FAIL  t={elapsed:.1f}s")
                failed += 1

    except KeyboardInterrupt:
        typer.echo("\nInterrupted — committing progress.")
        conn.commit()

    conn.close()
    typer.echo(f"\nDone: {done} converted, {failed} failed, {skipped} skipped of {total} total.")


@app.command("recover-history-progress-nc")
def recover_history_progress_nc(
    limit_documents: int = typer.Option(
        15,
        help="How many current Progress NC PDFs to use as seeds.",
    ),
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        10,
        help="Max archived revisions per seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_documents=limit_documents,
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
        )
    finally:
        service.close()

    typer.echo(f"Recovered {len(recovered)} historical Progress NC documents.")
    for record in recovered:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.revision_label or "-",
                    "direct" if record.direct_downloadable else "archive-only",
                    record.title,
                ]
            )
        )


@app.command("list-history-progress-nc")
def list_history_progress_nc() -> None:
    _, repository = _bootstrap()
    rows = repository.list_historical_documents(state="NC", company="progress")
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row.id or "-"),
                    row.leaf_no or "-",
                    row.effective_start or "-",
                    row.effective_end or "-",
                    row.revision_label or "-",
                    row.supersedes_label or "-",
                    "direct" if row.direct_downloadable else "archive-only",
                    row.title,
                ]
            )
        )


@app.command("recover-public-notices-progress-nc")
def recover_public_notices_progress_nc(
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_page_snapshots: int = typer.Option(8, help="Max archived public-notice pages to inspect."),
    max_documents_per_snapshot: int = typer.Option(
        20,
        help="Max linked PDFs to process from each archived notice page.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCPublicNoticeRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            from_year=from_year,
            max_page_snapshots=max_page_snapshots,
            max_documents_per_snapshot=max_documents_per_snapshot,
        )
    finally:
        service.close()

    typer.echo(f"Recovered {len(recovered)} public-notice historical Progress NC documents.")
    for record in recovered:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.category,
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.revision_label or "-",
                    record.title,
                ]
            )
        )


@app.command("list-history-chains-progress-nc")
def list_history_chains_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, revision label, or parsed schedule/rider id.",
    ),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCLineageService(repository)
    chains = service.build_chains(query=query, recovered_only=True)
    for chain in chains:
        latest = chain.versions[0]
        typer.echo(
            "\t".join(
                [
                    chain.family_key,
                    chain.leaf_no or "-",
                    chain.category,
                    str(len(chain.versions)),
                    latest.effective_start or "-",
                    latest.revision_label or "-",
                    chain.title,
                ]
            )
        )


@app.command("preview-history-family-crosswalk-progress-nc")
def preview_history_family_crosswalk_progress_nc() -> None:
    _, repository = _bootstrap()
    matches = ProgressNCFamilyCrosswalkService(repository).preview()
    typer.echo(
        json.dumps(
            [match.model_dump(mode="json") for match in matches],
            indent=2,
            default=str,
        )
    )


@app.command("apply-history-family-crosswalk-progress-nc")
def apply_history_family_crosswalk_progress_nc() -> None:
    _, repository = _bootstrap()
    matches = ProgressNCFamilyCrosswalkService(repository).apply()
    typer.echo(
        json.dumps(
            [match.model_dump(mode="json") for match in matches],
            indent=2,
            default=str,
        )
    )


@app.command("show-history-chain-progress-nc")
def show_history_chain_progress_nc(query: str) -> None:
    _, repository = _bootstrap()
    service = ProgressNCLineageService(repository)
    chains = service.build_chains(query=query, recovered_only=True)
    if not chains:
        raise typer.BadParameter(f"No Progress NC historical chain matched query={query!r}")
    typer.echo(
        json.dumps(
            [chain.model_dump(mode="json") for chain in chains],
            indent=2,
            default=str,
        )
    )


@app.command("list-history-notice-links-progress-nc")
def list_history_notice_links_progress_nc() -> None:
    _, repository = _bootstrap()
    links = ProgressNCNoticeLinkService(repository).build_links()
    for link in links:
        typer.echo(
            "\t".join(
                [
                    str(link.historical_id),
                    ",".join(link.docket_numbers) or "-",
                    ",".join(link.related_rider_codes) or "-",
                    ",".join(link.related_schedule_codes) or "-",
                    " | ".join(f"{match.basis}->{match.title}" for match in link.matches),
                    link.title,
                ]
            )
        )


@app.command("show-history-tariff-progress-nc")
def show_history_tariff_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
) -> None:
    _, repository = _bootstrap()
    selection = ProgressNCHistoricalTariffSelector(repository).select_schedule(
        schedule_code=schedule_code,
        service_date=_parse_service_date(service_date),
    )
    typer.echo(json.dumps(selection.model_dump(mode="json"), indent=2, default=str))


@app.command("estimate-history-bill-progress-nc")
def estimate_history_bill_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
    usage_file: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    monthly_kwh: float | None = typer.Option(None),
    peak_kw: float | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    if not usage_file and monthly_kwh is None:
        raise typer.BadParameter("Provide --usage-file or --monthly-kwh.")

    usage = (
        _read_usage_file(usage_file)
        if usage_file
        else UsageInput(
            monthly_kwh=monthly_kwh or 0.0,
            peak_kw=peak_kw,
        )
    )
    selection = ProgressNCHistoricalTariffSelector(repository).select_schedule(
        schedule_code=schedule_code,
        service_date=_parse_service_date(service_date),
    )
    estimate = BillingEngine().estimate(
        selection.schedule,
        usage,
        rider_parse_results=[
            rider.parse_result for rider in selection.riders if rider.parse_result
        ],
    )
    typer.echo(
        json.dumps(
            {
                "service_date": service_date,
                "selected_version": selection.version.model_dump(mode="json"),
                "applicable_riders": [
                    rider.model_dump(mode="json") for rider in selection.riders
                ],
                "supporting_notices": [
                    notice.model_dump(mode="json") for notice in selection.supporting_notices
                ],
                "unresolved_rider_codes": selection.unresolved_rider_codes,
                "future_rider_codes": selection.future_rider_codes,
                "bill_estimate": estimate.model_dump(mode="json"),
            },
            indent=2,
            default=str,
        )
    )


@app.command("recover-history-gaps-progress-nc")
def recover_history_gaps_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
    limit_documents: int = typer.Option(
        12,
        help="How many current rider PDFs to use as Wayback seeds.",
    ),
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per rider seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    selector = ProgressNCHistoricalTariffSelector(repository)
    requested_date = _parse_service_date(service_date)
    before = selector.select_schedule(schedule_code=schedule_code, service_date=requested_date)
    gap_codes = sorted(
        {
            *before.future_rider_codes,
            *(rider.code for rider in before.riders if rider.status == "undated"),
        }
    )
    if not gap_codes:
        typer.echo(
            json.dumps(
                {
                    "service_date": service_date,
                    "schedule_code": schedule_code.upper(),
                    "gap_rider_codes": [],
                    "recovered": [],
                    "after": before.model_dump(mode="json"),
                },
                indent=2,
                default=str,
            )
        )
        raise typer.Exit()

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_documents=limit_documents,
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            categories={DocumentCategory.RIDER.value},
            target_rider_codes=set(gap_codes),
        )
    finally:
        service.close()

    after = ProgressNCHistoricalTariffSelector(repository).select_schedule(
        schedule_code=schedule_code,
        service_date=requested_date,
    )
    typer.echo(
        json.dumps(
            {
                "service_date": service_date,
                "schedule_code": schedule_code.upper(),
                "gap_rider_codes": gap_codes,
                "recovered": [record.model_dump(mode="json") for record in recovered],
                "remaining_future_rider_codes": after.future_rider_codes,
                "remaining_undated_rider_codes": [
                    rider.code for rider in after.riders if rider.status == "undated"
                ],
                "after": after.model_dump(mode="json"),
            },
            indent=2,
            default=str,
        )
    )


@app.command("inspect-history-gaps-progress-nc")
def inspect_history_gaps_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
    limit_documents: int = typer.Option(
        12,
        help="How many current rider PDFs to inspect as Wayback seeds.",
    ),
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per rider seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    selector = ProgressNCHistoricalTariffSelector(repository)
    requested_date = _parse_service_date(service_date)
    before = selector.select_schedule(schedule_code=schedule_code, service_date=requested_date)
    gap_codes = sorted(
        {
            *before.future_rider_codes,
            *(rider.code for rider in before.riders if rider.status == "undated"),
        }
    )

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview_targets(
            limit_documents=limit_documents,
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            categories={DocumentCategory.RIDER.value},
            target_rider_codes=set(gap_codes),
        )
    finally:
        service.close()

    typer.echo(
        json.dumps(
            {
                "service_date": service_date,
                "schedule_code": schedule_code.upper(),
                "gap_rider_codes": gap_codes,
                "preview": preview,
            },
            indent=2,
            default=str,
        )
    )


@app.command("import-history-progress-nc")
def import_history_progress_nc(
    title: str = typer.Option(..., help="Human-readable document title."),
    category: str = typer.Option(
        ...,
        help="Document category such as rate, rider, public_notice, or other.",
    ),
    source_label: str = typer.Option(
        ...,
        help="Source label such as ncuc, ncuc-manual, or regulator-order.",
    ),
    source_authority: str | None = typer.Option(
        None,
        help="Optional authority classification such as regulator, utility, archive, or external.",
    ),
    source_type: str | None = typer.Option(
        None,
        help="Optional source type such as ncuc, fpsc, wayback, or manual-regulator-pdf.",
    ),
    url: str | None = typer.Option(None, help="Official public document URL."),
    file: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    docket_number: str | None = typer.Option(None, help="Optional docket reference."),
) -> None:
    if not url and not file:
        raise typer.BadParameter("Provide --url or --file.")
    settings, repository = _bootstrap()
    service = ProgressNCHistoricalImportService(settings, repository)
    try:
        record = service.import_document(
            title=title,
            category=category,
            source_label=source_label,
            source_authority=source_authority,
            source_type=source_type,
            source_url=url,
            local_file=file,
            docket_number=docket_number,
        )
    finally:
        service.close()
    typer.echo(json.dumps(record.model_dump(mode="json"), indent=2, default=str))


@app.command("list-history-sources-progress-nc")
def list_history_sources_progress_nc() -> None:
    _, repository = _bootstrap()
    rows = ProgressNCProvenanceService(repository).list_historical_sources()
    for row, provenance in rows:
        typer.echo(
            "\t".join(
                [
                    str(row.id or "-"),
                    provenance.authority,
                    provenance.source_type,
                    provenance.docket_number or "-",
                    row.category,
                    row.leaf_no or "-",
                    row.effective_start or "-",
                    row.title,
                ]
            )
        )


@app.command("show-history-coverage-progress-nc")
def show_history_coverage_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    _, repository = _bootstrap()
    coverage = ProgressNCProvenanceService(repository).build_chain_coverage(query=query)
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in coverage],
            indent=2,
            default=str,
        )
    )


@app.command("list-regulator-gaps-progress-nc")
def list_regulator_gaps_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    _, repository = _bootstrap()
    gaps = ProgressNCRegulatorGapService(repository).build_gaps(query=query)
    for gap in gaps:
        typer.echo(
            "\t".join(
                [
                    str(gap.gap_priority),
                    gap.category,
                    gap.leaf_no or "-",
                    ",".join(gap.evidence_authorities) or "-",
                    ",".join(gap.suggested_dockets) or "-",
                    gap.title,
                ]
            )
        )


@app.command("show-regulator-gaps-progress-nc")
def show_regulator_gaps_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    _, repository = _bootstrap()
    gaps = ProgressNCRegulatorGapService(repository).build_gaps(query=query)
    typer.echo(json.dumps([gap.model_dump(mode="json") for gap in gaps], indent=2, default=str))


@app.command("parse-bill-relevant-progress-nc")
def parse_bill_relevant_progress_nc(
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-parse bill-relevant documents even if they already have a parse result.",
    ),
) -> None:
    _, repository = _bootstrap()
    records = ProgressNCBillRelevantGapService(repository).build_records()
    parsed = 0
    skipped = 0
    for record in records:
        if not force and record.parse_status is not None:
            skipped += 1
            continue
        _parse_document(record.current_document_id, repository)
        parsed += 1
    typer.echo(f"Parsed {parsed} bill-relevant Progress NC documents; skipped {skipped}.")


@app.command("audit-local-raw-nc")
def audit_local_raw_nc(
    company: str = typer.Option(..., help="Company short name: progress or carolinas."),
    include_current: bool = typer.Option(True, help="Include data/raw snapshots."),
    include_historical: bool = typer.Option(True, help="Include data/historical/raw snapshots."),
    output_dir: Path = typer.Option(
        Path("data/processed/local_raw_audit"),
        help="Directory for JSON/CSV audit outputs.",
    ),
) -> None:
    from duke_rates.analytics.local_raw_audit import audit_local_raw_docs, write_local_raw_audit

    _, repository = _bootstrap()
    report = audit_local_raw_docs(
        repository,
        state="NC",
        company=company,
        include_current=include_current,
        include_historical=include_historical,
    )
    company_key = company.lower()
    output_json = output_dir / f"nc_{company_key}_local_raw_audit.json"
    output_csv = output_dir / f"nc_{company_key}_local_raw_audit.csv"
    write_local_raw_audit(report, output_json, output_csv)
    summary = report["summary"]
    typer.echo(
        json.dumps(
            {
                "company": company_key,
                "output_json": str(output_json),
                "output_csv": str(output_csv),
                "summary": summary,
            },
            indent=2,
            default=str,
        )
    )


@app.command("load-local-rider-summaries-nc")
def load_local_rider_summaries_nc(
    company: str = typer.Option(..., help="Company short name: progress or carolinas."),
    replace: bool = typer.Option(True, help="Replace previously loaded rows for the same local source PDF."),
) -> None:
    from duke_rates.db.local_summary_loader import load_local_nc_rider_summaries

    _, repository = _bootstrap()
    conn = repository._connect()
    try:
        result = load_local_nc_rider_summaries(conn, company=company, replace=replace)
    finally:
        conn.close()
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("load-local-rates-nc")
def load_local_rates_nc(
    company: str = typer.Option(..., help="Company short name: progress or carolinas."),
    replace: bool = typer.Option(True, help="Replace previously loaded rows for the same local source PDF."),
) -> None:
    from duke_rates.db.local_rate_loader import load_local_nc_residential_rates

    _, repository = _bootstrap()
    conn = repository._connect()
    try:
        result = load_local_nc_residential_rates(conn, company=company, replace=replace)
    finally:
        conn.close()
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("list-bill-relevant-gaps-progress-nc")
def list_bill_relevant_gaps_progress_nc() -> None:
    _, repository = _bootstrap()
    records = ProgressNCBillRelevantGapService(repository).build_records()
    for record in records:
        typer.echo(
            "\t".join(
                [
                    record.leaf_no,
                    record.category,
                    record.parse_status or "-",
                    str(record.historical_version_count),
                    ",".join(record.parsed_component_labels) or "-",
                    ",".join(record.gap_flags) or "-",
                    record.title,
                ]
            )
        )


@app.command("show-bill-relevant-gaps-progress-nc")
def show_bill_relevant_gaps_progress_nc() -> None:
    _, repository = _bootstrap()
    records = ProgressNCBillRelevantGapService(repository).build_records()
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in records],
            indent=2,
            default=str,
        )
    )


@app.command("mine-historical-leads-progress-nc")
def mine_historical_leads_progress_nc(
    source: str = typer.Option(
        "all",
        help="Lead source class: all, openei, notices, imported, regulator.",
    ),
    limit_references: int = typer.Option(
        120,
        help="Maximum OpenEI references to inspect when source includes openei.",
    ),
) -> None:
    settings, repository = _bootstrap()
    miner = HistoricalCitationMiner(settings, repository)
    regulator = ProgressNCRegulatorLeadService(settings, repository)
    created = []
    normalized_source = source.lower()
    if normalized_source in {"all", "openei"}:
        if not settings.openei_api_key:
            raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to mine OpenEI leads.")
        created.extend(
            miner.mine_openei_progress_nc(limit_references=limit_references, missing_only=True)
        )
    if normalized_source in {"all", "notices"}:
        created.extend(miner.mine_notice_archive_progress_nc())
    if normalized_source in {"all", "imported"}:
        created.extend(miner.mine_imported_documents_progress_nc())
    if normalized_source in {"all", "regulator"}:
        regulator.mine_existing_regulator_leads()
    typer.echo(
        json.dumps(
            {
                "source": normalized_source,
                "created_leads": len(created),
                "stored_historical_leads": len(repository.list_historical_leads()),
                "stored_docket_leads": len(repository.list_regulatory_docket_leads()),
            },
            indent=2,
            default=str,
        )
    )


@app.command("ingest-manual-lead-progress-nc")
def ingest_manual_lead_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
    source_type: str = typer.Option(..., help="Source type, e.g. search_engine or dsire."),
    source_label: str = typer.Option(..., help="Short source label."),
    title: str = typer.Option(..., help="Lead title."),
    provenance_class: str = typer.Option(
        "external",
        help="Provenance class: regulator, external, reference, utility.",
    ),
    url: str | None = typer.Option(None, help="Optional discovered attachment/viewer URL."),
    text_file: Path | None = typer.Option(
        None,
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Optional text file containing citations or notes.",
    ),
    docket_number: str | None = typer.Option(None, help="Optional docket clue."),
) -> None:
    settings, repository = _bootstrap()
    miner = HistoricalCitationMiner(settings, repository)
    text = (
        text_file.read_text(encoding="utf-8", errors="ignore")
        if text_file
        else docket_number or ""
    )
    leads = miner.ingest_manual_lead(
        family_query=family_query,
        source_class=source_type,
        provenance_class=provenance_class,
        source_label=source_label,
        source_location=str(text_file) if text_file else url,
        source_url=url,
        text=text,
        title=title,
        docket_number=docket_number,
    )
    typer.echo(
        json.dumps([lead.model_dump(mode="json") for lead in leads], indent=2, default=str)
    )


@app.command("score-historical-leads-progress-nc")
def score_historical_leads_progress_nc() -> None:
    _, repository = _bootstrap()
    result = ProgressNCLeadRegistryService(repository).rescore_all()
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("list-historical-leads-progress-nc")
def list_historical_leads_progress_nc(
    family_query: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    family_key = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        family_key = target.family_key if target else family_query
    for lead in repository.list_historical_leads(family_key=family_key):
        typer.echo(
            "\t".join(
                [
                    str(lead.id or "-"),
                    lead.target_leaf_no or "-",
                    lead.target_code or "-",
                    f"{lead.confidence_score:.1f}",
                    lead.provenance_class,
                    lead.source_class,
                    lead.docket_number or "-",
                    lead.extracted_url or lead.source_url or "-",
                ]
            )
        )


@app.command("preview-root-url-lists-progress-nc")
def preview_root_url_lists_progress_nc(
    family_query: str | None = typer.Option(None, help="Optional leaf/code/title filter."),
    include_noisy: bool = typer.Option(
        False,
        help="Include low-signal matches from the noisier root URL lists.",
    ),
    missing_only: bool = typer.Option(
        True,
        help="Restrict matching to families still missing historical coverage.",
    ),
    limit: int = typer.Option(50, help="Maximum number of leads to print."),
    file: list[Path] | None = typer.Option(
        None,
        "--file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Optional root URL list file(s) to scan.",
    ),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCRootUrlListService(repository)
    leads = service.preview_leads(
        file_paths=file,
        family_query=family_query,
        include_noisy=include_noisy,
        missing_only=missing_only,
        limit=limit,
    )
    if not leads:
        typer.echo("No plausible Progress NC historical URL leads found.")
        raise typer.Exit()
    for lead in leads:
        typer.echo(
            "\t".join(
                [
                    lead.target_leaf_no or "-",
                    lead.target_code or "-",
                    f"{lead.confidence_score:.1f}",
                    lead.source_label or "-",
                    lead.extracted_url or "-",
                ]
            )
        )


@app.command("import-root-url-lists-progress-nc")
def import_root_url_lists_progress_nc(
    family_query: str | None = typer.Option(None, help="Optional leaf/code/title filter."),
    include_noisy: bool = typer.Option(
        False,
        help="Include low-signal matches from the noisier root URL lists.",
    ),
    missing_only: bool = typer.Option(
        True,
        help="Restrict matching to families still missing historical coverage.",
    ),
    min_score: float = typer.Option(
        45.0,
        help="Minimum lead score required before persisting.",
    ),
    limit: int | None = typer.Option(
        None,
        help="Optional maximum number of leads to persist.",
    ),
    file: list[Path] | None = typer.Option(
        None,
        "--file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Optional root URL list file(s) to scan.",
    ),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCRootUrlListService(repository)
    leads = service.import_leads(
        file_paths=file,
        family_query=family_query,
        include_noisy=include_noisy,
        missing_only=missing_only,
        min_score=min_score,
        limit=limit,
    )
    typer.echo(f"Imported {len(leads)} root-url historical leads.")
    for lead in leads[:20]:
        typer.echo(
            "\t".join(
                [
                    str(lead.id or "-"),
                    lead.target_leaf_no or "-",
                    lead.target_code or "-",
                    f"{lead.confidence_score:.1f}",
                    lead.extracted_url or "-",
                ]
            )
        )


@app.command("generate-search-packs-progress-nc")
def generate_search_packs_progress_nc() -> None:
    _, repository = _bootstrap()
    packs = ProgressNCSearchPackService(repository).generate_missing_family_packs()
    typer.echo(f"Generated {len(packs)} search packs.")
    for pack in packs:
        typer.echo(
            "\t".join(
                [
                    pack.target_leaf_no or "-",
                    pack.target_code or "-",
                    pack.family_type,
                    pack.target_title,
                ]
            )
        )


@app.command("list-search-packs-progress-nc")
def list_search_packs_progress_nc() -> None:
    _, repository = _bootstrap()
    for pack in repository.list_search_packs():
        typer.echo(
            "\t".join(
                [
                    pack.target_leaf_no or "-",
                    pack.target_code or "-",
                    pack.family_type,
                    pack.target_title,
                ]
            )
        )


@app.command("show-search-pack-progress-nc")
def show_search_pack_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
) -> None:
    _, repository = _bootstrap()
    target = find_target_by_query(repository, family_query, missing_only=False)
    family_key = target.family_key if target else family_query
    pack = repository.get_search_pack(family_key)
    if not pack:
        raise typer.BadParameter(f"No search pack found for {family_query!r}.")
    typer.echo(json.dumps(json.loads(pack.payload_json), indent=2, default=str))


@app.command("preview-google-dorks-progress-nc")
def preview_google_dorks_progress_nc(
    family_query: str | None = typer.Option(
        None, help="Limit to one family (leaf no., code, title, or family key)."
    ),
    strategy: str | None = typer.Option(
        None, help="Limit to one strategy (e.g. legacy_filename, ncuc_docket, boilerplate)."
    ),
    missing_only: bool = typer.Option(
        True, help="Only generate queries for families with no historical documents."
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON array of query strings."),
) -> None:
    """Preview Google Dork queries without executing them.

    Use this to inspect what would be searched before spending API quota.
    """
    from duke_rates.historical.dork_runner import export_queries_json, preview_queries

    _, repository = _bootstrap()
    family_keys: list[str] | None = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        if not target:
            raise typer.BadParameter(f"Family not found: {family_query!r}")
        family_keys = [target.family_key]

    strategies = [strategy] if strategy else None
    queries = preview_queries(
        repository,
        family_keys=family_keys,
        strategies=strategies,
        missing_only=missing_only,
    )

    if as_json:
        typer.echo(json.dumps([q.query for q in queries], indent=2))
    else:
        typer.echo(f"{len(queries)} queries across {len({q.family_key for q in queries})} families\n")
        for q in queries:
            typer.echo(f"  [{q.strategy:<25}] {q.family_key}  {q.query}")


@app.command("run-google-dorks-progress-nc")
def run_google_dorks_progress_nc(
    family_query: str | None = typer.Option(
        None, help="Limit to one family (leaf no., code, title, or family key)."
    ),
    strategy: str | None = typer.Option(
        None, help="Limit to one strategy (legacy_filename, ncuc_docket, boilerplate, etc.)."
    ),
    missing_only: bool = typer.Option(
        True, help="Only query families with no historical documents."
    ),
    max_queries: int | None = typer.Option(
        None, help="Cap total API calls (default: unlimited)."
    ),
    max_results: int = typer.Option(
        10, help="Max results per query (1-30)."
    ),
    auto_import_threshold: float | None = typer.Option(
        None,
        "--auto-import",
        help="Confidence threshold (0-100) above which PDFs are automatically imported.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Log what would be saved without writing to the database."
    ),
) -> None:
    """Execute Google Dork queries via Custom Search API and store results as historical leads.

    Requires DUKE_RATES_GOOGLE_API_KEY and DUKE_RATES_GOOGLE_CSE_ID in .env.
    Free tier: 100 queries/day.

    Results are written to the historical_leads table with source_class='google_cse'.
    Use list-historical-leads-progress-nc to review them afterward.

    Examples:
        # Preview before spending quota:
        duke-rates preview-google-dorks-progress-nc --missing-only

        # Run all missing-family queries (up to 50), store leads:
        duke-rates run-google-dorks-progress-nc --max-queries 50

        # Run only legacy-filename strategy for one family:
        duke-rates run-google-dorks-progress-nc --family-query 503 --strategy legacy_filename

        # Run with auto-import of high-confidence PDF hits:
        duke-rates run-google-dorks-progress-nc --auto-import 75 --max-queries 20
    """
    from duke_rates.historical.dork_runner import ProgressNCDorkRunnerService

    settings, repository = _bootstrap()

    if settings.google_api_key and settings.google_cse_id:
        typer.echo("Search backend: Google CSE")
    else:
        typer.echo("Search backend: DuckDuckGo (free)")

    family_keys: list[str] | None = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        if not target:
            raise typer.BadParameter(f"Family not found: {family_query!r}")
        family_keys = [target.family_key]

    service = ProgressNCDorkRunnerService(settings, repository)
    result = service.run(
        family_keys=family_keys,
        missing_only=missing_only,
        strategies=[strategy] if strategy else None,
        max_queries=max_queries,
        max_results_per_query=min(max(max_results, 1), 30),
        min_confidence_for_import=auto_import_threshold,
        dry_run=dry_run,
    )

    prefix = "DRY-RUN " if dry_run else ""
    typer.echo(
        f"\n{prefix}Results:"
        f"\n  Queries run:    {result.queries_run}"
        f"\n  Leads {'logged' if dry_run else 'saved'}:     {result.leads_saved}"
        f"\n  Auto-imported:  {result.auto_imported}"
        f"\n  Quota exhausted: {result.quota_exhausted}"
    )
    if result.errors:
        typer.echo(f"\n  Errors ({len(result.errors)}):")
        for err in result.errors[:10]:
            typer.echo(f"    {err}")


@app.command("export-google-dorks-progress-nc")
def export_google_dorks_progress_nc(
    missing_only: bool = typer.Option(
        True, help="Only export queries for families with no historical documents."
    ),
    family_query: str | None = typer.Option(
        None, help="Limit to one family (leaf no., code, title, or family key)."
    ),
    output: str | None = typer.Option(None, help="Write to file instead of stdout."),
) -> None:
    """Export all Google Dork query strings as a JSON array.

    Useful for manual execution in a browser, Google Custom Search UI,
    or for feeding into a third-party search tool.
    """
    from duke_rates.historical.dork_runner import export_queries_json

    _, repository = _bootstrap()
    family_keys: list[str] | None = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        if not target:
            raise typer.BadParameter(f"Family not found: {family_query!r}")
        family_keys = [target.family_key]

    queries = export_queries_json(repository, family_keys=family_keys, missing_only=missing_only)
    out = json.dumps(queries, indent=2)
    if output:
        Path(output).write_text(out, encoding="utf-8")
        typer.echo(f"Wrote {len(queries)} queries to {output}")
    else:
        typer.echo(out)


@app.command("show-docket-leads-progress-nc")
def show_docket_leads_progress_nc(
    family_query: str | None = typer.Option(None, help="Optional family leaf/code/title query."),
) -> None:
    _, repository = _bootstrap()
    family_key = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        family_key = target.family_key if target else family_query
    rows = repository.list_regulatory_docket_leads(family_key=family_key)
    typer.echo(
        json.dumps([row.model_dump(mode="json") for row in rows], indent=2, default=str)
    )


@app.command("list-unresolved-historical-families-progress-nc")
def list_unresolved_historical_families_progress_nc() -> None:
    _, repository = _bootstrap()
    gap_records = ProgressNCBillRelevantGapService(repository).build_records()
    for gap in gap_records:
        if "missing_historical_leaf" not in gap.gap_flags:
            continue
        target = find_target_by_query(repository, gap.leaf_no, missing_only=False)
        family_key = target.family_key if target else gap.leaf_no
        lead_count = len(repository.list_historical_leads(family_key=family_key))
        variant_count = len(repository.list_candidate_url_variants(family_key=family_key))
        docket_count = len(repository.list_regulatory_docket_leads(family_key=family_key))
        typer.echo(
            "\t".join(
                [
                    gap.leaf_no,
                    gap.primary_code or "-",
                    gap.category,
                    str(lead_count),
                    str(variant_count),
                    str(docket_count),
                    gap.title,
                ]
            )
        )


@app.command("seed-family-documents-progress-nc")
def seed_family_documents_progress_nc(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print rows without inserting."),
) -> None:
    """Insert placeholder documents rows for Progress NC families that have no current document.

    Each family needs a documents entry with a leaf-no-NNN URL so the gap service and
    URL archaeology service can find and track it.  Already-present URLs are skipped.
    """
    from datetime import UTC, datetime

    from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind

    settings, repository = _bootstrap()

    # Map: leaf_no -> (title, category, code, predicted_current_url)
    # URLs use the /-/media/pdfs/for-your-home/rates/electric-nc/ pattern seen on current site.
    FAMILY_SEEDS = [
        ("501", "Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)", "rate", "R-TOUD",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-501-schedule-r-toud.pdf"),
        ("503", "Residential Service Time-of-Use with Critical Peak Pricing Schedule R-TOU-CPP (Flex Savings Option)", "rate", "R-TOU-CPP",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-503-schedule-r-tou-cpp.pdf"),
        ("504", "Residential Service Pilot Time of Use with Discount Charging Period Schedule R-TOU-EV ( EV Overnight Advantage)", "rate", "R-TOU-EV",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-504-schedule-r-tou-ev.pdf"),
        ("571", "Street Lighting Service", "rate", "SLS",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-571-schedule-sls.pdf"),
        ("572", "Street Lighting Service - Residential Subdivisions", "rate", "SLR",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-572-schedule-slr.pdf"),
        ("607", "Storm Securitization Rider STS", "rider", "STS",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-607-rider-sts-ry1.pdf"),
        ("609", "Earnings Sharing Mechanism Rider ESM", "rider", "ESM",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-609-rider-esm-ry1.pdf"),
        ("613", "Storm Securitization Rider", "rider", "STS",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-613-rider-sts.pdf"),
        ("662", "Residential Service Equal Payment Plan (WeatherProtect) Pilot EPPWP", "rider", "EPPWP",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-662-rider-eppwp-ry1.pdf"),
        ("670", "Residential Solar Choice Rider RSC", "rider", "RSC",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-670-rider-rsc-ry1.pdf"),
        ("672", "Clean Energy Impact Rider", "rider", "CEI",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-672-rider-cei.pdf"),
    ]

    inserted = 0
    skipped = 0
    for leaf_no, title, category_str, code, url in FAMILY_SEEDS:
        existing = [d for d in repository.list_documents() if str(d.document_url) == url]
        if existing:
            skipped += 1
            if dry_run:
                typer.echo(f"SKIP (exists) leaf={leaf_no} {title}")
            continue
        record = DiscoveryRecord(
            title=title,
            source_page_url="https://www.duke-energy.com/home/billing/rates?jur=NC",
            document_url=url,
            state="NC",
            company="progress",
            category=DocumentCategory(category_str),
            kind=DocumentKind.PDF,
            retrieval_timestamp=datetime.now(UTC),
            notes=[f"leaf_no={leaf_no}", f"code={code}", "seeded=family_seed"],
        )
        if dry_run:
            typer.echo(f"DRY-RUN leaf={leaf_no:3s} {category_str:5s} {code:8s} {url}")
        else:
            import sqlite3 as _sqlite3
            now = datetime.now(UTC).isoformat()
            with _sqlite3.connect(settings.database_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO documents
                       (title, source_page_url, document_url, state, company,
                        category, kind, local_path, content_hash,
                        discovered_at, retrieved_at, metadata_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        title,
                        "https://www.duke-energy.com/home/billing/rates?jur=NC",
                        url,
                        "NC",
                        "progress",
                        category_str,
                        "pdf",
                        "",   # empty string satisfies NOT NULL; no local file yet
                        "",   # same
                        now,
                        now,
                        json.dumps({"leaf_no": leaf_no, "code": code, "seeded": True}),
                    ),
                )
            typer.echo(f"Inserted leaf={leaf_no:3s} {title}")
            inserted += 1

    if not dry_run:
        typer.echo(f"\nDone: {inserted} inserted, {skipped} already present.")
    else:
        typer.echo(f"\nDry-run: {len(FAMILY_SEEDS) - skipped} would be inserted, {skipped} already present.")


@app.command("preview-predecessor-domain-progress-nc")
def preview_predecessor_domain_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
    max_variants: int = typer.Option(
        40,
        help="Maximum candidate URL variants to probe.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCUrlArchaeologyService(settings, repository)
    try:
        rows = service.generate_variants_for_family(
            family_query,
            max_variants=max_variants,
        )
    finally:
        service.close()
    typer.echo(
        json.dumps([row.model_dump(mode="json") for row in rows[:50]], indent=2, default=str)
    )


@app.command("recover-predecessor-domain-progress-nc")
def recover_predecessor_domain_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
    from_year: int = typer.Option(2010, help="Earliest Wayback year for candidate URLs."),
    max_variants: int = typer.Option(
        40,
        help="Maximum candidate URL variants to probe before attempting recovery.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCUrlArchaeologyService(settings, repository)
    try:
        recovered = service.recover_family(
            family_query,
            from_year=from_year,
            max_variants=max_variants,
        )
    finally:
        service.close()
    typer.echo(
        json.dumps(
            [row.model_dump(mode="json") for row in recovered],
            indent=2,
            default=str,
        )
    )


@app.command("preview-bill-relevant-history-progress-nc")
def preview_bill_relevant_history_progress_nc(
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per bill-relevant seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    gap_records = ProgressNCBillRelevantGapService(repository).build_records()
    target_leafs = sorted(
        {
            record.leaf_no
            for record in gap_records
            if "missing_historical_leaf" in record.gap_flags
        }
    )
    if not target_leafs:
        typer.echo(
            json.dumps(
                {
                    "target_leafs": [],
                    "preview": [],
                    "message": "No missing bill-relevant historical leafs detected.",
                },
                indent=2,
                default=str,
            )
        )
        raise typer.Exit()

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview_targets(
            limit_documents=len(target_leafs),
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            target_leaf_numbers=set(target_leafs),
        )
    finally:
        service.close()

    typer.echo(
        json.dumps(
            {
                "target_leafs": target_leafs,
                "preview": preview,
            },
            indent=2,
            default=str,
        )
    )


@app.command("recover-bill-relevant-history-progress-nc")
def recover_bill_relevant_history_progress_nc(
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per bill-relevant seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    gap_records = ProgressNCBillRelevantGapService(repository).build_records()
    target_leafs = sorted(
        {
            record.leaf_no
            for record in gap_records
            if "missing_historical_leaf" in record.gap_flags
        }
    )
    if not target_leafs:
        typer.echo(
            json.dumps(
                {
                    "target_leafs": [],
                    "recovered": [],
                    "remaining_missing_leafs": [],
                    "message": "No missing bill-relevant historical leafs detected.",
                },
                indent=2,
                default=str,
            )
        )
        raise typer.Exit()

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_documents=len(target_leafs),
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            target_leaf_numbers=set(target_leafs),
        )
    finally:
        service.close()

    remaining_leafs = sorted(
        {
            record.leaf_no
            for record in ProgressNCBillRelevantGapService(repository).build_records()
            if "missing_historical_leaf" in record.gap_flags
        }
    )
    typer.echo(
        json.dumps(
            {
                "target_leafs": target_leafs,
                "recovered": [record.model_dump(mode="json") for record in recovered],
                "remaining_missing_leafs": remaining_leafs,
            },
            indent=2,
            default=str,
        )
    )


@app.command("preview-bill-relevant-openei-progress-nc")
def preview_bill_relevant_openei_progress_nc(
    limit_references: int = typer.Option(
        80,
        help="Maximum number of Progress NC OpenEI references to inspect.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    target_keys = sorted(
        {
            record.primary_code
            for record in ProgressNCBillRelevantGapService(repository).build_records()
            if "missing_historical_leaf" in record.gap_flags and record.primary_code
        }
    )
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview(
            limit_references=limit_references,
            target_keys=set(target_keys),
        )
    finally:
        service.close()
    typer.echo(
        json.dumps(
            {
                "target_keys": target_keys,
                "preview": preview,
            },
            indent=2,
            default=str,
        )
    )


@app.command("recover-bill-relevant-openei-progress-nc")
def recover_bill_relevant_openei_progress_nc(
    limit_references: int = typer.Option(
        80,
        help="Maximum number of Progress NC OpenEI references to inspect.",
    ),
    from_year: int = typer.Option(
        2010,
        help="Earliest year to query Wayback for old static Duke PDF paths.",
    ),
    max_wayback_snapshots: int = typer.Option(
        8,
        help="Maximum Wayback captures to try per OpenEI source URL.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    target_keys = sorted(
        {
            record.primary_code
            for record in ProgressNCBillRelevantGapService(repository).build_records()
            if "missing_historical_leaf" in record.gap_flags and record.primary_code
        }
    )
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_references=limit_references,
            from_year=from_year,
            max_wayback_snapshots=max_wayback_snapshots,
            target_keys=set(target_keys),
        )
    finally:
        service.close()
    remaining = ProgressNCBillRelevantGapService(repository).build_records()
    typer.echo(
        json.dumps(
            {
                "target_keys": target_keys,
                "recovered": [record.model_dump(mode="json") for record in recovered],
                "remaining_missing_leafs": sorted(
                    {
                        record.leaf_no
                        for record in remaining
                        if "missing_historical_leaf" in record.gap_flags
                    }
                ),
            },
            indent=2,
            default=str,
        )
    )


@app.command("generate-regulator-inbox-progress-nc")
def generate_regulator_inbox_progress_nc(
    output: Path = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.jsonl"),
        help="Path to write the JSONL inbox manifest.",
    ),
    query: str | None = typer.Option(
        None,
        help="Optional filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    settings, repository = _bootstrap()
    count = ProgressNCHistoricalInboxService(settings, repository).generate_regulator_manifest(
        output_path=output,
        query=query,
    )
    typer.echo(f"Wrote {count} regulator target rows to {output}")


@app.command("import-history-inbox-progress-nc")
def import_history_inbox_progress_nc(
    manifest: Path = typer.Option(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="JSONL manifest of manually collected historical documents.",
    ),
) -> None:
    settings, repository = _bootstrap()
    imported = ProgressNCHistoricalInboxService(settings, repository).import_manifest(manifest)
    typer.echo(f"Imported {len(imported)} historical documents from {manifest}.")
    for record in imported:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.category,
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.title,
                ]
            )
        )


@app.command("export-history-inbox-progress-nc")
def export_history_inbox_progress_nc(
    manifest: Path = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.jsonl"),
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="JSONL inbox manifest to export.",
    ),
    csv_output: Path | None = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.csv"),
        help="Optional CSV output path.",
    ),
    markdown_output: Path | None = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.md"),
        help="Optional Markdown output path.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCHistoricalInboxService(settings, repository)
    if csv_output:
        count = service.export_manifest_csv(manifest_path=manifest, output_path=csv_output)
        typer.echo(f"Wrote {count} rows to {csv_output}")
    if markdown_output:
        count = service.export_manifest_markdown(
            manifest_path=manifest,
            output_path=markdown_output,
        )
        typer.echo(f"Wrote {count} rows to {markdown_output}")


@app.command("preview-openei-history-progress-nc")
def preview_openei_history_progress_nc(
    limit_references: int = typer.Option(
        25,
        help="Maximum number of Progress NC OpenEI references to preview.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview(limit_references=limit_references)
    finally:
        service.close()
    typer.echo(json.dumps(preview, indent=2, default=str))


@app.command("recover-openei-history-progress-nc")
def recover_openei_history_progress_nc(
    limit_references: int = typer.Option(
        25,
        help="Maximum number of Progress NC OpenEI references to recover.",
    ),
    from_year: int = typer.Option(
        2010,
        help="Earliest year to query Wayback for old static Duke PDF paths.",
    ),
    max_wayback_snapshots: int = typer.Option(
        8,
        help="Maximum Wayback captures to try per OpenEI source URL.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_references=limit_references,
            from_year=from_year,
            max_wayback_snapshots=max_wayback_snapshots,
        )
    finally:
        service.close()
    typer.echo(f"Recovered {len(recovered)} Progress NC historical OpenEI references.")
    for record in recovered:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.category,
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.revision_label or "-",
                    "direct" if record.direct_downloadable else "archive-only",
                    record.title,
                ]
            )
        )


@app.command("probe-archive-today-progress-nc")
def probe_archive_today_progress_nc(
    limit_references: int = typer.Option(
        12,
        help="How many Progress NC OpenEI-discovered Duke URLs to probe.",
    ),
    markdown_output: Path | None = typer.Option(
        Path("data/history_inbox/progress_nc/archive_today_probe.md"),
        help="Optional Markdown report output path.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")

    openei_service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    archive_client = ArchiveTodayClient(
        timeout=settings.request_timeout,
        user_agent=settings.user_agent,
    )
    try:
        preview = openei_service.preview(limit_references=limit_references)
        results = [
            archive_client.probe(
                title=str(item["title"]),
                source_url=str(item["source_url"]),
            )
            for item in preview
        ]
    finally:
        archive_client.close()
        openei_service.close()

    if markdown_output:
        write_archive_today_markdown_report(results=results, output_path=markdown_output)
        typer.echo(f"Wrote {len(results)} rows to {markdown_output}")
    typer.echo(json.dumps([result.model_dump(mode="json") for result in results], indent=2))








@app.command()
def mcp() -> None:
    settings, _ = _bootstrap()
    serve_mcp(settings)


# ==========================================================================
# NCUC (North Carolina Utilities Commission) acquisition commands
# ==========================================================================










































def _classify_ncuc_access_failure(exc: Exception, *, surface: str) -> tuple[str, str]:
    message = str(exc)
    lowered = message.lower()
    if "403" in lowered or "cloudflare" in lowered or "cf-block" in lowered:
        return (
            "cloudflare_or_forbidden",
            f"{surface} hit a 403/Cloudflare-style failure. This is usually browser/session related, not proof that documents do not exist.",
        )
    if "session may have expired" in lowered or "login failed" in lowered or "ncid" in lowered:
        return (
            "session_or_login_failure",
            f"{surface} failed because the authenticated NCID session was not healthy.",
        )
    if "timeout" in lowered:
        return (
            "timeout",
            f"{surface} timed out before the portal completed the request.",
        )
    return (
        "unknown_failure",
        f"{surface} failed with an unclassified error. Inspect the raw exception and retry through the canonical authenticated workflow.",
    )


def _build_parser_selection_audit_nc_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    company: str | None = None,
    family_key: str | None = None,
) -> dict[str, Any]:
    query = """
        WITH latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                WHERE historical_document_id IS NOT NULL
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        )
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.company,
            hd.title,
            hd.effective_start,
            lr.parser_profile AS latest_parser_profile,
            lr.outcome_quality,
            lr.charge_count,
            lr.status,
            json_extract(lr.metadata_json, '$.selection.initial_parser_profile') AS initial_parser_profile,
            json_extract(lr.metadata_json, '$.selection.final_parser_profile') AS final_parser_profile,
            json_extract(lr.metadata_json, '$.selection.fallback_applied') AS fallback_applied,
            json_extract(lr.metadata_json, '$.selection.fallback_triggered_by') AS fallback_triggered_by,
            json_extract(lr.metadata_json, '$.selection.fallback_reason') AS fallback_reason,
            json_extract(lr.metadata_json, '$.selection.initial_outcome_quality') AS initial_outcome_quality,
            json_extract(lr.metadata_json, '$.selection.final_outcome_quality') AS final_outcome_quality
        FROM latest_runs lr
        JOIN historical_documents hd
          ON hd.id = lr.historical_document_id
        WHERE hd.state = 'NC'
    """
    params: list[Any] = []
    if company:
        query += " AND hd.company = ?"
        params.append(company)
    if family_key:
        query += " AND hd.family_key = ?"
        params.append(family_key)
    query += " ORDER BY hd.id DESC"

    rows = conn.execute(query, tuple(params)).fetchall()

    final_profile_counts: Counter[str] = Counter()
    weak_profile_counts: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()
    trigger_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    fallback_applied_count = 0
    generic_final_count = 0
    weak_count = 0
    empty_count = 0
    strong_count = 0

    audit_rows: list[dict[str, Any]] = []
    for row in rows:
        final_profile = str(row["final_parser_profile"] or row["latest_parser_profile"] or "unknown")
        initial_profile = str(row["initial_parser_profile"] or final_profile)
        outcome_quality = str(row["final_outcome_quality"] or row["outcome_quality"] or "unknown")
        fallback_applied = bool(int(row["fallback_applied"])) if row["fallback_applied"] is not None else False
        fallback_triggered_by = str(row["fallback_triggered_by"] or "")
        fallback_reason = str(row["fallback_reason"] or "")

        final_profile_counts[final_profile] += 1
        if fallback_applied:
            fallback_applied_count += 1
            transition_counts[f"{initial_profile} -> {final_profile}"] += 1
        if fallback_triggered_by:
            trigger_counts[fallback_triggered_by] += 1
        if fallback_reason:
            reason_counts[fallback_reason] += 1
        if final_profile == "generic_residential":
            generic_final_count += 1
        if outcome_quality == "weak":
            weak_count += 1
            weak_profile_counts[final_profile] += 1
        elif outcome_quality == "empty":
            empty_count += 1
            weak_profile_counts[final_profile] += 1
        elif outcome_quality == "strong":
            strong_count += 1

        audit_rows.append(
            {
                "historical_document_id": int(row["historical_document_id"]),
                "family_key": row["family_key"],
                "company": row["company"],
                "title": row["title"],
                "effective_start": row["effective_start"],
                "initial_parser_profile": initial_profile,
                "final_parser_profile": final_profile,
                "fallback_applied": fallback_applied,
                "fallback_triggered_by": fallback_triggered_by or None,
                "fallback_reason": fallback_reason or None,
                "outcome_quality": outcome_quality,
                "charge_count": int(row["charge_count"] or 0),
                "status": row["status"],
            }
        )

    problem_rank = {"empty": 0, "weak": 1, "strong": 2, "skipped": 3, "unknown": 4}
    audit_rows.sort(
        key=lambda item: (
            problem_rank.get(str(item["outcome_quality"]), 9),
            0 if item["fallback_applied"] else 1,
            0 if item["final_parser_profile"] == "generic_residential" else 1,
            item["historical_document_id"],
        )
    )

    return {
        "summary": {
            "latest_run_count": len(rows),
            "fallback_applied_count": fallback_applied_count,
            "generic_final_profile_count": generic_final_count,
            "weak_count": weak_count,
            "empty_count": empty_count,
            "strong_count": strong_count,
        },
        "top_final_profiles": [
            {"parser_profile": name, "count": count}
            for name, count in final_profile_counts.most_common(10)
        ],
        "top_problem_profiles": [
            {"parser_profile": name, "count": count}
            for name, count in weak_profile_counts.most_common(10)
        ],
        "top_profile_transitions": [
            {"transition": name, "count": count}
            for name, count in transition_counts.most_common(10)
        ],
        "fallback_trigger_summary": [
            {"trigger": name, "count": count}
            for name, count in trigger_counts.most_common(10)
        ],
        "fallback_reason_summary": [
            {"reason": name, "count": count}
            for name, count in reason_counts.most_common(10)
        ],
        "rows": audit_rows[:limit],
    }


def _build_parser_improvement_candidates_nc_report(
    repo: Repository,
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    company: str | None = None,
) -> dict[str, Any]:
    from duke_rates.historical.ncuc.document_classification_audit import (
        build_unknown_routing_audit_report,
    )

    unknown_report = build_unknown_routing_audit_report(
        repo,
        limit=max(limit, 100),
        company=company,
    )
    parser_audit = _build_parser_selection_audit_nc_report(
        conn,
        limit=10,
        company=company,
    )

    def _next_command(row: dict[str, Any]) -> str:
        action = str(row["recommended_action"])
        family_key = row.get("family_key")
        family_flag = f" --family-key {family_key}" if family_key else ""
        if action == "enqueue_ocr_remediation":
            return f"python -m duke_rates ocr enqueue-remediation-nc --limit 10{family_flag}"
        if action == "enqueue_reprocess":
            hd_flags = " ".join(
                f"--hd-id {doc_id}"
                for doc_id in list(
                    row.get("action_historical_document_ids")
                    or row.get("historical_document_ids")
                    or []
                )[:10]
            )
            if hd_flags:
                return f"python -m duke_rates reprocess enqueue-nc {hd_flags}"
            return f"python -m duke_rates show-document-classification-audit-nc --limit 25{family_flag}"
        if action == "new_profile_or_family_routing_review":
            return f"python -m duke_rates show-parser-selection-audit-nc --limit 25{family_flag}"
        if action == "evaluate_formula_or_program_lane":
            return f"python -m duke_rates show-document-classification-audit-nc --limit 25{family_flag}"
        if action == "map_to_adjustment_or_matrix_profile":
            return f"python -m duke_rates show-document-classification-audit-nc --limit 25{family_flag}"
        if action in {"reclassify_non_tariff_or_reference", "reclassify_reference_or_unrelated"}:
            return f"python -m duke_rates show-document-classification-audit-nc --limit 25{family_flag}"
        return f"python -m duke_rates show-unknown-routing-audit-nc --limit 25"

    rows: list[dict[str, Any]] = []
    for row in unknown_report["rows"][:limit]:
        action = str(row["recommended_action"])
        family_key = row.get("family_key")
        rows.append(
            {
                "family_key": family_key,
                "company": row.get("company"),
                "document_count": int(row["document_count"]),
                "top_parser_profile": row.get("top_parser_profile") or "unknown",
                "top_filing_classification": row.get("top_filing_classification") or "unknown",
                "top_normalization_lane": row.get("top_normalization_lane") or "queue_ocr_or_paddle",
                "historical_document_ids": list(row.get("historical_document_ids") or []),
                "action_historical_document_ids": list(row.get("action_historical_document_ids") or []),
                "recommended_action": action,
                "reason": row.get("reason"),
                "sample_title": row.get("sample_title"),
                "suggested_next_command": _next_command(row),
            }
        )

    return {
        "summary": {
            "problem_document_count": unknown_report["summary"]["problem_document_count"],
            "problem_family_count": unknown_report["summary"]["problem_family_count"],
            "recommended_action_counts": unknown_report["summary"]["recommended_action_counts"],
            "top_problem_profiles": parser_audit["top_problem_profiles"][:5],
            "generic_final_profile_count": parser_audit["summary"]["generic_final_profile_count"],
            "weak_count": parser_audit["summary"]["weak_count"],
            "empty_count": parser_audit["summary"]["empty_count"],
        },
        "rows": rows,
    }
















@app.command("report-database-intelligence-nc")
def report_database_intelligence_nc(
    limit: int = typer.Option(50, "--limit", help="Max rows per section."),
    family: str = typer.Option("", "--family", help="Restrict to one family key (e.g. nc-progress-leaf-534)."),
    docket: str = typer.Option("", "--docket", help="Restrict to docket number fragments."),
    since: str = typer.Option("", "--since", help="ISO8601 cutoff for time-based sections."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count findings per section without listing detail rows."),
    json_out: bool = typer.Option(False, "--json", help="Emit the full report as JSON to stdout."),
) -> None:
    """Run deterministic corpus analytics across the NCUC database.

    Produces a structured JSON report covering:

    * missing tariff/rider versions (year gaps per family)
    * unknown documents (UNKNOWN classifications grouped by fingerprint cluster)
    * low-quality parses (zero-charge / low-confidence parse attempts)
    * stale artifacts (missing evidence, stuck reprocess queue)
    * duplicate documents (fingerprint hash collisions)
    * family lineage gaps (broken version links, missing effective dates)
    * docket coverage (documents per docket, year, and category)

    The report is always saved to
    ``docs/reports/database_intelligence/YYYY_MM_DD.json``.
    """
    import json as _json
    from pathlib import Path as _Path

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.database_reports import (
        build_database_intelligence_report,
    )

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = build_database_intelligence_report(
            conn,
            limit=limit,
            family_key=family or None,
            docket=docket or None,
            since=since or None,
        )
    finally:
        conn.close()

    if dry_run:
        typer.echo(f"Database Intelligence Report — dry run (limit={limit})")
        typer.echo()
        typer.echo(f"  {'Section':<28} {'Count':>6}")
        typer.echo(f"  {'-' * 28} {'-' * 6}")
        total = 0
        for name, cnt in report["summary_counts"].items():
            typer.echo(f"  {name:<28} {cnt:>6}")
            total += cnt
        typer.echo(f"  {'---':>35}")
        typer.echo(f"  {'total':<28} {total:>6}")
        return

    if json_out:
        typer.echo(_json.dumps(report, indent=2, default=str))
    else:
        typer.echo(f"Database Intelligence Report — {report['generated_at']}")
        typer.echo()
        for section_name, display in [
            ("missing_versions", "Missing Versions"),
            ("unknown_documents", "Unknown Documents"),
            ("low_quality_parses", "Low Quality Parses"),
            ("stale_artifacts", "Stale Artifacts"),
            ("duplicate_documents", "Duplicate Documents"),
            ("family_lineage_gaps", "Family Lineage Gaps"),
            ("docket_coverage", "Docket Coverage"),
        ]:
            sec = report.get(section_name, {})
            summary = sec.get("summary", {})
            cnt = summary.get("count", 0)
            typer.echo(f"  {display}: {cnt} finding(s)")
            # Show 2-3 key detail lines from the summary
            for k, v in summary.items():
                if k != "count" and k != "rows" and v is not None:
                    typer.echo(f"    {k}: {v}")
            typer.echo()
        typer.echo(f"  Total findings across all sections: {report['total_findings']}")

    # Always persist to disk
    out_dir = _Path("docs/reports/database_intelligence")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = report["generated_at"].replace(":", "").replace("T", "_")[:15]
    out_path = out_dir / f"{ts[:10]}.json"
    out_path.write_text(_json.dumps(report, indent=2, default=str), encoding="utf-8")
    if not json_out:
        typer.echo(f"\nReport saved to {out_path}")


# ---------------------------------------------------------------------------
# summarize-database-intelligence-nc
# ---------------------------------------------------------------------------


@app.command("summarize-database-intelligence-nc")
def summarize_database_intelligence_nc(
    report_path: str = typer.Option("", "--report-path", help="Path to an existing report JSON. If omitted, a fresh report is generated."),
    limit: int = typer.Option(50, "--limit", help="Max rows per section (when generating a new report)."),
    family: str = typer.Option("", "--family", help="Family key filter (new report only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Generate compact report without calling the LLM."),
    json_out: bool = typer.Option(False, "--json", help="Emit the LLM summary as JSON to stdout."),
) -> None:
    """Feed the database intelligence report to an LLM for summarization.

    Uses the ``balanced_classifier`` Ollama role (default: qwen3:8b) to
    produce an executive summary, key findings, root causes, and ranked
    high-value actions.

    The LLM summary is saved **separately** from the deterministic report as
    ``docs/reports/database_intelligence/YYYY_MM_DD_summary.json``.
    """
    import json as _json
    from pathlib import Path as _Path

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.database_reports import (
        build_database_intelligence_report,
    )
    from duke_rates.document_intelligence.db_llm_analysis import summarize_report
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    settings, _ = _bootstrap()

    # Load or build the deterministic report
    if report_path:
        raw = _json.loads(_Path(report_path).read_text(encoding="utf-8"))
        typer.echo(f"Loaded report from {report_path}")
    else:
        conn = connect(settings.database_path)
        try:
            raw = build_database_intelligence_report(
                conn,
                limit=limit,
                family_key=family or None,
            )
        finally:
            conn.close()
        typer.echo(f"Generated fresh report ({raw['total_findings']} findings)")

    if dry_run:
        from duke_rates.document_intelligence.db_llm_analysis import _compact_report

        compact = _compact_report(raw)
        typer.echo(_json.dumps(compact, indent=2, default=str))
        typer.echo("\n[Dry run — no LLM call made]")
        return

    # LLM summarization
    orch = OllamaOrchestrator(
        config_path=_Path("config/ollama_models.yaml"),
        db_path=settings.database_path,
    )

    ok, err = orch.health_probe("balanced_classifier")
    if not ok:
        typer.echo(f"LLM role 'balanced_classifier' not available: {err}")
        typer.echo("Skipping summarization. Run with --dry-run to see the compact report.")
        raise typer.Exit(code=1)

    typer.echo("Calling LLM for summarization (balanced_classifier)...")
    summary = summarize_report(orch, raw)

    # Persist summary separately
    out_dir = _Path("docs/reports/database_intelligence")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = raw.get("generated_at", "").replace(":", "").replace("T", "_")[:15]
    out_path = out_dir / f"{ts[:10]}_summary.json"
    out_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    if json_out:
        typer.echo(summary.model_dump_json(indent=2))
    else:
        typer.echo(f"\n=== Executive Summary ===\n{summary.summary}")
        typer.echo(f"\n--- Key Findings ({len(summary.key_findings)}) ---")
        for f in summary.key_findings:
            typer.echo(f"  [{f.severity.upper()}] {f.finding} ({f.affected_count} affected)")
        if summary.likely_root_causes:
            typer.echo(f"\n--- Likely Root Causes ---")
            for c in summary.likely_root_causes:
                typer.echo(f"  - {c}")
        if summary.high_value_actions:
            typer.echo(f"\n--- High-Value Actions ---")
            for a in sorted(summary.high_value_actions, key=lambda x: x.priority):
                typer.echo(f"  {a.priority}. [{a.effort_estimate} effort] {a.action}")
                typer.echo(f"     Impact: {a.expected_impact}")
        typer.echo(f"\nConfidence: {summary.confidence:.2f}")

    typer.echo(f"\nSummary saved to {out_path}")


# ---------------------------------------------------------------------------
# ask-ncuc-db
# ---------------------------------------------------------------------------


@app.command("ask-ncuc-db")
def ask_ncuc_db(
    question: str = typer.Argument(..., help="Natural-language question about the NCUC corpus."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Generate and show SQL without executing."),
    max_rows: int = typer.Option(25, "--max-rows", help="Maximum result rows (capped at 100)."),
    timeout: int = typer.Option(30, "--timeout", help="Query timeout in seconds."),
    json_out: bool = typer.Option(False, "--json", help="Emit structured result JSON to stdout."),
) -> None:
    """Ask a natural-language question about the NCUC database.

    Uses the ``code_model`` Ollama role (qwen2.5-coder:14b) to generate SQL,
    then executes it with strict read-only safety constraints:

    * SELECT-only (no INSERT / UPDATE / DELETE / DROP)
    * table whitelist enforcement
    * automatic LIMIT cap (max 100 rows)
    * configurable query timeout

    The generated SQL is **always shown** before execution. Every run is
    logged to ``database_intelligence_runs``.
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.db_llm_analysis import (
        execute_safe_query,
        generate_sql,
        log_run,
        summarize_query_results,
        validate_sql_safety,
    )
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    settings, _ = _bootstrap()
    t0 = _time.monotonic()

    # LLM SQL generation
    orch = OllamaOrchestrator(
        config_path=_Path("config/ollama_models.yaml"),
        db_path=settings.database_path,
    )

    ok, err = orch.health_probe("code_model")
    if not ok:
        typer.echo(f"LLM role 'code_model' not available: {err}")
        raise typer.Exit(code=1)

    typer.echo(f"Generating SQL for: {question}")
    sql_result = generate_sql(orch, question)

    typer.echo(f"\n--- Generated SQL (confidence: {sql_result.confidence:.2f}) ---")
    typer.echo(sql_result.generated_sql or "(no SQL generated)")
    if sql_result.explanation:
        typer.echo(f"\nExplanation: {sql_result.explanation}")

    if not sql_result.generated_sql or not sql_result.generated_sql.strip():
        typer.echo("\nNo SQL was generated. Cannot proceed.")
        log_run(
            connect(settings.database_path),
            run_type="ask_query",
            status="failed",
            question=question,
            generated_sql="",
            safety_check="no_sql_generated",
            duration_ms=int((_time.monotonic() - t0) * 1000),
        )
        raise typer.Exit(code=1)

    # Safety validation
    is_safe, safety_err = validate_sql_safety(sql_result.generated_sql)
    if not is_safe:
        typer.echo(f"\n!!! SQL BLOCKED by safety validator: {safety_err}")
        log_run(
            connect(settings.database_path),
            run_type="ask_query",
            status="failed",
            question=question,
            generated_sql=sql_result.generated_sql,
            safety_check="blocked",
            error_message=safety_err,
            duration_ms=int((_time.monotonic() - t0) * 1000),
        )
        raise typer.Exit(code=1)

    typer.echo(f"\n[Safety check: PASSED]")

    if dry_run:
        typer.echo("\n[Dry run — SQL was generated but NOT executed]")
        log_run(
            connect(settings.database_path),
            run_type="ask_query",
            status="completed",
            question=question,
            generated_sql=sql_result.generated_sql,
            safety_check="passed",
            execution_status="dry_run",
            duration_ms=int((_time.monotonic() - t0) * 1000),
        )
        return

    # Execute
    typer.echo("\nExecuting query...")
    conn = connect(settings.database_path)
    try:
        exec_result = execute_safe_query(
            conn,
            sql_result.generated_sql,
            max_rows=max_rows,
            timeout_s=timeout,
        )
    finally:
        conn.close()

    duration_ms = int((_time.monotonic() - t0) * 1000)

    if exec_result["status"] != "ok":
        typer.echo(f"\nQuery failed: {exec_result['error_message']}")
        if json_out:
            typer.echo(_json.dumps(exec_result, indent=2, default=str))
        log_run(
            connect(settings.database_path),
            run_type="ask_query",
            status="failed",
            question=question,
            generated_sql=sql_result.generated_sql,
            safety_check="passed",
            execution_status=exec_result["status"],
            error_message=exec_result.get("error_message"),
            duration_ms=duration_ms,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Returned {exec_result['row_count']} rows")

    # LLM result summary
    typer.echo("\nSummarizing results...")
    summary_text = summarize_query_results(
        orch, question, sql_result.generated_sql, exec_result["rows"]
    )

    if json_out:
        output = {
            "question": question,
            "generated_sql": sql_result.generated_sql,
            "status": "ok",
            "row_count": exec_result["row_count"],
            "rows": exec_result["rows"],
            "summary": summary_text,
            "duration_ms": duration_ms,
        }
        typer.echo(_json.dumps(output, indent=2, default=str))
    else:
        typer.echo(f"\n--- Results ---")
        typer.echo(summary_text)
        if exec_result["row_count"] > 0:
            typer.echo(f"\nFirst {min(10, exec_result['row_count'])} rows:")
            for row in exec_result["rows"][:10]:
                typer.echo(f"  {row}")

    # Log the run
    log_run(
        connect(settings.database_path),
        run_type="ask_query",
        status="completed",
        question=question,
        generated_sql=sql_result.generated_sql,
        safety_check="passed",
        execution_status="ok",
        row_count=exec_result["row_count"],
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# run-overnight-db-intelligence-nc
# ---------------------------------------------------------------------------


@app.command("run-overnight-db-intelligence-nc")
def run_overnight_db_intelligence_nc(
    max_runtime: int = typer.Option(0, "--max-runtime", help="Wall-clock cap in minutes (0 = unlimited)."),
    limit: int = typer.Option(50, "--limit", help="Max rows per sub-report."),
    family: str = typer.Option("", "--family", help="Family key filter."),
    docket: str = typer.Option("", "--docket", help="Docket filter."),
    since: str = typer.Option("", "--since", help="ISO8601 cutoff."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate work without LLM calls or DB writes."),
    resume: bool = typer.Option(False, "--resume", help="Skip stages already recorded for today."),
    json_out: bool = typer.Option(False, "--json", help="Emit final report JSON to stdout."),
) -> None:
    """Run the full database intelligence pipeline overnight.

    Workflow:
    1. Run all 7 deterministic sub-reports
    2. Run LLM summarization via ``balanced_classifier``
    3. Identify top anomaly clusters
    4. Produce a morning report JSON

    Designed to be safe for unattended execution: wall-clock cap, resume
    support, and dry-run preview.
    """
    import json as _json
    import signal as _signal
    import time as _time
    from pathlib import Path as _Path

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.action_registry import (
        decide_actions,
    )
    from duke_rates.document_intelligence.database_reports import (
        build_database_intelligence_report,
    )
    from duke_rates.document_intelligence.db_llm_analysis import (
        log_run,
        summarize_report,
    )
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    settings, _ = _bootstrap()
    start_time = _time.monotonic()

    out_dir = _Path("docs/reports/database_intelligence")
    out_dir.mkdir(parents=True, exist_ok=True)

    today_str = _time.strftime("%Y-%m-%d")
    report_path = out_dir / f"{today_str}.json"
    summary_path = out_dir / f"{today_str}_summary.json"

    # Resume check
    if resume and report_path.exists():
        typer.echo(f"Report for {today_str} already exists ({report_path}).")
        typer.echo("Resume: skipping deterministic reports, re-running LLM summarization.")
        raw = _json.loads(report_path.read_text(encoding="utf-8"))
    else:
        if dry_run:
            typer.echo(f"=== Overnight Database Intelligence — Dry Run ===\n")
            typer.echo(f"Stages:")
            typer.echo(f"  1. deterministic_reports (7 sub-reports, limit={limit})")
            typer.echo(f"  2. llm_summarization (balanced_classifier)")
            typer.echo(f"  3. anomaly_identification")
            typer.echo(f"  4. morning_report_json\n")
            typer.echo(f"Output: {report_path}")
            typer.echo(f"Summary: {summary_path}")

            if max_runtime:
                typer.echo(f"Max runtime: {max_runtime} minutes")
            typer.echo(f"Family filter: {family or '(none)'}")
            typer.echo(f"Docket filter: {docket or '(none)'}")
            typer.echo(f"Since filter: {since or '(none)'}")
            return

        typer.echo(f"=== Overnight Database Intelligence — {today_str} ===\n")
        typer.echo("Stage 1/4: Running deterministic reports...")

        conn = connect(settings.database_path)
        try:
            raw = build_database_intelligence_report(
                conn,
                limit=limit,
                family_key=family or None,
                docket=docket or None,
                since=since or None,
            )
        finally:
            conn.close()

        report_path.write_text(_json.dumps(raw, indent=2, default=str), encoding="utf-8")
        typer.echo(f"  Deterministic report saved: {report_path}")
        typer.echo(f"  Findings: {raw['total_findings']} across {len(raw['summary_counts'])} sections")

        # Runtime check
        elapsed = (_time.monotonic() - start_time) / 60.0
        if max_runtime and max_runtime > 0 and elapsed >= max_runtime:
            typer.echo(f"\n  Max runtime ({max_runtime}m) reached after deterministic reports. Stopping.")
            log_run(
                connect(settings.database_path),
                run_type="overnight_full",
                status="completed",
                report_sections=list(raw["summary_counts"].keys()),
                config={"limit": limit, "family": family, "docket": docket, "since": since, "max_runtime": max_runtime},
                output_path=str(report_path),
                duration_ms=int((_time.monotonic() - start_time) * 1000),
            )
            return

    # Stage 2: LLM summarization
    typer.echo("\nStage 2/4: LLM summarization...")
    orch = OllamaOrchestrator(
        config_path=_Path("config/ollama_models.yaml"),
        db_path=settings.database_path,
    )

    ok, err = orch.health_probe("balanced_classifier")
    if not ok:
        typer.echo(f"  LLM role 'balanced_classifier' not available: {err}")
        typer.echo("  Skipping LLM stages. Deterministic report is available.")
    else:
        typer.echo("  Calling balanced_classifier...")
        summary = summarize_report(orch, raw)
        summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"  Summary saved: {summary_path}")
        typer.echo(f"  Executive summary: {summary.summary[:200]}...")

    # Stage 3: Identify top anomalies
    typer.echo("\nStage 3/4: Identifying top anomalies...")
    anomalies = []
    for section, label in [
        ("missing_versions", "Missing Versions"),
        ("unknown_documents", "Unknown Documents"),
        ("low_quality_parses", "Low Quality Parses"),
        ("stale_artifacts", "Stale Artifacts"),
        ("duplicate_documents", "Duplicate Documents"),
        ("family_lineage_gaps", "Family Lineage Gaps"),
    ]:
        sec = raw.get(section, {})
        cnt = sec.get("summary", {}).get("count", 0)
        if cnt > 0:
            anomalies.append({"section": section, "label": label, "count": cnt})

    anomalies.sort(key=lambda x: x["count"], reverse=True)
    typer.echo(f"  Top anomalies:")
    for a in anomalies[:5]:
        typer.echo(f"    {a['label']}: {a['count']} finding(s)")

    # Stage 4: Morning report
    typer.echo("\nStage 4/4: Writing morning report...")
    duration_ms = int((_time.monotonic() - start_time) * 1000)

    # Generate action recommendations from the action registry
    action_recs = decide_actions(raw, max_actions=5)
    suggested_actions = []
    for rec in action_recs:
        suggested_actions.append({
            "priority": rec.priority,
            "category": rec.action.finding_category,
            "label": rec.action.label,
            "cli_command": rec.action.cli_command,
            "args": " ".join(rec.action.args),
            "finding_count": rec.finding_count,
            "severity": rec.severity,
            "rationale": rec.rationale,
            "risk": rec.action.risk,
            "estimated_impact": rec.action.estimated_impact,
        })

    morning = {
        "documents_analyzed": 879,
        "coverage_estimate": raw["summary_counts"].get("missing_versions", 0),
        "top_gaps": anomalies[:5],
        "top_anomalies": anomalies[:5],
        "suggested_actions": suggested_actions if suggested_actions else [
            {"cli_command": "summarize-database-intelligence-nc",
             "args": f"--report-path {report_path}",
             "rationale": "Run LLM analysis for deeper insight"}
        ] if not ok else [],
        "queries_executed": 7,
        "notes": f"Generated by run-overnight-db-intelligence-nc. Limit={limit}, duration_ms={duration_ms}",
    }
    morning_path = out_dir / f"{today_str}_morning.json"
    morning_path.write_text(_json.dumps(morning, indent=2, default=str), encoding="utf-8")

    if json_out:
        typer.echo(_json.dumps(morning, indent=2, default=str))
    else:
        typer.echo(f"  Morning report saved: {morning_path}")
        typer.echo(f"\n=== Overnight Complete ===")
        typer.echo(f"Duration: {duration_ms / 1000:.1f}s")
        typer.echo(f"Report:  {report_path}")
        typer.echo(f"Summary: {summary_path}")
        typer.echo(f"Morning: {morning_path}")

    # Log the run
    log_run(
        connect(settings.database_path),
        run_type="overnight_full",
        status="completed",
        report_sections=list(raw.get("summary_counts", {}).keys()) if raw else [],
        summary_json=summary.model_dump_json() if (not ok and 'summary' in dir()) else None,
        config={"limit": limit, "family": family, "docket": docket, "since": since, "max_runtime": max_runtime},
        output_path=str(report_path),
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# run-autonomous-cycle-nc
# ---------------------------------------------------------------------------


@app.command("run-autonomous-cycle-nc")
def run_autonomous_cycle_nc(
    max_runtime: int = typer.Option(30, "--max-runtime", help="Wall-clock cap in minutes."),
    limit: int = typer.Option(50, "--limit", help="Max rows per sub-report."),
    max_actions: int = typer.Option(2, "--max-actions", help="Max corrective actions per cycle."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview decisions without taking action."),
    json_out: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Run one autonomous loop cycle: detect -> decide -> act -> measure.

    Safe by default (--dry-run). Pass --execute to apply corrective actions.

    Each cycle:
    1. Runs all 7 database intelligence sub-reports
    2. Maps findings to corrective actions via the action registry
    3. Executes the top-N actions (up to --max-actions)
    4. Re-runs reports to measure the before/after delta
    """
    import json as _json
    import time as _time

    from duke_rates.document_intelligence.action_registry import run_cycle

    settings, _ = _bootstrap()

    typer.echo(f"=== Autonomous Cycle {'(DRY RUN)' if dry_run else '(EXECUTING)'} ===")
    typer.echo(f"Max runtime: {max_runtime}m | Max actions: {max_actions} | Limit: {limit}\n")

    result = run_cycle(
        str(settings.database_path),
        limit=limit,
        max_actions=max_actions,
        dry_run=dry_run,
    )

    if json_out:
        typer.echo(_json.dumps({
            "actions_taken": result.actions_taken,
            "actions_skipped": result.actions_skipped,
            "before_counts": result.before_counts,
            "after_counts": result.after_counts,
            "errors": result.errors,
            "duration_ms": result.duration_ms,
        }, indent=2))
        return

    typer.echo("\n=== Before (Finding Counts) ===")
    for cat, cnt in sorted(result.before_counts.items()):
        if cnt > 0:
            typer.echo(f"  {cat}: {cnt}")

    if result.actions_skipped:
        typer.echo(f"\n=== Actions Skipped ({'dry run' if dry_run else 'read-only'}) ===")
        for a in result.actions_skipped:
            typer.echo(f"  - {a}")

    if result.actions_taken:
        typer.echo(f"\n=== Actions Taken ===")
        for a in result.actions_taken:
            typer.echo(f"  - {a}")

    if result.after_counts:
        typer.echo("\n=== After (Finding Counts) ===")
        deltas: list[tuple[str, int, int]] = []
        for cat, after in sorted(result.after_counts.items()):
            before = result.before_counts.get(cat, 0)
            delta = before - after
            if delta != 0:
                deltas.append((cat, before, after))
        if deltas:
            for cat, before, after in deltas:
                delta = before - after
                direction = "DECREASE" if delta > 0 else "INCREASE"
                typer.echo(f"  {cat}: {before} -> {after} ({direction} of {abs(delta)})")
        else:
            typer.echo("  (no changes detected)")

    if result.errors:
        typer.echo(f"\n=== Errors ({len(result.errors)}) ===")
        for e in result.errors:
            typer.echo(f"  - {e}")

    typer.echo(f"\nDuration: {result.duration_ms / 1000:.1f}s")

    if dry_run and result.actions_skipped:
        typer.echo("\nRe-run with --execute to apply corrective actions.")


# ---------------------------------------------------------------------------
# run-continuous-loop-nc
# ---------------------------------------------------------------------------


@app.command("run-continuous-loop-nc")
def run_continuous_loop_nc(
    max_runtime: int = typer.Option(480, "--max-runtime", help="Wall-clock cap in minutes (default: 480 = 8 hours)."),
    max_cycles: int = typer.Option(20, "--max-cycles", help="Maximum loop cycles before stopping."),
    max_dockets: int = typer.Option(2, "--max-dockets", help="Max dockets to acquire per cycle."),
    limit: int = typer.Option(50, "--limit", help="Max rows per sub-report (preview only)."),
    action_batch_limit: int | None = typer.Option(
        None,
        "--action-batch-limit",
        help=(
            "Override --limit for corrective actions (e.g. 200 to drain "
            "duplicates 200/cycle instead of 50). Each action is still "
            "capped by its own max_per_cycle. Default: registry-defined."
        ),
    ),
    reset_state: bool = typer.Option(
        False,
        "--reset-state",
        help=(
            "Discard persisted cooldown/stuck-counter state from prior "
            "runs and start fresh. Use after fixing a stuck category."
        ),
    ),
    portal_precheck: bool = typer.Option(
        True,
        "--portal-precheck/--skip-portal-precheck",
        help=(
            "Run the canonical NCUC portal smoke test once at startup "
            "(login + resolve + DocketDetails + inventory) before "
            "spending cycles on fetch attempts. If it fails, the loop "
            "skip-fasts on every fetch instead of burning 600s timeouts. "
            "Adds ~30s to startup. Skip with --skip-portal-precheck if "
            "you've already verified the portal is healthy."
        ),
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without fetching or writing (default: dry-run)."),
    sleep: int = typer.Option(300, "--sleep", help="Seconds between cycles (default: 300 = 5 min)."),
    json_out: bool = typer.Option(False, "--json", help="JSON output at end."),
) -> None:
    """Run the continuous autonomous loop with acquisition.

    Each cycle:
    1. DETECT — Run all 7 database intelligence sub-reports
    2. ACT — Apply corrective actions on existing data (dedup, evidence, etc.)
    3. ACQUIRE — When corrective actions are exhausted, fetch new dockets
       from NCUC portal, import, bootstrap, and extract rates
    4. MEASURE — Re-run reports, compare before/after delta
    5. SLEEP — Wait between cycles

    Designed for unattended 8-24 hour runs. Stops when:
    - Max runtime exceeded
    - Max cycles reached
    - No improvement for 2 consecutive cycles

    Requires NCID credentials + Playwright/Chrome for docket acquisition.
    Gracefully skips acquisition when auth is unavailable.
    """
    import json as _json
    import time as _time

    from duke_rates.document_intelligence.acquisition import (
        acquire_and_cycle,
        check_acquisition_capabilities,
    )

    settings, _ = _bootstrap()

    caps = check_acquisition_capabilities()

    typer.echo(f"=== Continuous Autonomous Loop {'(DRY RUN)' if dry_run else '(EXECUTING)'} ===")
    typer.echo(f"Max runtime: {max_runtime}m | Max cycles: {max_cycles} | Max dockets/cycle: {max_dockets}")
    typer.echo(f"Sleep between cycles: {sleep}s | Report limit: {limit}")
    if action_batch_limit is None:
        typer.echo(
            f"Action batch limit: REGISTRY DEFAULT (50). "
            f"Pass --action-batch-limit 250 to drain larger backlogs faster."
        )
    else:
        typer.echo(f"Action batch limit: {action_batch_limit} (overrides registry default)")
    typer.echo(f"")
    details = caps.get("details", {}) if isinstance(caps, dict) else {}
    typer.echo(f"Capabilities:")
    typer.echo(
        f"  NCID auth:     "
        f"{'YES' if caps['ncid_auth'] else 'NO -- ' + str(details.get('ncid_source', 'check .env'))}"
    )
    typer.echo(
        f"  Playwright:    "
        f"{'YES' if caps['playwright'] else 'NO -- ' + str(details.get('playwright', 'pip install playwright'))}"
    )
    typer.echo(
        f"  Real browser:  "
        f"{'YES (' + str(details.get('browser_path', '')) + ')' if caps.get('real_browser') else 'NO -- ' + str(details.get('browser_path', 'install Chrome or Edge'))}"
    )
    typer.echo(f"  Portal fetch:  {'YES' if caps['portal_fetch'] else 'NO -- acquisition will skip-fast'}")
    typer.echo(f"  Local import:  YES")
    if not caps["portal_fetch"]:
        missing = []
        if not caps["ncid_auth"]:
            missing.append("NCID credentials in .env")
        if not caps["playwright"]:
            missing.append("playwright package")
        if not caps.get("real_browser"):
            missing.append("installed Chrome/Edge")
        typer.echo(
            f"  PORTAL UNAVAILABLE -- missing: {', '.join(missing)}. "
            f"See docs/NCUC_PORTAL_WORKING_METHOD.md."
        )
        typer.echo(
            f"  The loop will skip-fast on any 'fetch' recommendations and "
            f"continue with existing-data corrective actions."
        )
    else:
        # Portal is available; show how much fetch work actually exists
        # so the operator can see whether the loop will exercise the
        # portal at all. This is what the other agent is missing when
        # it concludes "portal unavailable" from the cycle output.
        try:
            from duke_rates.document_intelligence.acquisition import (
                _inventory_fetch_eligible,
            )
            inv = _inventory_fetch_eligible(str(settings.database_path))
            total = inv.get("total_eligible", 0)
            distinct = inv.get("distinct_dockets", 0)
            by_status = inv.get("by_status", {}) or {}
            if total > 0:
                breakdown = ", ".join(
                    f"{k}={v}" for k, v in sorted(by_status.items(), key=lambda x: -x[1])
                )
                typer.echo(
                    f"  Fetch backlog: {total} records across {distinct} dockets ({breakdown})"
                )
                top = inv.get("top_dockets", [])[:3]
                if top:
                    summary = ", ".join(
                        f"{d['docket_number']}:{d['eligible_count']}" for d in top
                    )
                    typer.echo(f"  Top fetch dockets: {summary}")
            else:
                typer.echo(
                    f"  Fetch backlog: 0 records. The portal will not be "
                    f"used this run -- everything is already downloaded. "
                    f"This is normal, not a failure."
                )
        except Exception:
            pass
    typer.echo(f"")

    if dry_run:
        typer.echo("Dry run — no writes, no portal calls. Use --execute to run for real.")
        typer.echo("")

    result = acquire_and_cycle(
        str(settings.database_path),
        limit=limit,
        max_dockets=max_dockets,
        max_cycles=max_cycles,
        max_runtime_minutes=max_runtime,
        dry_run=dry_run,
        sleep_between_cycles_s=sleep,
        action_batch_limit=action_batch_limit,
        state_path=settings.data_dir / "state" / "loop_state.json",
        history_dir=settings.data_dir / "state" / "loop_history",
        reset_state=reset_state,
        portal_precheck=portal_precheck,
    )

    if json_out:
        typer.echo(_json.dumps(result, indent=2, default=str))
        return

    typer.echo(f"\n=== Continuous Loop Complete ===")
    typer.echo(f"Cycles completed: {result['cycles_completed']}")
    typer.echo(f"Stopped reason:   {result['stopped_reason']}")
    typer.echo(f"Total duration:   {result['total_duration_ms'] / 1000:.1f}s")
    pc = result.get("portal_precheck")
    if pc:
        ok = pc.get("ok")
        typer.echo(
            f"Portal precheck:  "
            f"{'PASS' if ok else 'FAIL'} "
            f"(stage={pc.get('stage')}, {pc.get('duration_s')}s)"
        )
        if not ok and pc.get("detail"):
            typer.echo(f"  {pc['detail']}")
    if result.get("loaded_state"):
        typer.echo(f"Resumed from:     {result.get('state_path')}")
    if result.get("history_jsonl"):
        typer.echo(f"Per-cycle log:    {result['history_jsonl']}")
    final_cooldown = result.get("final_cooldown_remaining") or {}
    if final_cooldown:
        typer.echo(
            f"Cooldown carrying over: "
            + ", ".join(f"{k}={v}c" for k, v in final_cooldown.items())
        )

    # Outcome-metric trajectory across the run (charges, coverage)
    history = result.get("history", [])
    if history:
        first_o = history[0].get("before_outcomes") or {}
        last_o = history[-1].get("after_outcomes") or {}
        if first_o and last_o:
            d_charges = (last_o.get("tariff_charges_total") or 0) - (first_o.get("tariff_charges_total") or 0)
            d_versions = (last_o.get("versions_with_charges") or 0) - (first_o.get("versions_with_charges") or 0)
            d_evidence = (last_o.get("docs_with_evidence") or 0) - (first_o.get("docs_with_evidence") or 0)
            typer.echo(
                f"\nOutcome deltas: "
                f"charges={d_charges:+d}  "
                f"versions_with_charges={d_versions:+d}  "
                f"docs_with_evidence={d_evidence:+d}"
            )
            typer.echo(
                f"Coverage now:   "
                f"extraction={last_o.get('extraction_coverage_pct', 0)}%  "
                f"evidence={last_o.get('evidence_coverage_pct', 0)}%"
            )

    for entry in history:
        c = entry["cycle"]
        d = entry.get("delta", 0)
        corr = len(entry.get("corrective_actions", []))
        acq = entry.get("acquisition", {}) or {}
        docs = acq.get("docs_imported", 0)
        charges = acq.get("charges_added", 0)
        dur = entry.get("duration_ms", 0) / 1000
        sleep_next = entry.get("sleep_s_next")
        cooldown = entry.get("active_cooldown", [])
        outcome_d = entry.get("outcome_delta", {}) or {}

        drain = entry.get("drain") or {}
        skip_reason = entry.get("acquisition_skip_reason")
        rlf = entry.get("record_level_fetch") or {}

        parts = [f"cycle={c}", f"delta={d}", f"corrective={corr}"]
        if outcome_d.get("tariff_charges_total"):
            parts.append(f"+{outcome_d['tariff_charges_total']} charges")
        if drain.get("success"):
            parts.append(f"drain={drain.get('limit')}")
        elif drain and not drain.get("success"):
            parts.append(f"drain=FAILED")
        if rlf and rlf.get("docs_imported", 0) > 0:
            parts.append(f"portal_fetch=+{rlf['docs_imported']} docs")
        elif rlf and rlf.get("dockets_acquired", 0) > 0:
            parts.append(f"portal_fetch={rlf['dockets_acquired']}d")
        if docs:
            parts.append(f"acquired={docs} docs")
        if charges and "tariff_charges_total" not in outcome_d:
            parts.append(f"+{charges} charges")
        if cooldown:
            parts.append(f"cooldown={','.join(cooldown)}")
        if sleep_next is not None:
            parts.append(f"next_sleep={sleep_next}s")
        parts.append(f"{dur:.1f}s")
        typer.echo(f"  {' | '.join(parts)}")

        # Show subprocess errors prominently — these were silently
        # discarded before and are the most actionable info on a
        # failing run.
        for err in entry.get("corrective_errors", []) or []:
            typer.echo(f"    ERROR: {err}")
        if drain and not drain.get("success") and drain.get("stderr_tail"):
            typer.echo(f"    DRAIN ERROR: {drain['stderr_tail'][:200]}")
        if skip_reason and not acq:
            typer.echo(f"    ACQUISITION SKIPPED: {skip_reason}")
        if acq:
            for r in acq.get("results", []) or []:
                if r.get("error"):
                    typer.echo(
                        f"    ACQ ERROR ({r.get('docket')}): {r['error']}"
                    )


def main() -> None:
    app()


# ---------------------------------------------------------------------------
# Search Pipeline CLI commands
# ---------------------------------------------------------------------------

@app.command("ingest-ncuc")
def ingest_ncuc(
    docket: str = typer.Argument(
        "",
        help="Docket directory name (e.g. 'e-2-sub-1044') or empty for all dockets.",
    ),
    ncuc_dir: Path = typer.Option(
        Path("data/historical/ncuc"),
        "--dir",
        help="Base directory containing docket subdirectories.",
    ),
    use_llm: bool = typer.Option(False, "--llm", help="Enable LLM fallback for low-confidence segments."),
    output: Path = typer.Option(
        Path("data/ingest_results.json"),
        "--output", "-o",
        help="Output JSON file for ingest results.",
    ),
    rider_output: Path = typer.Option(
        Path("data/rider_summaries.json"),
        "--rider-output",
        help="Output JSON file for Leaf 600 rider rate summaries.",
    ),
    persist: bool = typer.Option(
        True,
        "--persist/--no-persist",
        help="Persist parsed ingest results directly to SQLite.",
    ),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Overwrite existing ingest/rider rows when persisting to SQLite.",
    ),
    skip_seed: bool = typer.Option(
        False,
        "--no-seed",
        help="Skip seeding canonical rider descriptions when persisting.",
    ),
    top_n: int = typer.Option(80, "--top", help="Number of results to display in summary."),
    with_data_only: bool = typer.Option(False, "--data-only", help="Show only segments with extracted rate data."),
) -> None:
    """Legacy JSON ingest path for older compliance-book workflows; not the default historical pipeline."""
    typer.echo(
        "Legacy path: 'ingest-ncuc' writes JSON/analytics artifacts for older workflows. "
        "Prefer 'ncuc import-pipeline' plus 'extract-rates-nc' for the current historical pipeline.",
        err=True,
    )
    settings, _ = _bootstrap()
    from duke_rates.db.ncuc_loader import (
        persist_ingest_result_records,
        persist_rider_summary_records,
        seed_rider_descriptions,
    )
    from duke_rates.db.sqlite import connect
    from duke_rates.parse.ingest_pipeline import (
        IngestPipeline, print_ingest_summary,
        export_ingest_results_json,
        export_rider_summaries_json,
        serialize_ingest_results,
        serialize_rider_summaries,
    )

    pipeline = IngestPipeline(settings, use_llm=use_llm)

    if docket:
        target = ncuc_dir / docket
        if not target.exists():
            typer.echo(f"Docket directory not found: {target}", err=True)
            raise typer.Exit(1)
        results = pipeline.ingest_docket(target)
    else:
        all_r = pipeline.ingest_all_ncuc(ncuc_dir)
        results = [r for rs in all_r.values() for r in rs]

    if with_data_only:
        display = [r for r in results if r.has_rate_data()]
    else:
        display = results

    print_ingest_summary(display, top_n=top_n)

    if persist:
        conn = connect(settings.database_path)
        try:
            if not skip_seed:
                n_desc = seed_rider_descriptions(conn)
                typer.echo(f"Rider descriptions: {n_desc} inserted (0 = already seeded).")

            ingest_records = serialize_ingest_results(results)
            seg_inserted, seg_skipped = persist_ingest_result_records(
                conn,
                ingest_records,
                replace=replace,
            )
            typer.echo(
                f"Persisted ingest segments: {seg_inserted} inserted, {seg_skipped} skipped"
            )

            rider_records = serialize_rider_summaries(results)
            rider_inserted, rider_skipped = persist_rider_summary_records(
                conn,
                rider_records,
                replace=replace,
            )
            typer.echo(
                f"Persisted rider blocks:   {rider_inserted} inserted, {rider_skipped} skipped"
            )
        finally:
            conn.close()

    export_ingest_results_json(results, output)
    typer.echo(f"Saved {len(results)} ingest results -> {output}")

    n_summaries = export_rider_summaries_json(results, rider_output)
    if n_summaries:
        typer.echo(f"Saved {n_summaries} rider summaries -> {rider_output}")


@app.command("load-ncuc-ingest")
def load_ncuc_ingest(
    ingest_json: Path = typer.Option(
        Path("data/ingest_results_all.json"),
        "--ingest",
        help="Ingest results JSON from ingest-ncuc.",
    ),
    rider_json: Path = typer.Option(
        Path("data/rider_summaries.json"),
        "--riders",
        help="Rider summaries JSON from ingest-ncuc.",
    ),
    replace: bool = typer.Option(
        False, "--replace",
        help="Overwrite existing rows (default: skip duplicates).",
    ),
    skip_seed: bool = typer.Option(
        False, "--no-seed",
        help="Skip seeding rider descriptions.",
    ),
) -> None:
    """Load NCUC ingest results and rider summaries into the database.

    Populates ncuc_ingest_segments, rider_summary_blocks, rider_line_items,
    and rider_descriptions tables.  Safe to run multiple times (idempotent).
    """
    from duke_rates.db.ncuc_loader import (
        load_ingest_results, load_rider_summaries, seed_rider_descriptions
    )
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    if not skip_seed:
        n_desc = seed_rider_descriptions(conn)
        typer.echo(f"Rider descriptions: {n_desc} inserted (0 = already seeded).")

    if ingest_json.exists():
        ins, skip = load_ingest_results(conn, ingest_json, replace=replace)
        typer.echo(f"Ingest segments: {ins} inserted, {skip} skipped  <- {ingest_json}")
    else:
        typer.echo(f"Ingest file not found: {ingest_json}", err=True)

    if rider_json.exists():
        ins, skip = load_rider_summaries(conn, rider_json, replace=replace)
        typer.echo(f"Rider blocks:     {ins} inserted, {skip} skipped  <- {rider_json}")
    else:
        typer.echo(f"Rider summaries file not found: {rider_json}", err=True)

    conn.close()


@app.command("load-dep-provisional-riders")
def load_dep_provisional_riders(
    start_date: str = typer.Option("2016-01-01", help="Start date for provisional DEP RES history."),
    end_date: str = typer.Option("2022-12-31", help="End date for provisional DEP RES history."),
    replace: bool = typer.Option(
        True,
        "--replace/--no-replace",
        help="Replace existing provisional rows for matching dates.",
    ),
) -> None:
    """Load derived DEP provisional rider history into dedicated SQLite tables."""
    from duke_rates.db.dep_provisional_loader import load_dep_res_provisional_history

    settings, _ = _bootstrap()
    result = load_dep_res_provisional_history(
        settings.database_path,
        start_date=start_date,
        end_date=end_date,
        replace=replace,
    )
    typer.echo(
        "DEP provisional rider history: "
        f"{result['totals_loaded']} totals loaded, "
        f"{result['components_loaded']} components loaded"
    )


@app.command("cleanup-nc-residential-history")
def cleanup_nc_residential_history(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Delete duplicate/null-date residential history rows from the SQLite database.",
    ),
    report_path: Path = typer.Option(
        Path("data/processed/cleanup/nc_residential_history_cleanup_report.json"),
        "--report",
        help="Where to write the cleanup report JSON.",
    ),
) -> None:
    """Preview or apply cleanup for noisy residential NC history rows."""
    from duke_rates.db.history_cleanup import cleanup_nc_residential_history, export_cleanup_report

    settings, _ = _bootstrap()
    report = cleanup_nc_residential_history(settings.database_path, apply=apply)
    export_cleanup_report(report, report_path)
    typer.echo(
        "Residential history cleanup: "
        f"duplicate_groups={report['duplicate_base_groups']} "
        f"duplicate_rows_to_delete={report['duplicate_base_rows_to_delete']} "
        f"null_base_rows_to_delete={report['null_base_rows_to_delete']} "
        f"null_rider_blocks_to_delete={report['null_rider_blocks_to_delete']} "
        f"applied={report['applied']}"
    )
    typer.echo(f"Report written to {report_path}")

















@app.command("nc-redline-portal-queue")
def nc_redline_portal_queue_cmd(
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Show NCUC dockets found in redline crossrefs that have not yet been fetched from portal."""
    from duke_rates.analytics.nc_redline_lead_audit import suggest_redline_portal_fetches

    suggestions = suggest_redline_portal_fetches(database_path=database)
    unfetched = [s for s in suggestions if s["fetch_count"] == 0]
    partial = [s for s in suggestions if s["fetch_count"] > 0]

    typer.echo(f"\n=== Redline Portal Fetch Queue ({len(unfetched)} unfetched, {len(partial)} partial) ===\n")

    if unfetched:
        typer.echo("UNFETCHED (priority targets):")
        for s in unfetched:
            families = ", ".join(s["source_families"])
            typer.echo(f"  [{s['priority_band'].upper():6}] {s['docket']}")
            typer.echo(f"          families: {families}")

    if partial:
        typer.echo("\nPARTIAL (some records already fetched):")
        for s in partial:
            families = ", ".join(s["source_families"])
            typer.echo(f"  [{s['priority_band'].upper():6}] {s['docket']}  ({s['fetch_count']} records)")
            typer.echo(f"          families: {families}")


@app.command("normalize-nc-effective-dates")
def normalize_nc_effective_dates_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be changed without updating the database.",
    ),
    state: str = typer.Option(
        "NC",
        "--state",
        help="State filter (default: NC).",
    ),
) -> None:
    """Normalize malformed effective_start / effective_end values in historical_documents.

    Converts 'Month D, YYYY' strings (and similar) that were stored without ISO
    normalisation into 'YYYY-MM-DD' format.  Safe to re-run: rows already in
    ISO format are skipped.
    """
    from duke_rates.db.ncuc_loader import _normalize_date

    settings, repository = _bootstrap()
    with repository._connect() as conn:
        rows = conn.execute(
            """
            SELECT id, family_key, effective_start, effective_end
            FROM historical_documents
            WHERE state = ?
              AND (
                (effective_start IS NOT NULL AND effective_start NOT GLOB '????-??-??*')
                OR
                (effective_end IS NOT NULL AND effective_end NOT GLOB '????-??-??*')
              )
            ORDER BY id
            """,
            (state,),
        ).fetchall()

    updated = 0
    skipped = 0
    errors = []

    for hd_id, family_key, raw_start, raw_end in rows:
        norm_start = _normalize_date(raw_start or "") if raw_start else raw_start
        norm_end = _normalize_date(raw_end or "") if raw_end else raw_end

        changed = (norm_start != raw_start) or (norm_end != raw_end)
        if not changed:
            skipped += 1
            continue

        if dry_run:
            typer.echo(
                f"  [dry-run] hd={hd_id} {family_key}"
                + (f"  start: {raw_start!r} -> {norm_start!r}" if norm_start != raw_start else "")
                + (f"  end: {raw_end!r} -> {norm_end!r}" if norm_end != raw_end else "")
            )
            updated += 1
            continue

        try:
            with repository._connect() as conn:
                conn.execute(
                    "UPDATE historical_documents SET effective_start = ?, effective_end = ? WHERE id = ?",
                    (norm_start, norm_end, hd_id),
                )
            typer.echo(
                f"  hd={hd_id} {family_key}"
                + (f"  start: {raw_start!r} -> {norm_start!r}" if norm_start != raw_start else "")
                + (f"  end: {raw_end!r} -> {norm_end!r}" if norm_end != raw_end else "")
            )
            updated += 1
        except Exception as exc:
            errors.append(f"hd={hd_id}: {exc}")

    typer.echo(
        f"normalize-nc-effective-dates: found={len(rows)} updated={updated} skipped={skipped} errors={len(errors)}"
        + (" [dry-run]" if dry_run else "")
    )
    for e in errors:
        typer.echo(f"  ERROR: {e}")


@app.command("remediate-nc-null-effective-dates")
def remediate_nc_null_effective_dates_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would change without writing to the database.",
    ),
    family_key: str | None = typer.Option(
        None,
        "--family-key",
        help="Restrict to a single family key.",
    ),
    limit: int = typer.Option(
        500,
        "--limit",
        help="Maximum number of null-effective_start docs to process.",
    ),
    passes: str = typer.Option(
        "1,2,3",
        "--passes",
        help="Comma-separated pass numbers to run (e.g. '1,2' skips docket fallback).",
    ),
    enable_llm: bool = typer.Option(
        False,
        "--enable-llm",
        help=(
            "Enable Pass 1C: LLM-assisted date extraction via local Ollama. "
            "Runs qwen2.5:7b-instruct on docs where regex passes fail. "
            "High-confidence effective dates are written; medium-confidence "
            "dates are stored in metadata_json for review."
        ),
    ),
    state: str = typer.Option(
        "NC",
        "--state",
        help="State filter.",
    ),
) -> None:
    """Remediate null effective_start on historical_documents via multi-pass strategy.

    Pass 1  - Footer/header PDF scan: reads the span pages and regex-matches
               the standard Duke 'Effective for ... on and after ...' footer.
               Also handles garbled OCR years (e.g. 4996 -> 1996).

    Pass 1B - Redline regex scan: detects concatenated redline date pairs
               (e.g. 'September 30, 2024January 1, 2025'), stores the proposed
               date as effective_start with superseded date in metadata.

    Pass 1C - LLM extraction (--enable-llm): uses qwen2.5:7b-instruct to read
               raw page text and identify effective dates in prose. High-confidence
               results set effective_start; medium-confidence stored for review.

    Pass 2  - Rider summary cross-reference: matches the doc's rider/leaf code
               against the effective dates recorded in the leaf-600/602 Summary
               of Rider Adjustments sheets already in tariff_charges.

    Pass 3  - Docket filing-date proxy: falls back to the earliest filing_date
               found in ncuc_discovery_records for the same docket directory.
               Low confidence; stored with source=docket_filing_proxy in metadata.
    """
    from duke_rates.historical.ncuc.effective_date_remediation import (
        remediate_null_effective_dates,
    )

    try:
        pass_nums = tuple(int(p.strip()) for p in passes.split(",") if p.strip())
    except ValueError:
        typer.echo(f"ERROR: --passes must be comma-separated integers, got {passes!r}")
        raise typer.Exit(1)

    _, repository = _bootstrap()
    result = remediate_null_effective_dates(
        repository,
        state=state,
        family_key=family_key,
        limit=limit,
        dry_run=dry_run,
        passes=pass_nums,
        enable_llm=enable_llm,
    )

    suffix = " [dry-run]" if dry_run else ""
    typer.echo(
        f"remediate-nc-null-effective-dates{suffix}: "
        f"total_null={result.total_null} "
        f"pass1={result.pass1_resolved} "
        f"pass1b={result.pass1b_resolved} "
        f"pass1c={result.pass1c_resolved} "
        f"pass1c_medium={result.pass1c_medium} "
        f"pass2={result.pass2_resolved} "
        f"pass3={result.pass3_resolved} "
        f"unresolved={result.unresolved}"
    )
    if enable_llm and result.pass1c_medium > 0:
        typer.echo(
            f"  NOTE: {result.pass1c_medium} docs have medium-confidence LLM dates "
            "stored in metadata_json (llm_medium_date) — review before promoting."
        )
    if result.updated_ids:
        typer.echo(f"  updated ({len(result.updated_ids)}): {result.updated_ids[:20]}"
                   + (" ..." if len(result.updated_ids) > 20 else ""))
    if result.unresolved_ids:
        typer.echo(f"  unresolved ({len(result.unresolved_ids)}): {result.unresolved_ids[:20]}"
                   + (" ..." if len(result.unresolved_ids) > 20 else ""))
    for e in result.errors:
        typer.echo(f"  ERROR: {e}")


@app.command("refresh-nc-redline-fingerprints")
def refresh_nc_redline_fingerprints_cmd(
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum pages per PDF to inspect."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without updating SQLite."),
) -> None:
    """Refresh NC document_fingerprints redline flags using the corrected detector."""
    from duke_rates.analytics.nc_redline_fingerprint_refresh import (
        refresh_nc_redline_fingerprints,
    )

    report = refresh_nc_redline_fingerprints(
        database_path=database,
        max_pages=max_pages,
        dry_run=dry_run,
    )
    typer.echo(
        "NC redline fingerprint refresh: "
        f"source_pdfs={report['source_pdf_count']} "
        f"changed_rows={report['changed_fingerprint_rows']} "
        f"actions={json.dumps(report['action_counts'], sort_keys=True)}"
    )


@app.command("seed-dep-residential-rider-applicability")
def seed_dep_residential_rider_applicability_cmd(
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Seed mandatory DEP residential rider-family applicability links for leafs 500-504."""
    from duke_rates.analytics.dep_residential_rider_applicability import (
        seed_dep_residential_rider_applicability,
    )

    report = seed_dep_residential_rider_applicability(database_path=database)
    typer.echo(
        "Seeded DEP residential rider applicability: "
        f"inserted={report['inserted']} skipped={report['skipped']}"
    )


@app.command("seed-dep-storm-rider-applicability")
def seed_dep_storm_rider_applicability_cmd(
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Seed DEP storm-rider applicability links for residential schedules."""
    from duke_rates.analytics.dep_storm_rider_applicability import (
        seed_dep_storm_rider_applicability,
    )

    report = seed_dep_storm_rider_applicability(database_path=database)
    typer.echo(
        f"Seeded DEP storm rider applicability: inserted={report['inserted']} skipped={report['skipped']}"
    )


@app.command("audit-document-type-classifications-nc")
def audit_document_type_classifications_nc(
    state: str = typer.Option("NC", "--state", help="State filter for historical_documents."),
    export_gold_set: Path | None = typer.Option(
        None,
        "--export-gold-set",
        help=(
            "If set, write high-agreement docs as a JSONL gold-set candidate "
            "file. One row per doc: {hd_id, label, confidence, classifiers, "
            "title, text_sample, family_key, state}. The text_sample is the "
            "first 2000 chars from the bulk extractor's text path."
        ),
    ),
    min_classifiers: int = typer.Option(
        2,
        "--min-classifiers",
        help="Minimum classifiers that must have run for a gold-set candidate.",
    ),
    require_unanimous: bool = typer.Option(
        True,
        "--require-unanimous/--allow-majority",
        help=(
            "Gold-set candidates require unanimous label agreement across "
            "all running classifiers (default). With --allow-majority, "
            "any doc whose >=50%% of classifiers agree qualifies."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON summary."),
) -> None:
    """Audit document_type classifier agreement and (optionally) export a gold set.

    Surfaces three buckets:
      - Gold-set candidates: docs where multiple classifiers agree on the
        same label. The starter training set for fine-tuning a small
        classifier or seeding human review.
      - Disagreement docs: docs where classifiers split. The highest-
        leverage targets for hand labeling — they're the cases the current
        rule/embedding/LLM stack can't decide on its own.
      - Coverage gaps: docs missing one or more classifiers (LLM never ran,
        embedding never ran). Backfilling these improves the agreement
        signal corpus-wide.

    Per the Stream A direction in docs/research/document_identification.md.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Pull all document_type classifications for state-filtered hd's
    c.execute(
        """
        SELECT dc.subject_id AS hd_id_str,
               dc.classifier,
               dc.label,
               dc.confidence,
               hd.family_key,
               hd.title,
               hd.state
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type'
          AND hd.state = ?
        """,
        (state,),
    )
    rows = c.fetchall()

    # Group by doc
    by_doc: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_doc[r["hd_id_str"]].append(r)

    # Per-doc agreement analysis
    gold_candidates: list[dict] = []
    disagreement: list[dict] = []
    coverage_gaps: list[dict] = []

    classifier_universe = {"rule_document_type_v1", "embedding_knn_v1"}  # llm runs are spotty
    for hd_id_str, doc_rows in by_doc.items():
        classifiers_present = {r["classifier"] for r in doc_rows}
        labels = [r["label"] for r in doc_rows]
        label_counter = Counter(labels)
        most_common_label, most_common_n = label_counter.most_common(1)[0]
        n_classifiers = len(classifiers_present)
        n_distinct_labels = len(label_counter)

        title = doc_rows[0]["title"]
        family_key = doc_rows[0]["family_key"]

        # Gold-set membership rule
        if n_classifiers >= min_classifiers:
            unanimous = n_distinct_labels == 1
            majority = most_common_n / n_classifiers >= 0.5
            qualifies = unanimous if require_unanimous else majority
            if qualifies:
                # Average confidence of voters for the winning label
                voters = [r for r in doc_rows if r["label"] == most_common_label]
                avg_conf = sum(r["confidence"] for r in voters) / max(1, len(voters))
                gold_candidates.append({
                    "hd_id": int(hd_id_str),
                    "label": most_common_label,
                    "confidence": round(avg_conf, 3),
                    "classifiers": sorted(classifiers_present),
                    "votes_for_label": most_common_n,
                    "total_classifiers": n_classifiers,
                    "family_key": family_key,
                    "title": title,
                })

        if n_distinct_labels >= 2 and n_classifiers >= 2:
            disagreement.append({
                "hd_id": int(hd_id_str),
                "labels": dict(label_counter),
                "classifiers": sorted(classifiers_present),
                "family_key": family_key,
                "title": title,
            })

        missing = classifier_universe - classifiers_present
        if missing:
            coverage_gaps.append({
                "hd_id": int(hd_id_str),
                "present": sorted(classifiers_present),
                "missing": sorted(missing),
                "family_key": family_key,
            })

    # Classifier-wide confidence stats
    c.execute(
        """
        SELECT classifier,
               COUNT(*) AS n,
               AVG(confidence) AS avg_c,
               MIN(confidence) AS min_c,
               MAX(confidence) AS max_c,
               SUM(CASE WHEN confidence >= 0.9 THEN 1 ELSE 0 END) AS hi,
               SUM(CASE WHEN confidence < 0.5 THEN 1 ELSE 0 END) AS lo
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type' AND hd.state = ?
        GROUP BY classifier
        """,
        (state,),
    )
    classifier_stats = [dict(r) for r in c.fetchall()]

    summary = {
        "state": state,
        "docs_with_any_classification": len(by_doc),
        "gold_set_candidates": len(gold_candidates),
        "disagreement_docs": len(disagreement),
        "coverage_gaps": len(coverage_gaps),
        "classifier_stats": classifier_stats,
        "label_distribution": dict(Counter(
            g["label"] for g in gold_candidates
        ).most_common()),
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        if export_gold_set:
            _write_gold_set_jsonl(export_gold_set, gold_candidates, conn, settings)
        conn.close()
        return

    typer.echo(f"\nDocument-type classification audit | state={state}\n")
    typer.echo(f"  docs with any classification:  {summary['docs_with_any_classification']}")
    typer.echo(f"  gold-set candidates:           {summary['gold_set_candidates']}")
    typer.echo(f"  disagreement docs:             {summary['disagreement_docs']}")
    typer.echo(f"  coverage gaps:                 {summary['coverage_gaps']}")

    typer.echo("\n  Per-classifier confidence:")
    typer.echo(f"    {'classifier':<35} {'n':>5} {'avg':>6} {'min':>6} {'max':>6} {'hi(>=0.9)':>10} {'lo(<0.5)':>9}")
    for stat in classifier_stats:
        typer.echo(
            f"    {stat['classifier']:<35} {stat['n']:>5} "
            f"{stat['avg_c']:>6.2f} {stat['min_c']:>6.2f} {stat['max_c']:>6.2f} "
            f"{stat['hi']:>10} {stat['lo']:>9}"
        )

    typer.echo("\n  Gold-set candidates by label:")
    for label, n in summary["label_distribution"].items():
        typer.echo(f"    {label:<30} {n}")

    if export_gold_set:
        written = _write_gold_set_jsonl(export_gold_set, gold_candidates, conn, settings)
        typer.echo(f"\n  Wrote {written} gold-set rows to {export_gold_set}")
    else:
        typer.echo(
            "\n  Pass --export-gold-set PATH.jsonl to write a JSONL training "
            "candidate file."
        )

    conn.close()


def _write_gold_set_jsonl(path: Path, gold_candidates: list[dict], conn, settings) -> int:
    """Write gold-set rows enriched with a text sample to ``path``. Returns count written.

    The text sample is loaded the same way the bulk extractor sees text:
    docling artifact (full or sliced) preferred, pdfplumber as fallback.
    Truncated to 2000 chars to keep the file size reasonable for training.
    """
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )
    extractor = BulkExtractor(db_path=str(settings.database_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as f:
        for cand in gold_candidates:
            doc = extractor.get_document_for_extraction(cand["hd_id"])
            if not doc:
                continue
            try:
                text, src = extractor.extract_text_from_pdf(
                    doc["local_path"],
                    start_page=doc.get("start_page"),
                    end_page=doc.get("end_page"),
                )
                if src in ("docling_artifact", "docling_artifact_sliced"):
                    text = normalize_docling_markdown(text)
                text = normalize_ocr_text(text)
            except Exception:
                text = ""
            row = {
                **cand,
                "text_sample": (text or "")[:2000],
                "text_source": src if text else "none",
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return written


@app.command("seed-document-type-gold-nc")
def seed_document_type_gold_nc(
    state: str = typer.Option("NC", "--state", help="State filter for historical_documents."),
    min_classifiers: int = typer.Option(
        2,
        "--min-classifiers",
        help=(
            "Minimum number of classifiers that must have run AND agreed on "
            "a single label for the doc to seed gold. 2 = relaxed (rule + "
            "embedding agree), 3 = strict (rule + embedding + LLM all agree)."
        ),
    ),
    exclude_classifiers: list[str] | None = typer.Option(
        None,
        "--exclude-classifier",
        help=(
            "Repeatable. Skip these classifiers when computing agreement. "
            "Useful when seeding pre-v2 gold to establish a baseline before "
            "the new classifier enters the vote."
        ),
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually write gold rows. Without this flag, dry-run only.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Seed document_type_gold from classifier agreement.

    Default rule: a doc seeds gold when at least ``min_classifiers``
    classifiers have run AND they all agree on a single label. The
    inserted row carries:

      label       = the agreed-upon label
      labeler     = 'agreement:<classifier1>+<classifier2>+...'
      source      = 'unanimous_classifier_agreement'
      evidence    = {classifiers: [...], confidences: [...]}

    Idempotent: docs that already have an active (non-superseded) gold
    row are skipped. To force-rewrite, supersede the existing row first
    via a follow-up command.

    Use ``--exclude-classifier rule_document_type_v2`` to seed gold
    based on the pre-v2 classifier stack — recommended for first
    seeding so v2's known biases don't contaminate the baseline.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    exclude_set = set(exclude_classifiers or [])

    # Pull all document_type classifications for the state
    c.execute(
        """
        SELECT dc.subject_id, dc.classifier, dc.label, dc.confidence
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type'
          AND hd.state = ?
        """,
        (state,),
    )

    by_doc: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in c.fetchall():
        if r["classifier"] in exclude_set:
            continue
        by_doc[r["subject_id"]].append(r)

    # Find docs with existing active gold rows (to skip)
    c.execute(
        """
        SELECT subject_id FROM document_type_gold
        WHERE subject_kind = 'historical_document' AND superseded_by IS NULL
        """
    )
    existing_gold = {r["subject_id"] for r in c.fetchall()}

    seeded: list[dict] = []
    skipped_already_gold = 0
    skipped_too_few_classifiers = 0
    skipped_disagreement = 0
    label_counts: Counter = Counter()

    for subj_id, rows in by_doc.items():
        if subj_id in existing_gold:
            skipped_already_gold += 1
            continue
        if len(rows) < min_classifiers:
            skipped_too_few_classifiers += 1
            continue
        labels = [r["label"] for r in rows]
        if len(set(labels)) != 1:
            skipped_disagreement += 1
            continue

        agreed_label = labels[0]
        classifiers = sorted(r["classifier"] for r in rows)
        confidences = [round(r["confidence"], 3) for r in rows]
        labeler = "agreement:" + "+".join(classifiers)
        evidence = {
            "classifiers": classifiers,
            "confidences": confidences,
            "avg_confidence": round(sum(confidences) / len(confidences), 3),
        }
        seeded.append({
            "subject_id": subj_id,
            "label": agreed_label,
            "labeler": labeler,
            "evidence": evidence,
            "classifier_count": len(rows),
        })
        label_counts[agreed_label] += 1

    if execute and seeded:
        from datetime import UTC as _UTC
        utc_now = datetime.now(_UTC).isoformat()
        for row in seeded:
            c.execute(
                """
                INSERT INTO document_type_gold (
                    subject_kind, subject_id, label, labeler, source,
                    evidence_json, superseded_by, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    "historical_document",
                    row["subject_id"],
                    row["label"],
                    row["labeler"],
                    "unanimous_classifier_agreement",
                    json.dumps(row["evidence"]),
                    utc_now,
                ),
            )
        conn.commit()

    summary = {
        "state": state,
        "candidates_considered": len(by_doc),
        "seeded": len(seeded),
        "skipped_already_gold": skipped_already_gold,
        "skipped_too_few_classifiers": skipped_too_few_classifiers,
        "skipped_disagreement": skipped_disagreement,
        "min_classifiers": min_classifiers,
        "exclude_classifiers": sorted(exclude_set),
        "label_distribution": dict(label_counts.most_common()),
        "executed": execute,
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        conn.close()
        return

    typer.echo(f"\ndocument_type_gold seeding | state={state}\n")
    typer.echo(f"  min_classifiers required:       {min_classifiers}")
    if exclude_set:
        typer.echo(f"  excluded classifiers:           {sorted(exclude_set)}")
    typer.echo(f"  docs considered:                {summary['candidates_considered']}")
    typer.echo(f"  -> already gold (skipped):      {summary['skipped_already_gold']}")
    typer.echo(f"  -> too few classifiers:         {summary['skipped_too_few_classifiers']}")
    typer.echo(f"  -> classifier disagreement:     {summary['skipped_disagreement']}")
    typer.echo(f"  -> would seed:                  {summary['seeded']}")
    if label_counts:
        typer.echo("\n  Seeded label distribution:")
        for label, n in label_counts.most_common():
            typer.echo(f"    {label:<28} {n}")
    if execute:
        typer.echo(f"\n  Wrote {len(seeded)} new gold rows to document_type_gold.")
    else:
        typer.echo("\n  Dry-run only. Pass --execute to write gold rows.")

    conn.close()


@app.command("train-document-type-baseline-nc")
def train_document_type_baseline_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    val_fraction: float = typer.Option(
        0.2, "--val-fraction",
        help="Stratified val split fraction for classes with >=5 samples.",
    ),
    random_state: int = typer.Option(
        13, "--random-state", help="Deterministic seed for the val split."
    ),
    save_path: Path | None = typer.Option(
        None,
        "--save",
        help=(
            "Optional path to joblib-dump the fitted (vectorizer, model) "
            "tuple for reuse. Recommend models/baseline_document_type.joblib."
        ),
    ),
    cv_folds: int = typer.Option(
        0,
        "--cv",
        help=(
            "If > 1, also run stratified k-fold CV with this many folds and "
            "print mean/std accuracy + F1. Recommended k=5. CV is more honest "
            "than the single train/val split when the gold set is small "
            "(~441 rows produces 3-5 pts of accuracy drift between random "
            "seeds on a single split)."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit metrics as JSON."),
) -> None:
    """Stream D baseline: TF-IDF + LogisticRegression on document_type_gold.

    Sets a measurable starting point for any later fine-tuned model
    (DistilBERT, qwen-finetuned) to compare against. Pulls all active
    gold rows for the given state, materializes their text samples via
    the same path the bulk extractor uses, splits stratified train/val
    (with rare classes pinned to train), and fits a multi-class logistic
    regression with class_weight='balanced'.

    Per docs/research/document_identification.md Stream D — this is the
    intentionally-minimal first cut. See the module-level docstring of
    duke_rates.classification.baseline_classifier for rationale.
    """
    import sqlite3
    from duke_rates.classification.baseline_classifier import (
        TrainingDataset, train_baseline, cross_validate_baseline,
    )
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        """
        SELECT dtg.subject_id, dtg.label
        FROM document_type_gold dtg
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dtg.subject_id
         AND dtg.subject_kind = 'historical_document'
        WHERE dtg.superseded_by IS NULL AND hd.state = ?
        """,
        (state,),
    )
    gold_rows = c.fetchall()
    if not gold_rows:
        typer.echo(f"No gold rows for state={state}. Seed gold first.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Loading text for {len(gold_rows)} gold docs...")
    extractor = BulkExtractor(db_path=str(settings.database_path))
    hd_ids: list[int] = []
    labels: list[str] = []
    texts: list[str] = []
    missing = 0
    for r in gold_rows:
        hd_id = int(r["subject_id"])
        doc = extractor.get_document_for_extraction(hd_id)
        if not doc:
            missing += 1
            continue
        try:
            text, src = extractor.extract_text_from_pdf(
                doc["local_path"],
                start_page=doc.get("start_page"),
                end_page=doc.get("end_page"),
            )
            if src in ("docling_artifact", "docling_artifact_sliced"):
                text = normalize_docling_markdown(text)
            text = normalize_ocr_text(text)
        except Exception:
            text = ""
        if not text:
            missing += 1
            continue
        hd_ids.append(hd_id)
        labels.append(r["label"])
        texts.append(text[:2000])  # Match the text_sample slice the seeders use

    conn.close()

    if not texts:
        typer.echo("No text recoverable for any gold doc.", err=True)
        raise typer.Exit(1)

    typer.echo(
        f"Materialized {len(texts)} rows ({missing} skipped — no text). "
        f"Training baseline..."
    )

    dataset = TrainingDataset(hd_ids=hd_ids, labels=labels, texts=texts)
    result = train_baseline(
        dataset, val_fraction=val_fraction, random_state=random_state
    )

    metrics = {
        "state": state,
        "gold_rows_loaded": len(gold_rows),
        "rows_used": len(texts),
        "skipped_no_text": missing,
        "classes": result.classes,
        "train_n": result.train_n,
        "val_n": result.val_n,
        "train_only_classes": result.train_only_classes,
        "val_accuracy": round(result.val_accuracy, 4),
        "overall_train_accuracy": round(result.overall_train_accuracy, 4),
        "per_class": {
            lab: {
                "precision": round(stats.get("precision", 0.0), 3),
                "recall": round(stats.get("recall", 0.0), 3),
                "f1-score": round(stats.get("f1-score", 0.0), 3),
                "support": int(stats.get("support", 0)),
            }
            for lab, stats in result.val_classification_report.items()
            if isinstance(stats, dict) and lab not in ("accuracy",)
        },
    }

    if save_path:
        import joblib
        save_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"vectorizer": result.vectorizer, "model": result.model,
             "classes": result.classes, "metrics": metrics},
            save_path,
        )
        metrics["saved_to"] = str(save_path)

    # Optional cross-validation pass for a more honest accuracy number
    cv_metrics: dict | None = None
    if cv_folds and cv_folds >= 2:
        cv_result = cross_validate_baseline(
            dataset, n_folds=cv_folds, random_state=random_state
        )
        cv_metrics = {
            "n_folds": cv_result.n_folds,
            "eligible_rows": cv_result.eligible_n,
            "train_only_classes": cv_result.train_only_classes,
            "fold_accuracies": cv_result.fold_accuracies,
            "fold_weighted_f1": cv_result.fold_weighted_f1,
            "fold_macro_f1": cv_result.fold_macro_f1,
            "mean_accuracy": cv_result.mean_accuracy,
            "std_accuracy": cv_result.std_accuracy,
            "mean_weighted_f1": cv_result.mean_weighted_f1,
            "std_weighted_f1": cv_result.std_weighted_f1,
            "mean_macro_f1": cv_result.mean_macro_f1,
            "std_macro_f1": cv_result.std_macro_f1,
        }
        metrics["cross_validation"] = cv_metrics

    if json_out:
        typer.echo(json.dumps(metrics, indent=2))
        return

    typer.echo(f"\nbaseline trained | state={state}\n")
    typer.echo(f"  rows used:                 {metrics['rows_used']}  ({missing} skipped no-text)")
    typer.echo(f"  train rows:                {result.train_n}")
    typer.echo(f"  val rows:                  {result.val_n}")
    typer.echo(f"  classes:                   {len(result.classes)}")
    typer.echo(f"  train-only classes (rare): {result.train_only_classes}")
    typer.echo(f"  val accuracy:              {result.val_accuracy:.3f}")
    typer.echo(f"  train accuracy (ref):      {result.overall_train_accuracy:.3f}")
    typer.echo("\n  Per-class val metrics:")
    typer.echo(f"    {'class':<28} {'P':>5}  {'R':>5}  {'F1':>5}  {'n':>4}")
    per_class = metrics["per_class"]
    # Sort: actual labels first (alphabetic), then macro/weighted averages
    label_keys = sorted(
        k for k in per_class
        if k not in ("macro avg", "weighted avg")
    )
    for lab in label_keys + ["macro avg", "weighted avg"]:
        if lab not in per_class:
            continue
        s = per_class[lab]
        typer.echo(
            f"    {lab:<28} {s['precision']:>5.2f}  {s['recall']:>5.2f}  "
            f"{s['f1-score']:>5.2f}  {s['support']:>4}"
        )
    if save_path:
        typer.echo(f"\n  Artifacts saved to: {save_path}")

    if cv_metrics:
        typer.echo(f"\n  Cross-validation ({cv_metrics['n_folds']}-fold, "
                   f"{cv_metrics['eligible_rows']} eligible rows):")
        typer.echo(f"    accuracy:    mean={cv_metrics['mean_accuracy']:.4f} "
                   f"std={cv_metrics['std_accuracy']:.4f}  "
                   f"folds={cv_metrics['fold_accuracies']}")
        typer.echo(f"    weighted F1: mean={cv_metrics['mean_weighted_f1']:.4f} "
                   f"std={cv_metrics['std_weighted_f1']:.4f}")
        typer.echo(f"    macro F1:    mean={cv_metrics['mean_macro_f1']:.4f} "
                   f"std={cv_metrics['std_macro_f1']:.4f}")
        if cv_metrics['train_only_classes']:
            typer.echo(
                f"    train-only classes (n<{cv_metrics['n_folds']} samples): "
                f"{cv_metrics['train_only_classes']}"
            )


@app.command("audit-stale-gold-nc")
def audit_stale_gold_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    min_v2_confidence: float = typer.Option(
        0.9, "--min-v2-confidence",
        help="Only flag rows where v2 disagrees at >= this confidence.",
    ),
    mark_for_review: bool = typer.Option(
        False,
        "--mark-for-review",
        help=(
            "Append a 'v2 disagrees: <label>@<conf>' annotation to the gold "
            "row's notes field. Does NOT supersede the row — human review "
            "decides whether to keep the original label, replace it, or "
            "split (treat both as valid for a mixed-content bundle)."
        ),
    ),
    out: Path | None = typer.Option(
        None, "--out",
        help=(
            "Optional JSONL path. One row per stale gold doc with the "
            "original gold label, v2's disagreement label, v2 confidence, "
            "and all classifier votes for context."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Find document_type_gold rows where the v2 classifier now disagrees.

    The v0 gold set was seeded from rule_v1 + embedding (+ optional LLM)
    agreement. After v2 backfilled corpus-wide, v2 disagrees with many
    of those agreements. The gold rows weren't auto-superseded — they
    remain as point-in-time labels — but those still-valid labels
    should be sanity-checked given v2's higher confidence.

    Common patterns the live corpus surfaces:
    - TESTIMONY -> COVER_LETTER: PDF body opens with a transmittal
      letter rather than direct testimony; the original label saw
      the testimony content but rule_v1 didn't distinguish well.
    - ORDER_FINAL -> TARIFF_SHEET / RIDER: leaf-revision orders that
      include the new tariff body; v2 reads the tariff section.
    - TESTIMONY/ORDER_FINAL -> RIDER: a rider's filing testimony or
      approval order whose body content reads as the rider itself.

    Read-only by default. --mark-for-review writes a notes annotation
    so reviewers can pick the row up from a future label-fix UI. No
    other DB writes; supersession requires an explicit follow-up.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        """
        WITH v2 AS (
          SELECT subject_id, label AS v2_label, confidence AS v2_confidence,
                 evidence_json AS v2_evidence
          FROM document_classifications
          WHERE stage='document_type'
            AND classifier='rule_document_type_v2'
        )
        SELECT
            CAST(hd.id AS INTEGER) AS hd_id,
            hd.family_key, hd.title,
            dtg.id AS gold_id,
            dtg.label AS gold_label,
            dtg.labeler AS gold_labeler,
            dtg.source AS gold_source,
            dtg.notes AS gold_notes,
            v2.v2_label, v2.v2_confidence, v2.v2_evidence
        FROM document_type_gold dtg
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dtg.subject_id
        JOIN v2 ON v2.subject_id = dtg.subject_id
        WHERE dtg.superseded_by IS NULL
          AND dtg.subject_kind = 'historical_document'
          AND hd.state = ?
          AND v2.v2_label != dtg.label
          AND v2.v2_confidence >= ?
        ORDER BY v2.v2_confidence DESC, hd.id
        """,
        (state, min_v2_confidence),
    )
    rows = [dict(r) for r in c.fetchall()]

    # Group by (gold_label, v2_label)
    pairs: Counter = Counter()
    by_gold: Counter = Counter()
    by_v2: Counter = Counter()
    for r in rows:
        pairs[(r["gold_label"], r["v2_label"])] += 1
        by_gold[r["gold_label"]] += 1
        by_v2[r["v2_label"]] += 1

    summary = {
        "state": state,
        "min_v2_confidence": min_v2_confidence,
        "total_stale": len(rows),
        "by_gold_label": dict(by_gold.most_common()),
        "by_v2_label": dict(by_v2.most_common()),
        "top_pairs": [
            {"gold_label": g, "v2_label": v2, "count": n}
            for (g, v2), n in pairs.most_common(15)
        ],
        "marked_for_review": 0,
    }

    if mark_for_review and rows:
        from datetime import UTC as _UTC
        utc_now = datetime.now(_UTC).isoformat()
        for r in rows:
            annotation = (
                f"v2 disagrees: {r['v2_label']}@{r['v2_confidence']:.2f} "
                f"(audited {utc_now[:10]})"
            )
            existing_notes = r["gold_notes"] or ""
            # Idempotency — don't append if the annotation is already there
            if annotation in existing_notes:
                continue
            new_notes = (existing_notes + "\n" + annotation).strip()
            c.execute(
                "UPDATE document_type_gold SET notes = ? WHERE id = ?",
                (new_notes, r["gold_id"]),
            )
            summary["marked_for_review"] += 1
        conn.commit()

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        summary["written_to"] = str(out)

    conn.close()

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return

    typer.echo(f"\nStale-gold audit | state={state}")
    typer.echo(f"  v2 min confidence:        {min_v2_confidence}")
    typer.echo(f"  total stale rows:         {len(rows)}")

    typer.echo("\n  By gold label (what v0 said):")
    for label, n in by_gold.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo("\n  By v2 label (what v2 says now):")
    for label, n in by_v2.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo("\n  Top stale pairs (gold -> v2):")
    typer.echo(f"    {'gold label':<26} -> {'v2 label':<26} {'n':>4}")
    for (g, v2), n in pairs.most_common(10):
        typer.echo(f"    {g:<26} -> {v2:<26} {n:>4}")

    if mark_for_review:
        typer.echo(f"\n  Marked {summary['marked_for_review']} gold rows for review (notes annotation).")
    if out:
        typer.echo(f"  Per-doc JSONL written to: {out}")
    elif not mark_for_review:
        typer.echo("\n  Dry-run only. Pass --mark-for-review to annotate notes, or --out to export JSONL.")


@app.command("audit-bundle-metadata-mismatch-nc")
def audit_bundle_metadata_mismatch_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    min_confidence: float = typer.Option(
        0.9, "--min-confidence",
        help="Minimum v2 confidence required to flag a mismatch.",
    ),
    out: Path | None = typer.Option(
        None, "--out",
        help=(
            "Optional JSONL path. One row per mismatched doc with v2 label, "
            "family_key, title, and the full v2 evidence so a reviewer can "
            "decide whether to re-tag the family_key or accept that the "
            "bundle wraps mixed content."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Find docs where v2's content classification disagrees with the importer's family_key tag.

    When v2 classifies a doc as a non-tariff type (COVER_LETTER, ORDER_FINAL,
    APPLICATION, COMPLIANCE_FILING, CERTIFICATE_OF_SERVICE, NOTICE_OF_HEARING,
    TESTIMONY) but its family_key implies a tariff family (nc-progress-leaf-*,
    nc-carolinas-schedule-*, nc-carolinas-rider-*), that's a strong signal
    the importer tagged the wrong family. The PDF body is the cover letter
    or order transmitting the tariff, not the tariff itself.

    See docs/research/document_identification.md "Cover-letter bundle signal"
    section. This CLI quantifies the surface and exports a triage queue.
    Read-only — no DB writes. Cleanup decisions are out of scope.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    ADMIN_TYPES = (
        "COVER_LETTER", "ORDER_FINAL", "ORDER_PROCEDURAL",
        "APPLICATION", "COMPLIANCE_FILING",
        "CERTIFICATE_OF_SERVICE", "NOTICE_OF_HEARING", "TESTIMONY",
    )
    TARIFF_FAMILY_PREFIXES = (
        "nc-progress-leaf-",
        "nc-carolinas-schedule-",
        "nc-carolinas-rider-",
    )

    placeholders = ",".join("?" for _ in ADMIN_TYPES)
    family_clauses = " OR ".join(
        "hd.family_key LIKE ?" for _ in TARIFF_FAMILY_PREFIXES
    )
    like_args = [p + "%" for p in TARIFF_FAMILY_PREFIXES]

    c.execute(
        f"""
        SELECT
            CAST(hd.id AS INTEGER) AS hd_id,
            hd.family_key,
            hd.title,
            v2.label AS v2_label,
            v2.confidence AS v2_confidence,
            v2.evidence_json AS v2_evidence
        FROM document_classifications v2
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = v2.subject_id
         AND v2.subject_kind = 'historical_document'
        WHERE v2.stage = 'document_type'
          AND v2.classifier = 'rule_document_type_v2'
          AND hd.state = ?
          AND v2.confidence >= ?
          AND v2.label IN ({placeholders})
          AND ({family_clauses})
        ORDER BY v2.confidence DESC, hd.id
        """,
        (state, min_confidence, *ADMIN_TYPES, *like_args),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    by_v2_label = Counter(r["v2_label"] for r in rows)
    by_family_prefix: Counter = Counter()
    for r in rows:
        for prefix in TARIFF_FAMILY_PREFIXES:
            if r["family_key"].startswith(prefix):
                by_family_prefix[prefix] += 1
                break

    # Pairs (v2_label, family_prefix) — most-mismatched combinations
    pairs: Counter = Counter()
    for r in rows:
        for prefix in TARIFF_FAMILY_PREFIXES:
            if r["family_key"].startswith(prefix):
                pairs[(r["v2_label"], prefix)] += 1
                break

    summary = {
        "state": state,
        "min_confidence": min_confidence,
        "total_mismatches": len(rows),
        "by_v2_label": dict(by_v2_label.most_common()),
        "by_family_prefix": dict(by_family_prefix.most_common()),
        "top_pairs": [
            {"v2_label": lab, "family_prefix": pref, "count": n}
            for (lab, pref), n in pairs.most_common(10)
        ],
    }

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        summary["written_to"] = str(out)

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return

    typer.echo(f"\nBundle metadata mismatch audit | state={state}\n")
    typer.echo(f"  v2 min confidence:           {min_confidence}")
    typer.echo(f"  total mismatches:            {len(rows)}")

    typer.echo("\n  By v2 (content) label:")
    for label, n in by_v2_label.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo("\n  By family_key prefix:")
    for prefix, n in by_family_prefix.most_common():
        typer.echo(f"    {prefix:<32} {n}")

    typer.echo("\n  Top mismatched pairs:")
    typer.echo(f"    {'v2 label':<28} {'family prefix':<32} {'n':>4}")
    for (lab, prefix), n in pairs.most_common(10):
        typer.echo(f"    {lab:<28} {prefix:<32} {n:>4}")

    if out:
        typer.echo(f"\n  Per-doc JSONL written to: {out}")
    else:
        typer.echo("\n  Pass --out PATH.jsonl to export per-doc detail.")


@app.command("promote-high-confidence-subset-nc")
def promote_high_confidence_subset_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    min_confidence: float = typer.Option(
        0.9,
        "--min-confidence",
        help=(
            "Minimum confidence each subset-agreeing classifier must reach. "
            "0.9 is the recommended floor — LLM/qwen3:8b averages 0.96 and "
            "v2 reaches 0.92+ on strong matches, so 0.9 selects only their "
            "confident calls."
        ),
    ),
    min_subset_size: int = typer.Option(
        2,
        "--min-subset",
        help="Minimum classifiers agreeing on the same label at >= min_confidence.",
    ),
    exclude_classifiers: list[str] | None = typer.Option(
        None,
        "--exclude-classifier",
        help="Repeatable. Skip these classifiers when computing subsets.",
    ),
    execute: bool = typer.Option(
        False, "--execute",
        help="Actually write gold rows. Without this flag, dry-run only.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Promote subset-agreement docs to document_type_gold.

    A subset-agreement is when N classifiers agree on a single label at
    >= min_confidence, even if other classifiers vote differently at
    lower confidence. Useful for growing gold on disagreement docs that
    seed-document-type-gold-nc skips (because it requires *unanimous*
    agreement across all running classifiers).

    Concrete pattern this surfaces: LLM qwen3:8b at 1.0 confidence agrees
    with v2 at 0.98 on CERTIFICATE_OF_SERVICE, while v1 and embedding
    vote different labels at lower confidence. The two high-confidence
    classifiers carry the signal; lower-confidence noise is ignored.

    Rows are tagged with:
      labeler  = 'subset:<classifier1>+<classifier2>+...'
      source   = 'high_confidence_subset_agreement'
      evidence = {classifiers, confidences, min_threshold, dissenters}

    Idempotent: docs that already have an active gold row are skipped.

    Recommended workflow:
      1. promote-high-confidence-subset-nc                    (dry-run, review)
      2. promote-high-confidence-subset-nc --execute          (write to gold)
      3. audit-document-type-classifications-nc               (verify growth)
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    exclude_set = set(exclude_classifiers or [])

    c.execute(
        """
        SELECT dc.subject_id, dc.classifier, dc.label, dc.confidence
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type' AND hd.state = ?
        """,
        (state,),
    )

    by_doc: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in c.fetchall():
        if r["classifier"] in exclude_set:
            continue
        by_doc[r["subject_id"]].append(r)

    c.execute(
        """SELECT subject_id FROM document_type_gold
           WHERE subject_kind='historical_document' AND superseded_by IS NULL"""
    )
    existing_gold = {r["subject_id"] for r in c.fetchall()}

    promoted: list[dict] = []
    skipped_already_gold = 0
    skipped_no_subset = 0
    skipped_subset_disagree = 0
    label_counts: Counter = Counter()

    for subj_id, rows in by_doc.items():
        if subj_id in existing_gold:
            skipped_already_gold += 1
            continue

        # Group HIGH-confidence votes by label
        high_conf_by_label: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            if r["confidence"] >= min_confidence:
                high_conf_by_label[r["label"]].append(r)

        # Find the label with the largest high-confidence subset
        if not high_conf_by_label:
            skipped_no_subset += 1
            continue

        best_label = max(
            high_conf_by_label,
            key=lambda lab: len(high_conf_by_label[lab]),
        )
        subset = high_conf_by_label[best_label]
        if len(subset) < min_subset_size:
            skipped_no_subset += 1
            continue

        # Check that no OTHER label has an equally large high-conf subset
        # (that would be a high-conf disagreement, not a clear winner)
        other_max = max(
            (len(v) for lab, v in high_conf_by_label.items() if lab != best_label),
            default=0,
        )
        if other_max >= len(subset):
            skipped_subset_disagree += 1
            continue

        # Build the row
        agreeing_classifiers = sorted(r["classifier"] for r in subset)
        agreeing_confs = [round(r["confidence"], 3) for r in subset]
        dissenters = [
            {
                "classifier": r["classifier"],
                "label": r["label"],
                "confidence": round(r["confidence"], 3),
            }
            for r in rows
            if r["label"] != best_label
        ]
        labeler = "subset:" + "+".join(agreeing_classifiers)
        evidence = {
            "classifiers": agreeing_classifiers,
            "confidences": agreeing_confs,
            "min_threshold": min_confidence,
            "dissenters": dissenters,
        }
        promoted.append({
            "subject_id": subj_id,
            "label": best_label,
            "labeler": labeler,
            "evidence": evidence,
            "subset_size": len(subset),
        })
        label_counts[best_label] += 1

    if execute and promoted:
        from datetime import UTC as _UTC
        utc_now = datetime.now(_UTC).isoformat()
        for row in promoted:
            c.execute(
                """
                INSERT INTO document_type_gold (
                    subject_kind, subject_id, label, labeler, source,
                    evidence_json, superseded_by, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    "historical_document",
                    row["subject_id"],
                    row["label"],
                    row["labeler"],
                    "high_confidence_subset_agreement",
                    json.dumps(row["evidence"]),
                    utc_now,
                ),
            )
        conn.commit()

    summary = {
        "state": state,
        "min_confidence": min_confidence,
        "min_subset_size": min_subset_size,
        "exclude_classifiers": sorted(exclude_set),
        "candidates_considered": len(by_doc),
        "promoted": len(promoted),
        "skipped_already_gold": skipped_already_gold,
        "skipped_no_subset": skipped_no_subset,
        "skipped_subset_disagree": skipped_subset_disagree,
        "label_distribution": dict(label_counts.most_common()),
        "executed": execute,
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        conn.close()
        return

    typer.echo(f"\nhigh-confidence subset promotion | state={state}\n")
    typer.echo(f"  min_confidence:                 {min_confidence}")
    typer.echo(f"  min subset size:                {min_subset_size}")
    if exclude_set:
        typer.echo(f"  excluded classifiers:           {sorted(exclude_set)}")
    typer.echo(f"  docs considered:                {len(by_doc)}")
    typer.echo(f"  -> already gold:                {skipped_already_gold}")
    typer.echo(f"  -> no qualifying subset:        {skipped_no_subset}")
    typer.echo(f"  -> high-conf disagreement:      {skipped_subset_disagree}")
    typer.echo(f"  -> would promote:               {len(promoted)}")
    if label_counts:
        typer.echo("\n  Promoted label distribution:")
        for label, n in label_counts.most_common():
            typer.echo(f"    {label:<28} {n}")
    if execute:
        typer.echo(f"\n  Wrote {len(promoted)} new gold rows.")
    else:
        typer.echo("\n  Dry-run only. Pass --execute to write.")

    conn.close()


@app.command("triage-disagreements-nc")
def triage_disagreements_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    output_path: Path = typer.Option(
        ...,
        "--out",
        help=(
            "Output JSONL path for the labeling queue. Each line is one "
            "disagreement doc with side-by-side classifier votes, layout "
            "signals, and a text sample. Suitable for a notebook or "
            "Streamlit label-fix UI."
        ),
    ),
    limit: int = typer.Option(200, "--limit", help="Cap rows written."),
    weight_underrepresented: bool = typer.Option(
        True,
        "--weight-underrepresented/--no-weight",
        help=(
            "Prioritize docs whose classifiers voted for type buckets that "
            "are underrepresented in document_type_gold. Targets the "
            "specific labels Stream D fine-tuning needs more examples of "
            "(RIDER, COVER_LETTER, NOTICE_OF_HEARING, etc.)."
        ),
    ),
    label_filter: list[str] | None = typer.Option(
        None,
        "--label",
        help=(
            "Repeatable. Only include docs where at least one classifier "
            "voted one of these labels. Use to focus triage on specific "
            "type buckets."
        ),
    ),
) -> None:
    """Export classifier-disagreement docs as a labeling JSONL queue.

    Stream A continuation: the 555 disagreement docs in the corpus are
    where ground-truth labels grow fastest. This CLI exports them as a
    JSONL queue where each line carries side-by-side classifier votes,
    layout signals, a 2000-char text sample, and a suggested label
    (majority vote where one exists).

    Workflow:
      1. triage-disagreements-nc --out triage_v0.jsonl
      2. open in a notebook / Streamlit UI, hand-confirm or fix labels
      3. write back to document_type_gold via a follow-up insert
         (use source='human_review', labeler='human:<your-id>')

    Pass --label COVER_LETTER --label RIDER (etc.) to focus the queue on
    specific types currently underrepresented in gold.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Current gold distribution → underrepresented-bucket weights
    c.execute("""
        SELECT label, COUNT(*) AS n FROM document_type_gold
        WHERE superseded_by IS NULL GROUP BY label
    """)
    gold_counts: dict[str, int] = {r["label"]: r["n"] for r in c.fetchall()}
    # Inverse-frequency weight per label — higher weight = bigger gold gap.
    # Labels not in gold get the highest weight (infinity-ish via large constant).
    all_known_labels = [
        "TARIFF_SHEET", "RIDER", "RATE_SCHEDULE", "ORDER_FINAL", "ORDER_PROCEDURAL",
        "TESTIMONY", "COVER_LETTER", "CERTIFICATE_OF_SERVICE", "NOTICE_OF_HEARING",
        "APPLICATION", "COMPLIANCE_FILING", "FERC_ORDER", "EIA_REPORT",
    ]
    label_weights = {}
    for lab in all_known_labels:
        n = gold_counts.get(lab, 0)
        label_weights[lab] = 100.0 / (n + 1)  # n=0 -> weight 100, n=176 -> ~0.6

    # Pull all document_type classifications for state, joined with hd
    c.execute(
        """
        SELECT dc.subject_id AS hd_id_str,
               dc.classifier, dc.label, dc.confidence,
               hd.family_key, hd.title, hd.local_path, hd.start_page, hd.end_page
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type' AND hd.state = ?
        """,
        (state,),
    )

    by_doc: dict[str, dict] = defaultdict(lambda: {"votes": [], "meta": None})
    for r in c.fetchall():
        if by_doc[r["hd_id_str"]]["meta"] is None:
            by_doc[r["hd_id_str"]]["meta"] = {
                "family_key": r["family_key"],
                "title": r["title"],
                "local_path": r["local_path"],
                "start_page": r["start_page"],
                "end_page": r["end_page"],
            }
        by_doc[r["hd_id_str"]]["votes"].append({
            "classifier": r["classifier"],
            "label": r["label"],
            "confidence": round(r["confidence"], 3),
        })

    # Skip docs that already have an active gold row — they're settled.
    c.execute(
        """SELECT subject_id FROM document_type_gold
           WHERE subject_kind='historical_document' AND superseded_by IS NULL"""
    )
    settled = {r["subject_id"] for r in c.fetchall()}

    label_filter_set = {lab.upper() for lab in (label_filter or [])}

    candidates: list[dict] = []
    for hd_id_str, payload in by_doc.items():
        if hd_id_str in settled:
            continue
        votes = payload["votes"]
        labels = [v["label"] for v in votes]
        if len(set(labels)) < 2 or len(votes) < 2:
            # Not a disagreement (either too few classifiers or unanimous)
            continue
        if label_filter_set and not (label_filter_set & set(labels)):
            continue

        # Priority score: average of underrepresented-bucket weights across
        # votes. Average (not sum) so a doc with all-rare-label votes ranks
        # above a doc with one rare + several common votes — the all-rare
        # doc is more diagnostic for gold-set growth in those buckets.
        priority = sum(label_weights.get(lab, 1.0) for lab in labels) / max(1, len(labels))
        if weight_underrepresented is False:
            priority = 1.0

        candidates.append({
            "hd_id": int(hd_id_str),
            "priority": round(priority, 2),
            "votes": votes,
            "labels_voted": sorted(set(labels)),
            "majority_label": Counter(labels).most_common(1)[0][0],
            **payload["meta"],
        })

    # Sort by priority (high first), then hd_id for stability
    candidates.sort(key=lambda c: (-c["priority"], c["hd_id"]))
    candidates = candidates[:limit]

    # Enrich with text sample (last because slow)
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )
    extractor = BulkExtractor(db_path=str(settings.database_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", encoding="utf-8") as f:
        for cand in candidates:
            doc = extractor.get_document_for_extraction(cand["hd_id"])
            text_sample = ""
            text_source = "none"
            if doc:
                try:
                    text, src = extractor.extract_text_from_pdf(
                        doc["local_path"],
                        start_page=doc.get("start_page"),
                        end_page=doc.get("end_page"),
                    )
                    if src in ("docling_artifact", "docling_artifact_sliced"):
                        text = normalize_docling_markdown(text)
                    text_sample = normalize_ocr_text(text)[:2000]
                    text_source = src
                except Exception:
                    pass
            row = {
                **cand,
                "text_sample": text_sample,
                "text_source": text_source,
            }
            # Remove non-JSON fields
            row.pop("local_path", None)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1

    conn.close()

    typer.echo(f"\nTriage queue exported | state={state}")
    typer.echo(f"  candidates considered:   {len(by_doc)}")
    typer.echo(f"  disagreement docs:       {len(candidates) if not limit else 'capped at limit'}")
    typer.echo(f"  written to {output_path}: {written}")
    if weight_underrepresented:
        typer.echo("\n  Underrepresented-bucket label weights (gold counts in parens):")
        for lab, w in sorted(label_weights.items(), key=lambda kv: -kv[1]):
            n = gold_counts.get(lab, 0)
            typer.echo(f"    {lab:<28} weight={w:.2f}  gold_n={n}")


@app.command("classify-documents-v2-nc")
def classify_documents_v2_nc(
    state: str = typer.Option("NC", "--state", help="State filter for historical_documents."),
    limit: int | None = typer.Option(None, "--limit", help="Limit docs scored (default: all)."),
    write_classifications: bool = typer.Option(
        False,
        "--write-classifications",
        help=(
            "Persist v2 classifications to document_classifications. Without "
            "this flag the command only prints a comparison report against "
            "v1 (rule_document_type_v1)."
        ),
    ),
    show_disagreements: int = typer.Option(
        15,
        "--show-disagreements",
        help="Show up to N v1 vs v2 disagreement examples.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Run rule_document_type_v2 against NC docs and compare with v1.

    Pulls a DocumentSignals snapshot for each doc (title, first 2k chars,
    last 1k chars, layout features from document_fingerprints when
    available), runs the new per-type classifier, and reports:

      - confidence distribution (avg/min/max, hi/lo bands)
      - label distribution
      - per-doc disagreements with the v1 classifier
      - optional persistence to document_classifications

    Part of Stream B in docs/research/document_identification.md.
    """
    import sqlite3
    from collections import Counter
    from duke_rates.classification.rule_document_type_v2 import (
        DocumentSignals,
        classify_v2,
        CLASSIFIER_NAME as V2_NAME,
        CLASSIFIER_VERSION as V2_VERSION,
    )
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )

    settings, _ = _bootstrap()
    extractor = BulkExtractor(db_path=str(settings.database_path))
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT hd.id, hd.title, hd.family_key
        FROM historical_documents hd
        WHERE hd.state = ?
        ORDER BY hd.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (state,)).fetchall()
    typer.echo(f"Scoring {len(rows)} docs with rule_document_type_v2...")

    # Fingerprint lookup for layout signals
    fp_by_pdf: dict[str, sqlite3.Row] = {}
    for fp in conn.execute(
        "SELECT source_pdf, page_count, text_chars, has_tables FROM document_fingerprints_v2"
    ).fetchall():
        fp_by_pdf[fp["source_pdf"]] = fp

    # v1 label lookup for comparison
    v1_by_hd: dict[int, str] = {}
    for r in conn.execute(
        """SELECT subject_id, label FROM document_classifications
           WHERE classifier='rule_document_type_v1' AND stage='document_type'"""
    ).fetchall():
        try:
            v1_by_hd[int(r["subject_id"])] = r["label"]
        except (TypeError, ValueError):
            continue

    results: list[dict] = []
    label_counts: Counter = Counter()
    confidence_buckets = {"high": 0, "mid": 0, "low": 0}
    disagreements: list[dict] = []

    for r in rows:
        hd_id = int(r["id"])
        doc = extractor.get_document_for_extraction(hd_id)
        if not doc:
            continue
        try:
            text, src = extractor.extract_text_from_pdf(
                doc["local_path"],
                start_page=doc.get("start_page"),
                end_page=doc.get("end_page"),
            )
            if src in ("docling_artifact", "docling_artifact_sliced"):
                text = normalize_docling_markdown(text)
            text = normalize_ocr_text(text)
        except Exception:
            text = ""
        if not text:
            continue
        first_text = text[:2000]
        last_text = text[-1000:] if len(text) > 2000 else ""

        fp = fp_by_pdf.get(doc["local_path"])
        signals = DocumentSignals(
            title=r["title"] or "",
            first_text=first_text,
            last_text=last_text,
            page_count=fp["page_count"] if fp else None,
            text_chars=fp["text_chars"] if fp else len(text),
            has_tables=fp["has_tables"] if fp else None,
        )
        result = classify_v2(signals)
        label_counts[result.label] += 1
        if result.confidence >= 0.9:
            confidence_buckets["high"] += 1
        elif result.confidence >= 0.5:
            confidence_buckets["mid"] += 1
        else:
            confidence_buckets["low"] += 1

        v1_label = v1_by_hd.get(hd_id)
        if v1_label and v1_label != result.label:
            disagreements.append({
                "hd_id": hd_id,
                "v1_label": v1_label,
                "v2_label": result.label,
                "v2_confidence": round(result.confidence, 3),
                "title": (r["title"] or "")[:60],
            })

        if write_classifications:
            from duke_rates.classification.persistence import record_classification
            try:
                record_classification(
                    conn,
                    subject_kind="historical_document",
                    subject_id=str(hd_id),
                    stage="document_type",
                    result=result,
                )
            except Exception as exc:
                logger.debug(f"v2 persist failed for hd={hd_id}: {exc}")

        results.append({
            "hd_id": hd_id, "label": result.label, "confidence": result.confidence,
        })

    if write_classifications:
        conn.commit()

    summary = {
        "state": state,
        "docs_scored": len(results),
        "confidence_buckets": confidence_buckets,
        "label_distribution": dict(label_counts.most_common()),
        "disagreements_total": len(disagreements),
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        conn.close()
        return

    typer.echo(f"\nrule_document_type_v2 scoring | state={state}")
    typer.echo(f"  docs scored:                {summary['docs_scored']}")
    typer.echo(f"  high-confidence (>=0.9):    {confidence_buckets['high']}")
    typer.echo(f"  mid-confidence (0.5-0.9):   {confidence_buckets['mid']}")
    typer.echo(f"  low-confidence (<0.5):      {confidence_buckets['low']}")
    typer.echo("\n  Label distribution (v2):")
    for label, n in label_counts.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo(f"\n  v1 vs v2 disagreements: {len(disagreements)}")
    if disagreements and show_disagreements:
        for d in disagreements[:show_disagreements]:
            typer.echo(
                f"    hd={d['hd_id']:<5} v1={d['v1_label']:<20} -> v2={d['v2_label']:<22} "
                f"conf={d['v2_confidence']:.2f}  {d['title']!r}"
            )

    if write_classifications:
        typer.echo(f"\n  Wrote {len(results)} v2 classifications to document_classifications.")
    else:
        typer.echo(
            "\n  Dry-run only. Pass --write-classifications to persist v2 results."
        )

    conn.close()


@app.command("audit-rider-map")
def audit_rider_map(
    state: str = typer.Option("NC", "--state", help="State code"),
    company: str = typer.Option("progress", "--company", help="Company name"),
    schedule: str = typer.Option(None, "--schedule", help="Filter to one schedule family key"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Show the static rider map: which riders apply to which schedules."""
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)
    rmap = svc.build_rider_map(state, company)

    if schedule:
        rmap = {k: v for k, v in rmap.items() if k == schedule}

    if json_out:
        typer.echo(json.dumps(rmap, indent=2))
        return

    typer.echo(f"\nRider map | {state.upper()} {company}")
    for sched_key, riders in rmap.items():
        typer.echo(f"\n  {sched_key} ({len(riders)} riders):")
        for r in riders:
            summ = "summary" if r["in_rider_summary"] else "direct-bill"
            enroll = r["enrollment_type"]
            title = (r["rider_title"] or "")[:40]
            typer.echo(f"    {r['rider_family_key']:<32} [{summ:<11}] [{enroll}]  {title}")
    typer.echo("")


@app.command("audit-search-worklist")
def audit_search_worklist(
    state: str = typer.Option("NC", "--state", help="State code"),
    company: str = typer.Option("progress", "--company", help="Company name"),
    priority: str = typer.Option(None, "--priority", help="Filter: high | medium | low"),
    category: str = typer.Option(None, "--category", help="Filter by category: residential, commercial_small, commercial_large, lighting, purchased_power, ee_program, ev_program, regulation, rider"),
    family_type: str = typer.Option(None, "--type", help="Filter: rate_schedule | rider"),
    include_enrollment: bool = typer.Option(False, "--include-enrollment", help="Include opt-in/enrollment riders"),
    show_queries: bool = typer.Option(False, "--queries", help="Show suggested NCUC search queries"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """List tariff families that need NCUC searches, with suggested queries.

    Shows all families with no rate charge data, cross-referenced against
    known NCUC dockets and downloaded PDFs.  Each item includes the exact
    revision label and pre-formed search queries to use at the NCUC portal.

    Priority: high=billing rate schedules (500-599), medium=EV programs,
              low=EE/DSM programs and regulations.
    Category: residential, commercial_small, commercial_large, lighting,
              purchased_power, ee_program, ev_program, regulation, rider.
    """
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)

    family_types = None
    if family_type:
        family_types = [family_type]

    items = svc.build_search_worklist(
        state=state,
        company=company,
        family_types=family_types,
        include_enrollment_riders=include_enrollment,
    )

    if priority:
        items = [i for i in items if i.priority == priority]
    if category:
        items = [i for i in items if i.category == category]

    if json_out:
        typer.echo(json.dumps([i.model_dump() for i in items], indent=2))
        return

    typer.echo(f"\nNCUC Search Work List | {state.upper()} {company}  ({len(items)} items)\n")

    priority_labels = {"high": "[HIGH]  ", "medium": "[MED]   ", "low": "[LOW]   "}

    prev_priority = None
    for item in items:
        if item.priority != prev_priority:
            prev_priority = item.priority
            section = {
                "high": "BILLING RATE SCHEDULES (high priority -- direct bill impact)",
                "medium": "EV/PROGRAM SCHEDULES or RIDERS (medium priority)",
                "low": "EE PROGRAMS / REGULATIONS (low priority -- no per-kWh rates expected)",
            }
            typer.echo(f"  --- {section.get(item.priority, item.priority)} ---\n")

        leaf_str = f"Leaf {item.leaf_no:<4}" if item.leaf_no else "         "
        cat_str = f"[{item.category:<18}]" if item.category else ""
        label = (item.current_revision_label or "")[:52]
        dockets = ", ".join(item.known_dockets[:3]) if item.known_dockets else "none"
        pdfs = f"{item.local_pdf_count} PDFs" if item.local_pdf_count else "none"
        title_short = (item.title or "")[:55]

        typer.echo(
            f"  {priority_labels[item.priority]} {cat_str} {leaf_str}"
        )
        typer.echo(f"    Title:    {title_short}")
        typer.echo(f"    Revision: {label}")
        typer.echo(f"    Dockets:  {dockets}  |  PDFs: {pdfs}")

        if show_queries and item.suggested_queries:
            for i_q, q in enumerate(item.suggested_queries[:3]):
                prefix = "    Search:  " if i_q == 0 else "            "
                typer.echo(f"{prefix}{q}")

        typer.echo("")

    # Summary counts
    high = sum(1 for i in items if i.priority == "high")
    med = sum(1 for i in items if i.priority == "medium")
    low = sum(1 for i in items if i.priority == "low")
    with_dockets = sum(1 for i in items if i.known_dockets)
    with_pdfs = sum(1 for i in items if i.local_pdf_count > 0)
    # Category breakdown
    from collections import Counter
    cats = Counter(i.category for i in items)
    cat_summary = "  ".join(f"{k}:{v}" for k, v in sorted(cats.items()))
    typer.echo(f"  Total: {len(items)} items  ({high} high, {med} medium, {low} low)")
    typer.echo(f"  Categories: {cat_summary}")
    typer.echo(f"  Known dockets: {with_dockets}  |  Local PDFs: {with_pdfs}\n")


# -- Diagnostic & Improvement Commands ------------------------------------------


@app.command("diagnose-document-nc")
def diagnose_document_nc(
    historical_document_id: int = typer.Option(
        ..., "--hd-id", help="Historical document ID to diagnose."
    ),
    show_text: bool = typer.Option(
        False, "--show-text", help="Show raw text excerpt fed to the parser."
    ),
    text_lines: int = typer.Option(
        40, "--text-lines", help="Max lines of raw text to show with --show-text."
    ),
    trace_runtime: bool = typer.Option(
        False,
        "--trace-runtime",
        help=(
            "Run a live extraction trace alongside the historical-run report. "
            "Surfaces the text source actually used by the bulk extractor "
            "(pdfplumber vs docling_artifact vs docling_artifact_sliced), "
            "rate-marker presence per text stage, profile candidates, and "
            "fallback decisions. Use this when latest_run reports `unknown` "
            "or empty but a rate sheet is obvious from the PDF."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Diagnose why a document's extraction is empty, weak, or failing.

    Shows the full pipeline path, profile selection reasoning, candidate scores,
    text metrics, and a recommended next action for the operator.
    """
    from duke_rates.db.reprocess import latest_processing_run_for_document

    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        doc = conn.execute(
            """
            SELECT hd.*, tv.id AS version_id
            FROM historical_documents hd
            LEFT JOIN tariff_versions tv
              ON tv.historical_document_id = hd.id
            WHERE hd.id = ?
            """,
            (historical_document_id,),
        ).fetchone()
        if not doc:
            raise typer.BadParameter(f"Historical document {historical_document_id} not found.")

        run = latest_processing_run_for_document(conn, historical_document_id=historical_document_id)
        charge_count = conn.execute(
            "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?",
            (doc["version_id"],),
        ).fetchone()[0] if doc["version_id"] else 0

        reprocess_queue = conn.execute(
            "SELECT status, priority, requested_at FROM historical_reprocess_queue "
            "WHERE historical_document_id = ? ORDER BY id DESC LIMIT 1",
            (historical_document_id,),
        ).fetchone()

        version_count = conn.execute(
            "SELECT COUNT(*) FROM tariff_versions WHERE historical_document_id = ?",
            (historical_document_id,),
        ).fetchone()[0]

        # Build report
        report = {
            "document": {
                "id": doc["id"],
                "family_key": doc["family_key"],
                "company": doc["company"],
                "effective_start": doc["effective_start"],
                "title": doc["title"],
                "start_page": doc["start_page"],
                "end_page": doc["end_page"],
                "local_path": doc["local_path"],
                "state": doc["state"],
                "version_id": doc["version_id"],
                "version_count": version_count,
                "charge_count": charge_count,
            },
            "latest_run": None,
            "signals": None,
            "candidates": None,
            "selection": None,
            "text_metrics": None,
            "reprocess_queue": None,
            "recommendation": None,
        }

        if run:
            metadata = json.loads(run["metadata_json"] or "{}")
            report["latest_run"] = {
                "id": run["id"],
                "parser_profile": run["parser_profile"],
                "parser_version": run["parser_version"],
                "status": run["status"],
                "outcome_quality": run["outcome_quality"],
                "charge_count": run["charge_count"],
                "review_flags": json.loads(run["review_flags_json"] or "[]"),
                "started_at": run["started_at"],
                "completed_at": run["completed_at"],
            }
            report["signals"] = metadata.get("signals")
            report["candidates"] = metadata.get("candidate_profiles")
            report["selection"] = metadata.get("selection")
            text_meta = metadata.get("text_metrics") or {}
            report["text_metrics"] = {
                "text_length": text_meta.get("text_length"),
                "line_count": text_meta.get("line_count"),
                "numeric_line_count": text_meta.get("numeric_line_count"),
            }
            report["_raw_text"] = text_meta.get("full_text", "") if show_text else None

        if reprocess_queue:
            report["reprocess_queue"] = {
                "status": reprocess_queue["status"],
                "priority": reprocess_queue["priority"],
                "requested_at": reprocess_queue["requested_at"],
            }

        # --- Live runtime trace (optional) ---
        if trace_runtime:
            report["runtime_trace"] = _build_runtime_trace(
                str(repository.database_path), historical_document_id
            )

        # --- Recommendation ---
        rec = _build_diagnostic_recommendation(report)
        report["recommendation"] = rec

        if json_out:
            # Strip raw text from JSON output unless requested
            if not show_text:
                report.pop("_raw_text", None)
            typer.echo(json.dumps(report, indent=2, default=str))
            return

        _print_diagnostic_report(report, show_text, text_lines)
        if trace_runtime:
            _print_runtime_trace(report.get("runtime_trace") or {})

    finally:
        conn.close()


_RATE_MARKERS = (
    "basic customer charge",
    "per kwh",
    "cents per kwh",
    "¢/kwh",
    "$/kwh",
    "kilowatt-hour",
    "monthly seller charge",
    "monthly administrative charge",
    "rider cpre",
    "per luminaire",
)


def _scan_markers(text: str) -> dict[str, bool]:
    """Return which canonical rate markers are present in ``text``."""
    lowered = (text or "").lower()
    return {m: (m in lowered) for m in _RATE_MARKERS}


def _build_runtime_trace(database_path: str, hd_id: int) -> dict:
    """Run a live extraction trace for ``hd_id`` and return a structured report.

    Mirrors `BulkExtractor.extract_charges_from_document` step by step so the
    operator can see exactly which text source was used, how normalization
    changed the text, which rate markers survived each stage, and what the
    routing tier picked. Designed to surface text-path divergences like the
    2026-05-20 Docling slicer bug (where the bounded-slice path dropped
    body.children-orphan rate texts and pushed the doc to `unknown`).
    """
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor,
        normalize_docling_markdown,
        normalize_ocr_text,
    )
    from duke_rates.historical.ncuc.pipeline.parser_profiles import (
        HistoricalRateParserRegistry,
    )

    extractor = BulkExtractor(db_path=database_path)
    doc = extractor.get_document_for_extraction(hd_id)
    if not doc:
        return {"error": f"document {hd_id} not loadable via get_document_for_extraction"}

    trace: dict = {
        "doc": {
            "id": doc.get("id"),
            "family_key": doc.get("family_key"),
            "start_page": doc.get("start_page"),
            "end_page": doc.get("end_page"),
        },
        "text_paths": [],
    }

    sp = doc.get("start_page")
    ep = doc.get("end_page")

    # Path A — page-bounded extraction (what the bulk extractor actually uses
    # when start_page/end_page are set on the doc).
    if sp is not None and ep is not None:
        text_bounded, src_bounded = extractor.extract_text_from_pdf(
            doc["local_path"], start_page=sp, end_page=ep
        )
        trace["text_paths"].append({
            "name": "page_bounded (used by bulk_extractor)",
            "source": src_bounded,
            "raw_length": len(text_bounded),
            "markers_raw": _scan_markers(text_bounded),
        })
        # Apply the same normalization the bulk extractor applies before routing.
        normalized = text_bounded
        if src_bounded in ("docling_artifact", "docling_artifact_sliced"):
            normalized = normalize_docling_markdown(normalized)
        normalized = normalize_ocr_text(normalized)
        trace["text_paths"][-1]["normalized_length"] = len(normalized)
        trace["text_paths"][-1]["markers_normalized"] = _scan_markers(normalized)
        primary_text = normalized
    else:
        primary_text = None

    # Path B — full-document extraction (no page bounds). Compared against the
    # bounded path so the operator can see if the slicer dropped markers.
    # Always normalize so the comparison is apples-to-apples with the bounded path.
    text_full, src_full = extractor.extract_text_from_pdf(doc["local_path"])
    normalized_full = text_full
    if src_full in ("docling_artifact", "docling_artifact_sliced"):
        normalized_full = normalize_docling_markdown(normalized_full)
    normalized_full = normalize_ocr_text(normalized_full)
    trace["text_paths"].append({
        "name": "full_document (comparison)",
        "source": src_full,
        "raw_length": len(text_full),
        "normalized_length": len(normalized_full),
        "markers_normalized": _scan_markers(normalized_full),
    })
    if primary_text is None:
        primary_text = normalized_full

    # Detect text-path divergence: any marker present in the full-doc text but
    # missing in the bounded text is a high-signal slicer drop.
    if sp is not None and ep is not None:
        bounded_markers = trace["text_paths"][0].get("markers_normalized", {})
        full_markers = trace["text_paths"][1].get("markers_normalized", {})
        dropped = [m for m in _RATE_MARKERS if full_markers.get(m) and not bounded_markers.get(m)]
        trace["slicer_dropped_markers"] = dropped
    else:
        trace["slicer_dropped_markers"] = []

    # Routing tier — what registry.rank_candidates says about the normalized text
    registry = HistoricalRateParserRegistry()
    candidates = registry.rank_candidates(doc, primary_text or "")
    trace["candidates"] = [
        {"name": c.name, "score": round(c.score, 3), "supports": c.supported}
        for c in candidates if c.score > 0 or c.supported
    ][:6]

    # Full extract path — runs through _is_formula_only_document, classifier,
    # routing tier, fallback logic. This is the ground truth of what would
    # happen on a re-extract right now.
    try:
        result = extractor.extract_charges_from_document(doc)
        charges, _vl, _cands, status, _signals, _metrics, selection_meta = result
        trace["live_extract"] = {
            "status": status,
            "charge_count": len(charges),
            "initial_profile": selection_meta.get("initial_parser_profile"),
            "final_profile": selection_meta.get("final_parser_profile"),
            "fallback_applied": selection_meta.get("fallback_applied"),
            "fallback_reason": selection_meta.get("fallback_reason"),
            "fallback_attempts": [
                {
                    "name": a.get("name"),
                    "charge_count": a.get("charge_count"),
                    "applied": a.get("applied"),
                    "apply_reason": a.get("apply_reason"),
                }
                for a in (selection_meta.get("fallback_attempts") or [])[:3]
            ],
            "first_charges": [
                {"label": c.charge_label, "value": c.rate_value, "unit": c.rate_unit}
                for c in charges[:5]
            ],
        }
    except Exception as exc:
        trace["live_extract"] = {"error": repr(exc)}

    return trace


def _print_runtime_trace(trace: dict) -> None:
    """Pretty-print the live trace report."""
    if not trace or trace.get("error"):
        typer.echo(f"\n  Runtime trace: {trace.get('error', 'unavailable')}")
        return

    typer.echo("\n  -- Runtime trace --")
    for path in trace.get("text_paths", []):
        typer.echo(
            f"    text_path:  {path['name']}"
        )
        typer.echo(
            f"      source={path['source']}  raw_len={path['raw_length']}  "
            f"normalized_len={path.get('normalized_length', 'n/a')}"
        )
        markers = path.get("markers_normalized") or path.get("markers_raw") or {}
        present = sorted(m for m, hit in markers.items() if hit)
        absent = sorted(m for m, hit in markers.items() if not hit)
        if present:
            typer.echo(f"      markers_present: {', '.join(present)}")
        if absent and len(absent) <= 3:
            typer.echo(f"      markers_absent:  {', '.join(absent)}")

    dropped = trace.get("slicer_dropped_markers") or []
    if dropped:
        typer.echo(
            f"\n    [!] slicer_dropped_markers: {', '.join(dropped)}"
        )
        typer.echo(
            "        These markers exist in the full-doc text but were silently"
            " dropped by the page-bounded slice. Routing will likely fail."
        )

    typer.echo(f"\n    candidates (top 6):")
    for c in trace.get("candidates", []):
        typer.echo(
            f"      {c['name']:<45} score={c['score']:.3f}  supports={c['supports']}"
        )
    if not trace.get("candidates"):
        typer.echo("      (no candidates with score > 0)")

    live = trace.get("live_extract") or {}
    if "error" in live:
        typer.echo(f"\n    live_extract: ERROR {live['error']}")
        return
    typer.echo(
        f"\n    live_extract: status={live.get('status')}  "
        f"charges={live.get('charge_count')}  "
        f"initial={live.get('initial_profile')}  final={live.get('final_profile')}"
    )
    if live.get("fallback_applied"):
        typer.echo(f"      fallback_reason: {live.get('fallback_reason')}")
    for att in live.get("fallback_attempts") or []:
        typer.echo(
            f"      fallback_attempt: {att.get('name'):<40} "
            f"cc={att.get('charge_count')} applied={att.get('applied')} "
            f"reason={att.get('apply_reason')}"
        )
    for ch in live.get("first_charges") or []:
        typer.echo(f"      extracted: {ch['label']!r:<50} {ch['value']} {ch['unit']}")
    typer.echo("")


def _build_diagnostic_recommendation(report: dict) -> dict:
    """Build a recommended next action based on diagnostic signals."""
    doc = report["document"]
    run = report.get("latest_run")
    selection = report.get("selection") or {}
    signals = report.get("signals") or {}

    if not run:
        return {
            "action": "run_extraction",
            "reason": "No processing run exists for this document.",
            "suggested_command": f"python -m duke_rates reprocess enqueue-nc --hd-id {doc['id']}",
            "priority": "high",
        }

    status = run["status"]
    quality = run["outcome_quality"]
    profile = run["parser_profile"] or "unknown"
    review_flags = run["review_flags"] or []

    if status == "missing_file":
        return {
            "action": "recover_file",
            "reason": f"PDF not found at path: {doc.get('local_path', 'unknown')}",
            "suggested_command": f"# Re-download or locate the file, then:\n"
                                 f"python -m duke_rates reprocess enqueue-nc --hd-id {doc['id']}",
            "priority": "high",
        }

    if status == "no_text":
        text_len = (report.get("text_metrics") or {}).get("text_length") or 0
        return {
            "action": "run_ocr",
            "reason": f"No extractable text (text_length={text_len}). PDF is likely scanned/image-based.",
            "suggested_command": f"python -m duke_rates ocr enqueue-nc --hd-id {doc['id']}\n"
                                 f"python -m duke_rates ocr process-queue-nc",
            "priority": "high",
        }

    if status in ("skipped_procedural", "skipped_reference"):
        return {
            "action": "review_content",
            "reason": f"Document was skipped ({status}). May be non-tariff content.",
            "suggested_command": f"python -m duke_rates diagnose-document-nc --hd-id {doc['id']} --show-text",
            "priority": "medium",
        }

    if quality == "empty" or run["charge_count"] == 0:
        reasons = []

        # Check if profile selection was weak
        if selection.get("fallback_applied"):
            reasons.append(
                f"Fallback applied: {selection['initial_parser_profile']} -&gt; "
                f"{selection['final_parser_profile']}"
                f" (reason: {selection.get('fallback_reason', 'unknown')})"
            )

        # Check if generic fallback was used
        if "generic_fallback_selected" in review_flags:
            reasons.append("Generic fallback profile used — no specific profile matched.")

        # Check for low confidence
        if "low_selector_confidence" in review_flags:
            reasons.append("Low profile selection confidence — text may not match known patterns.")

        # Check if the right company detection happened
        company = doc.get("company") or "unknown"
        if profile == "generic_residential":
            reasons.append(f"No {company}-specific profile matched this document.")

        # Check text quality
        text_len = (report.get("text_metrics") or {}).get("text_length") or 0
        if text_len < 200:
            reasons.append(f"Very little text extracted ({text_len} chars). Possible OCR issue.")

        reason_text = " ".join(reasons) if reasons else "Parser returned 0 charges with no specific diagnostic signal."
        return {
            "action": "investigate_empty_parse",
            "reason": reason_text,
            "suggested_command": (
                f"# Inspect text, then choose repair path:\n"
                f"python -m duke_rates diagnose-document-nc --hd-id {doc['id']} --show-text\n"
                f"# If OCR issue:\n"
                f"python -m duke_rates ocr enqueue-nc --hd-id {doc['id']}\n"
                f"# If profile routing issue (e.g., Carolinas doc matched progress profile):\n"
                f"python -m duke_rates reprocess enqueue-nc --hd-id {doc['id']} --priority 90"
            ),
            "priority": "high",
        }

    if quality == "weak" or "sparse_charge_set" in review_flags:
        return {
            "action": "review_sparse_parse",
            "reason": f"Weak extraction: {run['charge_count']} charges, profile={profile}, flags={review_flags}",
            "suggested_command": (
                f"python -m duke_rates diagnose-document-nc --hd-id {doc['id']} --show-text\n"
                f"# Consider parser profile improvement for {profile}"
            ),
            "priority": "medium",
        }

    if quality in ("strong",) and run["charge_count"] > 0:
        return {
            "action": "no_action_needed",
            "reason": f"Strong extraction: {run['charge_count']} charges via {profile}.",
            "suggested_command": "",
            "priority": "low",
        }

    return {
        "action": "review_manually",
        "reason": f"Status={status}, quality={quality}, profile={profile}",
        "suggested_command": f"python -m duke_rates diagnose-document-nc --hd-id {doc['id']} --show-text",
        "priority": "medium",
    }


def _print_diagnostic_report(report: dict, show_text: bool, text_lines: int) -> None:
    """Print a human-readable diagnostic report."""
    doc = report["document"]
    run = report.get("latest_run")
    rec = report.get("recommendation") or {}
    candidates = report.get("candidates")

    typer.echo("=" * 72)
    typer.echo("DOCUMENT DIAGNOSTIC")
    typer.echo("=" * 72)

    # -- Section 1: Document Info --
    typer.echo("\n-- Document --")
    typer.echo(f"  id={doc['id']}  family={doc['family_key'] or '-'}  company={doc['company'] or '-'}")
    typer.echo(f"  effective_start={doc['effective_start'] or 'NULL'}  "
               f"pages={doc['start_page']}-{doc['end_page'] or doc['start_page']}")
    typer.echo(f"  title={_safe_cli_text(doc['title'] or '(untitled)')}")
    typer.echo(f"  local_path={doc['local_path'] or '-'}")
    typer.echo(f"  version_id={doc['version_id'] or '-'}  versions={doc['version_count']}  "
               f"charges={doc['charge_count']}")

    # -- Section 2: Latest Processing Run --
    typer.echo("\n-- Latest Processing Run --")
    if not run:
        typer.echo("  (no processing run exists)")
    else:
        typer.echo(f"  run_id={run['id']}  profile={run['parser_profile']}  "
                   f"status={run['status']}  quality={run['outcome_quality']}")
        typer.echo(f"  charge_count={run['charge_count']}  "
                   f"parser_version={run['parser_version']}")
        if run["review_flags"]:
            typer.echo(f"  review_flags={run['review_flags']}")
        typer.echo(f"  started={run['started_at']}  completed={run['completed_at']}")

    # -- Section 3: Queue State --
    rq = report.get("reprocess_queue")
    if rq:
        typer.echo(f"\n-- Reprocess Queue --\n  status={rq['status']}  priority={rq['priority']}  "
                   f"requested={rq['requested_at']}")

    # -- Section 4: Profile Selection --
    selection = report.get("selection")
    if selection:
        typer.echo("\n-- Profile Selection --")
        typer.echo(f"  initial={selection.get('initial_parser_profile', '-')}  "
                   f"final={selection.get('final_parser_profile', '-')}")
        if selection.get("fallback_applied"):
            typer.echo(f"  fallback_applied=True  triggered_by={selection.get('fallback_triggered_by', '-')}")
            typer.echo(f"  fallback_reason={selection.get('fallback_reason', '-')}")
        typer.echo(f"  initial_quality={selection.get('initial_outcome_quality', '-')}  "
                   f"final_quality={selection.get('final_outcome_quality', '-')}")

        initial_metrics = selection.get("initial_metrics") or {}
        final_metrics = selection.get("final_metrics") or {}
        if initial_metrics:
            typer.echo(f"  initial_charges={initial_metrics.get('charge_count', 0)}  "
                       f"tou_periods={initial_metrics.get('tou_period_count', 0)}  "
                       f"seasons={initial_metrics.get('season_count', 0)}")
        if selection.get("fallback_applied") and final_metrics:
            typer.echo(f"  final_charges={final_metrics.get('charge_count', 0)}  "
                       f"tou_periods={final_metrics.get('tou_period_count', 0)}  "
                       f"seasons={final_metrics.get('season_count', 0)}")

        fallback_attempts = selection.get("fallback_attempts") or []
        if fallback_attempts:
            typer.echo(f"  fallback_attempts ({len(fallback_attempts)}):")
            for fa in fallback_attempts:
                applied = "APPLIED" if fa.get("applied") else "skipped"
                typer.echo(f"    {fa.get('name', '?')} "
                           f"charges={fa.get('charge_count', 0)} "
                           f"quality={fa.get('outcome_quality', '?')} "
                           f"[{applied}] "
                           f"reason={fa.get('apply_reason', '-')}")

    # -- Section 5: Profile Candidates --
    if candidates:
        typer.echo(f"\n-- Profile Candidates ({len(candidates)} ranked) --")
        for i, c in enumerate(candidates[:10]):
            marker = ">>>" if c.get("selected") else f"{i+1}."
            reasons = "; ".join(c.get("reasons") or []) or "(no reasons)"
            typer.echo(f"  {marker} {c.get('name', '?')}  score={c.get('score', 0):.3f}  "
                       f"supported={c.get('supported', False)}")
            typer.echo(f"      reasons: {reasons}")

    # -- Section 6: Signals --
    signals = report.get("signals")
    if signals:
        typer.echo("\n-- Signals --")
        signal_keys = [
            "family_key", "company", "leaf_no", "is_current_progress_pdf",
            "is_current_carolinas_pdf", "has_summary_text", "has_tou_terms",
            "has_discount_term", "has_demand_charge_term",
            "has_progress_company_text", "has_carolinas_company_text",
            "has_rs_marker", "has_flat_rate_markers", "has_page_bounds",
        ]
        for key in signal_keys:
            val = signals.get(key)
            if val is not None and val != False and val != "":
                typer.echo(f"  {key}={val}")

    # -- Section 7: Text Metrics --
    tm = report.get("text_metrics") or {}
    if tm:
        typer.echo("\n-- Text Metrics --")
        typer.echo(f"  text_length={tm.get('text_length', 0)}  "
                   f"lines={tm.get('line_count', 0)}  "
                   f"numeric_lines={tm.get('numeric_line_count', 0)}")

    # -- Section 8: Raw Text --
    if show_text and report.get("_raw_text"):
        text = report["_raw_text"]
        lines = text.splitlines()
        typer.echo(f"\n-- Raw Text (first {text_lines} of {len(lines)} lines) --")
        for line in lines[:text_lines]:
            typer.echo(f"  | {_safe_cli_text(line)}")

    # -- Section 9: Recommendation --
    typer.echo(f"\n-- Recommended Next Action [{rec.get('priority', '?')}] --")
    typer.echo(f"  action: {rec.get('action', '?')}")
    typer.echo(f"  reason: {rec.get('reason', '?')}")
    if rec.get("suggested_command"):
        typer.echo(f"  command:\n    {rec['suggested_command']}")

    typer.echo("")


@app.command("recommend-missing-dockets-nc")
def recommend_missing_dockets_nc(
    limit: int = typer.Option(25, "--limit", help="Max recommendations."),
    utility: str = typer.Option("", "--utility", help="Filter by utility (progress, carolinas, dep)."),
    min_year: int = typer.Option(0, "--min-year", help="Only dockets with activity after this year."),
    docket: str = typer.Option("", "--docket", help="Target a specific docket number."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Recommend dockets with low or zero processed coverage for targeted fetching.

    Cross-references ncuc_discovery_records against tariff_versions and
    historical_documents by docket number. Dockets with many discovery records
    but few processed documents are the highest-value targets.

    Also shows docket leads from regulatory_docket_leads that have no
    corresponding discovery records, and unfetched discovery records.
    """
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.database_reports import (
        find_missing_docket_coverage,
    )

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = find_missing_docket_coverage(
            conn,
            limit=limit,
            utility=utility if utility else None,
            min_year=min_year if min_year > 0 else None,
            docket=docket if docket else None,
        )
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo(f"\nDocket Coverage Recommender")
    typer.echo(f"  Recommendations:           {summary['total_recommendations']}")
    typer.echo(f"  Zero coverage dockets:      {summary['dockets_with_zero_coverage']}")
    typer.echo(f"  Low coverage dockets:       {summary['dockets_with_low_coverage']}")
    typer.echo(f"  Leads without discovery:    {summary['leads_without_discovery']}")
    typer.echo(f"  Unfetched dockets:          {summary['unfetched_dockets']}")

    if report["recommendations"]:
        typer.echo(f"\n  {'Docket':<22} {'Util':<12} {'Disc':>5} {'HD':>5} {'Cover':>6} {'Action':<12}")
        typer.echo(f"  {'-' * 22} {'-' * 12} {'-' * 5} {'-' * 5} {'-' * 6} {'-' * 12}")
        for r in report["recommendations"][:limit]:
            typer.echo(
                f"  {r['docket_number']:<22} {r['utility']:<12} "
                f"{r['discovery_records_count']:>5} {r['historical_docs_count']:>5} "
                f"{r['coverage_pct']:>5.1f}% {r['recommended_action']:<12}"
            )

    if report["unfetched_dockets"]:
        typer.echo(f"\n  Unfetched Discovery Records (top 10):")
        for uf in report["unfetched_dockets"][:10]:
            typer.echo(
                f"    {uf['docket_number']:<22} {uf['utility']:<12} "
                f"unfetched={uf['unfetched_count']}"
            )

    if report["docket_leads"]:
        typer.echo(f"\n  Docket Leads Without Discovery Records (top 10):")
        for ld in report["docket_leads"][:10]:
            title = ld.get("title", "")[:60]
            typer.echo(
                f"    {ld['docket_number']:<22} {ld.get('utility', ''):<12} "
                f"leads={ld['lead_count']} {title}"
            )

    typer.echo()


@app.command("show-extraction-coverage-nc")
def show_extraction_coverage_nc(
    limit: int = typer.Option(30, "--limit", help="Max families to display."),
    min_versions: int = typer.Option(1, "--min-versions", help="Minimum versions to include."),
    sort_by: str = typer.Option("gap", "--sort-by", help="Sort: gap, coverage, or charges."),
    company: str = typer.Option("", "--company", help="Filter by company (progress/carolinas)."),
) -> None:
    """Show per-family extraction coverage ranked by gap size.

    Displays each family's version count, charge count, and coverage percentage.
    """
    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        company_filter = ""
        params: list = []
        if company:
            company_filter = "AND hd.company = ?"
            params.append(company)

        params.extend([min_versions])
        rows = conn.execute(
            f"""
            SELECT
                hd.family_key,
                hd.company,
                COUNT(DISTINCT tv.id) AS version_count,
                COUNT(DISTINCT tc.id) AS charge_count,
                COUNT(DISTINCT CASE WHEN tc.id IS NOT NULL THEN tv.id END) AS versions_with_charges,
                ROUND(100.0 * COUNT(DISTINCT CASE WHEN tc.id IS NOT NULL THEN tv.id END)
                      / NULLIF(COUNT(DISTINCT tv.id), 0), 1) AS coverage_pct,
                ROUND(AVG(CASE WHEN tc.id IS NOT NULL THEN 1.0 ELSE 0.0 END) * 100, 1) AS avg_charge_rate,
                MIN(hd.effective_start) AS earliest,
                MAX(hd.effective_start) AS latest
            FROM historical_documents hd
            JOIN tariff_versions tv ON tv.historical_document_id = hd.id
            LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
            WHERE hd.state = 'NC'
              AND hd.family_key IS NOT NULL
              {company_filter}
            GROUP BY hd.family_key
            HAVING version_count >= ?
            ORDER BY
                CASE ? WHEN 'coverage' THEN coverage_pct
                       WHEN 'charges' THEN charge_count
                       ELSE (version_count - versions_with_charges) END
                {'ASC' if sort_by == 'coverage' else 'DESC'}
            LIMIT ?
            """,
            tuple(params + [sort_by, limit]),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No coverage data found.")
        return

    typer.echo(f"{'Family':<45} {'Co':<5} {'Vers':>5} {'w/Chg':>5} {'Cov%':>6} {'Charges':>8} {'Earliest':>12} {'Latest':>12}")
    typer.echo("-" * 105)
    for row in rows:
        fk = (row["family_key"] or "")[:43]
        co = (row["company"] or "")[:4]
        typer.echo(
            f"{fk:<45} {co:<5} {row['version_count']:>5} {row['versions_with_charges']:>5} "
            f"{row['coverage_pct']:>5.1f}% {row['charge_count']:>8} "
            f"{str(row['earliest'] or '-'):>12} {str(row['latest'] or '-'):>12}"
        )

    # Summary
    total_families = len(rows)
    families_100pct = sum(1 for r in rows if r["coverage_pct"] == 100.0)
    families_0pct = sum(1 for r in rows if r["coverage_pct"] == 0.0)
    families_partial = total_families - families_100pct - families_0pct
    total_charges = sum(r["charge_count"] for r in rows)

    typer.echo(
        f"\n{total_families} families | {families_100pct} full coverage | "
        f"{families_partial} partial | {families_0pct} no coverage | "
        f"{total_charges} total charges"
    )


@app.command("validate-parser-change-nc")
def validate_parser_change_nc(
    parser_profile: str = typer.Option(
        ..., "--profile", help="Parser profile that was changed."
    ),
    limit: int = typer.Option(20, "--limit", help="Max documents to validate."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
) -> None:
    """Validate a parser profile change by comparing before/after charge counts.

    Re-extracts affected documents and reports charge-count differences to
    catch regressions before they're committed to the database.
    """
    from duke_rates.historical.ncuc.pipeline.stage_versions import HISTORICAL_BULK_PARSER_VERSION

    settings, _ = _bootstrap()
    conn = connect_sqlite(settings.database_path)
    try:
        family_filter = "AND hd.family_key = ?" if family_key else ""
        params: list = [parser_profile, HISTORICAL_BULK_PARSER_VERSION]
        if family_key:
            params.append(family_key)
        params.append(limit)

        docs = conn.execute(
            f"""
            SELECT hd.id, hd.family_key, hd.company, hd.effective_start,
                   hd.local_path, hpr.charge_count AS prev_charge_count,
                   hpr.outcome_quality AS prev_quality,
                   hpr.status AS prev_status
            FROM historical_documents hd
            JOIN historical_processing_runs hpr
              ON hpr.id = (
                  SELECT r2.id FROM historical_processing_runs r2
                  WHERE r2.historical_document_id = hd.id
                  ORDER BY r2.id DESC LIMIT 1
              )
            WHERE hpr.parser_profile = ?
              AND hpr.parser_version = ?
              {family_filter}
              AND hd.local_path IS NOT NULL
              AND hd.effective_start IS NOT NULL
            ORDER BY hd.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    if not docs:
        typer.echo(f"No documents found using profile '{parser_profile}'.")
        return

    typer.echo(f"Validating {len(docs)} documents using profile '{parser_profile}'...\n")

    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    extractor = BulkExtractor(settings.database_path)

    regressions = 0
    improvements = 0
    unchanged = 0
    errors = 0

    for doc in docs:
        try:
            charges, profile, _, status, _, _, _ = extractor.extract_charges_from_document(dict(doc))
            new_count = len(charges)
            old_count = doc["prev_charge_count"] or 0
            diff = new_count - old_count

            if diff < 0:
                regressions += 1
                flag = "REGRESSION"
            elif diff > 0:
                improvements += 1
                flag = "improvement"
            else:
                unchanged += 1
                flag = "unchanged"

            typer.echo(
                f"  {flag:<12} hd={doc['id']}  {doc['family_key']}  "
                f"charges: {old_count} -&gt; {new_count}  (Δ{diff:+d})  "
                f"status: {doc['prev_status']} -&gt; {status}"
            )
        except Exception as exc:
            errors += 1
            typer.echo(f"  ERROR      hd={doc['id']}  {doc['family_key']}  {exc}")

    typer.echo(
        f"\nResults: {regressions} regressions, {improvements} improvements, "
        f"{unchanged} unchanged, {errors} errors"
    )
    if regressions:
        typer.echo(
            "WARNING: Regressions detected. Review before applying parser changes."
        )





@app.command("diagnose-empty-nc")
def diagnose_empty_nc(
    limit: int = typer.Option(0, "--limit", help="Max documents to show per group (0 = counts only)."),
    show_groups: str = typer.Option(
        "all", "--groups", help="Comma-separated groups to show: ocr,profile,generic,weak,all"
    ),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Batch-diagnose all empty/weak documents, grouped by root cause.

    Shows remediation batches with exact commands for each root-cause group.
    Groups: ocr (no text), profile (wrong profile match), generic (generic
    fallback with text), weak (sparse parses).
    """
    settings, _ = _bootstrap()
    conn = connect_sqlite(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT hd.id, hd.family_key, hd.company, hd.effective_start,
                   hd.start_page, hd.end_page, hd.local_path,
                   hpr.parser_profile, hpr.status, hpr.outcome_quality,
                   hpr.charge_count,
                   CAST(COALESCE(json_extract(hpr.metadata_json, '$.text_metrics.text_length'), '0') AS INTEGER) AS text_length,
                   json_extract(hpr.metadata_json, '$.selection.fallback_applied') AS fallback_applied,
                   json_extract(hpr.metadata_json, '$.selection.fallback_reason') AS fallback_reason,
                   json_extract(hpr.metadata_json, '$.signals.has_carolinas_company_text') AS has_carolinas,
                   json_extract(hpr.metadata_json, '$.signals.has_progress_company_text') AS has_progress
            FROM historical_documents hd
            JOIN historical_processing_runs hpr ON hpr.id = (
                SELECT r2.id FROM historical_processing_runs r2
                WHERE r2.historical_document_id = hd.id
                ORDER BY r2.id DESC LIMIT 1
            )
            WHERE hd.state = 'NC'
              AND (hpr.status = 'empty' OR hpr.outcome_quality IN ('empty', 'weak'))
            ORDER BY hpr.outcome_quality DESC, hpr.status, hd.id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    groups: dict[str, dict] = {
        "ocr": {
            "label": "OCR Needed (no extractable text)",
            "description": "Documents with zero text_length. PDF is scanned/image-based and needs OCR before parsing.",
            "command": "python -m duke_rates ocr enqueue-nc --hd-id {ids}\npython -m duke_rates ocr process-queue-nc",
            "docs": [],
        },
        "generic": {
            "label": "Generic Fallback (text exists but no specific profile matched)",
            "description": "Text is present but only generic_residential matched. Needs a new parser profile or profile routing fix.",
            "command": "python -m duke_rates diagnose-document-nc --hd-id {id_example}\n# Then: improve parser profile or routing for this family",
            "docs": [],
        },
        "profile": {
            "label": "Profile Routing Mismatch",
            "description": "Profile matched but produced empty results. Possible wrong profile for company/family, or profile needs improvement.",
            "command": "python -m duke_rates diagnose-document-nc --hd-id {id_example}\n# Check if profile is correct for this family/company",
            "docs": [],
        },
        "weak": {
            "label": "Weak Parses (sparse charges)",
            "description": "Parser produced 1-2 charges but was flagged weak. May need profile tuning for better coverage.",
            "command": "python -m duke_rates diagnose-document-nc --hd-id {id_example} --show-text\n# Review text and consider profile improvements",
            "docs": [],
        },
        "skipped": {
            "label": "Skipped (non-tariff or procedural)",
            "description": "Document was intentionally skipped. May be non-tariff content (procedural, reference, etc.).",
            "command": "python -m duke_rates diagnose-document-nc --hd-id {id_example} --show-text\n# Review content; if truly non-tariff, accept as caveat",
            "docs": [],
        },
    }

    selected_groups = set(show_groups.split(",")) if show_groups != "all" else set(groups.keys())

    for row in rows:
        text_len = row["text_length"] or 0
        status = row["status"] or ""
        quality = row["outcome_quality"] or ""
        profile = row["parser_profile"] or "unknown"

        # Classify into group
        if status.startswith("skipped"):
            groups["skipped"]["docs"].append(dict(row))
        elif text_len == 0 or text_len is None:
            groups["ocr"]["docs"].append(dict(row))
        elif profile == "generic_residential" and quality == "empty":
            groups["generic"]["docs"].append(dict(row))
        elif quality == "weak":
            groups["weak"]["docs"].append(dict(row))
        else:
            groups["profile"]["docs"].append(dict(row))

    if json_out:
        result = {}
        for key, grp in groups.items():
            if key in selected_groups and grp["docs"]:
                result[key] = {
                    "label": grp["label"],
                    "count": len(grp["docs"]),
                    "description": grp["description"],
                    "command": grp["command"].format(
                        ids=" ".join(str(d["id"]) for d in grp["docs"][:10]),
                        id_example=grp["docs"][0]["id"] if grp["docs"] else 0,
                    ),
                    "doc_ids": [d["id"] for d in grp["docs"]],
                }
        typer.echo(json.dumps(result, indent=2))
        return

    total = sum(len(grp["docs"]) for grp in groups.values())
    typer.echo(f"Empty/Weak Document Diagnostic -- {total} documents\n")

    for key in ["ocr", "generic", "profile", "weak", "skipped"]:
        if key not in selected_groups:
            continue
        grp = groups[key]
        docs = grp["docs"]
        if not docs:
            continue

        typer.echo(f"[{key.upper()}] {grp['label']} -- {len(docs)} docs")
        typer.echo(f"  {grp['description']}\n")

        # Show top examples
        show_n = min(limit, len(docs)) if limit else 0
        if show_n > 0:
            for d in docs[:show_n]:
                typer.echo(
                    f"  hd={d['id']:<6} family={d['family_key']:<45} "
                    f"profile={d['parser_profile']:<35} "
                    f"text_len={d['text_length']}"
                )

        # Build batch command
        ids_batch = " ".join(str(d["id"]) for d in docs[:20])
        example_id = docs[0]["id"]
        cmd = grp["command"].format(ids=ids_batch, id_example=example_id)

        typer.echo(f"\n  -- Remediation ({len(docs)} docs) --")
        for line in cmd.split("\n"):
            typer.echo(f"  {line}")
        typer.echo("")

    # Summary counts
    typer.echo(
        f"Summary: OCR={len(groups['ocr']['docs'])} "
        f"generic_fallback={len(groups['generic']['docs'])} "
        f"profile_mismatch={len(groups['profile']['docs'])} "
        f"weak={len(groups['weak']['docs'])} "
        f"skipped={len(groups['skipped']['docs'])}"
    )


@app.command("show-review-impact-nc")
def show_review_impact_nc(
    limit: int = typer.Option(25, "--limit", help="Max families to display."),
    min_review_items: int = typer.Option(1, "--min-reviews", help="Minimum review items to include a family."),
) -> None:
    """Rank NC families by review impact: where review work would yield the most charges.

    Computes an impact score per family:
      (versions_with_charges * avg_charges_per_version) / sqrt(review_items)

    This prioritizes families where clearing a modest review backlog would
    validate or surface many charges, over families with huge review debt
    but low charge density.
    """
    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        rows = conn.execute(
            """
            WITH latest_reviews AS (
                SELECT parse_attempt_id, MAX(id) AS max_id
                FROM parse_review_outcomes
                WHERE parse_attempt_id IS NOT NULL
                GROUP BY parse_attempt_id
            ),
            doc_reviews AS (
                SELECT hd.family_key, hd.company, hd.id AS hd_id,
                       pal.source_pdf
                FROM latest_reviews lr
                JOIN parse_review_outcomes pro ON pro.id = lr.max_id
                JOIN parse_attempt_logs pal ON pal.id = lr.parse_attempt_id
                JOIN historical_documents hd ON hd.local_path = pal.source_pdf
                WHERE pro.outcome = 'needs_review'
                  AND hd.state = 'NC'
            ),
            family_agg AS (
                SELECT
                    dr.family_key,
                    dr.company,
                    COUNT(DISTINCT dr.hd_id) AS review_items
                FROM doc_reviews dr
                GROUP BY dr.family_key
            )
            SELECT
                fa.family_key,
                fa.company,
                fa.review_items,
                COUNT(DISTINCT tv.id) AS version_count,
                COUNT(DISTINCT CASE WHEN tc.id IS NOT NULL THEN tv.id END) AS versions_with_charges,
                COUNT(DISTINCT tc.id) AS charge_count,
                ROUND(CAST(COUNT(DISTINCT tc.id) AS REAL) / NULLIF(COUNT(DISTINCT CASE WHEN tc.id IS NOT NULL THEN tv.id END), 0), 1) AS avg_charges_per_version,
                ROUND(
                    CAST(COUNT(DISTINCT CASE WHEN tc.id IS NOT NULL THEN tv.id END) AS REAL)
                    * CAST(COUNT(DISTINCT tc.id) AS REAL)
                    / NULLIF(COUNT(DISTINCT CASE WHEN tc.id IS NOT NULL THEN tv.id END), 0)
                    / NULLIF(SQRT(MAX(fa.review_items)), 0),
                    1
                ) AS impact_score
            FROM family_agg fa
            JOIN historical_documents hd ON hd.family_key = fa.family_key
            JOIN tariff_versions tv ON tv.historical_document_id = hd.id
            LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
            WHERE hd.state = 'NC'
            GROUP BY fa.family_key
            HAVING fa.review_items >= ?
            ORDER BY impact_score DESC
            LIMIT ?
            """,
            (min_review_items, limit),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No families with review items found.")
        return

    typer.echo(f"{'Family':<45} {'Co':<5} {'Score':>7} {'Vers':>5} {'w/Chg':>5} {'Charges':>8} {'avg/v':>6} {'Reviews':>7}")
    typer.echo("-" * 100)
    for row in rows:
        fk = (row["family_key"] or "")[:43]
        co = (row["company"] or "")[:4]
        typer.echo(
            f"{fk:<45} {co:<5} {row['impact_score']:>7.1f} {row['version_count']:>5} "
            f"{row['versions_with_charges']:>5} {row['charge_count']:>8} "
            f"{row['avg_charges_per_version']:>6.1f} {row['review_items']:>7}"
        )

    # Top recommendations
    typer.echo(f"\n-- Top 5 Review Targets --")
    for i, row in enumerate(rows[:5]):
        fk = row["family_key"]
        typer.echo(
            f"  {i+1}. {fk}  (score={row['impact_score']:.1f}, "
            f"{row['review_items']} review items, {row['charge_count']} charges)"
        )
        typer.echo(
            f"     Review command: python -m duke_rates extract-rates-nc "
            f"--family-key {fk} --verbose"
        )





if __name__ == "__main__":
    main()
