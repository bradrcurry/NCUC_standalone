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
# Progress NC sub-app — historical recovery / leads / search packs / inbox.
from duke_rates.cli_commands.progress import progress_app
# Doc-intel sub-app — Docling, classification, embedding, LLM probe, gold-set.
from duke_rates.cli_commands.doc_intel import doc_intel_app

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
app.add_typer(progress_app, name="progress")
app.add_typer(doc_intel_app, name="doc-intel")


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
            return f"python -m duke_rates doc-intel show-document-classification-audit --limit 25{family_flag}"
        if action == "new_profile_or_family_routing_review":
            return f"python -m duke_rates show-parser-selection-audit-nc --limit 25{family_flag}"
        if action == "evaluate_formula_or_program_lane":
            return f"python -m duke_rates doc-intel show-document-classification-audit --limit 25{family_flag}"
        if action == "map_to_adjustment_or_matrix_profile":
            return f"python -m duke_rates doc-intel show-document-classification-audit --limit 25{family_flag}"
        if action in {"reclassify_non_tariff_or_reference", "reclassify_reference_or_unrelated"}:
            return f"python -m duke_rates doc-intel show-document-classification-audit --limit 25{family_flag}"
        return f"python -m duke_rates doc-intel show-unknown-routing-audit --limit 25"

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
        200,
        "--action-batch-limit",
        help=(
            "Per-cycle --limit for corrective actions and drain steps. "
            "Each action is still capped by its own max_per_cycle. "
            "Bumped from 50 to 200 (2026-05-23) so an 8h run can drain "
            "thousand-row backlogs (e.g. 3,100 low_quality_parses in "
            "~16 cycles instead of 62). Pass a smaller value if you "
            "want shorter per-cycle drain times."
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
        typer.echo(f"Action batch limit: {action_batch_limit}")
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
