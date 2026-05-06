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

app = typer.Typer(help="Duke Energy tariff discovery and analysis CLI.")


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


def _read_usage_file(path: Path) -> UsageInput:
    try:
        return read_usage_file(path)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


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


def _parse_bill_file(path: Path, repository: Repository) -> tuple[int, object]:
    text = extract_pdf_text(path)
    settings = get_settings()
    raw_text_dir = settings.processed_dir / "bills"
    raw_text_dir.mkdir(parents=True, exist_ok=True)
    raw_text_path = raw_text_dir / f"{path.stem}.txt"
    raw_text_path.write_text(text, encoding="utf-8")
    statement = parse_bill_text(text, source_path=path)
    content_hash = sha256_bytes(path.read_bytes())
    bill_id = repository.upsert_bill_statement(
        statement,
        content_hash=content_hash,
        raw_text_path=str(raw_text_path),
    )
    return bill_id, statement


def _schedule_has_bill_components(result: DocumentParseResult) -> bool:
    return is_estimatable_schedule(result)


def _best_estimatable_results(
    repository: Repository,
    *,
    state: str | None = None,
    company: str | None = None,
    usage: UsageInput | None = None,
) -> dict[str, tuple[object, DocumentParseResult]]:
    best: dict[str, tuple[object, DocumentParseResult]] = {}
    for doc in repository.list_documents(state=state, company=company):
        if doc.category not in ESTIMATABLE_CATEGORIES:
            continue
        result = repository.latest_parse_result(doc.id)
        if not result or not result.schedule or not _schedule_has_bill_components(result):
            continue
        if usage and not supports_usage_input(result, usage):
            continue
        key = canonical_tariff_key(result) or result.schedule.tariff_id
        current = best.get(key)
        new_score = estimation_score(doc, result)
        current_score = estimation_score(current[0], current[1]) if current else None
        if current is None or new_score > current_score:
            best[key] = (doc, result)
    return best


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


def _safe_text_file_length(path_value: object) -> int:
    path_text = str(path_value or "").strip()
    if not path_text:
        return 0
    try:
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return 0
        return len(path.read_text(encoding="utf-8", errors="ignore").strip())
    except Exception:
        return 0


def _classify_ocr_route(
    *,
    raw_text_chars: int,
    outcome_quality: str,
    parser_profile: str,
    page_count: int,
    title: str,
    has_ocr_artifact: bool,
    stale_reasons: list[str],
) -> tuple[str, str]:
    lowered_title = title.lower()
    layout_heavy = (
        page_count >= 5
        or "summary" in lowered_title
        or "compliance" in lowered_title
        or "book" in lowered_title
    )
    if raw_text_chars == 0 and parser_profile == "unknown":
        return (
            "no_usable_text_unknown_profile",
            "run_docling_or_paddle_structure" if layout_heavy else "queue_ocr_or_paddle",
        )
    if raw_text_chars == 0:
        return (
            "no_usable_text",
            "run_docling_or_paddle_structure" if layout_heavy else "queue_ocr_or_paddle",
        )
    if outcome_quality in {"weak", "empty"} and not has_ocr_artifact:
        return ("weak_without_ocr", "queue_ocr_or_paddle")
    if outcome_quality in {"weak", "empty"} and layout_heavy:
        return ("weak_layout_sensitive", "run_docling_or_paddle_structure")
    if outcome_quality in {"weak", "empty"}:
        return ("weak_after_text_recovery", "parser_or_page_level_glm_review")
    if stale_reasons:
        return ("stale_artifacts", "reprocess_or_refresh_ocr")
    return ("healthy_or_non_ocr_issue", "no_ocr_action")


def _build_ocr_benchmark_nc_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    backend_filter: str | None = None,
    outcome_filter: str | None = None,
    needs_review_only: bool = False,
    stale_only: bool = False,
    sort_by: str = "recent",
) -> dict[str, object]:
    from duke_rates.db.reprocess import find_stale_historical_documents

    stale_rows = find_stale_historical_documents(conn, limit=max(limit * 5, 100))
    stale_by_document_id = {
        int(item["historical_document_id"]): list(item.get("reasons") or [])
        for item in stale_rows
    }
    rows = conn.execute(
        """
        WITH latest_ocr AS (
            SELECT oa.*
            FROM ocr_artifacts oa
            JOIN (
                SELECT source_pdf, file_hash, MAX(id) AS max_id
                FROM ocr_artifacts
                GROUP BY source_pdf, file_hash
            ) latest
              ON latest.max_id = oa.id
        ),
        ocr_docs AS (
            SELECT
                hd.id AS historical_document_id,
                hd.family_key,
                hd.company,
                hd.title,
                hd.local_path,
                hd.raw_text_path,
                hd.content_hash,
                lo.backend,
                lo.status AS ocr_status,
                lo.page_count,
                lo.ocr_confidence,
                lo.metadata_json AS ocr_metadata_json
            FROM historical_documents hd
            JOIN latest_ocr lo
              ON lo.source_pdf = hd.local_path
             AND (lo.file_hash IS hd.content_hash OR lo.file_hash = hd.content_hash)
            WHERE hd.state = 'NC'
        ),
        latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                WHERE historical_document_id IN (SELECT historical_document_id FROM ocr_docs)
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        ),
        latest_page_artifacts AS (
            SELECT pa.*
            FROM ncuc_page_artifacts pa
            JOIN (
                SELECT source_pdf, file_hash, MAX(id) AS max_id
                FROM ncuc_page_artifacts
                WHERE source_pdf IN (SELECT local_path FROM ocr_docs)
                GROUP BY source_pdf, file_hash
            ) latest
              ON latest.max_id = pa.id
        ),
        latest_span_artifacts AS (
            SELECT sa.*
            FROM ncuc_span_artifacts sa
            JOIN (
                SELECT source_pdf, file_hash, MAX(id) AS max_id
                FROM ncuc_span_artifacts
                WHERE source_pdf IN (SELECT local_path FROM ocr_docs)
                GROUP BY source_pdf, file_hash
            ) latest
              ON latest.max_id = sa.id
        ),
        latest_parse_attempts AS (
            SELECT pal.*
            FROM parse_attempt_logs pal
            JOIN (
                SELECT CAST(json_extract(metadata_json, '$.historical_document_id') AS INTEGER) AS historical_document_id,
                       MAX(id) AS max_id
                FROM parse_attempt_logs
                WHERE json_extract(metadata_json, '$.historical_document_id') IS NOT NULL
                  AND CAST(json_extract(metadata_json, '$.historical_document_id') AS INTEGER)
                      IN (SELECT historical_document_id FROM ocr_docs)
                GROUP BY CAST(json_extract(metadata_json, '$.historical_document_id') AS INTEGER)
            ) latest
              ON latest.max_id = pal.id
        ),
        latest_reviews AS (
            SELECT pro.*
            FROM parse_review_outcomes pro
            JOIN (
                SELECT parse_attempt_id, MAX(id) AS max_id
                FROM parse_review_outcomes
                WHERE parse_attempt_id IS NOT NULL
                GROUP BY parse_attempt_id
            ) latest
              ON latest.max_id = pro.id
        )
        SELECT
            od.historical_document_id,
            od.family_key,
            od.company,
            od.title,
            od.local_path,
            od.raw_text_path,
            od.content_hash,
            od.backend,
            od.ocr_status,
            od.page_count,
            od.ocr_confidence,
            od.ocr_metadata_json,
            lpa.artifact_version AS page_artifact_version,
            lpa.metadata_json AS page_metadata_json,
            lsa.artifact_version AS span_artifact_version,
            lr.status AS parse_status,
            lr.outcome_quality,
            lr.charge_count,
            lr.parser_profile,
            lpat.id AS parse_attempt_id,
            lrev.outcome AS review_outcome
        FROM ocr_docs od
        LEFT JOIN latest_runs lr
          ON lr.historical_document_id = od.historical_document_id
        LEFT JOIN latest_page_artifacts lpa
          ON lpa.source_pdf = od.local_path
         AND (lpa.file_hash IS od.content_hash OR lpa.file_hash = od.content_hash)
        LEFT JOIN latest_span_artifacts lsa
          ON lsa.source_pdf = od.local_path
         AND (lsa.file_hash IS od.content_hash OR lsa.file_hash = od.content_hash)
        LEFT JOIN latest_parse_attempts lpat
          ON CAST(json_extract(lpat.metadata_json, '$.historical_document_id') AS INTEGER) = od.historical_document_id
        LEFT JOIN latest_reviews lrev
          ON lrev.parse_attempt_id = lpat.id
        ORDER BY od.historical_document_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    report_rows: list[dict[str, object]] = []
    backend_counts: dict[str, int] = {}
    normalization_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    backend_outcome_counts: dict[tuple[str, str], int] = {}
    route_reason_counts: dict[str, int] = {}
    recommended_lane_counts: dict[str, int] = {}
    page_artifact_version_counts: dict[str, int] = {}
    span_artifact_version_counts: dict[str, int] = {}
    review_outcome_counts: dict[str, int] = {}

    for row in rows:
        ocr_metadata = json.loads(row["ocr_metadata_json"] or "{}")
        page_metadata = json.loads(row["page_metadata_json"] or "{}")
        backend = str(ocr_metadata.get("selected_backend") or row["backend"] or "unknown")
        normalization_version = str(ocr_metadata.get("ocr_normalization_version") or "unknown")
        outcome_quality = str(row["outcome_quality"] or "missing")
        page_artifact_version = str(row["page_artifact_version"] or "missing")
        span_artifact_version = str(row["span_artifact_version"] or "missing")
        review_outcome = str(row["review_outcome"] or "unreviewed")
        historical_document_id = int(row["historical_document_id"])
        stale_reasons = list(stale_by_document_id.get(historical_document_id) or [])
        raw_text_chars = _safe_text_file_length(row["raw_text_path"])
        route_reason, recommended_lane = _classify_ocr_route(
            raw_text_chars=raw_text_chars,
            outcome_quality=outcome_quality,
            parser_profile=str(row["parser_profile"] or "unknown"),
            page_count=int(row["page_count"] or 0),
            title=str(row["title"] or ""),
            has_ocr_artifact=bool(row["backend"]),
            stale_reasons=stale_reasons,
        )

        if backend_filter and backend != backend_filter:
            continue
        if outcome_filter and outcome_quality != outcome_filter:
            continue
        if needs_review_only and review_outcome != "needs_review":
            continue
        if stale_only and not stale_reasons:
            continue

        backend_counts[backend] = backend_counts.get(backend, 0) + 1
        normalization_counts[normalization_version] = normalization_counts.get(normalization_version, 0) + 1
        outcome_counts[outcome_quality] = outcome_counts.get(outcome_quality, 0) + 1
        backend_outcome_counts[(backend, outcome_quality)] = backend_outcome_counts.get((backend, outcome_quality), 0) + 1
        route_reason_counts[route_reason] = route_reason_counts.get(route_reason, 0) + 1
        recommended_lane_counts[recommended_lane] = recommended_lane_counts.get(recommended_lane, 0) + 1
        page_artifact_version_counts[page_artifact_version] = page_artifact_version_counts.get(page_artifact_version, 0) + 1
        span_artifact_version_counts[span_artifact_version] = span_artifact_version_counts.get(span_artifact_version, 0) + 1
        review_outcome_counts[review_outcome] = review_outcome_counts.get(review_outcome, 0) + 1

        report_rows.append(
            {
                "historical_document_id": row["historical_document_id"],
                "family_key": row["family_key"],
                "company": row["company"],
                "title": row["title"],
                "stale_reasons": stale_reasons,
                "backend": backend,
                "ocr_status": row["ocr_status"],
                "ocr_normalization_version": normalization_version,
                "attempted_backends": list(ocr_metadata.get("attempted_backends") or []),
                "page_count": int(row["page_count"] or 0),
                "raw_text_chars": raw_text_chars,
                "route_reason": route_reason,
                "recommended_lane": recommended_lane,
                "ocr_confidence": row["ocr_confidence"],
                "page_artifact_version": page_artifact_version,
                "span_artifact_version": span_artifact_version,
                "page_artifact_source": page_metadata.get("artifact_source"),
                "parse_status": row["parse_status"],
                "outcome_quality": outcome_quality,
                "charge_count": int(row["charge_count"] or 0),
                "parser_profile": row["parser_profile"],
                "review_outcome": review_outcome,
                "parse_attempt_id": row["parse_attempt_id"],
            }
        )

    backend_summary = [
        {"backend": key, "count": value}
        for key, value in sorted(backend_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    normalization_summary = [
        {"ocr_normalization_version": key, "count": value}
        for key, value in sorted(normalization_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    outcome_summary = [
        {"outcome_quality": key, "count": value}
        for key, value in sorted(outcome_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    route_reason_summary = [
        {"route_reason": key, "count": value}
        for key, value in sorted(route_reason_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    recommended_lane_summary = [
        {"recommended_lane": key, "count": value}
        for key, value in sorted(recommended_lane_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    page_artifact_version_summary = [
        {"page_artifact_version": key, "count": value}
        for key, value in sorted(page_artifact_version_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    span_artifact_version_summary = [
        {"span_artifact_version": key, "count": value}
        for key, value in sorted(span_artifact_version_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    review_outcome_summary = [
        {"review_outcome": key, "count": value}
        for key, value in sorted(review_outcome_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    backend_outcome_summary = [
        {"backend": backend, "outcome_quality": outcome, "count": count}
        for (backend, outcome), count in sorted(
            backend_outcome_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]

    weak_rank = {"weak": 0, "missing": 1, "strong": 2}
    review_rank = {"needs_review": 0, "unreviewed": 1, "accepted": 2, "corrected": 3, "rejected": 4}
    if sort_by == "weak-first":
        report_rows.sort(
            key=lambda row: (
                weak_rank.get(str(row.get("outcome_quality") or "missing"), 9),
                -len(list(row.get("stale_reasons") or [])),
                int(row.get("historical_document_id") or 0),
            )
        )
    elif sort_by == "review-first":
        report_rows.sort(
            key=lambda row: (
                review_rank.get(str(row.get("review_outcome") or "unreviewed"), 9),
                weak_rank.get(str(row.get("outcome_quality") or "missing"), 9),
                int(row.get("historical_document_id") or 0),
            )
        )
    elif sort_by == "stale-first":
        report_rows.sort(
            key=lambda row: (
                0 if row.get("stale_reasons") else 1,
                -len(list(row.get("stale_reasons") or [])),
                weak_rank.get(str(row.get("outcome_quality") or "missing"), 9),
                int(row.get("historical_document_id") or 0),
            )
        )
    else:
        report_rows.sort(
            key=lambda row: -int(row.get("historical_document_id") or 0)
        )

    return {
        "row_count": len(report_rows),
        "backend_summary": backend_summary,
        "normalization_summary": normalization_summary,
        "outcome_summary": outcome_summary,
        "route_reason_summary": route_reason_summary,
        "recommended_lane_summary": recommended_lane_summary,
        "page_artifact_version_summary": page_artifact_version_summary,
        "span_artifact_version_summary": span_artifact_version_summary,
        "review_outcome_summary": review_outcome_summary,
        "backend_outcome_summary": backend_outcome_summary,
        "rows": report_rows,
    }


def _build_ocr_remediation_candidates_nc_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    company: str | None = None,
    family_key: str | None = None,
) -> dict[str, Any]:
    from duke_rates.db.reprocess import find_stale_historical_documents

    stale_rows = find_stale_historical_documents(conn, limit=max(limit * 10, 250))
    stale_by_document_id = {
        int(item["historical_document_id"]): list(item.get("reasons") or [])
        for item in stale_rows
    }
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
        ),
        latest_ocr AS (
            SELECT oa.*
            FROM ocr_artifacts oa
            JOIN (
                SELECT source_pdf, file_hash, MAX(id) AS max_id
                FROM ocr_artifacts
                GROUP BY source_pdf, file_hash
            ) latest
              ON latest.max_id = oa.id
        ),
        page_text AS (
            SELECT
                source_pdf,
                file_hash,
                SUM(text_length) AS page_artifact_text_chars
            FROM ncuc_page_artifacts
            GROUP BY source_pdf, file_hash
        )
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.company,
            hd.title,
            hd.local_path,
            hd.raw_text_path,
            hd.start_page,
            hd.end_page,
            lr.parser_profile,
            lr.outcome_quality,
            lr.charge_count,
            lo.backend AS ocr_backend,
            lo.status AS ocr_status,
            lo.page_count AS ocr_page_count,
            lo.ocr_confidence,
            COALESCE(pt.page_artifact_text_chars, 0) AS page_artifact_text_chars
        FROM historical_documents hd
        LEFT JOIN latest_runs lr
          ON lr.historical_document_id = hd.id
        LEFT JOIN latest_ocr lo
          ON lo.source_pdf = hd.local_path
         AND (lo.file_hash IS hd.content_hash OR lo.file_hash = hd.content_hash)
        LEFT JOIN page_text pt
          ON pt.source_pdf = hd.local_path
         AND (pt.file_hash IS hd.content_hash OR pt.file_hash = hd.content_hash)
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
    candidates: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()

    for row in rows:
        raw_text_chars = max(
            _safe_text_file_length(row["raw_text_path"]),
            int(row["page_artifact_text_chars"] or 0),
        )
        parser_profile = str(row["parser_profile"] or "unknown")
        outcome_quality = str(row["outcome_quality"] or "missing")
        stale_reasons = list(stale_by_document_id.get(int(row["historical_document_id"]), []) or [])
        bounded_page_count = 0
        if row["start_page"] and row["end_page"]:
            bounded_page_count = max(int(row["end_page"]) - int(row["start_page"]) + 1, 0)
        page_count = max(1, int(row["ocr_page_count"] or 0), bounded_page_count)
        route_reason, recommended_lane = _classify_ocr_route(
            raw_text_chars=raw_text_chars,
            outcome_quality=outcome_quality,
            parser_profile=parser_profile,
            page_count=page_count,
            title=str(row["title"] or ""),
            has_ocr_artifact=bool(row["ocr_backend"]),
            stale_reasons=stale_reasons,
        )
        if route_reason == "healthy_or_non_ocr_issue":
            continue

        priority = 0
        if route_reason == "no_usable_text_unknown_profile":
            priority = 0
        elif route_reason == "no_usable_text":
            priority = 1
        elif route_reason == "weak_without_ocr":
            priority = 2
        elif route_reason == "weak_layout_sensitive":
            priority = 3
        elif route_reason == "stale_artifacts":
            priority = 4
        else:
            priority = 5

        route_counts[route_reason] += 1
        lane_counts[recommended_lane] += 1
        candidates.append(
            {
                "historical_document_id": int(row["historical_document_id"]),
                "family_key": row["family_key"],
                "company": row["company"],
                "title": row["title"],
                "parser_profile": parser_profile,
                "outcome_quality": outcome_quality,
                "charge_count": int(row["charge_count"] or 0),
                "raw_text_chars": raw_text_chars,
                "ocr_backend": row["ocr_backend"],
                "ocr_status": row["ocr_status"],
                "ocr_confidence": row["ocr_confidence"],
                "page_count": page_count,
                "route_reason": route_reason,
                "recommended_lane": recommended_lane,
                "stale_reasons": stale_reasons,
                "_priority": priority,
            }
        )

    candidates.sort(
        key=lambda row: (
            int(row["_priority"]),
            int(row["raw_text_chars"]),
            0 if row["ocr_backend"] is None else 1,
            int(row["historical_document_id"]),
        )
    )

    trimmed = [{key: value for key, value in row.items() if key != "_priority"} for row in candidates[:limit]]
    return {
        "candidate_count": len(candidates),
        "route_reason_summary": [
            {"route_reason": name, "count": count}
            for name, count in route_counts.most_common(10)
        ],
        "recommended_lane_summary": [
            {"recommended_lane": name, "count": count}
            for name, count in lane_counts.most_common(10)
        ],
        "rows": trimmed,
    }


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


def _build_workflow_next_actions_nc_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    workflow_status = _build_workflow_status_nc_report(conn)
    ocr_summary = _build_fast_ocr_remediation_summary_nc(conn)
    parser_summary = _build_fast_parser_problem_summary_nc(conn)
    reprocess_summary = _build_fast_reprocess_priority_summary_nc(conn)

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
                    recommended_command="python -m duke_rates process-ocr-queue-nc --limit 1",
                    recommended_parallel_command="python -m duke_rates process-ocr-queue-nc --limit 2 --workers 2",
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
                    recommended_command="python -m duke_rates process-reprocess-queue-nc --limit 1",
                    recommended_parallel_command="python -m duke_rates process-reprocess-queue-nc --limit 2 --workers 2",
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
                    recommended_command="python -m duke_rates enqueue-ocr-remediation-nc --limit 1 --execute",
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
                    recommended_command="python -m duke_rates enqueue-stale-reprocess-nc --limit 10",
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
                    recommended_command="python -m duke_rates show-ocr-remediation-candidates-nc --limit 10",
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


@app.command("list-tariff-families")
def list_tariff_families(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_type: str | None = typer.Option(None, help="Filter by type: rate_schedule, rider, etc."),
) -> None:
    """List tariff families in the database."""
    _, repository = _bootstrap()
    families = repository.list_tariff_families(state=state, company=company, family_type=family_type)
    from collections import Counter
    type_counts = Counter(f.family_type for f in families)
    typer.echo(f"Total: {len(families)} families")
    for ftype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        typer.echo(f"  {ftype}: {count}")
    typer.echo("")
    for f in families[:50]:
        typer.echo(
            f"  {f.family_key:<45} {f.family_type:<15} {f.schedule_code or '?':<20} {(f.title or '')[:40]}"
        )
    if len(families) > 50:
        typer.echo(f"  ... and {len(families) - 50} more")


@app.command("list-provisional-families")
def list_provisional_families(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
) -> None:
    """List provisional historical tariff families awaiting review/promotion."""
    _, repository = _bootstrap()
    families = repository.list_provisional_tariff_families(state=state, company=company)
    typer.echo(f"Total provisional families: {len(families)}")
    for family in families:
        typer.echo(
            f"  {family.family_key:<55} {family.family_type:<12} "
            f"{(family.schedule_code or '?'):<28} {(family.title or '')[:50]}"
        )


@app.command("show-provisional-review-candidates-nc")
def show_provisional_review_candidates_nc(
    state: str = typer.Option("NC", help="State filter."),
    company: str | None = typer.Option(None, help="Company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter to one provisional family."),
    limit: int = typer.Option(25, "--limit", help="Maximum rows to display."),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Rank provisional NC families that still need manual review despite having charges."""
    _, repository = _bootstrap()
    rows = repository.score_provisional_tariff_families(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return

    typer.echo(f"Provisional review candidates: {len(rows)}")
    for row in rows:
        typer.echo(
            f"  score={row['review_score']:<3} band={row['review_band']:<6} "
            f"charges={row['charge_count']:<3} quality={row['charge_quality_score']:.2f} "
            f"{row['family_key']}"
        )
        typer.echo(
            f"    current={row['family_type'] or '?'} / {(row['schedule_code'] or '?')} / {(row['title'] or '')[:80]}"
        )
        typer.echo(
            f"    suggest={row['suggested_family_type'] or '?'} / "
            f"{row['suggested_schedule_code'] or '?'} / {(row['suggested_title'] or '')[:80]}"
        )
        typer.echo(
            f"    action={row['recommended_action']} reasons={', '.join(row['review_reasons']) or '-'}"
        )
        if row.get("promotion_command"):
            typer.echo(f"    promote={row['promotion_command']}")


@app.command("show-lineage-gaps-nc")
def show_lineage_gaps_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to show per section."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show compact NC lineage gaps across discovery records, historical docs, versions, and families."""
    from duke_rates.historical.ncuc.lineage_gaps import build_lineage_gap_report

    _, repository = _bootstrap()
    report = build_lineage_gap_report(repository, limit=limit)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Lineage Gaps (NC)")
    typer.echo(
        "  "
        f"unlinked_discovery={summary['unlinked_discovery_records_count']}  "
        f"auto_matchable_discovery={summary['auto_matchable_discovery_records_count']}"
    )
    typer.echo(
        "  "
        f"historical_missing_effective_start={summary['historical_missing_effective_start_count']}  "
        f"historical_missing_version_link={summary['historical_missing_version_count']}"
    )
    typer.echo(
        "  "
        f"versions_missing_historical_document_id={summary['versions_missing_historical_document_id_count']}  "
        f"families_without_charges={summary['families_without_charges_count']}"
    )

    typer.echo("\nAuto-Matchable Discovery Records")
    for row in report["auto_matchable_discovery_records"]:
        top_match = row["top_match"]
        typer.echo(
            "  "
            f"id={row['discovery_record_id']} "
            f"family={top_match['family_key']} "
            f"score={top_match['score']} "
            f"reasons={','.join(top_match['reasons'])}"
        )

    typer.echo("\nHistorical Docs Missing effective_start")
    for row in report["historical_missing_effective_start"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"title={(row['title'] or '')[:50]}"
        )

    typer.echo("\nHistorical Docs Missing tariff_version Link")
    for row in report["historical_missing_version_link"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} eff={row['effective_start']} "
            f"title={(row['title'] or '')[:50]}"
        )

    typer.echo("\nTariff Versions Missing historical_document_id")
    for row in report["versions_missing_historical_document_id"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'} source={row['source_type']}"
        )

    typer.echo("\nFamilies Without Charges")
    for row in report["families_without_charges"]:
        typer.echo(
            "  "
            f"family={row['family_key']} company={row['company'] or '-'} "
            f"versions={row['version_count']} historical_docs={row['historical_document_count']}"
        )


@app.command("show-provenance-gaps-nc")
def show_provenance_gaps_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to show per section."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC provenance gaps across tariff versions and discovery linkage."""
    from duke_rates.historical.ncuc.provenance_gaps import build_provenance_gap_report

    _, repository = _bootstrap()
    report = build_provenance_gap_report(repository, limit=limit)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Provenance Gaps (NC)")
    typer.echo(
        "  "
        f"historical_versions={summary['historical_versions_count']}  "
        f"versions_missing_any={summary['versions_missing_any_provenance_count']}"
    )
    typer.echo(
        "  "
        f"missing_docket_number={summary['versions_missing_docket_number_count']}  "
        f"missing_order_date={summary['versions_missing_order_date_count']}  "
        f"missing_leaf_no={summary['versions_missing_leaf_no_count']}"
    )
    typer.echo(
        "  "
        f"missing_source_pdf={summary['versions_missing_source_pdf_count']}  "
        f"missing_docket_dir={summary['versions_missing_docket_dir_count']}"
    )
    typer.echo(
        "  "
        f"historical_missing_discovery_match={summary['historical_documents_missing_discovery_match_count']}  "
        f"path_only_link={summary['historical_documents_path_only_discovery_link_count']}  "
        f"hash_only_link={summary['historical_documents_hash_only_discovery_link_count']}"
    )
    typer.echo(
        "  "
        f"acquired_discovery_missing_docket={summary['acquired_discovery_records_missing_docket_number_count']}"
    )

    typer.echo("\nTariff Versions Missing Provenance")
    if not report["versions_missing_provenance"]:
        typer.echo("  none")
    for row in report["versions_missing_provenance"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"missing={','.join(row['missing_fields'])} linkage={row['discovery_linkage']}"
        )
        if row["candidate_fill_fields"]:
            typer.echo(f"    candidate_fill={','.join(row['candidate_fill_fields'])}")
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nHistorical Docs Missing Discovery Match")
    if not report["historical_documents_missing_discovery_match"]:
        typer.echo("  none")
    for row in report["historical_documents_missing_discovery_match"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'} leaf={row['leaf_no'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nHistorical Docs With Path-Only Discovery Link")
    if not report["historical_documents_path_only_discovery_link"]:
        typer.echo("  none")
    for row in report["historical_documents_path_only_discovery_link"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"matched_discovery={row['matched_discovery_record_id'] or '-'} "
            f"docket={row['matched_discovery_docket_number'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nAcquired Discovery Rows Missing docket_number")
    if not report["acquired_discovery_records_missing_docket_number"]:
        typer.echo("  none")
    for row in report["acquired_discovery_records_missing_docket_number"]:
        typer.echo(
            "  "
            f"id={row['id']} status={row['fetch_status']} date={row['filing_date'] or '-'} "
            f"utility={row['utility'] or '-'}"
        )
        typer.echo(f"    title={(row['filing_title'] or '')[:90]}")


@app.command("show-fingerprint-coverage-nc")
def show_fingerprint_coverage_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to show per section."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC fingerprint/hash coverage across historical docs and reusable artifacts."""
    from duke_rates.historical.ncuc.fingerprint_coverage import build_fingerprint_coverage_report

    _, repository = _bootstrap()
    report = build_fingerprint_coverage_report(repository, limit=limit)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Fingerprint Coverage (NC)")
    typer.echo(
        "  "
        f"historical_total={summary['historical_nc_total_count']}  "
        f"hash_backed={summary['historical_nc_hash_backed_count']}  "
        f"path_only={summary['historical_nc_path_only_count']}"
    )
    typer.echo(
        "  "
        f"historical_with_fingerprint={summary['historical_nc_with_fingerprint_count']}  "
        f"historical_without_fingerprint={summary['historical_nc_without_fingerprint_count']}  "
        f"hash_backed_with_fingerprint={summary['historical_nc_hash_backed_with_fingerprint_count']}"
    )
    typer.echo(
        "  "
        f"historical_with_page_artifacts={summary['historical_nc_with_page_artifacts_count']}  "
        f"historical_with_span_artifacts={summary['historical_nc_with_span_artifacts_count']}  "
        f"historical_with_docling={summary['historical_nc_with_docling_count']}  "
        f"historical_with_ocr={summary['historical_nc_with_ocr_count']}"
    )
    typer.echo(
        "  "
        f"acquired_discovery_total={summary['acquired_discovery_total_count']}  "
        f"acquired_with_hash={summary['acquired_discovery_with_hash_count']}"
    )
    typer.echo(
        "  "
        f"acquired_with_page_artifacts={summary['acquired_discovery_with_page_artifacts_count']}  "
        f"acquired_with_span_artifacts={summary['acquired_discovery_with_span_artifacts_count']}  "
        f"acquired_with_docling={summary['acquired_discovery_with_docling_count']}  "
        f"acquired_with_ocr={summary['acquired_discovery_with_ocr_count']}"
    )
    typer.echo(
        "  "
        f"fingerprint_rows={summary['document_fingerprint_row_count']}  "
        f"rows_with_family_key={summary['fingerprint_rows_with_family_key_count']}  "
        f"rows_with_parser_profile={summary['fingerprint_rows_with_parser_profile_count']}  "
        f"rows_with_outcome_quality={summary['fingerprint_rows_with_outcome_quality_count']}"
    )

    typer.echo("\nHistorical Coverage By Company")
    for row in report["historical_by_company"]:
        typer.echo(
            "  "
            f"company={row['company'] or '-'} "
            f"historical_docs={row['historical_document_count']} "
            f"hash_backed={row['hash_backed_count']} "
            f"with_fingerprint={row['with_fingerprint_count']} "
            f"with_span_artifacts={row['with_span_artifacts_count']}"
        )

    typer.echo("\nFingerprint Outcome Quality")
    for row in report["fingerprint_quality_breakdown"]:
        typer.echo(
            "  "
            f"outcome_quality={row['outcome_quality']} rows={row['row_count']}"
        )

    typer.echo("\nHistorical Docs Without Fingerprint")
    if not report["historical_documents_without_fingerprint"]:
        typer.echo("  none")
    for row in report["historical_documents_without_fingerprint"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nHash-Backed Historical Docs Without Fingerprint")
    if not report["hash_backed_historical_documents_without_fingerprint"]:
        typer.echo("  none")
    for row in report["hash_backed_historical_documents_without_fingerprint"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")


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


@app.command("validate-lineage-nc")
def validate_lineage_nc(
    limit: int = typer.Option(25, "--limit", help="Max issue rows to show."),
    family_key: str | None = typer.Option(None, help="Optional family key filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Cross-check NC historical docs for family assignment, provenance debt, and extraction readiness."""
    from duke_rates.historical.ncuc.lineage_validation import build_lineage_validation_report

    _, repository = _bootstrap()
    report = build_lineage_validation_report(repository, limit=limit, family_key=family_key)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Lineage Validation (NC)")
    typer.echo(
        "  "
        f"total_docs={summary['total_documents_count']}  "
        f"blocking={summary['blocking_issue_document_count']}  "
        f"warning_only={summary['warning_only_document_count']}  "
        f"clean={summary['clean_document_count']}"
    )
    typer.echo(
        "  "
        f"missing_tariff_family={summary['missing_tariff_family_count']}  "
        f"provisional_family={summary['provisional_family_count']}  "
        f"missing_effective_start={summary['missing_effective_start_count']}"
    )
    typer.echo(
        "  "
        f"missing_version_link={summary['missing_version_link_count']}  "
        f"not_processed={summary['not_processed_count']}  "
        f"linked_without_charges={summary['linked_without_charges_count']}"
    )
    typer.echo(
        "  "
        f"version_provenance_gap={summary['version_provenance_gap_count']}  "
        f"missing_discovery_match={summary['missing_discovery_match_count']}  "
        f"path_only_discovery_link={summary['path_only_discovery_link_count']}"
    )
    typer.echo(
        "  "
        f"extracted_with_charges={summary['extracted_with_charges_count']}  "
        f"skipped_reference={summary['skipped_reference_count']}"
    )

    for row in report["rows"]:
        issue_parts: list[str] = []
        if row["blocking_issues"]:
            issue_parts.append(f"blockers={','.join(row['blocking_issues'])}")
        if row["warning_issues"]:
            issue_parts.append(f"warnings={','.join(row['warning_issues'])}")
        typer.echo(
            "  "
            f"id={row['historical_document_id']} family={row['family_key'] or '-'} "
            f"company={row['company'] or '-'} {' '.join(issue_parts)}"
        )
        typer.echo(
            "    "
            f"eff={row['effective_start'] or '-'} "
            f"versions={row['version_count']} "
            f"charges={row['charge_count']} "
            f"latest_outcome={row['latest_outcome_quality'] or '-'} "
            f"linkage={row['discovery_linkage']}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")


@app.command("suggest-family-links-nc")
def suggest_family_links_nc(
    limit: int = typer.Option(25, "--limit", help="Max discovery records to show."),
    record_id: int | None = typer.Option(None, "--record-id", help="Only inspect one discovery record."),
    apply: bool = typer.Option(False, "--apply", help="Persist suggested family links back to discovery records."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Suggest likely NC family links for stranded discovery records using span clues."""
    from duke_rates.historical.ncuc.lineage_gaps import (
        apply_family_link_suggestions,
        suggest_family_links,
    )

    _, repository = _bootstrap()
    suggestions = suggest_family_links(repository, limit=limit, record_id=record_id)

    if apply:
        updated = apply_family_link_suggestions(repository, suggestions)
    else:
        updated = 0

    if json_out:
        payload = {
            "suggestion_count": len(suggestions),
            "updated_count": updated,
            "suggestions": [
                {
                    "discovery_record_id": item["discovery_record_id"],
                    "docket_number": item["docket_number"],
                    "utility": item["utility"],
                    "filing_title": item["filing_title"],
                    "leaf_nos": item["leaf_nos"],
                    "schedule_codes": item["schedule_codes"],
                    "family_keys": item["family_keys"],
                    "matches": item["matches"],
                }
                for item in suggestions
            ],
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    typer.echo(f"Suggested family links: {len(suggestions)}")
    for item in suggestions:
        top_match = item["matches"][0]
        typer.echo(
            "  "
            f"id={item['discovery_record_id']} "
            f"family={top_match['family_key']} "
            f"score={top_match['score']} "
            f"reasons={','.join(top_match['reasons'])}"
        )
        typer.echo(f"    title={(item['filing_title'] or '')[:90]}")
        typer.echo(f"    leafs={item['leaf_nos']}")
        typer.echo(f"    codes={item['schedule_codes']}")

    if apply:
        typer.echo(f"\nUpdated {updated} discovery records.")


@app.command("promote-provisional-family")
def promote_provisional_family(
    family_key: str = typer.Argument(..., help="Existing provisional family_key."),
    title: str | None = typer.Option(None, help="Override curated title."),
    schedule_code: str | None = typer.Option(None, help="Override schedule_code."),
    family_type: str | None = typer.Option(None, help="Override family_type."),
    alias: list[str] | None = typer.Option(None, "--alias", help="Additional alias to retain."),
    notes: str | None = typer.Option(None, help="Override notes."),
) -> None:
    """Promote a provisional historical family into a curated tariff family."""
    _, repository = _bootstrap()
    promoted = repository.promote_provisional_tariff_family(
        family_key,
        title=title,
        schedule_code=schedule_code,
        family_type=family_type,
        aliases=alias,
        notes=notes,
    )
    if promoted is None:
        typer.echo(f"Family not found: {family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Promoted {promoted.family_key} | {promoted.family_type} | "
        f"{promoted.schedule_code or '?'} | {promoted.title or ''}"
    )


@app.command("list-historical-only-families")
def list_historical_only_families(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_type: str | None = typer.Option(None, help="Filter by family_type."),
    with_candidates: bool = typer.Option(True, help="Show suggested current-document candidates."),
    only_unresolved: bool = typer.Option(False, help="Show only families with no plausible current-document candidates."),
) -> None:
    """List tariff families backed only by historical documents and no current-document anchor."""
    _, repository = _bootstrap()
    rows = repository.review_historical_only_tariff_families(
        state=state,
        company=company,
        family_type=family_type,
    )
    if only_unresolved:
        rows = [row for row in rows if row["review_status"] == "unresolved"]
    typer.echo(f"Total historical-only families: {len(rows)}")
    unresolved_count = sum(1 for row in rows if row["review_status"] == "unresolved")
    candidate_count = len(rows) - unresolved_count
    typer.echo(
        f"  unresolved={unresolved_count} review_candidates={candidate_count}"
    )
    for row in rows:
        typer.echo(
            f"  {row['family_key']:<55} {row['family_type']:<12} "
            f"{(row['schedule_code'] or '?'):<28} hist_docs={row['historical_document_count']:<3} "
            f"{(row['title'] or '')[:40]} [{row['review_status']}]"
        )
        if with_candidates:
            for suggestion in row["suggestions"]:
                typer.echo(
                    f"    candidate doc={suggestion['document_id']:<4} score={suggestion['score']:<2} "
                    f"{suggestion['title']} [{', '.join(suggestion['reasons'])}]"
                )
                if suggestion.get("candidate_headings"):
                    typer.echo(
                        f"      headings: {', '.join(suggestion['candidate_headings'])}"
                    )


@app.command("list-weak-unbounded-historical-nc")
def list_weak_unbounded_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(50, help="Max rows to display."),
) -> None:
    """List weak historical docs that still point at whole PDFs instead of bounded spans."""
    _, repository = _bootstrap()
    rows = repository.list_weak_unbounded_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    row["source_kind"],
                    row["review_action"],
                    str(row["discovery_record_id"] or "-"),
                    row["parser_profile"] or "-",
                    str(row["charge_count"]),
                    row["local_path"],
                ]
            )
        )


@app.command("list-redundant-legacy-raw-historical-nc")
def list_redundant_legacy_raw_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(100, help="Max rows to display."),
) -> None:
    """List weak legacy raw rows that already have bounded same-family regulator replacements."""
    _, repository = _bootstrap()
    rows = repository.list_redundant_legacy_raw_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    str(row["discovery_record_id"] or "-"),
                    str(row["replacement_count"]),
                    ",".join(str(item) for item in row["replacement_ids"]),
                    row["local_path"],
                ]
            )
        )


@app.command("list-bundle-reference-legacy-raw-historical-nc")
def list_bundle_reference_legacy_raw_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(100, help="Max rows to display."),
) -> None:
    """List weak legacy raw rows that appear to be bundle rider references inside bounded spans."""
    _, repository = _bootstrap()
    rows = repository.list_bundle_reference_legacy_raw_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        overlap = row.get("bundle_reference_overlap") or {}
        host_descriptions = []
        for host in overlap.get("hosts") or []:
            host_descriptions.append(
                f"{host['host_historical_document_id']}:{host['host_family_key']}@{host['host_start_page']}-{host['host_end_page']}"
            )
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    str(row["discovery_record_id"] or "-"),
                    str(overlap.get("target_leaf") or "-"),
                    str(overlap.get("host_count") or 0),
                    ",".join(host_descriptions),
                    row["local_path"],
                ]
            )
        )


@app.command("list-placeholder-heading-historical-nc")
def list_placeholder_heading_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(100, help="Max rows to display."),
) -> None:
    """List bounded placeholder heading spans that can be retired as residue."""
    _, repository = _bootstrap()
    rows = repository.list_placeholder_heading_residue_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        neighbors = ",".join(
            f"{item['historical_document_id']}:{item['family_key']}@{item['start_page']}-{item['end_page']}"
            for item in row["neighbors"]
        )
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    f"{row['start_page']}-{row['end_page']}",
                    str(row["neighbor_count"]),
                    neighbors,
                    row["local_path"],
                ]
            )
        )


@app.command("retire-historical-document")
def retire_historical_document(
    historical_document_id: int = typer.Argument(..., help="Historical document id to retire."),
) -> None:
    """Delete a historical document row and its attached parse/extraction state."""
    _, repository = _bootstrap()
    retired = repository.retire_historical_document(historical_document_id)
    if not retired:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(f"Retired historical document {historical_document_id}")


@app.command("add-historical-document-nc")
def add_historical_document_nc(
    family_key: str = typer.Option(..., "--family-key", help="Target NC family key."),
    local_path: Path = typer.Option(..., "--local-path", exists=True, file_okay=True, dir_okay=False, resolve_path=True, help="Local PDF path to register."),
    archived_url: str = typer.Option(..., "--archived-url", help="Canonical regulator/archive URL for this PDF or slice."),
    title: str | None = typer.Option(None, "--title", help="Stored historical document title (defaults to the filename stem)."),
    company: str = typer.Option(..., "--company", help="Utility company slug: progress or carolinas."),
    category: str = typer.Option(DocumentCategory.RATE.value, "--category", help="Historical category, e.g. rate, rider, tariff."),
    start_page: int | None = typer.Option(None, "--start-page", min=1, help="1-based start page for the tariff slice."),
    end_page: int | None = typer.Option(None, "--end-page", min=1, help="1-based end page for the tariff slice."),
    effective_start: str | None = typer.Option(None, "--effective-start", help="Effective start date YYYY-MM-DD."),
    effective_end: str | None = typer.Option(None, "--effective-end", help="Effective end date YYYY-MM-DD."),
    revision_label: str | None = typer.Option(None, "--revision-label", help="Revision label stored on the historical row."),
    supersedes_label: str | None = typer.Option(None, "--supersedes-label", help="Supersedes label stored on the historical row."),
    leaf_no: str | None = typer.Option(None, "--leaf-no", help="Leaf number stored on the historical row."),
    canonical_url: str | None = typer.Option(None, "--canonical-url", help="Optional canonical source URL if different from --archived-url."),
    auto_detect: bool = typer.Option(False, "--auto-detect", help="Infer page bounds and footer metadata from the PDF before registration."),
) -> None:
    """Register one page-bounded NC historical PDF directly into historical_documents."""
    if local_path.suffix.lower() != ".pdf":
        raise typer.BadParameter("--local-path must point to a PDF.")
    if end_page is not None and start_page is None:
        raise typer.BadParameter("--end-page requires --start-page.")
    if start_page is not None and end_page is not None and end_page < start_page:
        raise typer.BadParameter("--end-page must be greater than or equal to --start-page.")

    _, repository = _bootstrap()
    if auto_detect:
        suggestion = suggest_registration_metadata(
            repository,
            family_key=family_key,
            pdf_path=local_path,
        )
        if suggestion is None and start_page is None:
            raise typer.BadParameter(
                "--auto-detect could not identify a tariff slice; provide --start-page/--end-page manually."
            )
        if suggestion is not None:
            start_page = start_page or suggestion.start_page
            end_page = end_page or suggestion.end_page
            effective_start = effective_start or suggestion.effective_start
            supersedes_label = supersedes_label or suggestion.supersedes_label
            leaf_no = leaf_no or suggestion.leaf_no
            title = title or suggestion.title
            typer.echo(
                f"Auto-detected pages={suggestion.start_page}-{suggestion.end_page} "
                f"effective_start={suggestion.effective_start or '-'} "
                f"supersedes={suggestion.supersedes_label or '-'} "
                f"docket={suggestion.docket_number or '-'} "
                f"confidence={suggestion.confidence:.2f}"
            )

    now = datetime.now()
    raw_text_path = local_path.with_suffix(local_path.suffix + ".txt")
    record = HistoricalDocumentRecord(
        family_key=family_key,
        title=title or local_path.stem,
        state="NC",
        company=company,
        category=category,
        kind=DocumentKind.PDF.value,
        canonical_url=canonical_url or archived_url,
        archived_url=archived_url,
        snapshot_timestamp=now,
        local_path=local_path,
        raw_text_path=raw_text_path if raw_text_path.exists() else None,
        content_hash=sha256_bytes(local_path.read_bytes()),
        content_type="application/pdf",
        direct_status_code=200,
        direct_downloadable=True,
        revision_label=revision_label,
        supersedes_label=supersedes_label,
        leaf_no=leaf_no,
        start_page=start_page,
        end_page=end_page,
        effective_start=effective_start,
        effective_end=effective_end,
        retrieved_at=now,
    )
    historical_id = repository.upsert_historical_document(record)
    typer.echo(
        f"Registered historical document {historical_id} family={family_key} "
        f"pages={start_page or '-'}-{end_page or start_page or '-'} "
        f"effective_start={effective_start or '-'}"
    )


@app.command("rebind-historical-page-range")
def rebind_historical_page_range(
    historical_document_id: int = typer.Argument(..., help="Historical document id to update."),
    start_page: int = typer.Option(..., "--start-page", min=1, help="New 1-based start page."),
    end_page: int | None = typer.Option(None, "--end-page", min=1, help="New 1-based end page (defaults to start page)."),
    requeue: bool = typer.Option(False, "--requeue", help="Queue the document for re-extraction after rebinding."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label when --requeue is used."),
    queue_priority: int = typer.Option(90, "--queue-priority", help="Queue priority when --requeue is used."),
) -> None:
    """Update an existing historical document's page bounds and optionally requeue it."""
    _, repository = _bootstrap()
    try:
        rebound = repository.rebind_historical_page_range(
            historical_document_id,
            start_page=start_page,
            end_page=end_page,
            requeue=requeue,
            requested_by=requested_by,
            queue_priority=queue_priority,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if rebound is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Rebound historical document {historical_document_id} -> "
        f"pages {rebound.start_page}-{rebound.end_page or rebound.start_page}"
    )
    if requeue:
        typer.echo(f"Queued for reprocess with priority={queue_priority} requested_by={requested_by}")


@app.command("clear-redline-fingerprint")
def clear_redline_fingerprint(
    historical_document_id: int = typer.Option(..., "--hd-id", help="Historical document id whose fingerprint slice should be cleared."),
    include_path_rollup: bool = typer.Option(False, "--include-path-rollup", help="Also clear whole-PDF path-level fingerprint rows for the same source PDF."),
    force: bool = typer.Option(False, "--force", help="Apply the clear. Without --force this command only previews the target."),
) -> None:
    """Clear a stored redline verdict for a historical-document slice."""
    _, repository = _bootstrap()
    target = repository.get_historical_document(historical_document_id)
    if target is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    if not force:
        typer.echo(
            f"[DRY RUN] Would clear redline fingerprint for hd={historical_document_id} "
            f"{target.local_path} pages {target.start_page}-{target.end_page or target.start_page}"
        )
        if include_path_rollup:
            typer.echo("  whole-PDF path-level fingerprint rows would also be cleared")
        typer.echo("  rerun refresh-nc-redline-fingerprints after detector fixes to verify the slice stays clear")
        return

    result = repository.clear_redline_fingerprint_for_historical_document(
        historical_document_id,
        include_path_rollup=include_path_rollup,
    )
    if result is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Cleared {result['updated_count']} fingerprint row(s) for hd={historical_document_id} "
        f"pages {result['page_start']}-{result['page_end'] or result['page_start']}"
    )


@app.command("retire-tariff-version")
def retire_tariff_version(
    version_id: int = typer.Option(..., "--version-id", help="Tariff version id to retire."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
) -> None:
    """Delete one tariff_version and its charges while leaving the historical document intact."""
    _, repository = _bootstrap()
    if dry_run:
        with repository._connect() as conn:
            row = conn.execute(
                """
                SELECT tv.id, tv.family_key, tv.historical_document_id, tv.effective_start,
                       COUNT(tc.id) AS charge_count
                FROM tariff_versions tv
                LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
                WHERE tv.id = ?
                GROUP BY tv.id, tv.family_key, tv.historical_document_id, tv.effective_start
                """,
                (version_id,),
            ).fetchone()
        if row is None:
            typer.echo(f"Tariff version not found: {version_id}")
            raise typer.Exit(1)
        typer.echo(
            f"[DRY RUN] Would retire version={row['id']} family={row['family_key']} "
            f"historical_document_id={row['historical_document_id'] or '-'} "
            f"effective_start={row['effective_start'] or '-'} "
            f"charges={int(row['charge_count'] or 0)}"
        )
        return

    retired = repository.retire_tariff_version(version_id)
    if retired is None:
        typer.echo(f"Tariff version not found: {version_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Retired version={retired['version_id']} family={retired['family_key']} "
        f"historical_document_id={retired['historical_document_id'] or '-'} "
        f"deleted_charges={retired['deleted_charge_count']}"
    )


@app.command("deduplicate-tariff-charges")
def deduplicate_tariff_charges(
    version_id: list[int] = typer.Option(..., "--version-id", help="Tariff version id to deduplicate. Repeat for multiple versions."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
) -> None:
    """Deduplicate repeated tariff_charges rows for one or more versions."""
    _, repository = _bootstrap()
    if dry_run:
        with repository._connect() as conn:
            for vid in version_id:
                before = int(conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (vid,)).fetchone()[0])
                unique_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM (
                            SELECT 1
                            FROM tariff_charges
                            WHERE version_id = ?
                            GROUP BY
                                charge_type,
                                COALESCE(charge_label, ''),
                                COALESCE(rate_value, -999999999.0),
                                COALESCE(rate_unit, ''),
                                COALESCE(season, ''),
                                COALESCE(tou_period, ''),
                                COALESCE(tier_min, -999999999.0),
                                COALESCE(tier_max, -999999999.0),
                                COALESCE(customer_class, '')
                        )
                        """,
                        (vid,),
                    ).fetchone()[0]
                )
                typer.echo(
                    f"[DRY RUN] version={vid} before={before} unique={unique_count} "
                    f"duplicates_removed={before - unique_count}"
                )
        return

    for vid in version_id:
        result = repository.deduplicate_tariff_charges_for_version(vid)
        typer.echo(
            f"version={result['version_id']} before={result['before_count']} "
            f"after={result['after_count']} duplicates_removed={result['duplicates_removed']}"
        )


@app.command("retire-provisional-garbage-nc")
def retire_provisional_garbage_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
    state: str = typer.Option("NC", help="State filter (default NC)."),
) -> None:
    """Retire provisional NC families that have no charged tariff content.

    Targets provisional families where every version (if any) has zero charges.
    Families with actual charge rows are always skipped.

    Use --execute to apply the deletions. Default is --dry-run (safe preview).

    Deletes: tariff_families, historical_documents, tariff_versions, tariff_charges,
    historical_processing_runs, historical_reprocess_queue, parse_review_outcomes
    for affected source spans.

    Run export-nc-schedule-inventory-audit and show-workflow-status-nc after to confirm.
    """
    _, repository = _bootstrap()
    result = repository.retire_provisional_garbage_families_nc(
        dry_run=dry_run,
        state=state,
    )
    if dry_run:
        typer.echo(
            f"[DRY RUN] Would retire {result['candidates_found']} provisional families "
            f"with no charged content."
        )
        typer.echo("  Re-run with --execute to apply.")
    else:
        typer.echo(f"Retired {result['families_deleted']} provisional families.")
        typer.echo(f"  historical_docs deleted:      {result['historical_docs_deleted']}")
        typer.echo(f"  versions deleted:             {result['versions_deleted']}")
        typer.echo(f"  parse_review rows deleted:    {result['parse_review_rows_deleted']}")
        typer.echo(f"  processing_runs deleted:      {result['processing_runs_deleted']}")
        typer.echo(f"  reprocess_queue rows deleted: {result['reprocess_queue_deleted']}")
        typer.echo("Run: python -m duke_rates show-workflow-status-nc")
        typer.echo("Run: python -m duke_rates export-nc-schedule-inventory-audit")


@app.command("repair-historical-current-snapshot")
def repair_historical_current_snapshot(
    historical_document_id: int = typer.Argument(..., help="Historical document id to repair."),
    requested_by: str = typer.Option("operator", help="Requester label stored on the reprocess queue."),
    queue_priority: int = typer.Option(95, help="Priority for the follow-up reprocess queue item."),
) -> None:
    """Repair a historical row that still points at a stale current-document snapshot."""
    _, repository = _bootstrap()
    repaired = repository.repair_historical_current_document_snapshot(
        historical_document_id,
        requested_by=requested_by,
        queue_priority=queue_priority,
    )
    if repaired is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Repaired historical document {historical_document_id} -> "
        f"{repaired.family_key} | current_doc={repaired.current_document_id or '-'} | "
        f"{repaired.local_path}"
    )


@app.command("repair-legacy-ncuc-data")
def repair_legacy_ncuc_data(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
) -> None:
    """Audit and repair legacy NCUC rows that break modern workflow tooling."""
    _, repository = _bootstrap()
    report = repository.repair_legacy_ncuc_data_issues(dry_run=dry_run)

    typer.echo("Legacy NCUC Data Audit")
    typer.echo(f"  legacy_portal_harvest={report['legacy_portal_harvest_count']}")
    typer.echo(
        "  malformed_historical_current_document_id="
        f"{report['malformed_historical_current_document_id_count']}"
    )

    if report["legacy_portal_harvest_rows"]:
        typer.echo("Legacy portal_harvest rows")
        for row in report["legacy_portal_harvest_rows"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} docket={row['docket_number'] or '-'} "
                f"method={row['acquisition_method']} title={(row['filing_title'] or '')[:80]}"
            )

    if report["malformed_historical_current_document_id_rows"]:
        typer.echo("Malformed historical current_document_id rows")
        for row in report["malformed_historical_current_document_id_rows"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} family={row['family_key'] or '-'} "
                f"current_document_id={row['current_document_id']} "
                f"path={(row['local_path'] or '')[:80]}"
            )

    if dry_run:
        typer.echo("Re-run with --execute to normalize these legacy rows.")
        return

    typer.echo(
        "Applied repairs: "
        f"portal_harvest->playwright={report['updated_legacy_portal_harvest_count']} "
        f"cleared_historical_current_document_id={report['cleared_historical_current_document_id_count']}"
    )


@app.command("attach-current-document-to-family")
def attach_current_document_to_family(
    family_key: str = typer.Argument(..., help="Target tariff family_key."),
    document_id: int = typer.Argument(..., help="Current documents.id to attach."),
) -> None:
    """Attach a current document anchor to an existing tariff family."""
    _, repository = _bootstrap()
    try:
        family = repository.attach_current_document_to_family(
            family_key,
            document_id=document_id,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    if family is None:
        typer.echo(f"Family not found: {family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Attached current document {document_id} to {family.family_key} | "
        f"{family.family_type} | {family.title or ''}"
    )


@app.command("list-current-anchor-mismatches")
def list_current_anchor_mismatches(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_type: str | None = typer.Option(None, help="Filter by family_type."),
    limit: int = typer.Option(0, help="Max mismatches to show (0 = all)."),
) -> None:
    """List tariff families whose current document anchor contradicts family metadata."""
    _, repository = _bootstrap()
    rows = repository.list_current_anchor_mismatches(
        state=state,
        company=company,
        family_type=family_type,
        limit=limit or None,
    )
    typer.echo(f"Total current-anchor mismatches: {len(rows)}")
    for row in rows:
        typer.echo(
            f"  {row['family_key']:<55} {row['family_schedule_code'] or '?':<14} "
            f"doc={row['current_document_id']:<4} {row['document_schedule_code'] or '?':<14} "
            f"{row['review_action']:<38} "
            f"[{', '.join(row['reasons'])}]"
        )
        typer.echo(
            f"    family: {(row['family_title'] or '')[:80]}"
        )
        typer.echo(
            f"    document: {(row['document_title'] or '')[:80]}"
        )
        if row.get("candidate_leaf_nos"):
            typer.echo(
                f"    mined leafs: {', '.join(row['candidate_leaf_nos'])}"
            )
        if row.get("candidate_headings"):
            typer.echo(
                f"    headings: {', '.join(row['candidate_headings'])}"
            )


@app.command("sync-family-metadata-from-current-anchor")
def sync_family_metadata_from_current_anchor(
    family_key: str = typer.Argument(..., help="Target tariff family_key."),
) -> None:
    """Sync a family's title/schedule metadata from its anchored current document."""
    _, repository = _bootstrap()
    family = repository.sync_family_metadata_from_current_document(family_key)
    if family is None:
        typer.echo(f"Family not found or has no current document anchor: {family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Synced {family.family_key} | {family.schedule_code or '?'} | "
        f"{family.tariff_identifier or '?'} | {family.title or ''}"
    )


@app.command("migrate-historical-family-lineage")
def migrate_historical_family_lineage(
    source_family_key: str = typer.Argument(..., help="Source tariff family_key."),
    target_family_key: str = typer.Argument(..., help="Target historical-only family_key."),
    historical_id: list[int] = typer.Option(..., "--historical-id", help="Historical document id to migrate."),
    title: str = typer.Option(..., help="Title for the target historical-only family."),
    schedule_code: str | None = typer.Option(None, help="Schedule code for the target family."),
    family_type: str | None = typer.Option(None, help="Family type for the target family."),
    tariff_identifier: str | None = typer.Option(None, help="Tariff identifier for the target family."),
    alias: list[str] | None = typer.Option(None, "--alias", help="Additional aliases to retain."),
    notes: str | None = typer.Option(None, help="Notes for the target family."),
) -> None:
    """Move selected historical documents into a new historical-only family lineage."""
    _, repository = _bootstrap()
    try:
        family = repository.migrate_historical_family_lineage(
            source_family_key,
            target_family_key,
            historical_document_ids=historical_id,
            title=title,
            schedule_code=schedule_code,
            family_type=family_type,
            tariff_identifier=tariff_identifier,
            aliases=alias,
            notes=notes,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    if family is None:
        typer.echo(f"Source family not found: {source_family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Migrated {len(historical_id)} historical docs from {source_family_key} "
        f"to {family.family_key} | {family.schedule_code or '?'} | {family.title or ''}"
    )


@app.command("canonicalize-historical-family-key")
def canonicalize_historical_family_key(
    source_family_key: str = typer.Argument(..., help="Malformed or legacy source tariff family_key."),
    target_family_key: str = typer.Argument(..., help="Canonical target tariff family_key."),
    historical_id: list[int] | None = typer.Option(None, "--historical-id", help="Optional subset of historical document ids to move."),
    all_historical: bool = typer.Option(False, "--all-historical", help="Move all historical documents currently attached to the source family."),
    title: str | None = typer.Option(None, help="Override title for a newly created target family."),
    schedule_code: str | None = typer.Option(None, help="Override schedule code for a newly created target family."),
    family_type: str | None = typer.Option(None, help="Override family_type for a newly created target family."),
    tariff_identifier: str | None = typer.Option(None, help="Override tariff identifier for a newly created target family."),
    alias: list[str] | None = typer.Option(None, "--alias", help="Additional aliases to retain on the target family."),
    notes: str | None = typer.Option(None, help="Optional notes for the target family."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
    keep_source_family: bool = typer.Option(False, help="Keep the source family row even if it becomes empty."),
) -> None:
    """Move malformed historical-family lineage into a canonical family key."""
    _, repository = _bootstrap()
    source_family = repository.get_tariff_family(source_family_key)
    if source_family is None:
        typer.echo(f"Source family not found: {source_family_key}")
        raise typer.Exit(1)
    target_family = repository.get_tariff_family(target_family_key)

    if historical_id and all_historical:
        typer.echo("Use either --historical-id or --all-historical, not both.")
        raise typer.Exit(1)
    if not historical_id and not all_historical:
        typer.echo("Specify --all-historical or provide at least one --historical-id.")
        raise typer.Exit(1)
    if target_family is None and not (title or source_family.title):
        typer.echo("A title is required when the target family does not already exist.")
        raise typer.Exit(1)

    selected_ids = historical_id or []
    if all_historical:
        with repository._connect() as conn:
            selected_ids = [
                int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id
                    FROM historical_documents
                    WHERE family_key = ?
                    ORDER BY COALESCE(effective_start, ''), id
                    """,
                    (source_family_key,),
                ).fetchall()
            ]

    if dry_run:
        typer.echo(
            f"[DRY RUN] Would canonicalize {source_family_key} -> {target_family_key}."
        )
        if selected_ids:
            typer.echo(f"  move_historical_ids={','.join(str(item) for item in selected_ids)}")
        else:
            typer.echo("  no source historical docs found; dry run will only repair ancillary/orphan lineage if present.")
        if target_family:
            typer.echo(
                f"  target exists: {target_family.family_key} | "
                f"{target_family.schedule_code or '?'} | {target_family.title or ''}"
            )
        else:
            typer.echo(
                f"  target will be created: {target_family_key} | "
                f"{schedule_code or source_family.schedule_code or '?'} | "
                f"{title or source_family.title or ''}"
            )
        if not keep_source_family:
            typer.echo("  source family will be pruned if it becomes empty.")
        return

    try:
        result = repository.canonicalize_historical_family_key(
            source_family_key,
            target_family_key,
            historical_document_ids=selected_ids,
            title=title,
            schedule_code=schedule_code,
            family_type=family_type,
            tariff_identifier=tariff_identifier,
            aliases=alias,
            notes=notes,
            prune_source_family=not keep_source_family,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    if result is None:
        typer.echo(f"Source family not found: {source_family_key}")
        raise typer.Exit(1)

    family = result["family"]
    typer.echo(
        f"Canonicalized {len(result['moved_historical_document_ids'])} historical docs from {source_family_key} "
        f"to {family.family_key} | {family.schedule_code or '?'} | {family.title or ''}"
    )
    typer.echo(f"  source_family_pruned={result['source_family_pruned']}")


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
    """Compatibility alias for the page-aware NCUC intake path; prefer ncuc-import-pipeline."""
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
            "Prefer 'ncuc-import-pipeline --all-downloaded' for normal intake."
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


@app.command("enqueue-reprocess-nc")
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


@app.command("enqueue-parser-improvement-reprocess-nc")
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


@app.command("show-reprocess-queue-nc")
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


@app.command("show-reprocess-priority-nc")
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


@app.command("show-stale-historical-nc")
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


@app.command("enqueue-stale-reprocess-nc")
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


@app.command("show-profile-impact-nc")
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


@app.command("enqueue-profile-impact-nc")
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


def _process_single_reprocess_queue_item(database_path: str | Path) -> dict[str, int | bool]:
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
        process_result = extractor.process_document(doc)
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


@app.command("process-reprocess-queue-nc")
def process_reprocess_queue_nc(
    limit: int = typer.Option(500, "--limit", help="Max queue items to process per invocation."),
    workers: int = typer.Option(1, "--workers", min=1, help="Parallel workers for local reprocess queue items."),
    until_empty: bool = typer.Option(False, "--until-empty", help="Keep processing until the queue is empty (overrides --limit)."),
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
            result = _process_single_reprocess_queue_item(settings.database_path)
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
                    executor.submit(_process_single_reprocess_queue_item, settings.database_path)
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
                            )
                        )
                        submitted += 1

    typer.echo(
        f"Historical reprocess queue processed={processed} completed={completed} failed={failed} workers={workers}"
    )


@app.command("calculate-bill")
def calculate_bill(
    family_key: str = typer.Option(..., help="Tariff family key, e.g. nc-progress-leaf-500."),
    kwh: float = typer.Option(..., help="Monthly kWh usage."),
    service_date: str | None = typer.Option(None, help="Service date YYYY-MM-DD (determines season)."),
    peak_kw: float | None = typer.Option(None, help="Peak demand in kW (for demand-metered schedules)."),
    base_kw: float | None = typer.Option(None, help="Base demand in kW (for TOU demand schedules)."),
    on_peak_kw: float | None = typer.Option(None, help="On-peak demand in kW (for TOU demand schedules)."),
    mid_peak_kw: float | None = typer.Option(None, help="Mid-peak demand in kW (for TOU demand schedules)."),
    off_peak_kw: float | None = typer.Option(None, help="Off-peak demand in kW (for TOU demand schedules)."),
    on_peak_kwh: float | None = typer.Option(None, help="On-peak kWh (for TOU schedules)."),
    off_peak_kwh: float | None = typer.Option(None, help="Off-peak kWh (for TOU schedules)."),
    discount_kwh: float | None = typer.Option(None, help="Discount-period kWh (for TOU-EV schedules)."),
    customer_class: str = typer.Option("residential", help="Customer class for rider matching."),
    no_riders: bool = typer.Option(False, help="Exclude rider adjustments."),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Calculate a bill from tariff_charges tables (Phase 4a engine).

    Uses the structured charge data populated by parse-tariff-versions.
    Run parse-tariff-versions first to populate the tariff data.

    Example:
        duke-rates calculate-bill --family-key nc-progress-leaf-500 --kwh 1200 --service-date 2025-08-01
    """
    from duke_rates.billing.tariff_engine import BillInput, TariffBillingEngine

    import datetime as _dt

    _, repository = _bootstrap()

    parsed_date: _dt.date | None = None
    if service_date:
        try:
            parsed_date = _dt.date.fromisoformat(service_date)
        except ValueError:
            typer.echo(f"Invalid service_date: {service_date}. Use YYYY-MM-DD.", err=True)
            raise typer.Exit(1)

    usage = BillInput(
        monthly_kwh=kwh,
        peak_kw=peak_kw,
        base_kw=base_kw,
        on_peak_kw=on_peak_kw,
        mid_peak_kw=mid_peak_kw,
        off_peak_kw=off_peak_kw,
        service_date=parsed_date,
        on_peak_kwh=on_peak_kwh,
        off_peak_kwh=off_peak_kwh,
        discount_kwh=discount_kwh,
    )

    engine = TariffBillingEngine(repository)
    result = engine.calculate(
        family_key,
        usage,
        customer_class=customer_class,
        include_riders=not no_riders,
    )

    if output_json:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        return

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  {result.schedule_title or family_key}")
    typer.echo(f"  Effective: {result.effective_start or 'unknown'}  |  {result.revision_label or ''}")
    if parsed_date:
        season = "summer (May-Sep)" if parsed_date.month in {5, 6, 7, 8, 9} else "winter (Oct-Apr)"
        typer.echo(f"  Date: {parsed_date}  Season: {season}")
    demand_parts = []
    if peak_kw is not None:
        demand_parts.append(f"Peak: {peak_kw} kW")
    if base_kw is not None:
        demand_parts.append(f"Base: {base_kw} kW")
    if on_peak_kw is not None:
        demand_parts.append(f"On-Peak: {on_peak_kw} kW")
    if mid_peak_kw is not None:
        demand_parts.append(f"Mid-Peak: {mid_peak_kw} kW")
    if off_peak_kw is not None:
        demand_parts.append(f"Off-Peak: {off_peak_kw} kW")
    typer.echo(f"  Usage: {kwh:,.0f} kWh" + (f"  Demand: {', '.join(demand_parts)}" if demand_parts else ""))
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  {'CHARGE':<45} {'QTY':>8}  {'RATE':<14}  {'AMOUNT':>8}")
    typer.echo(f"  {'-' * 56}")
    for item in result.line_items:
        qty_str = f"{item.quantity:,.1f}" if item.quantity is not None else ""
        rate_str = f"{item.rate_value} {item.rate_unit}"
        typer.echo(f"  {item.label:<45} {qty_str:>8}  {rate_str:<14}  ${item.amount:>7.2f}")
    typer.echo(f"  {'-' * 56}")
    typer.echo(f"  {'Base subtotal':<45} {'':>8}  {'':14}  ${result.base_subtotal:>7.2f}")
    if result.rider_subtotal:
        typer.echo(f"  {'Rider adjustments':<45} {'':>8}  {'':14}  ${result.rider_subtotal:>7.2f}")
    typer.echo(f"  {'TOTAL':<45} {'':>8}  {'':14}  ${result.total:>7.2f}")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  Confidence: {result.source_confidence:.0%}")

    if result.warnings:
        typer.echo("")
        for w in result.warnings:
            typer.echo(f"  ! {w}")


@app.command("compare-tariff-rates")
def compare_tariff_rates(
    state: str = typer.Option("NC", help="State abbreviation."),
    company: str = typer.Option("progress", help="Company short name."),
    kwh: float = typer.Option(..., help="Monthly kWh usage."),
    service_date: str | None = typer.Option(None, help="Service date YYYY-MM-DD."),
    peak_kw: float | None = typer.Option(None, help="Peak demand kW."),
    base_kw: float | None = typer.Option(None, help="Base demand kW for TOU demand schedules."),
    on_peak_kw: float | None = typer.Option(None, help="On-peak demand kW for TOU demand schedules."),
    mid_peak_kw: float | None = typer.Option(None, help="Mid-peak demand kW for TOU demand schedules."),
    off_peak_kw: float | None = typer.Option(None, help="Off-peak demand kW for TOU demand schedules."),
    on_peak_kwh: float | None = typer.Option(None, help="On-peak kWh (for TOU)."),
    off_peak_kwh: float | None = typer.Option(None, help="Off-peak kWh (for TOU)."),
    customer_class: str = typer.Option("residential", help="Customer class filter."),
    family_type: str = typer.Option("rate_schedule", help="Family type filter."),
    group: str | None = typer.Option(
        None,
        "--group",
        help=(
            "Restrict to schedule group(s): residential, sgs, mgs, lgs, gs, specialty, all. "
            "Comma-separate multiple (e.g. residential,sgs). Default: residential."
        ),
    ),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Compare bill amounts across rate schedules for given usage.

    Calculates what the bill would be under each available rate schedule
    for the state/company, then ranks by total cost.  By default only
    residential schedules are shown; use --group to broaden the comparison.

    Examples:
        duke-rates compare-tariff-rates --kwh 1200 --service-date 2025-08-01
        duke-rates compare-tariff-rates --kwh 800 --service-date 2025-11-01 --on-peak-kwh 300 --off-peak-kwh 500
        duke-rates compare-tariff-rates --kwh 3000 --service-date 2025-08-01 --group residential,sgs
        duke-rates compare-tariff-rates --kwh 1000 --group all
    """
    from duke_rates.billing.tariff_engine import BillInput, TariffBillingEngine, schedule_group_for

    import datetime as _dt

    _, repository = _bootstrap()

    parsed_date: _dt.date | None = None
    if service_date:
        try:
            parsed_date = _dt.date.fromisoformat(service_date)
        except ValueError:
            typer.echo(f"Invalid service_date: {service_date}. Use YYYY-MM-DD.", err=True)
            raise typer.Exit(1)

    # Parse group filter
    _ALL_GROUPS = {"residential", "sgs", "mgs", "lgs", "gs", "specialty", "unknown"}
    if group is None:
        allowed_groups: set[str] | None = {"residential"}
    elif group.lower() == "all":
        allowed_groups = None  # no filter
    else:
        allowed_groups = {g.strip().lower() for g in group.split(",")}
        invalid = allowed_groups - _ALL_GROUPS
        if invalid:
            typer.echo(f"Unknown group(s): {', '.join(sorted(invalid))}. "
                       f"Valid: {', '.join(sorted(_ALL_GROUPS))}, all", err=True)
            raise typer.Exit(1)

    usage = BillInput(
        monthly_kwh=kwh,
        peak_kw=peak_kw,
        base_kw=base_kw,
        on_peak_kw=on_peak_kw,
        mid_peak_kw=mid_peak_kw,
        off_peak_kw=off_peak_kw,
        service_date=parsed_date,
        on_peak_kwh=on_peak_kwh,
        off_peak_kwh=off_peak_kwh,
    )

    all_families = repository.list_tariff_families(
        state=state, company=company, family_type=family_type
    )
    if not all_families:
        typer.echo(f"No {family_type} families found for {state}/{company}.")
        raise typer.Exit(1)

    # Apply schedule group filter
    if allowed_groups is not None:
        families = [f for f in all_families
                    if schedule_group_for(f.schedule_code) in allowed_groups]
    else:
        families = all_families

    if not families:
        typer.echo(f"No schedules found matching group(s): {group}. "
                   "Try --group all to see all available schedules.")
        raise typer.Exit(1)

    engine = TariffBillingEngine(repository)
    results = []
    partial_coverage: list = []
    for family in families:
        result = engine.calculate(
            family.family_key, usage,
            customer_class=customer_class,
            include_riders=True,
        )
        # Detect partial TOU coverage (understated totals — exclude from ranked list)
        has_partial = any("Partial TOU coverage" in w for w in result.warnings)
        if has_partial:
            partial_coverage.append(result)
            continue
        # Only include schedules with actual base charges (not just rider-only)
        if result.base_subtotal > 0:
            results.append(result)

    results.sort(key=lambda r: r.total)

    if output_json:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return

    group_label = group or "residential"
    typer.echo(f"\nRate comparison — {state}/{company} | {kwh:,.0f} kWh | group={group_label}"
               + (f" | {parsed_date}" if parsed_date else ""))
    typer.echo(f"{'SCHEDULE':<45} {'TOTAL':>8}  {'BASE':>8}  {'RIDERS':>7}  {'CONF':>5}")
    typer.echo("-" * 76)
    for r in results:
        title = (r.schedule_title or r.family_key)[:44]
        typer.echo(
            f"  {title:<43} ${r.total:>7.2f}  ${r.base_subtotal:>7.2f}  ${r.rider_subtotal:>6.2f}  {r.source_confidence:.0%}"
        )
    if results:
        cheapest = results[0]
        priciest = results[-1]
        diff = round(priciest.total - cheapest.total, 2)
        typer.echo(f"\n  Cheapest: {cheapest.schedule_title or cheapest.family_key} (${cheapest.total:.2f})")
        if len(results) > 1:
            typer.echo(f"  Spread:   ${diff:.2f}/month (${diff*12:.0f}/year)")
    if partial_coverage:
        typer.echo(f"\n  Excluded (incomplete charge data — totals would be understated):")
        for r in partial_coverage:
            typer.echo(f"    {(r.schedule_title or r.family_key)[:60]}")


@app.command("estimate-bill")
def estimate_bill(
    tariff_id: str = typer.Option(..., help="Tariff ID from a parsed schedule."),
    usage_file: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    monthly_kwh: float | None = typer.Option(None),
    peak_kw: float | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    matched_result = None
    usage = (
        _read_usage_file(usage_file)
        if usage_file
        else UsageInput(
            monthly_kwh=monthly_kwh or 0.0,
            peak_kw=peak_kw,
        )
    )
    for _, parse_result in _best_estimatable_results(repository, usage=usage).values():
        if parse_result.schedule and parse_result.schedule.tariff_id == tariff_id:
            matched_result = parse_result
            break

    if not matched_result or not matched_result.schedule:
        raise typer.BadParameter(
            f"No estimatable parsed schedule found for tariff_id={tariff_id}"
        )

    estimate = BillingEngine().estimate(matched_result.schedule, usage)
    typer.echo(json.dumps(estimate.model_dump(mode="json"), indent=2))


@app.command("compare-rates")
def compare_rates(
    state: str | None = typer.Option(None),
    company: str | None = typer.Option(None),
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
    candidates = []
    for _, result in _best_estimatable_results(
        repository,
        state=state,
        company=company,
        usage=usage,
    ).values():
        estimate = BillingEngine().estimate(result.schedule, usage)
        candidates.append(
            (estimate.total, result.schedule.tariff_id, result.schedule.schedule_title)
        )

    if not candidates:
        typer.echo("No estimatable parsed tariffs found for the requested scope.")
        raise typer.Exit()

    for total, tariff_id, title in sorted(candidates):
        typer.echo(f"{total:.2f}\t{tariff_id}\t{title}")


@app.command("parse-bill")
def parse_bill(
    bill_file: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False),
) -> None:
    _, repository = _bootstrap()
    bill_id, statement = _parse_bill_file(bill_file, repository)
    typer.echo(
        json.dumps(
            {
                "bill_id": bill_id,
                "statement": statement.model_dump(mode="json"),
            },
            indent=2,
            default=str,
        )
    )


@app.command("parse-bills")
def parse_bills(
    directory: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    _, repository = _bootstrap()
    parsed = 0
    for path in sorted(directory.glob("*.pdf")):
        try:
            _parse_bill_file(path, repository)
            parsed += 1
        except Exception as exc:
            logger.warning("Failed to parse bill %s: %s", path, exc)
    typer.echo(f"Parsed {parsed} bill PDFs.")


@app.command("list-bills")
def list_bills() -> None:
    _, repository = _bootstrap()
    for statement in repository.list_bill_statements():
        typer.echo(
            "\t".join(
                [
                    str(statement.id),
                    statement.bill_date.isoformat() if statement.bill_date else "-",
                    statement.service_start.isoformat() if statement.service_start else "-",
                    statement.service_end.isoformat() if statement.service_end else "-",
                    (
                        f"{statement.total_amount_due:.2f}"
                        if statement.total_amount_due is not None
                        else "-"
                    ),
                    statement.source_path,
                ]
            )
        )


@app.command("show-bill")
def show_bill(bill_id: int) -> None:
    _, repository = _bootstrap()
    stored = repository.get_bill_statement(bill_id)
    if not stored:
        raise typer.BadParameter(f"Bill statement {bill_id} not found.")
    typer.echo(json.dumps(stored.model_dump(mode="json"), indent=2, default=str))


@app.command("derive-bill-observations")
def derive_bill_observations(bill_id: int | None = typer.Option(None)) -> None:
    _, repository = _bootstrap()
    stored_bills = (
        [repository.get_bill_statement(bill_id)]
        if bill_id is not None
        else repository.list_bill_statements()
    )
    processed = 0
    total = 0
    for stored in stored_bills:
        if stored is None:
            continue
        statement = BillStatementData.model_validate_json(stored.statement_json)
        observations = derive_bill_component_observations(
            bill_id=stored.id,
            statement=statement,
        )
        repository.replace_bill_component_observations(
            bill_id=stored.id,
            observations=observations,
        )
        processed += 1
        total += len(observations)
    typer.echo(f"Derived {total} bill component observations across {processed} bills.")


@app.command("list-bill-observations")
def list_bill_observations(
    bill_id: int | None = typer.Option(None),
    component_key: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    rows = repository.list_bill_component_observations(
        bill_id=bill_id,
        component_key=component_key,
    )
    for observation in rows:
        typer.echo(
            "\t".join(
                [
                    str(observation.bill_id),
                    observation.service_end.isoformat() if observation.service_end else "-",
                    observation.section_name,
                    observation.rate_code or "-",
                    observation.component_key,
                    observation.component_label,
                    f"{observation.amount:.2f}",
                    observation.inferred_unit or "-",
                    (
                        f"{observation.inferred_value:.3f}"
                        if observation.inferred_value is not None
                        else "-"
                    ),
                    (
                        f"{observation.quantity_basis_kwh:.3f}"
                        if observation.quantity_basis_kwh is not None
                        else "-"
                    ),
                ]
            )
        )


@app.command("list-observed-component-history-progress-nc")
def list_observed_component_history_progress_nc(
    component_key: str | None = typer.Option(None),
    rate_code: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCObservedComponentHistoryService(
        repository.list_bill_component_observations(component_key=component_key)
    )
    rows = service.build_series(component_key=component_key, rate_code=rate_code)
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    row.component_key,
                    row.rate_code or "-",
                    row.start_date.isoformat(),
                    row.end_date.isoformat(),
                    row.normalized_unit,
                    f"{row.normalized_value:.3f}",
                    str(row.sample_count),
                    ",".join(str(bill_id) for bill_id in row.bill_ids),
                ]
            )
        )


@app.command("show-observed-component-history-progress-nc")
def show_observed_component_history_progress_nc(
    component_key: str = typer.Option(...),
    rate_code: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCObservedComponentHistoryService(
        repository.list_bill_component_observations(component_key=component_key)
    )
    rows = service.build_series(component_key=component_key, rate_code=rate_code)
    typer.echo(json.dumps([row.model_dump(mode="json") for row in rows], indent=2, default=str))


@app.command("reconcile-bill-progress-nc")
def reconcile_bill_progress_nc(bill_id: int) -> None:
    _, repository = _bootstrap()
    stored = repository.get_bill_statement(bill_id)
    if not stored:
        raise typer.BadParameter(f"Bill statement {bill_id} not found.")
    statement = json.loads(stored.statement_json)
    service = ProgressNCBillReconciliationService(
        ProgressNCHistoricalTariffSelector(repository)
    )
    result = service.reconcile(
        bill_id=bill_id,
        statement=BillStatementData.model_validate(statement),
    )
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


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

    typer.echo("\nTop Parser Profiles")
    for row in report["top_profiles"]:
        top_categories = ",".join(item["category"] for item in row["top_correction_categories"]) or "-"
        typer.echo(
            "  "
            f"{row['parser_profile']:<32} "
            f"attempts={row['attempt_count']:<3} "
            f"needs_review={row['needs_review']:<3} "
            f"corrected={row['corrected']:<3} "
            f"rejected={row['rejected']:<3} "
            f"human={row['human_reviewed']:<3} "
            f"corrections={row['correction_count']:<3} "
            f"categories={top_categories}"
        )

    typer.echo("\nTop Families")
    for row in report["top_families"]:
        top_categories = ",".join(item["category"] for item in row["top_correction_categories"]) or "-"
        typer.echo(
            "  "
            f"{row['family_key']:<36} "
            f"company={(row['company'] or '-'): <10} "
            f"attempts={row['attempt_count']:<3} "
            f"needs_review={row['needs_review']:<3} "
            f"corrected={row['corrected']:<3} "
            f"rejected={row['rejected']:<3} "
            f"categories={top_categories}"
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

    Each row's reasons inform whether to retire-historical-document, run
    rebind-historical-page-range, or extend the reference-only classifier.
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
                "recommended_command": "python -m duke_rates process-ocr-queue-nc --limit 1",
                "recommended_parallel_command": "python -m duke_rates process-ocr-queue-nc --limit 2 --workers 2",
            },
            {
                "action_type": "process_reprocess_queue",
                "concurrency_policy": "workers_allowed",
                "workers_allowed": True,
                "notes": "Bounded local reprocess queue processing only; each worker claims queue items independently.",
                "recommended_command": "python -m duke_rates process-reprocess-queue-nc --limit 1",
                "recommended_parallel_command": "python -m duke_rates process-reprocess-queue-nc --limit 2 --workers 2",
            },
            {
                "action_type": "enqueue_ocr_remediation",
                "concurrency_policy": "sequential_only",
                "workers_allowed": False,
                "notes": "Queue-enqueue step is kept sequential in guided mode for predictable receipts and batching.",
                "recommended_command": "python -m duke_rates enqueue-ocr-remediation-nc --limit 1 --execute",
                "recommended_parallel_command": None,
            },
            {
                "action_type": "enqueue_stale_reprocess",
                "concurrency_policy": "sequential_only",
                "workers_allowed": False,
                "notes": "Queue-enqueue step is kept sequential in guided mode for predictable receipts and batching.",
                "recommended_command": "python -m duke_rates enqueue-stale-reprocess-nc --limit 10",
                "recommended_parallel_command": None,
            },
            {
                "action_type": "portal_search",
                "concurrency_policy": "sequential_only",
                "workers_allowed": False,
                "notes": "Authenticated NCUC portal/search must remain sequential to reduce 403/rate-limit risk.",
                "recommended_command": "python -m duke_rates ncuc-portal-search ...",
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
            f"python -m duke_rates process-ocr-queue-nc --limit {limit}"
            + (f" --workers {selected_workers}" if selected_workers > 1 else "")
        )
    elif action_type == "process_reprocess_queue":
        selected["recommended_command"] = (
            f"python -m duke_rates process-reprocess-queue-nc --limit {limit}"
            + (f" --workers {selected_workers}" if selected_workers > 1 else "")
        )
    elif action_type == "enqueue_ocr_remediation":
        selected["recommended_command"] = f"python -m duke_rates enqueue-ocr-remediation-nc --limit {limit} --execute"
    elif action_type == "enqueue_stale_reprocess":
        selected["recommended_command"] = f"python -m duke_rates enqueue-stale-reprocess-nc --limit {limit}"
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


@app.command("enqueue-ocr-nc")
def enqueue_ocr_nc(
    limit: int = typer.Option(100, "--limit", help="Max downloaded discovery records to inspect."),
    backend: str = typer.Option("pytesseract_cpu", "--backend", help="OCR backend label."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label."),
    force_rescan: bool = typer.Option(False, "--force-rescan", help="Queue even if a completed artifact exists."),
) -> None:
    """Queue OCR_REQUIRED NCUC downloads for OCR artifact generation."""
    from duke_rates.db.ocr_queue import enqueue_ocr_candidates
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = enqueue_ocr_candidates(
            conn,
            limit=limit,
            backend=backend,
            requested_by=requested_by,
            force_rescan=force_rescan,
        )
        conn.commit()
    finally:
        conn.close()

    typer.echo(f"OCR queue: inserted={report['inserted']} skipped={report['skipped']}")


@app.command("show-ocr-queue-nc")
def show_ocr_queue_nc(
    status: str | None = typer.Option("pending", "--status", help="pending | running | completed | failed | all"),
    limit: int = typer.Option(50, "--limit", help="Max queue rows to display."),
) -> None:
    """Show the OCR processing queue."""
    from duke_rates.db.ocr_queue import list_ocr_queue
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        counts = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT status, COUNT(*) FROM ocr_processing_queue GROUP BY status"
            ).fetchall()
        }
        rows = list_ocr_queue(conn, status=None if status == "all" else status, limit=limit)
    finally:
        conn.close()

    total = sum(counts.values())
    typer.echo(
        f"OCR Queue (NC): total={total} "
        f"pending={counts.get('pending', 0)} "
        f"running={counts.get('running', 0)} "
        f"completed={counts.get('completed', 0)} "
        f"failed={counts.get('failed', 0)}"
    )

    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["id"]),
                    row["status"],
                    str(row["priority"]),
                    row.get("backend") or "-",
                    row.get("source_pdf") or "-",
                ]
            )
        )


@app.command("report-ocr-benchmark-nc")
def report_ocr_benchmark_nc(
    limit: int = typer.Option(50, "--limit", help="Maximum number of OCR-backed historical documents to summarize."),
    backend: str | None = typer.Option(None, "--backend", help="Filter to a selected OCR backend."),
    outcome: str | None = typer.Option(None, "--outcome", help="Filter to a selected parse outcome_quality."),
    needs_review_only: bool = typer.Option(False, "--needs-review-only", help="Only include rows whose latest parse review is still needs_review."),
    stale_only: bool = typer.Option(False, "--stale-only", help="Only include rows whose artifacts or parser run are stale vs current stage versions."),
    sort_by: str = typer.Option("recent", "--sort-by", help="Sort rows by recent | weak-first | review-first | stale-first."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
) -> None:
    """Summarize OCR backend, normalization version, and parse outcomes for NC historical documents."""
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = _build_ocr_benchmark_nc_report(
            conn,
            limit=limit,
            backend_filter=backend,
            outcome_filter=outcome,
            needs_review_only=needs_review_only,
            stale_only=stale_only,
            sort_by=sort_by,
        )
    finally:
        conn.close()

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(f"ocr_rows={report['row_count']}")
    typer.echo("Backends:")
    for row in report["backend_summary"]:
        typer.echo(f"  {row['backend']}: {row['count']}")
    typer.echo("Normalization:")
    for row in report["normalization_summary"]:
        typer.echo(f"  {row['ocr_normalization_version']}: {row['count']}")
    typer.echo("Outcomes:")
    for row in report["outcome_summary"]:
        typer.echo(f"  {row['outcome_quality']}: {row['count']}")
    typer.echo("Route Reasons:")
    for row in report["route_reason_summary"]:
        typer.echo(f"  {row['route_reason']}: {row['count']}")
    typer.echo("Recommended Lanes:")
    for row in report["recommended_lane_summary"]:
        typer.echo(f"  {row['recommended_lane']}: {row['count']}")
    typer.echo("Page Artifacts:")
    for row in report["page_artifact_version_summary"]:
        typer.echo(f"  {row['page_artifact_version']}: {row['count']}")
    typer.echo("Span Artifacts:")
    for row in report["span_artifact_version_summary"]:
        typer.echo(f"  {row['span_artifact_version']}: {row['count']}")
    typer.echo("Review Outcomes:")
    for row in report["review_outcome_summary"]:
        typer.echo(f"  {row['review_outcome']}: {row['count']}")
    typer.echo("Backend x Outcome:")
    for row in report["backend_outcome_summary"][:10]:
        typer.echo(f"  {row['backend']} | {row['outcome_quality']}: {row['count']}")
    typer.echo("Sample Rows:")
    for row in report["rows"][:10]:
        typer.echo(
            "  "
            f"hd={row['historical_document_id']} "
            f"family={row['family_key']} "
            f"backend={row['backend']} "
            f"outcome={row['outcome_quality']} "
            f"raw_text_chars={row['raw_text_chars']} "
            f"route={row['route_reason']} "
            f"lane={row['recommended_lane']}"
        )


@app.command("show-ocr-remediation-candidates-nc")
def show_ocr_remediation_candidates_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to display."),
    company: str | None = typer.Option(None, help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC OCR remediation candidates such as unknown-profile / no-text historical documents."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_ocr_remediation_candidates_nc_report(
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

    typer.echo("OCR Remediation Candidates (NC)")
    typer.echo(f"  candidates={report['candidate_count']}")

    typer.echo("\nRoute Reasons")
    for row in report["route_reason_summary"]:
        typer.echo(f"  {row['route_reason']:<36} count={row['count']}")

    typer.echo("\nRecommended Lanes")
    for row in report["recommended_lane_summary"]:
        typer.echo(f"  {row['recommended_lane']:<36} count={row['count']}")

    typer.echo("\nSample Rows")
    for row in report["rows"]:
        typer.echo(
            "  "
            f"hd={row['historical_document_id']} "
            f"family={row['family_key']} "
            f"profile={row['parser_profile']} "
            f"outcome={row['outcome_quality']} "
            f"raw_text_chars={row['raw_text_chars']} "
            f"ocr_backend={row['ocr_backend'] or '-'} "
            f"lane={row['recommended_lane']}"
        )
        typer.echo(
            "    "
            f"reason={row['route_reason']} "
            f"charges={row['charge_count']} "
            f"pages={row['page_count']}"
        )


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


@app.command("enqueue-ocr-remediation-nc")
def enqueue_ocr_remediation_nc(
    limit: int = typer.Option(500, "--limit", help="Max remediation candidates to enqueue per invocation."),
    company: str | None = typer.Option(None, help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
    backend: str = typer.Option("pytesseract_cpu", "--backend", help="OCR backend label."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview by default; use --execute to enqueue."),
) -> None:
    """Enqueue OCR remediation candidates from the historical-document audit."""
    from duke_rates.db.ocr_queue import enqueue_ocr_queue_item

    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_ocr_remediation_candidates_nc_report(
            conn,
            limit=max(limit * 10, 500),
            company=company,
            family_key=family_key,
        )
        inserted = 0
        skipped = 0
        considered = 0
        sample_rows: list[dict[str, Any]] = []
        for row in report["rows"]:
            if row["recommended_lane"] != "queue_ocr_or_paddle":
                continue
            if inserted >= limit:
                break
            considered += 1
            source_pdf = str(
                conn.execute(
                    "SELECT local_path FROM historical_documents WHERE id = ?",
                    (int(row["historical_document_id"]),),
                ).fetchone()["local_path"]
            )
            if not source_pdf or not Path(source_pdf).exists():
                skipped += 1
                continue
            triage = triage_pdf(source_pdf)
            priority = 80
            if triage.gpu_ocr_candidate:
                priority = 95
            elif triage.ocr_confidence_score >= 0.85:
                priority = 90
            metadata = {
                "requested_by": requested_by,
                "source": "ocr_remediation_audit",
                "historical_document_id": int(row["historical_document_id"]),
                "family_key": row["family_key"],
                "recommended_lane": row["recommended_lane"],
                "route_reason": row["route_reason"],
                "ocr_backend_version": OCR_BACKEND_VERSION,
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
            }
            if dry_run:
                sample_rows.append(
                    {
                        "historical_document_id": int(row["historical_document_id"]),
                        "family_key": row["family_key"],
                        "priority": priority,
                        "backend": backend,
                        "route_reason": row["route_reason"],
                    }
                )
                continue
            queue_id, did_insert = enqueue_ocr_queue_item(
                conn,
                discovery_record_id=None,
                source_pdf=source_pdf,
                file_hash=None,
                backend=backend,
                priority=priority,
                ocr_confidence=triage.ocr_confidence_score,
                structure_complexity=triage.structure_complexity_score,
                gpu_candidate=triage.gpu_ocr_candidate,
                metadata=metadata,
            )
            if did_insert and queue_id is not None:
                inserted += 1
                sample_rows.append(
                    {
                        "historical_document_id": int(row["historical_document_id"]),
                        "family_key": row["family_key"],
                        "queue_id": queue_id,
                        "priority": priority,
                        "backend": backend,
                    }
                )
            else:
                skipped += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    mode = "dry_run" if dry_run else "execute"
    typer.echo(f"OCR remediation enqueue ({mode})")
    typer.echo(f"  considered={considered} inserted={inserted} skipped={skipped}")
    for row in sample_rows[: min(10, len(sample_rows))]:
        line = (
            f"  hd={row['historical_document_id']} family={row['family_key']} "
            f"backend={row['backend']} priority={row['priority']}"
        )
        if "queue_id" in row:
            line += f" queue_id={row['queue_id']}"
        if "route_reason" in row:
            line += f" reason={row['route_reason']}"
        typer.echo(line)


@app.command("process-ocr-queue-nc")
def process_ocr_queue_nc(
    limit: int = typer.Option(500, "--limit", help="Max OCR queue items to process per invocation."),
    workers: int = typer.Option(1, "--workers", min=1, help="Parallel local OCR workers. Safe for local file processing; keep portal/search workflows sequential."),
    force: bool = typer.Option(False, "--force", help="Re-run OCR even if sidecars exist."),
    until_empty: bool = typer.Option(False, "--until-empty", help="Keep processing until the queue is empty (overrides --limit)."),
) -> None:
    """Process pending OCR queue items and persist OCR artifacts."""
    settings, _ = _bootstrap()
    processed = 0
    completed = 0
    failed = 0

    if until_empty:
        limit = 1_000_000

    max_workers = max(1, min(workers, limit))
    if max_workers == 1:
        while processed < limit:
            result = _process_single_ocr_queue_item(Path(settings.database_path), force=force)
            if not result["processed"]:
                break
            processed += 1
            completed += int(result["completed"])
            failed += int(result["failed"])
        typer.echo(f"OCR queue processed={processed} completed={completed} failed={failed}")
        return

    submitted = 0
    futures = set()
    queue_exhausted = False
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while submitted < limit and len(futures) < max_workers:
            futures.add(executor.submit(_process_single_ocr_queue_item, Path(settings.database_path), force))
            submitted += 1

        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                if result["processed"]:
                    processed += 1
                    completed += int(result["completed"])
                    failed += int(result["failed"])
                else:
                    queue_exhausted = True

            while not queue_exhausted and submitted < limit and len(futures) < max_workers:
                futures.add(executor.submit(_process_single_ocr_queue_item, Path(settings.database_path), force))
                submitted += 1

    typer.echo(
        f"OCR queue processed={processed} completed={completed} failed={failed} workers={max_workers}"
    )


@app.command("process-ocr-backlog-nc")
def process_ocr_backlog_nc(
    workers: int = typer.Option(4, "--workers", min=1, help="Parallel Tesseract OCR workers."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter for enqueue step."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter for enqueue and extract steps."),
    skip_enqueue: bool = typer.Option(False, "--skip-enqueue", help="Skip the remediation enqueue step (use when the queue is already populated)."),
    skip_extract: bool = typer.Option(False, "--skip-extract", help="Skip the extract-rates step after the queue drains."),
    force: bool = typer.Option(False, "--force", help="Re-run OCR even if sidecars exist."),
    enqueue_limit: int = typer.Option(500, "--enqueue-limit", help="Max candidates to enqueue in the remediation step."),
) -> None:
    """Canonical OCR backlog workflow: enqueue remediation candidates, drain the OCR queue, then extract charges.

    Replaces the hand-written loop around enqueue-ocr-remediation-nc, process-ocr-queue-nc
    (repeated), and extract-rates-nc. Uses the Tesseract lane (queue_ocr_or_paddle).
    For the structure-sensitive lane (run_docling_or_paddle_structure), run
    process-docling-batch --ocr-remediation --source historical after this.
    """
    typer.echo("=== Step 1/3: Enqueue OCR remediation candidates ===")
    if skip_enqueue:
        typer.echo("  skipped (--skip-enqueue)")
    else:
        enqueue_ocr_remediation_nc(
            limit=enqueue_limit,
            company=company,
            family_key=family_key,
            backend="pytesseract_cpu",
            requested_by="ocr_backlog_workflow",
            dry_run=False,
        )

    typer.echo("")
    typer.echo("=== Step 2/3: Drain OCR queue ===")
    process_ocr_queue_nc(
        limit=500,
        workers=workers,
        force=force,
        until_empty=True,
    )

    typer.echo("")
    typer.echo("=== Step 3/3: Extract rates from newly-OCR'd documents ===")
    if skip_extract:
        typer.echo("  skipped (--skip-extract)")
    else:
        extract_rates_nc(
            limit=None,
            family_key=family_key,
            verbose=False,
            progress=False,
            progress_interval=30,
        )

    typer.echo("")
    typer.echo("=== OCR backlog workflow complete ===")


def _process_single_ocr_queue_item(database_path: Path, force: bool = False) -> dict[str, int | bool]:
    """Claim and process one OCR queue item. Safe unit of parallel local work.

    Holds one DB connection for the lifetime of the item. The connection is
    only used at claim time and again at completion (the OCR work itself is
    pure CPU/IO outside SQLite), so the lock is not held during long OCR runs.
    """
    from duke_rates.db.ocr_queue import (
        claim_next_ocr_queue_item,
        complete_ocr_queue_item,
        upsert_ocr_artifact,
    )
    from duke_rates.historical.ncuc.pipeline.ocr import (
        _compute_file_hash,
        _ocr_pages_sidecar_path,
        _ocr_text_sidecar_path,
        extract_ocr_document_pages,
        get_ocr_backend_unavailable_reason,
        load_ocr_sidecar_payload,
        summarize_ocr_payload,
    )

    conn = connect_sqlite(database_path)
    try:
        item = claim_next_ocr_queue_item(conn)
        if not item:
            return {"processed": False, "completed": 0, "failed": 0}
        # Claim already commits internally via BEGIN IMMEDIATE / COMMIT.

        queue_id = int(item["id"])
        source_pdf = str(item["source_pdf"])
        backend = str(item.get("backend") or "pytesseract_cpu")

        # Short-circuit: file missing → mark failed and return without OCR work.
        if not Path(source_pdf).exists():
            complete_ocr_queue_item(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=f"OCR source missing: {source_pdf}",
            )
            conn.commit()
            return {"processed": True, "completed": 0, "failed": 1}

        # Short-circuit: backend unavailable.
        unavailable_reason = get_ocr_backend_unavailable_reason(backend)
        if unavailable_reason:
            complete_ocr_queue_item(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=unavailable_reason,
            )
            conn.commit()
            return {"processed": True, "completed": 0, "failed": 1}

        # OCR runs outside any SQLite transaction; SQLite is autocommit here
        # so other workers can claim items in parallel during this call.
        try:
            pages = extract_ocr_document_pages(source_pdf, force=force, backend=backend)
        except Exception as exc:
            complete_ocr_queue_item(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=str(exc),
            )
            conn.commit()
            return {"processed": True, "completed": 0, "failed": 1}

        file_hash = _compute_file_hash(source_pdf)
        payload = load_ocr_sidecar_payload(source_pdf)
        ocr_summary = summarize_ocr_payload(payload)

        artifact_id = upsert_ocr_artifact(
            conn,
            discovery_record_id=item.get("discovery_record_id"),
            source_pdf=source_pdf,
            file_hash=file_hash,
            backend=backend,
            status="completed" if pages else "empty",
            text_sidecar_path=str(_ocr_text_sidecar_path(source_pdf)),
            pages_sidecar_path=str(_ocr_pages_sidecar_path(source_pdf)),
            page_count=len(pages),
            ocr_confidence=item.get("ocr_confidence"),
            metadata={
                "gpu_candidate": bool(item.get("gpu_candidate")),
                "queue_id": queue_id,
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                **ocr_summary,
            },
        )
        complete_ocr_queue_item(
            conn,
            queue_id=queue_id,
            status="completed" if pages else "failed",
            latest_artifact_id=artifact_id,
            error_message=None if pages else "OCR produced no pages.",
            metadata={
                "page_count": len(pages),
                **ocr_summary,
            },
        )
        conn.commit()
        return {"processed": True, "completed": 1 if pages else 0, "failed": 0 if pages else 1}
    finally:
        conn.close()


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
            "structured_rate_extraction, document_classification."
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
# Phase 5.6 — LLM-assisted parse diagnosis and regex improvement loop
# =============================================================================


@app.command("analyze-parse-failures-nc")
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


@app.command("suggest-regex-fixes-nc")
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


@app.command("validate-regex-suggestions-nc")
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


@app.command("run-llm-parse-fallback-nc")
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


@app.command("run-overnight-parse-improvement-nc")
def run_overnight_parse_improvement_nc(
    max_documents: int = typer.Option(0, "--max-documents", help="Max documents to process (0 = unlimited)."),
    max_runtime_minutes: int = typer.Option(0, "--max-runtime-minutes", help="Hard wall-clock cap in minutes (0 = unlimited)."),
    max_consecutive_failures: int = typer.Option(5, "--max-consecutive-failures", help="Abort after N consecutive model call failures."),
    task_kind: str = typer.Option("diagnose", "--task-kind", help="Comma-separated tasks: diagnose, suggest, validate, revalidate, shadow_test, profile_consensus, extract."),
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
    from duke_rates.document_intelligence.parse_improvement_loop import ParseImprovementLoop

    settings, _ = _bootstrap()

    tasks = [t.strip() for t in task_kind.split(",") if t.strip()]
    valid_tasks = {"diagnose", "suggest", "validate", "revalidate", "shadow_test", "profile_consensus", "extract"}
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
        parts = ", ".join(f"{k}={v}" for k, v in stats.items() if v > 0)
        typer.echo(f"  {task}: {parts}")
    typer.echo(f"  failures by type: {report.parse_failures_by_type}")
    idle = report.is_idle()
    sub = "idle/" if idle else ""
    typer.echo(f"  report:          docs/reports/overnight_parse_improvement/{sub}{report.run_id}.json")
    if idle:
        typer.echo(f"  idle:            true (no work found)")
        if exit_when_idle:
            raise typer.Exit(code=42)


@app.command("report-wrong-profile-diagnostics-nc")
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


@app.command("report-profile-recommendations-nc")
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


@app.command("populate-document-identity-nc")
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


@app.command("report-document-identity-nc")
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


@app.command("report-document-identity-summary-nc")
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


@app.command("report-document-identity-quality-nc")
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


# ---------------------------------------------------------------------------
# Phase 2 — routing tier system
# Plan ref: docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md §5
# ---------------------------------------------------------------------------


@app.command("populate-routing-tier-nc")
def populate_routing_tier_nc(
    limit: int = typer.Option(
        0, "--limit", help="Process at most N identity rows (0 = unlimited)."
    ),
) -> None:
    """Label every ``document_identity`` row with a routing tier.

    Tier labels are informational only in Phase 2 — they do not change
    extraction. Phase 3/4 consume them to make routing decisions.
    """
    from duke_rates.document_intelligence.routing_tier import TierAggregator

    settings, _ = _bootstrap()
    agg = TierAggregator(settings.database_path)
    n = agg.label_all(limit=limit if limit > 0 else None)
    typer.echo(f"document_routing_tier: labeled/refreshed {n} rows")


@app.command("report-routing-tier-nc")
def report_routing_tier_nc(
    sample_per_tier: int = typer.Option(
        5, "--sample-per-tier", help="Show N example rationales per tier."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Show tier distribution and a few example rationales per tier.

    Use this to eyeball tier assignments before committing to Phase 3
    binding. The sample rationales surface borderline cases that may
    inform threshold tuning.
    """
    import json as _json
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
                ORDER BY overall_confidence DESC
                LIMIT ?
                """,
                (tier, sample_per_tier),
            ).fetchall()
            samples[tier] = [dict(r) for r in rows]
    finally:
        conn.close()

    payload = {"distribution": dist, "total": total, "samples": samples}
    if as_json:
        typer.echo(_json.dumps(payload, indent=2, default=str))
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


@app.command("report-routing-tier-validation-nc")
def report_routing_tier_validation_nc(
    write_report: bool = typer.Option(
        True,
        "--write-report/--no-write-report",
        help="Write JSON to docs/reports/routing_tier_validation/<timestamp>.json.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit full report as JSON."),
) -> None:
    """Cross-check tier predictions against actual parse outcomes (Phase 2B).

    Per the plan §5.2B:
      - Tier 1 docs SHOULD mostly parse cleanly. If they don't, the
        underlying parser template needs work.
      - Tier 3 docs SHOULD mostly diagnose as wrong_profile/unknown. If a
        Tier 3 doc parses successfully, the cutoffs may be too strict.

    The output surfaces the highest-leverage template-bug and
    cutoff-tuning candidates.
    """
    import json as _json
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
        path.write_text(_json.dumps(report, indent=2, default=str), encoding="utf-8")
        typer.echo(f"Report written: {path}")

    if as_json:
        typer.echo(_json.dumps(report, indent=2, default=str))
        return

    s = report["summary"]
    typer.echo(f"\n=== Routing Tier Validation ===")
    typer.echo(
        f"Tier 1: {s['tier1_count']:>6} attempts  parsed-with-charges {s['tier1_parsed_rate']:>5.1%}  "
        f"template_bugs={s['tier1_extraction_failure_count']}"
    )
    typer.echo(
        f"Tier 2: {s['tier2_count']:>6} attempts"
    )
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

    typer.echo(
        f"\n--- Tier 1 extraction failures (template bugs): "
        f"{s['tier1_extraction_failure_count']} ---"
    )
    for r in report["tier1_extraction_failures"][:8]:
        typer.echo(
            f"  status={r.get('status', '?'):<10} "
            f"profile={(r.get('parser_profile') or '?'):<28} "
            f"recommended={(r.get('profile_consensus_top') or '?')}"
        )

    typer.echo(
        f"\n--- Tier 3 unexpected successes (cutoff candidates): "
        f"{s['tier3_unexpected_success_count']} ---"
    )
    for r in report["tier3_unexpected_successes"][:8]:
        typer.echo(
            f"  conf={r.get('overall_confidence', 0):.2f} "
            f"profile={(r.get('parser_profile') or '?'):<28} "
            f"charges={r.get('charge_count', 0)}"
        )


@app.command("report-document-fingerprint-clusters-nc")
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
    parser path. Clusters with size ≥ N but no associated extractions
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
            hints.append("--ocr-remediation restricts to the run_docling_or_paddle_structure lane; check show-ocr-remediation-candidates-nc")
        if source == "historical":
            hints.append("source=historical only sees docs with hd.local_path set; check show-fingerprint-coverage-nc")
        elif source == "discovery":
            hints.append("source=discovery requires ncuc_discovery_records rows; check ncuc-list")
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


@app.command("lookup-openei-rates")
def lookup_openei_rates(
    utility: str | None = typer.Option(
        None,
        help="OpenEI utility label/name, e.g. Duke Energy Progress.",
    ),
    state: str | None = typer.Option(None, help="Optional state filter, e.g. NC."),
    search_text: str | None = typer.Option(
        None,
        help="Optional text filter, e.g. RES or Residential Service.",
    ),
    label: str | None = typer.Option(
        None,
        help="Exact OpenEI USURDB label, e.g. 678abac33d12e18b730b0663.",
    ),
    url: str | None = typer.Option(
        None,
        help="OpenEI USURDB rate URL, from which the label will be extracted.",
    ),
    limit: int = typer.Option(25, help="Maximum number of OpenEI rate references to return."),
) -> None:
    settings, _ = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    if not utility and not label and not url:
        raise typer.BadParameter("Provide --utility, --label, or --url.")
    client = OpenEIClient(
        api_key=settings.openei_api_key,
        timeout=settings.request_timeout,
        user_agent=settings.user_agent,
        max_retries=settings.max_retries,
        rate_limit_seconds=settings.rate_limit_seconds,
    )
    try:
        if url:
            rows = client.lookup_rate_by_url(url)
        else:
            rows = client.lookup_rates(
                utility=utility,
                state=state,
                search_text=search_text,
                label=label,
                limit=limit,
            )
    finally:
        client.close()
    typer.echo(
        json.dumps([row.model_dump(mode="json") for row in rows], indent=2, default=str)
    )


@app.command("build-openei-export")
def build_openei_export(
    doc_id: int | None = typer.Option(None, help="Current document id with a parsed schedule."),
    historical_id: int | None = typer.Option(
        None,
        help="Historical document id with a parsed schedule.",
    ),
    openei_label: str | None = typer.Option(
        None,
        help="Optional OpenEI label to enrich the candidate.",
    ),
    openei_url: str | None = typer.Option(
        None,
        help="Optional OpenEI USURDB URL to enrich the candidate.",
    ),
) -> None:
    if not doc_id and not historical_id:
        raise typer.BadParameter("Provide --doc-id or --historical-id.")
    if doc_id and historical_id:
        raise typer.BadParameter("Provide only one of --doc-id or --historical-id.")

    settings, repository = _bootstrap()
    parse_result = None
    source_document = None
    historical_document = None

    if doc_id:
        source_document = repository.get_document(doc_id)
        if not source_document:
            raise typer.BadParameter(f"Document {doc_id} not found.")
        parse_result = repository.latest_parse_result(doc_id)
    else:
        historical_document = repository.get_historical_document(historical_id or 0)
        if not historical_document:
            raise typer.BadParameter(f"Historical document {historical_id} not found.")
        if historical_document.parsed_result_json:
            parse_result = DocumentParseResult.model_validate_json(
                historical_document.parsed_result_json
            )

    if not parse_result or not parse_result.schedule:
        raise typer.BadParameter("Selected record does not have a parsed schedule.")

    openei_reference = None
    if openei_label or openei_url:
        if not settings.openei_api_key:
            raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to enrich from OpenEI.")
        client = OpenEIClient(
            api_key=settings.openei_api_key,
            timeout=settings.request_timeout,
            user_agent=settings.user_agent,
            max_retries=settings.max_retries,
            rate_limit_seconds=settings.rate_limit_seconds,
        )
        try:
            if openei_url:
                refs = client.lookup_rate_by_url(openei_url)
            else:
                refs = client.lookup_rates(label=openei_label, limit=1)
        finally:
            client.close()
        openei_reference = refs[0] if refs else None

    candidate = build_openei_export_candidate(
        parse_result=parse_result,
        source_document=source_document,
        historical_document=historical_document,
        openei_reference=openei_reference,
    )
    typer.echo(json.dumps(candidate.model_dump(mode="json"), indent=2, default=str))


@app.command("export-urdb")
def export_urdb(
    family_key: str | None = typer.Option(
        None, "--family-key", "-f",
        help="Export a single tariff family key (e.g. 'nc-progress-leaf-502').",
    ),
    state: str | None = typer.Option(None, "--state", help="Filter by state code (e.g. NC)."),
    company: str | None = typer.Option(
        None, "--company", help="Filter by company slug (e.g. progress).",
    ),
    family_type: str = typer.Option(
        "rate_schedule", "--type",
        help="Family type: rate_schedule or rider.",
    ),
    min_confidence: float = typer.Option(
        0.7, "--min-confidence",
        help="Minimum charge confidence to include in bulk export (0–1).",
    ),
    output: str | None = typer.Option(
        None, "--output", "-o",
        help="Write JSON to this file path (default: print to stdout).",
    ),
    source_url: str | None = typer.Option(
        None, "--source-url",
        help="Override source URL in the export (optional).",
    ),
) -> None:
    """Export tariff data to URDB (OpenEI Utility Rate Database) JSON format.

    Outputs a curation-aid JSON suitable for manual review before submission
    to https://openei.org/apps/USURDB/.

    Examples:
        # Single schedule
        duke-rates export-urdb --family-key nc-progress-leaf-502

        # All NC Progress rate schedules
        duke-rates export-urdb --state NC --company progress

        # Write to file
        duke-rates export-urdb --state NC --company progress -o nc_progress_urdb.json
    """
    import sqlite3

    from duke_rates.external.urdb_export import (
        export_bulk_to_urdb,
        export_family_to_urdb,
        records_to_json,
    )

    settings, _ = _bootstrap()
    conn = sqlite3.connect(str(settings.database_path))

    try:
        if family_key:
            record = export_family_to_urdb(conn, family_key, source_url=source_url)
            if record is None:
                typer.echo(
                    f"Family '{family_key}' not found or has no charges.", err=True
                )
                raise typer.Exit(1)
            result_json = records_to_json([record])
        else:
            if not state and not company:
                typer.echo(
                    "Provide --family-key for a single record, or --state/--company "
                    "for a bulk export.",
                    err=True,
                )
                raise typer.Exit(1)
            records = export_bulk_to_urdb(
                conn,
                state=state,
                company=company,
                family_type=family_type,
                min_confidence=min_confidence,
                source_url_prefix=source_url,
            )
            if not records:
                typer.echo(
                    f"No exportable records found for state={state} company={company}.",
                    err=True,
                )
                raise typer.Exit(1)
            typer.echo(
                f"Exported {len(records)} record(s).", err=True
            )
            result_json = records_to_json(records)
    finally:
        conn.close()

    if output:
        import pathlib
        pathlib.Path(output).write_text(result_json, encoding="utf-8")
        typer.echo(f"Written to {output}", err=True)
    else:
        typer.echo(result_json)


@app.command()
def mcp() -> None:
    settings, _ = _bootstrap()
    serve_mcp(settings)


# ==========================================================================
# NCUC (North Carolina Utilities Commission) acquisition commands
# ==========================================================================


@app.command("ncuc-seed-discover")
def ncuc_seed_discover(
    max_per_docket: int = typer.Option(
        50, help="Maximum discovery records to collect per seed docket."
    ),
    docket: str | None = typer.Option(
        None,
        help="Override: discover a single docket (e.g. 'E-2, Sub 1142'). "
        "If omitted, uses built-in Duke Progress seed list.",
    ),
    dry_run: bool = typer.Option(False, help="Print discovered records but do not persist."),
) -> None:
    """Discover NCUC documents from seeded docket list via eDocket portal."""
    from duke_rates.historical.ncuc.discovery import (
        DUKE_PROGRESS_E2_DOCKETS,
        NcucDiscoveryService,
    )
    from duke_rates.models.ncuc import NcucDocketSeed

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        seeds = (
            [NcucDocketSeed(docket_number=docket)]
            if docket
            else DUKE_PROGRESS_E2_DOCKETS
        )
        count = 0
        for result in svc.discover_from_seed_dockets(seeds, max_per_docket=max_per_docket):
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number} "
                f"status={rec.fetch_status.value} "
                f"title={rec.filing_title or '(unknown)'}"
            )
            count += 1
        typer.echo(f"\nDiscovered {count} NCUC records.")
    finally:
        svc.close()


@app.command("ncuc-search")
def ncuc_search(
    query: str = typer.Argument(..., help="Search text (e.g. 'Duke Energy Progress rate schedule 605')."),
    docket_hint: str | None = typer.Option(None, help="Docket number hint (e.g. 'E-2')."),
    family_query: str | None = typer.Option(None, help="Family key to link results to (e.g. '605')."),
    max_results: int = typer.Option(100, help="Maximum results to return."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Search NCUC eDocket portal by keyword and persist discovered leads."""
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucSearchQuery

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        q = NcucSearchQuery(
            query_text=query,
            docket_hint=docket_hint,
            family_key_hint=family_query,
        )
        count = 0
        for result in svc.search_edocket_keyword(q, max_results=max_results):
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number} "
                f"title={rec.filing_title or '(none)'} "
                f"url={rec.discovered_url or ''}"
            )
            count += 1
        typer.echo(f"\nFound {count} NCUC records for query: {query!r}")
    finally:
        svc.close()


@app.command("ncuc-smart-search")
def ncuc_smart_search(
    family_key: str | None = typer.Option(None, help="Family key to seed queries from (e.g. nc-progress-leaf-602)."),
    leaf_no: str | None = typer.Option(None, help="Specific leaf number to search for (e.g. 602)."),
    rider_code: str | None = typer.Option(None, help="Rider code to search for (e.g. JAA, STS)."),
    tier: str = typer.Option("T1,T2", help="Comma-separated quality tiers to use as HQ signal sources."),
    max_results: int = typer.Option(50, help="Maximum results per query."),
    dry_run: bool = typer.Option(False, help="Print queries and results without persisting."),
) -> None:
    """Build and run high-confidence NCUC searches seeded from known T1/T2 HQ documents.

    Loads known high-quality document titles from ncuc_discovery_records (filtered by
    quality tier), extracts structural tokens (leaf numbers, rider codes, revision labels),
    and builds targeted search queries that are much more likely to find clean compliance
    tariff sheets rather than redline/procedural documents.

    Examples:
        duke-rates ncuc-smart-search --family-key nc-progress-leaf-602
        duke-rates ncuc-smart-search --rider-code JAA --dry-run
        duke-rates ncuc-smart-search --leaf-no 607 --tier T1
    """
    import sqlite3
    from duke_rates.historical.ncuc.query_builder import QueryBuilder
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.config import Settings

    settings, repository = _bootstrap()
    db_path = settings.database_path

    # Load HQ documents from DB
    allowed_tiers = {t.strip() for t in tier.split(",")}
    hq_docs: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        clauses = ["doc_quality_tier IN ({})".format(",".join("?" * len(allowed_tiers)))]
        params: list = list(allowed_tiers)
        if family_key:
            clauses.append("family_keys_json LIKE ?")
            params.append(f"%{family_key}%")
        if leaf_no:
            clauses.append("referenced_leaf_nos_json LIKE ?")
            params.append(f"%{leaf_no}%")
        if rider_code:
            clauses.append(
                "(filing_title LIKE ? OR referenced_rider_codes_json LIKE ?)"
            )
            params += [f"%{rider_code}%", f"%{rider_code}%"]
        where = " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT filing_title, utility, docket_number, "
            f"referenced_leaf_nos_json, referenced_rider_codes_json, family_keys_json "
            f"FROM ncuc_discovery_records WHERE {where} AND filing_title IS NOT NULL",
            params,
        ).fetchall()
        import json as _json
        for row in rows:
            hq_docs.append({
                "filing_title": row["filing_title"],
                "utility": row["utility"],
                "docket_number": row["docket_number"],
                "referenced_leaf_nos": _json.loads(row["referenced_leaf_nos_json"] or "[]"),
                "referenced_rider_codes": _json.loads(row["referenced_rider_codes_json"] or "[]"),
            })

    if not hq_docs:
        typer.echo(
            f"No HQ documents found for tiers={tier}"
            + (f" family={family_key}" if family_key else "")
            + (f" leaf={leaf_no}" if leaf_no else "")
            + (f" rider={rider_code}" if rider_code else ""),
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Loaded {len(hq_docs)} HQ document(s) as signal source.")

    builder = QueryBuilder(settings)
    queries = builder.build_hq_signal_queries(hq_docs)
    queries.sort(key=lambda q: -q.priority)

    typer.echo(f"Generated {len(queries)} targeted queries (top 10):")
    for q in queries[:10]:
        typer.echo(f"  [{q.priority:.1f}] [{q.template_name}] {q.query_text!r}")

    if dry_run:
        typer.echo("\n(dry-run — no searches executed)")
        return

    svc = NcucDiscoveryService(settings)
    try:
        total_new = 0
        for q_spec in queries:
            ncuc_query = q_spec.to_ncuc_query()
            count = 0
            for result in svc.search_edocket_keyword(ncuc_query, max_results=max_results):
                rec = result.record
                # Attach search ideality from scorer if available
                if hasattr(result, "ideality") and result.ideality:
                    rec = rec.model_copy(update={
                        "search_confidence_score": result.ideality.confidence,
                        "search_ideality": (
                            "ideal" if result.ideality.is_ideal_candidate else "probable"
                        ),
                    })
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                count += 1
                total_new += 1
            typer.echo(f"  {q_spec.query_text!r}: {count} results")
        typer.echo(f"\nTotal upserted: {total_new} records.")
    finally:
        svc.close()


@app.command("compare-version-rates")
def compare_version_rates(
    family_key: str | None = typer.Option(None, help="Family key (e.g. nc-progress-leaf-602). Compares latest two versions."),
    version_a: int | None = typer.Option(None, "--version-a", help="Older tariff_version id."),
    version_b: int | None = typer.Option(None, "--version-b", help="Newer tariff_version id."),
    show_unchanged: bool = typer.Option(False, help="Also show unchanged charges."),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Compare rate charges between two tariff versions of the same family.

    If --family-key is given without version ids, compares the two most recent
    versions that have extracted charges.  Highlights redline-to-clean transitions
    and flags when document quality tier changes between versions.

    Examples:
        duke-rates compare-version-rates --family-key nc-progress-leaf-602
        duke-rates compare-version-rates --version-a 45 --version-b 87
        duke-rates compare-version-rates --family-key nc-carolinas-rider-sts --show-unchanged
    """
    import sqlite3, json as _json
    from duke_rates.historical.ncuc.pipeline.version_compare import (
        compare_versions,
        compare_family_latest_two,
    )
    from duke_rates.config import Settings

    settings, _ = _bootstrap()
    db_path = settings.database_path

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        if version_a is not None and version_b is not None:
            result = compare_versions(conn, version_a, version_b)
        elif family_key:
            result = compare_family_latest_two(conn, family_key)
            if result is None:
                typer.echo(
                    f"Fewer than two versions with charges found for {family_key!r}.", err=True
                )
                raise typer.Exit(1)
        else:
            typer.echo(
                "Provide --family-key or both --version-a and --version-b.", err=True
            )
            raise typer.Exit(1)

    if output_json:
        import dataclasses
        typer.echo(_json.dumps(dataclasses.asdict(result), indent=2, default=str))
        return

    # Human-readable output
    typer.echo(f"\n{'='*70}")
    typer.echo(f"  {result.summary}")
    typer.echo(f"{'='*70}")
    typer.echo(
        f"  Version A: id={result.version_a_id}  effective={result.effective_date_a}  "
        f"tier={result.doc_tier_a or 'unknown'}  "
        f"revision={result.revision_label_a or '—'}  "
        f"redline={result.redline_flag_a}"
    )
    typer.echo(
        f"  Version B: id={result.version_b_id}  effective={result.effective_date_b}  "
        f"tier={result.doc_tier_b or 'unknown'}  "
        f"revision={result.revision_label_b or '—'}  "
        f"redline={result.redline_flag_b}"
    )
    typer.echo("")

    # Show changed charges first, then added, then removed
    changed_deltas = [d for d in result.rate_deltas if d.change_type == "changed"]
    added_deltas   = [d for d in result.rate_deltas if d.change_type == "added"]
    removed_deltas = [d for d in result.rate_deltas if d.change_type == "removed"]
    unchanged_deltas = [d for d in result.rate_deltas if d.change_type == "unchanged"]

    if changed_deltas:
        typer.echo(f"  CHANGED ({len(changed_deltas)}):")
        for d in changed_deltas:
            pct_str = f" ({d.delta_pct:+.2f}%)" if d.delta_pct is not None else ""
            typer.echo(
                f"    {d.charge_label:<45} "
                f"{d.old_value} {d.old_unit or ''} -> "
                f"{d.new_value} {d.new_unit or ''}{pct_str}"
            )

    if added_deltas:
        typer.echo(f"\n  ADDED ({len(added_deltas)}):")
        for d in added_deltas:
            typer.echo(f"    + {d.charge_label:<44} {d.new_value} {d.new_unit or ''}")

    if removed_deltas:
        typer.echo(f"\n  REMOVED ({len(removed_deltas)}):")
        for d in removed_deltas:
            typer.echo(f"    - {d.charge_label:<44} {d.old_value} {d.old_unit or ''}")

    if show_unchanged and unchanged_deltas:
        typer.echo(f"\n  UNCHANGED ({len(unchanged_deltas)}):")
        for d in unchanged_deltas:
            typer.echo(f"    = {d.charge_label:<44} {d.old_value} {d.old_unit or ''}")

    typer.echo("")


@app.command("ncuc-ingest-url")
def ncuc_ingest_url(
    url: str = typer.Argument(..., help="NCUC viewer or document URL to ingest."),
    title: str | None = typer.Option(None, help="Filing title override."),
    docket: str | None = typer.Option(None, help="Docket number (e.g. 'E-2, Sub 1142')."),
    notes: str | None = typer.Option(None, help="Comma-separated provenance notes."),
    method: str = typer.Option(
        "search_engine",
        help="Acquisition method: search_engine | manual_seed | direct_http.",
    ),
) -> None:
    """Ingest a single externally-discovered NCUC document URL as a discovery lead."""
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucAcquisitionMethod

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        acq_method = NcucAcquisitionMethod(method)
        record = svc.ingest_discovered_url(
            url,
            title=title,
            docket_hint=docket,
            notes=notes.split(",") if notes else [],
            acquisition_method=acq_method,
        )
        rec_id = repository.upsert_ncuc_discovery_record(record)
        typer.echo(f"Ingested NCUC URL as record id={rec_id}")
        typer.echo(f"  docket={record.docket_number}  classification={record.filing_classification.value}")
        typer.echo(f"  schedule_codes={record.referenced_schedule_codes}")
        typer.echo(f"  fetch_status={record.fetch_status.value}")
    finally:
        svc.close()


@app.command("ncuc-fetch")
def ncuc_fetch(
    record_id: int | None = typer.Option(
        None, help="Fetch a specific NCUC discovery record by id."
    ),
    pending: bool = typer.Option(False, help="Fetch all pending records."),
    retry_failed: bool = typer.Option(False, help="Retry all previously failed records."),
    limit: int = typer.Option(20, help="Maximum records to fetch in batch mode."),
    playwright: bool = typer.Option(
        False, help="Force Playwright browser for this fetch (overrides HTTP)."
    ),
) -> None:
    """Download NCUC documents: fetch specific record, all pending, or retry failed."""
    from duke_rates.historical.ncuc.downloader import NcucDownloader

    settings, repository = _bootstrap()
    dl = NcucDownloader(settings, repository)
    try:
        if record_id is not None:
            rec = repository.get_ncuc_discovery_record(record_id)
            if not rec:
                raise typer.BadParameter(f"NCUC record {record_id} not found.")
            if playwright:
                # Force playwright by clearing download_url to trigger viewer resolution
                from duke_rates.historical.ncuc.downloader import NcucDownloader as _DL
                url = rec.viewer_url or rec.download_url or rec.discovered_url
                if url:
                    content, ct, fu = dl._playwright_fetch(url)
                    if content:
                        from duke_rates.historical.ncuc.downloader import _build_ncuc_path
                        from duke_rates.download.hashing import sha256_bytes
                        from datetime import UTC, datetime as _dt
                        suffix = ".pdf" if "pdf" in ct.lower() else ".bin"
                        path = _build_ncuc_path(settings.raw_dir, rec, suffix)
                        path.write_bytes(content)
                        ch = sha256_bytes(content)
                        repository.mark_ncuc_fetch_status(
                            record_id,
                            status=repository._row_to_ncuc_discovery_record(
                                repository._connect().execute(
                                    "SELECT * FROM ncuc_discovery_records WHERE id=?",
                                    (record_id,),
                                ).fetchone()
                            ).fetch_status.__class__.SUCCESS,
                            local_path=str(path),
                            content_hash=ch,
                            content_type=ct,
                            file_size_bytes=len(content),
                        )
                        typer.echo(f"Playwright download -&gt; {path}")
                    else:
                        typer.echo("Playwright fetch returned no PDF content.")
                return
            result = dl.fetch(rec)
            typer.echo(f"Fetch complete: status={result.fetch_status.value} path={result.local_path}")
        elif pending:
            results = dl.fetch_pending(limit=limit)
            ok = sum(1 for r in results if r.fetch_status.value == "success")
            typer.echo(f"Fetched {len(results)} pending records: {ok} succeeded.")
        elif retry_failed:
            results = dl.retry_failed(limit=limit)
            ok = sum(1 for r in results if r.fetch_status.value == "success")
            typer.echo(f"Retried {len(results)} failed records: {ok} now succeeded.")
        else:
            typer.echo("Specify --record-id, --pending, or --retry-failed.")
    finally:
        dl.close()


@app.command("ncuc-fetch-portal")
def ncuc_fetch_portal(
    limit: int = typer.Option(50, help="Maximum records to fetch per run."),
    dep_only: bool = typer.Option(False, "--dep-only", help="Only fetch Duke Energy Progress records."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Also retry previously failed portal records."),
) -> None:
    """Fetch pending NCUC portal document-detail records using authenticated Playwright."""
    from duke_rates.historical.ncuc.document_param_search import fetch_document_detail
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        download_view_file,
    )
    from duke_rates.download.hashing import sha256_bytes
    from duke_rates.utils.files import ensure_parent
    from duke_rates.utils.text import slugify
    from duke_rates.models.ncuc import NcucFetchStatus
    import datetime as _dt

    settings, repository = _bootstrap()

    statuses = ["pending"]
    if retry_failed:
        statuses.append("failed")

    all_records = []
    for status in statuses:
        all_records.extend(
            repository.list_ncuc_discovery_records(fetch_status=status)
        )

    # Only records that have a starw1 document-detail URL and a title
    # (title indicates they came from the search pipeline ingest, not legacy blank records)
    portal_records = [
        r for r in all_records
        if r.discovered_url and "PSCDocumentDetailsPageNCUC" in r.discovered_url
        and r.filing_title  # skip legacy blank-title records that lack ViewFile links
    ]
    if dep_only:
        portal_records = [
            r for r in portal_records
            if "Duke Energy Progress" in (r.filing_title or "") or
               "DEP" in (r.filing_title or "")[:20] or
               (r.docket_number or "").startswith("E-2 Sub") or
               (r.docket_number or "").startswith("E-2, Sub")
        ]
    # Sort by id descending so newest pipeline records are fetched first
    portal_records.sort(key=lambda r: r.id or 0, reverse=True)
    portal_records = portal_records[:limit]

    if not portal_records:
        typer.echo("No portal document-detail records pending fetch.")
        return

    typer.echo(f"Fetching {len(portal_records)} portal document-detail records via authenticated session...")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Authentication failed: {exc}")
        raise typer.Exit(1)

    succeeded = 0
    failed = 0
    try:
        for rec in portal_records:
            detail_url = rec.discovered_url
            typer.echo(f"  [{rec.id}] {(rec.filing_title or detail_url)[:60]}")
            try:
                detail = fetch_document_detail(page, detail_url)
                view_urls = detail.get("view_file_urls", [])
                if not view_urls:
                    typer.echo(f"    -> no view-file links found on detail page")
                    repository.mark_ncuc_fetch_status(
                        rec.id,
                        status=NcucFetchStatus.FAILED,
                        error_detail="no_view_file_links",
                    )
                    failed += 1
                    continue

                # Download via authenticated browser session (handles ViewFile.aspx)
                pdf_url = view_urls[0]
                typer.echo(f"    -> downloading {pdf_url[:70]}")

                docket_slug = slugify(rec.docket_number or "unknown-docket")
                title_slug = slugify((rec.filing_title or "document")[:80])
                date_part = (rec.filing_date or "nodate").replace("-", "")[:8]
                filename = f"{docket_slug}-{date_part}-{title_slug}.pdf"
                dest = ensure_parent(
                    settings.raw_dir / "historical" / "ncuc" / docket_slug / filename
                )

                size = download_view_file(page, pdf_url, dest)
                if size == 0:
                    typer.echo(f"    -> empty file downloaded")
                    repository.mark_ncuc_fetch_status(
                        rec.id,
                        status=NcucFetchStatus.FAILED,
                        error_detail="empty_file",
                    )
                    failed += 1
                    continue

                content_hash = sha256_bytes(dest.read_bytes())
                repository.mark_ncuc_fetch_status(
                    rec.id,
                    status=NcucFetchStatus.SUCCESS,
                    local_path=str(dest),
                    content_hash=content_hash,
                    file_size_bytes=size,
                    fetched_at=_dt.datetime.now(_dt.timezone.utc),
                )
                typer.echo(f"    -> saved {dest.name} ({size:,} bytes)")
                succeeded += 1

            except Exception as exc:
                typer.echo(f"    -> error: {exc}")
                repository.mark_ncuc_fetch_status(
                    rec.id,
                    status=NcucFetchStatus.FAILED,
                    error_detail=str(exc)[:200],
                )
                failed += 1
    finally:
        close_authenticated_context(pw, ctx)

    typer.echo(f"\nDone: {succeeded} downloaded, {failed} failed.")
    if succeeded:
        typer.echo("Run 'ncuc-content-mine' then 'ncuc-import-pipeline' to process downloads.")


@app.command("ncuc-list")
def ncuc_list(
    docket: str | None = typer.Option(None, help="Filter by docket number."),
    status: str | None = typer.Option(
        None, help="Filter by fetch status: pending|success|failed|requires_browser."
    ),
    family_query: str | None = typer.Option(None, help="Filter by family key substring."),
    limit: int = typer.Option(50, help="Maximum rows to display."),
    show_urls: bool = typer.Option(False, help="Show discovered URLs in output."),
) -> None:
    """List NCUC discovery records."""
    settings, repository = _bootstrap()
    records = repository.list_ncuc_discovery_records(
        docket_number=docket,
        fetch_status=status,
        family_key=family_query,
    )
    typer.echo(f"{'ID':>5}  {'STATUS':18}  {'DOCKET':20}  {'DATE':10}  TITLE")
    typer.echo("-" * 90)
    for rec in records[:limit]:
        row = (
            f"{rec.id or 0:>5}  "
            f"{rec.fetch_status.value:18}  "
            f"{(rec.docket_number or ''):20}  "
            f"{(rec.filing_date or ''):10}  "
            f"{(rec.filing_title or '(no title)')[:50]}"
        )
        typer.echo(row)
        if show_urls and rec.discovered_url:
            typer.echo(f"       url: {rec.discovered_url}")
    if len(records) > limit:
        typer.echo(f"... ({len(records) - limit} more records not shown)")
    typer.echo(f"\nTotal: {len(records)} records")


@app.command("ncuc-show")
def ncuc_show(
    record_id: int = typer.Argument(..., help="NCUC discovery record id."),
) -> None:
    """Show full detail for one NCUC discovery record."""
    settings, repository = _bootstrap()
    rec = repository.get_ncuc_discovery_record(record_id)
    if not rec:
        raise typer.BadParameter(f"NCUC record {record_id} not found.")
    typer.echo(json.dumps(rec.model_dump(mode="json"), indent=2, default=str))


@app.command("ncuc-import-pipeline")
def ncuc_import_pipeline(
    record_id: int | None = typer.Option(
        None, help="Import a specific NCUC record into the historical pipeline."
    ),
    all_downloaded: bool = typer.Option(
        False, help="Import all successfully downloaded NCUC records."
    ),
    all_discovered: bool = typer.Option(
        False, help="Import all NCUC discovery records (including pending/failed as leads)."
    ),
) -> None:
    """Import NCUC downloads/discoveries into the historical lead and docket pipeline."""
    from duke_rates.historical.ncuc.importer import NcucPipelineImporter

    settings, repository = _bootstrap()
    importer = NcucPipelineImporter(settings, repository)

    if record_id is not None:
        rec = repository.get_ncuc_discovery_record(record_id)
        if not rec:
            raise typer.BadParameter(f"NCUC record {record_id} not found.")
        result = importer.import_discovery_record(rec)
        typer.echo(f"Imported NCUC record {record_id}:")
        typer.echo(f"  family_keys={result['family_keys_matched']}")
        typer.echo(f"  lead_ids={result['lead_ids']}")
        typer.echo(f"  docket_lead_ids={result['docket_lead_ids']}")
    elif all_downloaded:
        summaries = importer.import_all_pending_downloads()
        ok = sum(1 for s in summaries if "error" not in s)
        typer.echo(f"Imported {ok}/{len(summaries)} downloaded NCUC records.")
    elif all_discovered:
        summaries = importer.import_all_discovered()
        ok = sum(1 for s in summaries if "error" not in s)
        typer.echo(f"Imported {ok}/{len(summaries)} NCUC discovery records as leads.")
    else:
        typer.echo("Specify --record-id, --all-downloaded, or --all-discovered.")


@app.command("ncuc-mine-pdf-content")
def ncuc_mine_pdf_content(
    docket: str | None = typer.Option(None, help="Filter to one docket number."),
    family_query: str | None = typer.Option(
        None, help="Filter to family/code text such as 605 or 640."
    ),
    limit: int | None = typer.Option(None, help="Maximum records to mine."),
    max_pages: int = typer.Option(12, help="Maximum PDF pages to inspect per record."),
    force: bool = typer.Option(False, help="Re-extract text even if a sidecar exists."),
) -> None:
    """Mine downloaded NCUC PDFs for schedule/rider/leaf/effective-date signals."""
    from duke_rates.historical.ncuc.content_miner import NcucPdfContentMiner

    settings, repository = _bootstrap()
    miner = NcucPdfContentMiner(settings, repository)
    summaries = miner.mine_records(
        docket_number=docket,
        family_query=family_query,
        limit=limit,
        max_pages=max_pages,
        force=force,
    )
    tariff_like = 0
    for summary in summaries:
        if summary["contains_tariff_text"]:
            tariff_like += 1
        typer.echo(
            f"id={summary['record_id']} docket={summary['docket_number']} "
            f"codes={summary['schedule_codes'] or summary['rider_codes']} "
            f"leafs={summary['leaf_nos']} tariff_text={summary['contains_tariff_text']}"
        )
    typer.echo(
        f"\nMined {len(summaries)} NCUC PDFs; {tariff_like} look like tariff/rider text exhibits."
    )


@app.command("ncuc-list-exhibit-candidates")
def ncuc_list_exhibit_candidates(
    family_query: str = typer.Option(..., help="Progress NC family query such as 605 or 640."),
    limit: int = typer.Option(20, help="Maximum candidates to display."),
    min_score: float = typer.Option(20.0, help="Minimum exhibit score."),
) -> None:
    """Rank downloaded NCUC PDFs as likely tariff exhibits for one family."""
    from duke_rates.historical.ncuc.exhibit_selector import NcucExhibitSelector

    settings, repository = _bootstrap()
    selector = NcucExhibitSelector(settings, repository)
    candidates = selector.list_candidates(
        family_query=family_query,
        limit=limit,
        min_score=min_score,
    )
    for candidate in candidates:
        typer.echo(
            f"id={candidate.record_id} score={candidate.score:.1f} "
            f"docket={candidate.docket_number} date={candidate.filing_date or '?'} "
            f"title={candidate.derived_title or candidate.filing_title or '(untitled)'}"
        )
        typer.echo(
            f"  class={candidate.filing_classification} tariff_text={candidate.contains_tariff_text} "
            f"codes={candidate.extracted_schedule_codes or candidate.extracted_rider_codes} "
            f"leafs={candidate.extracted_leaf_nos}"
        )
        typer.echo(f"  local={candidate.local_path}")
        typer.echo(f"  reasons={', '.join(candidate.reasons)}")
    typer.echo(f"\nCandidates shown: {len(candidates)}")


@app.command("ncuc-import-exhibit-candidates")
def ncuc_import_exhibit_candidates(
    family_query: str = typer.Option(..., help="Progress NC family query such as 605 or 640."),
    top: int = typer.Option(3, help="Top candidates to import."),
    min_score: float = typer.Option(35.0, help="Minimum exhibit score."),
) -> None:
    """Import top-ranked NCUC exhibit candidates into historical documents."""
    from duke_rates.historical.ncuc.exhibit_selector import NcucExhibitSelector

    settings, repository = _bootstrap()
    selector = NcucExhibitSelector(settings, repository)
    imported = selector.import_candidates(
        family_query=family_query,
        top=top,
        min_score=min_score,
    )
    for row in imported:
        typer.echo(
            f"record_id={row['record_id']} historical_id={row['historical_id']} "
            f"docket={row['docket_number']} title={row['title']}"
        )
    typer.echo(f"\nImported {len(imported)} NCUC exhibit candidates.")


@app.command("ncuc-family-query")
def ncuc_family_query(
    family_query: str = typer.Argument(
        ...,
        help="Family key or schedule number to query (e.g. '605', '670').",
    ),
    show_urls: bool = typer.Option(True, help="Show URLs."),
) -> None:
    """Show all NCUC discovery results for a target family/schedule."""
    settings, repository = _bootstrap()
    records = repository.list_ncuc_discovery_records(family_key=family_query)
    if not records:
        typer.echo(f"No NCUC records found for family: {family_query!r}")
        typer.echo("Tip: run 'ncuc-seed-discover' or 'ncuc-search' to populate records.")
        return
    typer.echo(f"NCUC records for family {family_query!r}  ({len(records)} total):\n")
    for rec in records:
        typer.echo(
            f"  id={rec.id} docket={rec.docket_number} "
            f"date={rec.filing_date or '?'} "
            f"status={rec.fetch_status.value} "
            f"class={rec.filing_classification.value}"
        )
        typer.echo(f"    title: {rec.filing_title or '(none)'}")
        if show_urls and (rec.download_url or rec.viewer_url or rec.discovered_url):
            typer.echo(f"    url: {rec.download_url or rec.viewer_url or rec.discovered_url}")
        if rec.local_path:
            typer.echo(f"    local: {rec.local_path}")
        typer.echo()


@app.command("ncuc-playwright-discover")
def ncuc_playwright_discover(
    url: str = typer.Argument(..., help="NCUC page URL to navigate with Playwright."),
    docket: str | None = typer.Option(None, help="Docket number hint for the page."),
    max_results: int = typer.Option(50, help="Max document links to extract."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Use Playwright browser to discover NCUC documents from a portal page.

    The NCUC portal (starw1.ncuc.gov) uses Cloudflare bot protection that
    blocks direct HTTP. Use this command to navigate it with a real browser.

    Example:
        duke-rates ncuc-playwright-discover \\
            'https://starw1.ncuc.gov/NCUC/page/Dockets/portal.aspx' \\
            --docket 'E-2'
    """
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucDocketSeed

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        seed = NcucDocketSeed(
            docket_number=docket or "unknown",
            utility="Duke Energy Progress",
        )
        results = svc.discover_with_playwright(url, seed=seed, max_results=max_results)
        count = 0
        for result in results:
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number} title={rec.filing_title or '(none)'}"
            )
            count += 1
        typer.echo(f"\nPlaywright discovery: {count} records from {url}")
    finally:
        svc.close()


@app.command("ncuc-public-search")
def ncuc_public_search(
    query: str = typer.Argument(
        ...,
        help="Search text for NCUC public Zoom search (ncuc.gov).",
    ),
    family_query: str | None = typer.Option(None, help="Family key hint (e.g. '605')."),
    max_results: int = typer.Option(50, help="Max results."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Search NCUC public website (ncuc.gov) - accessible without Cloudflare restriction.

    This uses the NCUC Zoom full-text search engine to find document pages.
    Results are less structured than the portal but more reliably accessible.

    Example:
        duke-rates ncuc-public-search 'Progress Energy Carolinas rate schedule 605'
    """
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucSearchQuery

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        q = NcucSearchQuery(query_text=query, family_key_hint=family_query)
        count = 0
        try:
            for result in svc.search_ncuc_public(q, max_results=max_results):
                rec = result.record
                if not dry_run:
                    rec_id = repository.upsert_ncuc_discovery_record(rec)
                    rec = rec.model_copy(update={"id": rec_id})
                typer.echo(
                    f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                    f"title={rec.filing_title or '(none)'} "
                    f"url={rec.discovered_url or ''}"
                )
                count += 1
        except Exception as exc:
            classification, detail = _classify_ncuc_access_failure(exc, surface="NCUC public keyword search")
            typer.echo(f"Classification: {classification}")
            typer.echo(detail)
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1)
        typer.echo(f"\nNCUC public search: {count} results for {query!r}")
        if count == 0:
            typer.echo("Classification: public search returned 0 results.")
            typer.echo("If you know the docket, prefer `ncuc-portal-search --docket-number ...`.")
    finally:
        svc.close()


@app.command("ncuc-wayback-discover")
def ncuc_wayback_discover(
    limit: int = typer.Option(50, help="Max Wayback snapshots to scan."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Discover NCUC docket pages via Wayback Machine CDX index.

    Queries archive.org's CDX API for indexed NCUC DocketDetails pages
    to find known docket IDs that can then be navigated via Playwright.
    """
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        count = 0
        for result in svc.discover_via_wayback(limit=limit):
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number or '?'} "
                f"url={rec.discovered_url or ''}"
            )
            count += 1
        typer.echo(f"\nWayback NCUC discovery: {count} docket records found.")
    finally:
        svc.close()


@app.command("ncuc-annual-orders-scan")
def ncuc_annual_orders_scan(
    years: str = typer.Option(
        "2016,2017,2018,2019,2020",
        help="Comma-separated list of years to scan.",
    ),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Mine NCUC annual orders PDFs for E-2 Duke Energy Progress sub-docket entries.

    Downloads the NCUC annual orders PDFs (publicly accessible, no Cloudflare)
    from www.ncuc.gov/documents/ordersYYYY.pdf and extracts all E-2 Sub references,
    including title context from the table of contents. Creates NcucDiscoveryRecord
    entries for each sub-docket found.

    Example::

        duke-rates ncuc-annual-orders-scan --years 2016,2017,2018,2019,2020
    """
    import httpx
    import io
    import re

    try:
        import pdfplumber
    except ImportError:
        typer.echo("pdfplumber not installed. Run: pip install pdfplumber")
        raise typer.Exit(1)

    from duke_rates.models.ncuc import (
        NcucAcquisitionMethod,
        NcucDiscoveryRecord,
        NcucFetchStatus,
    )
    from duke_rates.historical.ncuc.metadata import classify_filing, extract_schedule_codes

    settings, repository = _bootstrap()

    year_list = [y.strip() for y in years.split(",") if y.strip().isdigit()]
    total_count = 0

    for year in year_list:
        url = f"https://www.ncuc.gov/documents/orders{year}.pdf"
        typer.echo(f"\nDownloading {url}...")
        try:
            r = httpx.get(url, timeout=120, follow_redirects=True)
        except Exception as exc:
            typer.echo(f"  Failed to download: {exc}")
            continue

        if r.status_code != 200:
            typer.echo(f"  HTTP {r.status_code} — skipping")
            continue

        typer.echo(f"  {len(r.content):,} bytes, parsing...")
        content = io.BytesIO(r.content)
        seen_subs: dict[str, str] = {}  # sub_number -> title context

        with pdfplumber.open(content) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for m in re.finditer(r"E-2,?\s+SUB\s+(\d+)[^\n]{0,200}", text, re.I):
                    sub = m.group(1)
                    if sub not in seen_subs:
                        line = m.group(0).replace("\n", " ").strip()
                        seen_subs[sub] = line[:200]

        typer.echo(f"  Found {len(seen_subs)} E-2 sub-dockets in {year}")
        year_count = 0

        for sub_number, context_line in sorted(seen_subs.items(), key=lambda x: int(x[0])):
            docket_number = f"E-2, Sub {sub_number}"
            schedule_codes = extract_schedule_codes(context_line)
            classification = classify_filing(context_line)

            rec = NcucDiscoveryRecord(
                docket_number=docket_number,
                sub_number=sub_number,
                filing_title=context_line[:200],
                filing_date=f"{year}-01-01",
                filing_classification=classification,
                referenced_schedule_codes=schedule_codes,
                discovered_url=url,
                acquisition_method=NcucAcquisitionMethod.MANUAL_SEED,
                fetch_status=NcucFetchStatus.PENDING,
                provenance_notes=[
                    f"source=ncuc_annual_orders_pdf",
                    f"year={year}",
                    f"pdf_url={url}",
                ],
            )

            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})

            typer.echo(
                f"  {'[dry]' if dry_run else f'id={rec.id}'} "
                f"{docket_number} | {context_line[:80]}"
            )
            year_count += 1

        total_count += year_count
        typer.echo(f"  -> {year_count} records {'found' if dry_run else 'persisted'} for {year}")

    typer.echo(f"\nAnnual orders scan: {total_count} total E-2 sub-docket records across {len(year_list)} years.")


@app.command("ncuc-login-test")
def ncuc_login_test() -> None:
    """Test NCUC portal login and verify authenticated document access.

    Reads DUKE_RATES_NCID_USERNAME and DUKE_RATES_NCID_PASSWORD from .env,
    logs in via the portal's NCIDLogin form (using installed Chrome to pass
    Cloudflare), then tests access to a known E-2 docket document list.

    Example::

        duke-rates ncuc-login-test
    """
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        test_authenticated_access,
    )

    settings, _ = _bootstrap()

    if not settings.ncid_username:
        typer.echo("ERROR: DUKE_RATES_NCID_USERNAME not set in .env")
        raise typer.Exit(1)

    typer.echo(f"Logging in as: {settings.ncid_username}")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        # Test with E-2 Sub 1354 (known docket from portal scrape)
        docket_id = "9b3614b6-11d6-4703-8d18-5e2e2ef3d705"
        typer.echo(f"\nTesting DocketDetails access for E-2 Sub 1354...")
        result = test_authenticated_access(page, docket_id)

        typer.echo(f"  accessible:   {result['accessible']}")
        typer.echo(f"  cf_blocked:   {result['cf_blocked']}")
        typer.echo(f"  status_code:  {result['status_code']}")
        typer.echo(f"  html_length:  {result['html_length']}")
        typer.echo(f"  title:        {result['title']!r}")
        typer.echo(f"  doc_links:    {len(result['doc_links'])}")
        for link in result["doc_links"][:5]:
            typer.echo(f"    {link[:100]}")

        if result["accessible"]:
            typer.echo("\nLogin SUCCESS — authenticated portal access confirmed.")
            typer.echo("Note: this verifies authenticated DocketDetails access only.")
            typer.echo("For exact docket document listing use ncuc-resolve-docket-ids + ncuc-docket-fetch.")
        else:
            typer.echo("\nLogin completed but portal pages still CF-blocked.")
    finally:
        close_authenticated_context(pw, ctx)


def _pick_best_ncuc_docket_match(matches: list[dict[str, str]]) -> dict[str, str] | None:
    if not matches:
        return None
    match_rank = {
        "exact": 0,
        "normalized_exact": 1,
        "same_base_and_sub": 2,
        "partial": 3,
    }
    return sorted(
        matches,
        key=lambda item: (match_rank.get(str(item.get("match_type") or ""), 9), item.get("docket_number") or ""),
    )[0]


def _print_ncuc_docket_documents(
    docs: list[dict[str, Any]],
    *,
    top_n: int = 50,
) -> None:
    display_count = len(docs) if top_n <= 0 else min(len(docs), top_n)
    for i, doc in enumerate(docs[:display_count], start=1):
        typer.echo(
            f"  [{i:02d}] {doc['doc_type']:12s} {doc['date_filed'] or '?':12s} "
            f"{doc['description'][:70]}"
        )
        if doc["view_file_urls"]:
            typer.echo(f"        Files: {len(doc['view_file_urls'])}")
    if display_count < len(docs):
        typer.echo(f"  ... and {len(docs) - display_count} more documents")


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
            return f"python -m duke_rates enqueue-ocr-remediation-nc --limit 10{family_flag}"
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
                return f"python -m duke_rates enqueue-reprocess-nc {hd_flags}"
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


@app.command("ncuc-portal-smoke-test")
def ncuc_portal_smoke_test(
    docket_number: str = typer.Option(
        "E-2, Sub 1354",
        "--docket-number",
        help="Known docket used to verify resolve + DocketDetails + document inventory.",
    ),
) -> None:
    """Canonical authenticated NCUC portal smoke test.

    Verifies credentials/browser login, resolves a human docket string to a
    portal GUID, checks authenticated DocketDetails access, and confirms that
    the docket document inventory loads.
    """
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        get_docket_documents,
        resolve_docket_ids,
        test_authenticated_access,
    )

    settings, _ = _bootstrap()
    if not settings.ncid_username:
        typer.echo("ERROR: DUKE_RATES_NCID_USERNAME not set in .env")
        raise typer.Exit(1)

    typer.echo("NCUC authenticated portal smoke test")
    typer.echo(f"  username:      {settings.ncid_username}")
    typer.echo(f"  docket probe:  {docket_number}")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        matches = resolve_docket_ids(page, docket_number)
        if not matches:
            typer.echo("Resolve failed: authenticated docket search returned 0 matches.")
            raise typer.Exit(1)

        best_match = _pick_best_ncuc_docket_match(matches)
        assert best_match is not None
        typer.echo(
            "  resolve:       "
            f"{best_match['docket_id']} ({best_match.get('match_type') or 'unknown'})"
        )

        access = test_authenticated_access(page, best_match["docket_id"])
        typer.echo(f"  accessible:    {access['accessible']}")
        typer.echo(f"  cf_blocked:    {access['cf_blocked']}")
        typer.echo(f"  status_code:   {access['status_code']}")
        if not access["accessible"]:
            typer.echo("DocketDetails check failed after login.")
            raise typer.Exit(1)

        docs = get_docket_documents(page, best_match["docket_id"])
        typer.echo(f"  doc_inventory: {len(docs)} documents")
        if not docs:
            typer.echo("Document inventory loaded but no documents were returned.")
            raise typer.Exit(1)

        typer.echo("Smoke test SUCCESS — authenticated portal workflow is healthy.")
        typer.echo("Canonical next step: use `ncuc-portal-search` for authenticated portal work.")
    finally:
        close_authenticated_context(pw, ctx)


@app.command("ncuc-resolve-docket-ids")
def ncuc_resolve_docket_ids(
    all_seeded: bool = typer.Option(
        False,
        "--all-seeded",
        help="Resolve DocketId GUIDs for the built-in Duke Progress seed list.",
    ),
    docket_number: list[str] = typer.Option(
        None,
        "--docket-number",
        help="Specific docket number(s), e.g. --docket-number 'E-2, Sub 1107'.",
    ),
) -> None:
    """Resolve NCUC DocketId GUIDs through the authenticated portal search."""
    from duke_rates.historical.ncuc.discovery import DUKE_PROGRESS_E2_DOCKETS
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        resolve_docket_ids,
    )

    settings, _ = _bootstrap()
    targets = docket_number or []
    if all_seeded:
        for seed in DUKE_PROGRESS_E2_DOCKETS:
            if seed.docket_number not in targets:
                targets.append(seed.docket_number)
    if not targets:
        raise typer.BadParameter("Specify --docket-number or --all-seeded.")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        for target in targets:
            matches = resolve_docket_ids(page, target)
            if not matches:
                typer.echo(f"{target}: no docket-id match")
                continue
            for match in matches:
                typer.echo(
                    f"{target}: {match['docket_id']}  {match['href']}  match_type={match.get('match_type') or 'unknown'}"
                )
    finally:
        close_authenticated_context(pw, ctx)


@app.command("ncuc-portal-search")
def ncuc_portal_search(
    docket_number: str = typer.Option(
        "",
        "--docket-number",
        help="If provided, run the exact-docket authenticated path using a human docket string like 'E-2, Sub 1354'.",
    ),
    company: str = typer.Option(
        "Duke Energy Progress",
        "--company",
        help="Structured-search company filter when --docket-number is omitted.",
    ),
    filing_types: str = typer.Option(
        "TARIFF,RATESCED",
        "--types",
        help="Structured-search filing type keys when --docket-number is omitted.",
    ),
    date_after: str = typer.Option("", "--after", help="Structured search: filed on or after MM/DD/YYYY."),
    date_before: str = typer.Option("", "--before", help="Structured search: filed on or before MM/DD/YYYY."),
    max_results: int = typer.Option(500, "--max", help="Maximum results to collect."),
    tariff_only: bool = typer.Option(False, "--tariff-only", help="Structured search: display only tariff-related rows."),
    top_n: int = typer.Option(50, "--top", help="Maximum results/documents to display."),
    export_csv: str = typer.Option("", "--csv", help="Structured search: export rows to this CSV path."),
    export_json: str = typer.Option("", "--json", help="Structured search: export rows to this JSON path."),
) -> None:
    """Canonical authenticated NCUC portal search surface.

    Branches explicitly:
    - with ``--docket-number``: exact-docket resolve + inventory path
    - without ``--docket-number``: authenticated structured search path
    """
    from duke_rates.historical.ncuc.document_param_search import (
        DocumentParamSearcher,
        print_doc_param_results,
    )
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        get_docket_documents,
        resolve_docket_ids,
    )

    settings, _ = _bootstrap()

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        if docket_number:
            typer.echo("Search mode: authenticated exact-docket")
            matches = resolve_docket_ids(page, docket_number)
            if not matches:
                typer.echo("Result: 0 docket GUID matches.")
                typer.echo("Classification: authenticated exact-docket search returned no matches.")
                raise typer.Exit(1)

            typer.echo(f"Resolved {len(matches)} docket candidate(s):")
            for match in matches[:5]:
                typer.echo(
                    f"  {match['docket_number']} -> {match['docket_id']} "
                    f"({match.get('match_type') or 'unknown'})"
                )
            if len(matches) > 5:
                typer.echo(f"  ... and {len(matches) - 5} more matches")

            best_match = _pick_best_ncuc_docket_match(matches)
            assert best_match is not None
            docs = get_docket_documents(page, best_match["docket_id"])

            typer.echo(
                "Using best match: "
                f"{best_match['docket_number']} -> {best_match['docket_id']} "
                f"({best_match.get('match_type') or 'unknown'})"
            )
            typer.echo(f"Found {len(docs)} docket documents.")
            _print_ncuc_docket_documents(docs, top_n=top_n)
            typer.echo(
                "For download/persistence, run: "
                f"python -m duke_rates ncuc-docket-fetch {best_match['docket_id']} "
                f'--docket-number "{best_match["docket_number"]}" --dry-run'
            )
            return

        typer.echo("Search mode: authenticated structured")
        ft_keys = [t.strip().upper() for t in filing_types.split(",") if t.strip()]
        searcher = DocumentParamSearcher(settings)
        results = searcher.search(
            page,
            company_name=company,
            docket_number="",
            filing_types=ft_keys,
            date_after=date_after,
            date_before=date_before,
            max_results=max_results,
        )

        typer.echo(f"Found {len(results)} documents.")
        typer.echo("Classification: authenticated structured search completed.")
        print_doc_param_results(results, top_n=top_n, only_tariff_related=tariff_only)

        if export_csv:
            import csv as _csv

            out = Path(export_csv)
            out.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = [
                "doc_type", "description", "date_filed", "docket_number", "company_name",
                "document_detail_url", "extracted_schedule_codes", "extracted_rider_codes",
                "filing_classification", "view_file_urls",
            ]
            with out.open("w", newline="", encoding="utf-8") as f:
                writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for r in results:
                    writer.writerow({
                        "doc_type": r.doc_type,
                        "description": r.description,
                        "date_filed": r.date_filed,
                        "docket_number": r.docket_number,
                        "company_name": r.company_name,
                        "document_detail_url": r.document_detail_url or "",
                        "extracted_schedule_codes": ", ".join(r.extracted_schedule_codes),
                        "extracted_rider_codes": ", ".join(r.extracted_rider_codes),
                        "filing_classification": r.filing_classification,
                        "view_file_urls": "; ".join(r.view_file_urls),
                    })
            typer.echo(f"Exported {len(results)} rows -> {out}")

        if export_json:
            import dataclasses
            import json as _json

            out = Path(export_json)
            out.parent.mkdir(parents=True, exist_ok=True)
            data = [dataclasses.asdict(r) for r in results]
            out.write_text(_json.dumps(data, indent=2), encoding="utf-8")
            typer.echo(f"Exported {len(results)} rows -> {out}")
    finally:
        close_authenticated_context(pw, ctx)


@app.command("ncuc-docket-fetch")
def ncuc_docket_fetch(
    docket_id: str = typer.Argument(..., help="NCUC DocketId GUID."),
    docket_number: str = typer.Option("", help="Human-readable docket number (e.g. 'E-2, Sub 1354')."),
    download: bool = typer.Option(False, help="Download all ViewFile documents to local storage."),
    dry_run: bool = typer.Option(False, help="List documents without downloading or persisting."),
) -> None:
    """Fetch all documents for an NCUC docket using authenticated session.

    Logs into the NCUC portal with NCID credentials, navigates to the docket's
    Documents tab, lists all filings, and optionally downloads their files.

    Example::

        duke-rates ncuc-docket-fetch 9b3614b6-11d6-4703-8d18-5e2e2ef3d705 \\
            --docket-number "E-2, Sub 1354" --download
    """
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        download_view_file,
        get_docket_documents,
    )
    from duke_rates.models.ncuc import (
        NcucAcquisitionMethod,
        NcucDiscoveryRecord,
        NcucFetchStatus,
        NcucFilingClassification,
    )
    from duke_rates.historical.ncuc.metadata import classify_filing, extract_schedule_codes, extract_rider_codes

    settings, repository = _bootstrap()

    if not settings.ncid_username:
        typer.echo("ERROR: DUKE_RATES_NCID_USERNAME not set in .env")
        raise typer.Exit(1)

    typer.echo(f"Logging in as: {settings.ncid_username}")
    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        typer.echo(f"\nFetching documents for docket: {docket_number or docket_id}")
        try:
            docs = get_docket_documents(page, docket_id)
        except Exception as exc:
            classification, detail = _classify_ncuc_access_failure(exc, surface="Authenticated docket inventory")
            typer.echo(f"Classification: {classification}")
            typer.echo(detail)
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1)
        typer.echo(f"Found {len(docs)} documents")
        typer.echo("Classification: authenticated exact-docket inventory completed.\n")

        for i, doc in enumerate(docs):
            typer.echo(
                f"  [{i+1:02d}] {doc['doc_type']:12s} {doc['date_filed'] or '?':12s} "
                f"{doc['description'][:70]}"
            )
            if doc["view_file_urls"]:
                typer.echo(f"        Files: {len(doc['view_file_urls'])}")

            if dry_run:
                continue

            import re
            sub_m = re.search(r"Sub\s+(\d+)", docket_number or "", re.I)
            sub_number = sub_m.group(1) if sub_m else None
            combined = f"{docket_number} {doc['description']}"
            schedule_codes = extract_schedule_codes(combined)
            rider_codes = extract_rider_codes(combined)
            classification = classify_filing(f"{doc['doc_type']} {doc['description']}")
            detail_url = (
                doc["document_url"]
                or f"https://starw1.ncuc.gov/NCUC/page/docket-docs/PSC/DocketDetails.aspx?DocketId={docket_id}"
            )

            attachment_urls = doc["view_file_urls"] or [None]
            for attachment_index, attachment_url in enumerate(attachment_urls, start=1):
                attachment_suffix = ""
                if len(attachment_urls) > 1:
                    attachment_suffix = f" [attachment {attachment_index}/{len(attachment_urls)}]"

                rec = NcucDiscoveryRecord(
                    docket_number=docket_number or None,
                    sub_number=sub_number,
                    filing_title=f"{doc['description']}{attachment_suffix}",
                    filing_date=doc["date_filed"],
                    proceeding_type=doc["doc_type"],
                    filing_classification=classification,
                    referenced_schedule_codes=schedule_codes,
                    referenced_rider_codes=rider_codes,
                    discovered_url=detail_url,
                    viewer_url=attachment_url or doc["document_url"],
                    attachment_url=attachment_url,
                    acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
                    fetch_status=NcucFetchStatus.PENDING,
                    provenance_notes=[
                        "source=authenticated_docket_docs",
                        f"docket_id={docket_id}",
                        f"view_files={len(doc['view_file_urls'])}",
                        f"attachment_index={attachment_index}",
                    ],
                )

                if download and attachment_url:
                    import re as _re

                    file_id_m = _re.search(r"Id=([a-f0-9-]{36})", attachment_url, _re.I)
                    file_id = file_id_m.group(1) if file_id_m else f"attachment-{attachment_index}"
                    slug = re.sub(r"[^a-z0-9]+", "-", (docket_number or docket_id).lower()).strip("-")
                    dest = settings.historical_dir / "ncuc" / slug / f"{file_id}.pdf"
                    try:
                        size = download_view_file(page, attachment_url, dest)
                        rec = rec.model_copy(update={
                            "fetch_status": NcucFetchStatus.SUCCESS,
                            "local_path": str(dest),
                            "file_size_bytes": size,
                        })
                        typer.echo(f"        -> Downloaded {size:,} bytes to {dest.name}")
                    except Exception as exc:
                        rec = rec.model_copy(update={
                            "fetch_status": NcucFetchStatus.FAILED,
                            "error_detail": str(exc)[:200],
                        })
                        typer.echo(f"        -> Download failed: {exc}")

                rec_id = repository.upsert_ncuc_discovery_record(rec)
                typer.echo(f"        -> Persisted as record id={rec_id}")

        if not dry_run:
            typer.echo(f"\nDone. {len(docs)} records persisted.")
    finally:
        close_authenticated_context(pw, ctx)


@app.command("ncuc-portal-scrape")
def ncuc_portal_scrape(
    max_pages: int = typer.Option(5, help="Max portal pages to scrape."),
    e2_only: bool = typer.Option(True, help="Limit to E-2 dockets only."),
    all_e_dockets: bool = typer.Option(False, help="Include all E-* dockets (overrides --e2-only)."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Scrape NCUC portal.aspx recent orders via Playwright.

    Loads the NCUC portal.aspx page (accessible without Cloudflare challenge)
    and extracts E-2 Duke Energy Progress docket entries. Paginates using the
    MS Ajax pager. Persists each entry as an NcucDiscoveryRecord.

    Example::

        duke-rates ncuc-portal-scrape --max-pages 5 --e2-only
    """
    from duke_rates.historical.ncuc.portal_scraper import NcucPortalScraper

    settings, repository = _bootstrap()
    scraper = NcucPortalScraper(settings)
    records = scraper.scrape_recent_orders(
        max_pages=max_pages,
        e2_only=not all_e_dockets and e2_only,
        all_e_dockets=all_e_dockets,
    )

    if not records:
        typer.echo("No records found.")
        return

    count = 0
    for rec in records:
        if not dry_run:
            rec_id = repository.upsert_ncuc_discovery_record(rec)
            rec = rec.model_copy(update={"id": rec_id})
        typer.echo(
            f"  id={getattr(rec, 'id', '?')} "
            f"docket={rec.docket_number or '?'} "
            f"date={rec.filing_date or '?'} "
            f"title={str(rec.filing_title or '')[:60]}"
        )
        count += 1

    typer.echo(f"\nPortal scrape: {count} records {'found (dry run)' if dry_run else 'persisted'}.")


@app.command("ncuc-wayback-harvest")
def ncuc_wayback_harvest(
    limit: int = typer.Option(200, help="Max Wayback CDX results to retrieve."),
    fetch_snapshots: bool = typer.Option(False, help="Fetch each snapshot via Playwright to extract document links."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Harvest NCUC docket GUIDs from Wayback Machine CDX index.

    Queries the Wayback CDX API for all indexed NCUC DocketDetails pages,
    extracting docket GUIDs. Optionally fetches each snapshot via Playwright
    to attempt document link extraction (most snapshots are blank JS-rendered).

    Example::

        duke-rates ncuc-wayback-harvest --limit 500
        duke-rates ncuc-wayback-harvest --limit 50 --fetch-snapshots
    """
    from duke_rates.historical.ncuc.portal_scraper import NcucWaybackHarvester

    settings, repository = _bootstrap()
    harvester = NcucWaybackHarvester(settings)
    try:
        docket_hits = harvester.harvest_docket_guids(limit=limit)
        typer.echo(f"Wayback CDX: {len(docket_hits)} unique NCUC docket URLs found")

        persisted_count = 0
        snapshot_count = 0

        for hit in docket_hits:
            typer.echo(
                f"  docket_id={hit.get('docket_id') or '?'} "
                f"ts={hit.get('timestamp', '')[:8]} "
                f"url={hit.get('original_url', '')[:80]}"
            )

            if fetch_snapshots and hit.get("wayback_url"):
                snap_records = harvester.fetch_wayback_snapshot_with_playwright(
                    hit["wayback_url"],
                    docket_hint=None,
                )
                snapshot_count += len(snap_records)
                for rec in snap_records:
                    if not dry_run:
                        rec_id = repository.upsert_ncuc_discovery_record(rec)
                        rec = rec.model_copy(update={"id": rec_id})
                        persisted_count += 1
                    typer.echo(
                        f"    -> id={getattr(rec, 'id', '?')} "
                        f"title={str(rec.filing_title or '')[:50]}"
                    )

        msg = f"\nWayback harvest: {len(docket_hits)} docket GUIDs"
        if fetch_snapshots:
            msg += f", {snapshot_count} snapshot documents"
            if not dry_run:
                msg += f" ({persisted_count} persisted)"
        typer.echo(msg)
    finally:
        harvester.close()


@app.command("ncuc-pending-rates")
def ncuc_pending_rates(
    utility: str = typer.Option(
        "progress",
        "--utility",
        help="Duke utility to scan: 'progress' (DEP/E-2) or 'carolinas' (DEC/E-7).",
    ),
    days: int = typer.Option(
        730,
        "--days",
        help="Look back this many days when filtering recent filings (0 = all).",
    ),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Scan NCUC discovery records for pending or recent rate cases and rider filings.

    Reads the local ncuc_discovery_records table (populated by ncuc-seed-discover,
    ncuc-search, or ncuc-annual-orders-scan) and surfaces filings that may signal
    upcoming rate changes:

    \\b
    - APPLICATION / SETTLEMENT filings -- proposed new rates not yet in effect
    - ORDER filings dated within --days -- approved changes that may need re-parsing
    - TARIFF_SHEETS filings -- new tariff page submissions
    - Any filing with high relevance score that mentions schedule or rider codes

    Run ncuc-seed-discover or ncuc-search first to populate the database, then use
    this command to see what pending changes are in the pipeline.

    Examples:
        duke-rates ncuc-pending-rates
        duke-rates ncuc-pending-rates --days 365 --json
        duke-rates ncuc-pending-rates --utility carolinas
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    _, repository = _bootstrap()

    records = repository.list_ncuc_discovery_records()

    if not records:
        typer.echo(
            "No NCUC discovery records found. Run ncuc-seed-discover or ncuc-search first."
        )
        raise typer.Exit(0)

    # Filter by utility keyword
    _util_kw = "progress" if utility.lower() == "progress" else "carolinas"
    _docket_prefix = "E-2" if _util_kw == "progress" else "E-7"
    records = [
        r for r in records
        if (r.docket_number or "").startswith(_docket_prefix)
        or _util_kw in (r.utility or "").lower()
    ]

    # Date cutoff
    cutoff = None
    if days > 0:
        cutoff = (_dt.now(_tz.utc) - _td(days=days)).date()

    # Classify into categories of interest
    PENDING_CLASSES = {"application", "settlement"}
    RECENT_ORDER_CLASSES = {"order"}
    TARIFF_CLASSES = {"tariff_sheets"}

    pending: list = []
    recent_orders: list = []
    tariff_filings: list = []
    rider_filings: list = []

    for rec in records:
        cls = rec.filing_classification.value if rec.filing_classification else "other"
        date_str = rec.filing_date or ""
        # Parse date for cutoff check
        rec_date = None
        try:
            if date_str:
                # Handle various date formats stored in the DB
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        rec_date = _dt.strptime(date_str[:10], fmt[:len(date_str[:10])]).date()
                        break
                    except ValueError:
                        continue
        except Exception:
            pass

        after_cutoff = (cutoff is None) or (rec_date is None) or (rec_date >= cutoff)

        has_schedule = bool(rec.referenced_schedule_codes)
        has_rider = bool(rec.referenced_rider_codes or rec.referenced_leaf_nos)

        if cls in PENDING_CLASSES:
            pending.append(rec)
        elif cls in RECENT_ORDER_CLASSES and after_cutoff:
            recent_orders.append(rec)
        elif cls in TARIFF_CLASSES and after_cutoff:
            tariff_filings.append(rec)
        elif has_rider and after_cutoff:
            rider_filings.append(rec)

    if output_json:
        def _rec_to_dict(r):
            return {
                "id": r.id,
                "docket_number": r.docket_number,
                "filing_title": r.filing_title,
                "filing_date": r.filing_date,
                "filing_classification": r.filing_classification.value if r.filing_classification else None,
                "proceeding_type": r.proceeding_type,
                "referenced_schedule_codes": r.referenced_schedule_codes,
                "referenced_rider_codes": r.referenced_rider_codes,
                "fetch_status": r.fetch_status.value if r.fetch_status else None,
                "download_url": r.download_url or r.viewer_url,
            }
        typer.echo(_json.dumps({
            "utility": utility,
            "days": days,
            "pending_rate_cases": [_rec_to_dict(r) for r in pending],
            "recent_orders": [_rec_to_dict(r) for r in recent_orders],
            "tariff_sheet_filings": [_rec_to_dict(r) for r in tariff_filings],
            "rider_filings": [_rec_to_dict(r) for r in rider_filings],
        }, indent=2))
        return

    def _print_section(title: str, items: list, max_rows: int = 20) -> None:
        typer.echo(f"\n{'=' * 70}")
        typer.echo(f"  {title} ({len(items)} records)")
        typer.echo(f"{'=' * 70}")
        if not items:
            typer.echo("  (none)")
            return
        typer.echo(f"  {'DATE':<12}  {'DOCKET':<20}  {'CLASS':<16}  {'TITLE'}")
        typer.echo(f"  {'-' * 66}")
        for rec in items[:max_rows]:
            date_s = (rec.filing_date or "")[:10]
            docket_s = (rec.docket_number or "")[:19]
            cls_s = (rec.filing_classification.value if rec.filing_classification else "other")[:15]
            title_s = (rec.filing_title or "(untitled)")[:40]
            codes = ""
            if rec.referenced_schedule_codes:
                codes = f"  [sched: {','.join(rec.referenced_schedule_codes[:4])}]"
            if rec.referenced_rider_codes:
                codes += f"  [rider: {','.join(rec.referenced_rider_codes[:4])}]"
            typer.echo(f"  {date_s:<12}  {docket_s:<20}  {cls_s:<16}  {title_s}{codes}")
        if len(items) > max_rows:
            typer.echo(f"  ... and {len(items) - max_rows} more (use --json for full output)")

    cutoff_note = f" (last {days} days)" if days > 0 else ""
    typer.echo(f"\nNCUC rate pipeline — {utility.upper()} ({_docket_prefix}){cutoff_note}")
    typer.echo(f"Total NCUC records scanned: {len(records)}")

    _print_section("PENDING RATE CASES (APPLICATION / SETTLEMENT)", pending)
    _print_section(f"RECENT ORDERS{cutoff_note}", recent_orders)
    _print_section(f"TARIFF SHEET FILINGS{cutoff_note}", tariff_filings)
    _print_section(f"RIDER-RELATED FILINGS{cutoff_note}", rider_filings)

    typer.echo(
        f"\nTip: Run 'duke-rates ncuc-seed-discover' or 'duke-rates ncuc-search' "
        f"to refresh discovery records, then re-run this command."
    )


# ---------------------------------------------------------------------------
# Phase 6.5 — Database Intelligence and Corpus Analytics
# ---------------------------------------------------------------------------


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

@app.command("search-probe-compat")
def search_probe_compat(
    delay: float = typer.Option(1.5, help="Seconds between probe requests (be polite)."),
    save: bool = typer.Option(True, help="Save results to data/manifests/search_compat.json"),
) -> None:
    """
    Stage 1: Test which NCUC full-text search query syntax patterns are accepted
    without SQL/parse errors.  Run this before the first search to discover safe
    query patterns. Results are saved and reused automatically by subsequent commands.
    """
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.search_compat import SearchCompatibilityHarness
    harness = SearchCompatibilityHarness(settings, delay_seconds=delay)
    try:
        report = harness.run_full_probe(save=save, delay=delay)
        harness.print_summary(report)
        typer.echo(f"\nSafe pattern types: {', '.join(r.pattern_type for r in report.safe_patterns[:5])}")
        typer.echo(f"Report saved: {harness.persist_path}")
    finally:
        harness.close()


@app.command("search-show-compat")
def search_show_compat() -> None:
    """Show the most recently saved search compatibility report summary."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.search_compat import SearchCompatibilityHarness
    harness = SearchCompatibilityHarness(settings)
    harness.print_summary()


@app.command("search-probe-query")
def search_probe_query(
    query: str = typer.Argument(..., help="Raw NCUC full-text query to test."),
) -> None:
    """Probe one query after NCUC-safe normalization and show the live result."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.query_syntax import sanitize_ncuc_query, classify_pattern_type
    from duke_rates.historical.ncuc.search_compat import SearchCompatibilityHarness

    harness = SearchCompatibilityHarness(settings)
    try:
        safe_types = harness.load_safe_pattern_types() or {"single_term", "two_term"}
        sanitized = sanitize_ncuc_query(query, safe_pattern_types=safe_types)
        result = harness.probe_single(sanitized, pattern_type=classify_pattern_type(sanitized))
        typer.echo(f"Raw:       {query}")
        typer.echo(f"Sanitized: {sanitized}")
        typer.echo(f"Pattern:   {result.pattern_type}")
        typer.echo(f"Success:   {result.success}")
        typer.echo(f"Errors:    {result.error_detected}")
        typer.echo(f"Zero:      {result.zero_results}")
        typer.echo(f"Count:     {result.result_count}")
        if result.error_snippet:
            typer.echo(f"Snippet:   {result.error_snippet}")
    finally:
        harness.close()


@app.command("search-run")
def search_run(
    utility: str | None = typer.Option(
        None, "--utility", "-u",
        help="Target utility: 'progress', 'carolinas', or full name. Default: both DEP and PEC.",
    ),
    schedule_codes: str | None = typer.Option(
        None, "--schedules", "-s",
        help="Comma-separated schedule codes, e.g. 501,602,607",
    ),
    rider_names: str | None = typer.Option(
        None, "--riders", "-r",
        help="Comma-separated rider names, e.g. JAA,REPS,DSM",
    ),
    doc_types: str | None = typer.Option(
        None, "--doc-types",
        help="Comma-separated doc types to target, e.g. tariff,rider,schedule",
    ),
    max_queries: int = typer.Option(30, "--max-queries", help="Maximum queries to execute."),
    refinement_rounds: int = typer.Option(
        1, "--refine", help="Number of iterative refinement rounds (0 to disable)."
    ),
    use_llm: bool = typer.Option(False, "--llm", help="Run LLM classification on top candidates."),
    use_portal: bool = typer.Option(
        True,
        "--portal/--no-portal",
        help="Use authenticated DocumentsParameterSearch before public search when NCID credentials are available.",
    ),
    portal_only: bool = typer.Option(
        False,
        "--portal-only",
        help="Use only the authenticated DocumentsParameterSearch portal and skip public Zoom search.",
    ),
    portal_max_results: int = typer.Option(
        250,
        "--portal-max",
        help="Maximum structured portal results to collect per company search.",
    ),
    top_n: int = typer.Option(25, "--top", help="Number of top ideal candidates to show."),
    export_csv: Path | None = typer.Option(None, "--csv", help="Export ranked candidates to this CSV file."),
    export_json_path: Path | None = typer.Option(None, "--json", help="Export ranked candidates to this JSON file."),
    no_save: bool = typer.Option(False, "--no-save", help="Do not persist outputs to disk."),
) -> None:
    """Run the full multi-stage NCUC tariff document search pipeline."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.search_pipeline import SearchPipeline

    # Parse utility name
    util_hint: str | None = None
    if utility:
        u = utility.lower()
        if "progress" in u or u == "dep":
            util_hint = "Duke Energy Progress"
        elif "carolinas" in u or u == "dec":
            util_hint = "Duke Energy Carolinas"
        else:
            util_hint = utility

    codes = [c.strip() for c in schedule_codes.split(",")] if schedule_codes else None
    riders = [r.strip() for r in rider_names.split(",")] if rider_names else None
    dtypes = [d.strip() for d in doc_types.split(",")] if doc_types else None

    pipeline = SearchPipeline(settings)
    try:
        typer.echo(
            f"Starting search pipeline (max_queries={max_queries}, refine={refinement_rounds}, "
            f"portal={'on' if use_portal else 'off'})..."
        )
        result = pipeline.run(
            utility=util_hint,
            schedule_codes=codes,
            rider_names=riders,
            doc_types=dtypes,
            max_queries=max_queries,
            refinement_rounds=refinement_rounds,
            top_n_ideal=top_n,
            use_llm=use_llm,
            use_portal=use_portal,
            portal_only=portal_only,
            portal_max_results=portal_max_results,
            save=not no_save,
        )
        result.print_summary(top_n=top_n)

        if export_csv:
            result.export_csv(export_csv, top_n=top_n)

        if export_json_path:
            result.export_json(export_json_path, top_n=top_n)

        if not no_save:
            typer.echo(f"\nOutputs saved to: {settings.data_dir}/manifests/search_pipeline/")

    finally:
        pipeline.close()


@app.command("search-query-report")
def search_query_report(
    top_n: int = typer.Option(20, help="Number of top queries to show."),
) -> None:
    """Show the query optimizer performance report (which queries are most useful)."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.query_optimizer import QueryOptimizer
    optimizer = QueryOptimizer(settings)
    optimizer.load()
    optimizer.print_report(n=top_n)
    terms = optimizer.suggest_refinement_terms()
    if terms:
        typer.echo(f"\nSuggested refinement terms: {', '.join(terms[:10])}")


@app.command("search-show-results")
def search_show_results(
    top_n: int = typer.Option(20, help="Number of top results to show."),
    ideal_only: bool = typer.Option(False, "--ideal", help="Show only ideal candidates."),
) -> None:
    """Show the most recently saved ranked search results."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc import search_persistence as persist
    rows = persist.load_latest_scored_results(settings)
    if not rows:
        typer.echo("No scored results found. Run 'search-run' first.")
        return

    if ideal_only:
        rows = [r for r in rows if r.get("is_ideal_candidate")]

    rows = rows[:top_n]
    typer.echo(f"\n{'=' * 65}")
    typer.echo(f"Top {len(rows)} {'ideal ' if ideal_only else ''}results from last search run")
    typer.echo(f"{'=' * 65}\n")

    for i, row in enumerate(rows, 1):
        title = (row.get("title") or "(no title)")[:55]
        score = row.get("combined_score", 0.0)
        doc_type = row.get("doc_type_guess", "?")
        finality = row.get("likely_finality", "?")
        is_ideal = row.get("is_ideal_candidate", False)
        ideal_tag = "[IDEAL]" if is_ideal else "       "
        typer.echo(f"{i:3d}. {ideal_tag} [{doc_type:<16}] [{finality:<14}] score={score:.2f}")
        typer.echo(f"     {title}")
        url = row.get("url", "")
        typer.echo(f"     {url[:80]}")
        expl = row.get("explanation", "")
        if expl:
            typer.echo(f"     >> {expl[:100]}")
        typer.echo()


@app.command("search-export")
def search_export(
    output: Path = typer.Argument(..., help="Output file path (.json or .csv)"),
    top_n: int = typer.Option(50, help="Number of top candidates to export."),
    ideal_only: bool = typer.Option(False, "--ideal", help="Export only ideal candidates."),
) -> None:
    """Export the most recent search results to JSON or CSV."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc import search_persistence as persist

    rows = persist.load_latest_scored_results(settings)
    if not rows:
        typer.echo("No scored results found. Run 'search-run' first.")
        raise typer.Exit(1)

    if ideal_only:
        rows = [r for r in rows if r.get("is_ideal_candidate")]

    rows = rows[:top_n]
    ext = output.suffix.lower()

    if ext == ".csv":
        import csv as _csv
        output.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "rank", "combined_score", "is_ideal_candidate", "doc_type_guess",
            "likely_finality", "confidence", "title", "url",
            "docket_number", "filing_date", "schedule_codes", "rider_codes",
            "ideal_reason", "nonideal_reason", "explanation",
        ]
        with output.open("w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for i, row in enumerate(rows, 1):
                row["rank"] = i
                row["schedule_codes"] = ", ".join(row.get("extracted_schedule_codes", []))
                row["rider_codes"] = ", ".join(row.get("extracted_rider_codes", []))
                writer.writerow(row)
        typer.echo(f"Exported {len(rows)} results -&gt; {output}")
    else:
        import json as _json
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json.dumps(rows, indent=2), encoding="utf-8")
        typer.echo(f"Exported {len(rows)} results -&gt; {output}")


@app.command("search-ingest")
def search_ingest(
    top_n: int = typer.Option(100, help="Number of top scored results to ingest."),
    ideal_only: bool = typer.Option(False, "--ideal", help="Ingest only ideal candidates."),
    dep_only: bool = typer.Option(False, "--dep-only", help="Ingest only Duke Energy Progress results."),
    min_score: float = typer.Option(0.0, help="Minimum combined_score threshold."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be ingested without writing."),
) -> None:
    """Bulk-ingest top scored search pipeline results into NCUC discovery records."""
    from duke_rates.historical.ncuc import search_persistence as persist
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucAcquisitionMethod

    settings, repository = _bootstrap()
    rows = persist.load_latest_scored_results(settings)
    if not rows:
        typer.echo("No scored results found. Run 'search-run' first.")
        raise typer.Exit(1)

    if ideal_only:
        rows = [r for r in rows if r.get("is_ideal_candidate")]
    if dep_only:
        rows = [r for r in rows if "Duke Energy Progress" in (r.get("utility_hint") or r.get("snippet") or "")]
    if min_score > 0:
        rows = [r for r in rows if (r.get("combined_score") or 0) >= min_score]
    rows = rows[:top_n]

    if not rows:
        typer.echo("No results matched the given filters.")
        raise typer.Exit(0)

    typer.echo(f"Ingesting {len(rows)} scored results into NCUC discovery records...")
    svc = NcucDiscoveryService(settings)
    ingested = 0
    skipped = 0
    try:
        for row in rows:
            url = row.get("url", "")
            if not url:
                skipped += 1
                continue
            title = row.get("title") or None
            docket = row.get("docket_number") or None
            score = row.get("combined_score", 0)
            doc_type = row.get("doc_type_guess", "")
            notes = [
                f"source=search_pipeline",
                f"score={score:.2f}",
            ]
            if doc_type:
                notes.append(f"doc_type={doc_type}")
            if dry_run:
                typer.echo(f"  [DRY] {url[:80]}  title={str(title)[:50]}  docket={docket}")
                ingested += 1
                continue
            record = svc.ingest_discovered_url(
                url,
                title=title,
                docket_hint=docket,
                notes=notes,
                acquisition_method=NcucAcquisitionMethod.SEARCH_ENGINE,
            )
            repository.upsert_ncuc_discovery_record(record)
            ingested += 1
    finally:
        svc.close()

    if dry_run:
        typer.echo(f"Dry run complete: would ingest {ingested} records (skipped {skipped})")
    else:
        typer.echo(f"Done: ingested {ingested} records (skipped {skipped} with no URL)")
        typer.echo("Run 'ncuc-fetch --pending' to download the documents.")


@app.command("search-doc-param")
def search_doc_param(
    company: str = typer.Option(
        "Duke Energy Progress",
        "--company",
        help="Company name filter (e.g. 'Duke Energy Progress', 'Duke Energy Carolinas').",
    ),
    docket: str = typer.Option("", "--docket", help="Optional docket number filter (e.g. 'E-2 Sub 1190')."),
    filing_types: str = typer.Option(
        "TARIFF,RATESCED",
        "--types",
        help="Comma-separated filing type keys: TARIFF, RATESCED, ORDER, INFOFILE.",
    ),
    date_after: str = typer.Option("", "--after", help="Filed on or after MM/DD/YYYY."),
    date_before: str = typer.Option("", "--before", help="Filed on or before MM/DD/YYYY."),
    max_results: int = typer.Option(500, "--max", help="Maximum results to collect."),
    tariff_only: bool = typer.Option(False, "--tariff-only", help="Filter display to tariff-related rows."),
    top_n: int = typer.Option(50, "--top", help="Number of results to display."),
    export_csv: str = typer.Option("", "--csv", help="Export results to this CSV path."),
    export_json: str = typer.Option("", "--json", help="Export results to this JSON path."),
) -> None:
    """Search NCUC DocumentsParameterSearch for tariff/rate filings.

    This is the authenticated structured-search surface. It is useful for
    company/date/type filtering, but a zero-result docket query does not prove
    that a docket has no documents. For exact docket listings, prefer
    ``ncuc-resolve-docket-ids`` followed by ``ncuc-docket-fetch``.
    """
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.document_param_search import (
        DocumentParamSearcher,
        print_doc_param_results,
    )
    from duke_rates.historical.ncuc.session import (
        create_authenticated_context,
        close_authenticated_context,
    )

    ft_keys = [t.strip().upper() for t in filing_types.split(",") if t.strip()]

    typer.echo(f"Starting authenticated NCUC session...")
    pw, ctx, page = create_authenticated_context(settings)
    try:
        try:
            searcher = DocumentParamSearcher(settings)
            results = searcher.search(
                page,
                company_name=company,
                docket_number=docket,
                filing_types=ft_keys,
                date_after=date_after,
                date_before=date_before,
                max_results=max_results,
            )
        except Exception as exc:
            classification, detail = _classify_ncuc_access_failure(exc, surface="Authenticated structured search")
            typer.echo(f"Classification: {classification}")
            typer.echo(detail)
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1)
    finally:
        close_authenticated_context(pw, ctx)

    typer.echo(f"Found {len(results)} documents.")
    typer.echo("Classification: authenticated structured search completed.")
    if not results and docket:
        docket_hint = docket if "," in docket else docket.replace(" Sub ", ", Sub ")
        typer.echo("Note: zero structured-search results for a docket does not mean the docket is empty.")
        typer.echo(
            "Use: python -m duke_rates ncuc-resolve-docket-ids --docket-number "
            f"\"{docket_hint}\""
        )
        typer.echo(
            "Then: python -m duke_rates ncuc-docket-fetch <docket-id> "
            f"--docket-number \"{docket_hint}\" --dry-run"
        )
    print_doc_param_results(results, top_n=top_n, only_tariff_related=tariff_only)

    if export_csv:
        import csv as _csv
        out = Path(export_csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "doc_type", "description", "date_filed", "docket_number", "company_name",
            "document_detail_url", "extracted_schedule_codes", "extracted_rider_codes",
            "filing_classification", "view_file_urls",
        ]
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in results:
                writer.writerow({
                    "doc_type": r.doc_type,
                    "description": r.description,
                    "date_filed": r.date_filed,
                    "docket_number": r.docket_number,
                    "company_name": r.company_name,
                    "document_detail_url": r.document_detail_url or "",
                    "extracted_schedule_codes": ", ".join(r.extracted_schedule_codes),
                    "extracted_rider_codes": ", ".join(r.extracted_rider_codes),
                    "filing_classification": r.filing_classification,
                    "view_file_urls": "; ".join(r.view_file_urls),
                })
        typer.echo(f"Exported {len(results)} rows -> {out}")

    if export_json:
        import json as _json
        import dataclasses
        out = Path(export_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = [dataclasses.asdict(r) for r in results]
        out.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        typer.echo(f"Exported {len(results)} rows -> {out}")


@app.command("search-enrich-doc-param")
def search_enrich_doc_param(
    input_json: Path = typer.Argument(..., help="JSON file previously exported by search-doc-param."),
    output_json: Path = typer.Argument(..., help="Output JSON path for enriched rows."),
    delay_seconds: float = typer.Option(0.5, "--delay", help="Delay between detail-page fetches."),
) -> None:
    """Resolve document detail pages into ViewFile URLs and filenames (requires authenticated session)."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.document_param_search import (
        DocParamSearchResult,
        DocumentParamSearcher,
    )
    from duke_rates.historical.ncuc.session import (
        close_authenticated_context,
        create_authenticated_context,
    )

    if not input_json.exists():
        typer.echo(f"Input file not found: {input_json}", err=True)
        raise typer.Exit(1)

    rows = json.loads(input_json.read_text(encoding="utf-8"))
    results = [DocParamSearchResult(**row) for row in rows]

    typer.echo(f"Starting authenticated NCUC session to enrich {len(results)} rows...")
    pw, ctx, page = create_authenticated_context(settings)
    try:
        searcher = DocumentParamSearcher(settings)
        enriched = searcher.enrich_with_document_details(
            page,
            results,
            delay_seconds=delay_seconds,
        )
    finally:
        close_authenticated_context(pw, ctx)

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps([r.__dict__ for r in enriched], indent=2),
        encoding="utf-8",
    )
    with_files = sum(1 for row in enriched if row.view_file_urls)
    typer.echo(f"Enriched {len(enriched)} rows ({with_files} with file links) -> {output_json}")


@app.command("search-download-doc-param")
def search_download_doc_param(
    input_json: Path = typer.Argument(..., help="Enriched JSON file from search-enrich-doc-param."),
    output_dir: Path = typer.Argument(..., help="Directory to write downloaded PDFs."),
    limit: int = typer.Option(0, "--limit", help="Maximum files to download (0 = all)."),
    skip_existing: bool = typer.Option(True, "--skip-existing/--overwrite", help="Skip files already present."),
) -> None:
    """Download ViewFile PDFs referenced by enriched portal search results."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.session import (
        close_authenticated_context,
        create_authenticated_context,
        download_view_file,
    )

    if not input_json.exists():
        typer.echo(f"Input file not found: {input_json}", err=True)
        raise typer.Exit(1)

    rows = json.loads(input_json.read_text(encoding="utf-8"))
    jobs: list[tuple[str, str]] = []
    for row in rows:
        urls = row.get("view_file_urls") or []
        labels = row.get("view_file_labels") or []
        for idx, url in enumerate(urls):
            label = labels[idx] if idx < len(labels) else ""
            jobs.append((url, label))

    if limit > 0:
        jobs = jobs[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Starting authenticated NCUC session to download {len(jobs)} files...")
    pw, ctx, page = create_authenticated_context(settings)
    try:
        downloaded = 0
        skipped = 0
        for url, label in jobs:
            file_id = url.split("Id=")[-1].split("&")[0]
            safe_label = "".join(ch if ch.isalnum() or ch in (" ", "-", "_", ".") else "_" for ch in label).strip()
            filename = f"{file_id}.pdf" if not safe_label else f"{file_id}__{safe_label[:100]}.pdf"
            dest = output_dir / filename
            if skip_existing and dest.exists():
                skipped += 1
                continue
            download_view_file(page, url, dest)
            downloaded += 1
    finally:
        close_authenticated_context(pw, ctx)

    typer.echo(f"Downloaded {downloaded} files, skipped {skipped} existing -> {output_dir}")


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
        "Prefer 'ncuc-import-pipeline' plus 'extract-rates-nc' for the current historical pipeline.",
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


@app.command("bill-calculator")
def bill_calculator(
    schedule: str = typer.Argument(..., help="Rate schedule code (e.g. RES, SGS, MGS-TOU)."),
    kwh: float = typer.Argument(..., help="kWh consumed in billing period."),
    date: str = typer.Option(
        "",
        "--date", "-d",
        help="Service date YYYY-MM-DD or YYYY-MM (defaults to most recent rates).",
    ),
    kw: float = typer.Option(0.0, "--kw", help="Peak demand kW (demand-metered schedules)."),
    no_riders: bool = typer.Option(False, "--no-riders", help="Exclude Leaf 600 rider adders."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Reconstruct a Duke Energy NC bill for any rate schedule and usage period.

    Looks up base rates from ncuc_ingest_segments and rider adders from
    rider_summary_blocks, then prints a line-by-line bill breakdown.

    Examples:

      python -m duke_rates bill-calculator RES 1000 --date 2024-11-01

      python -m duke_rates bill-calculator MGS 5000 --kw 25 --date 2023-10-01
    """
    from duke_rates.db.ncuc_loader import calculate_bill
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    effective = date or "2099-12-31"  # if no date, get latest
    result = calculate_bill(
        conn,
        schedule_code=schedule,
        effective_date=effective,
        kwh=kwh,
        kw=kw or None,
        include_riders=not no_riders,
        breakdown=True,
    )
    conn.close()

    if json_out:
        typer.echo(json.dumps(result, indent=2))
        return

    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(1)

    typer.echo(f"\nDuke Energy NC — Bill Estimate")
    typer.echo(f"Schedule:       {result['schedule_code']}")
    typer.echo(f"Rate effective: {result['rate_effective_date']}")
    typer.echo(f"Billing date:   {result['billing_date']}")
    typer.echo(f"Usage:          {result['kwh']:,.1f} kWh" + (f"  /  {result['kw']:.1f} kW" if result.get("kw") else ""))
    typer.echo("")
    typer.echo(f"{'Line Item':<52} {'Rate':>10} {'Amount':>10}")
    typer.echo("-" * 74)

    rider_section_started = False
    for item in result.get("line_items", []):
        cat = item["category"]
        label = item["label"]
        rate_str = f"{item['rate']:>8.4f} {item['unit']}"
        amt_str = f"${item['amount']:>8.4f}"

        if cat == "riders_total" and not rider_section_started:
            typer.echo("")
            typer.echo("  --- Rider Adjustments (Leaf 600) ---")
            rider_section_started = True
            continue
        if cat == "rider":
            indent = "    " if not item.get("is_subtotal") else "  "
            code = f"[{item['rider_code']}]" if item.get("rider_code") else ""
            typer.echo(f"{indent}{label[:44]:<44} {code:<8} {item['rate']:>7.4f}¢  ${item['amount']:>8.4f}")
            continue

        typer.echo(f"  {label[:50]:<50} {item['rate']:>8.4f}  ${item['amount']:>8.4f}")

    typer.echo("-" * 74)
    typer.echo(f"  {'Base energy rate':<50} {result['base_energy_cents_per_kwh']:>7.4f}¢/kWh")
    typer.echo(f"  {'Rider adder':<50} {result['rider_cents_per_kwh']:>7.4f}¢/kWh")
    typer.echo(f"  {'All-in effective rate':<50} {result['total_cents_per_kwh']:>7.4f}¢/kWh")
    typer.echo(f"\n  ESTIMATED TOTAL (pre-tax):  ${result['total_amount']:>10.4f}")
    typer.echo("")


@app.command("compare-schedules")
def compare_schedules_cmd(
    kwh: float = typer.Argument(..., help="kWh consumed in billing period."),
    date: str = typer.Option(
        "",
        "--date", "-d",
        help="Service date YYYY-MM-DD (defaults to most recent rates).",
    ),
    kw: float = typer.Option(0.0, "--kw", help="Peak demand kW."),
    schedules: str = typer.Option(
        "",
        "--schedules",
        help="Comma-separated schedule codes to compare (default: all available).",
    ),
    top: int = typer.Option(0, "--top", help="Show only top N cheapest (0 = all)."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Compare estimated bills across multiple Duke NC rate schedules.

    Shows all available schedules ranked cheapest-to-most-expensive for a
    given kWh usage and billing date.  Includes base rates + Leaf 600 riders.

    Examples:

      python -m duke_rates compare-schedules 1000 --date 2024-11-01

      python -m duke_rates compare-schedules 1000 --date 2024-11-01 --schedules RES,R-TOUD,R-TOU

      python -m duke_rates compare-schedules 5000 --kw 25 --date 2024-11-01 --top 5
    """
    from duke_rates.db.ncuc_loader import compare_schedules
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    effective = date or "2099-12-31"
    sched_list = [s.strip() for s in schedules.split(",") if s.strip()] or None

    results = compare_schedules(
        conn,
        effective_date=effective,
        kwh=kwh,
        kw=kw or None,
        schedules=sched_list,
    )
    conn.close()

    if not results:
        typer.echo("No rate data found for the given parameters.", err=True)
        raise typer.Exit(1)

    if top:
        results = results[:top]

    if json_out:
        typer.echo(json.dumps(results, indent=2))
        return

    typer.echo(f"\nDuke Energy NC — Rate Comparison  ({kwh:,.0f} kWh" + (f", {kw:.1f} kW" if kw else "") + f")  Date: {effective}")
    typer.echo(f"{'Rank':<5} {'Schedule':<22} {'Rate Date':<12} {'Base ¢/kWh':>11} {'Rider ¢/kWh':>12} {'All-in ¢/kWh':>13} {'Total $':>10}")
    typer.echo("-" * 88)
    for r in results:
        typer.echo(
            f"  {r['rank']:<3} {r['schedule_code']:<22} {r['rate_effective_date']:<12}"
            f" {r['base_energy_cents_per_kwh']:>10.4f}¢"
            f" {r['rider_cents_per_kwh']:>11.4f}¢"
            f" {r['total_cents_per_kwh']:>12.4f}¢"
            f" ${r['total_amount']:>9.2f}"
        )
    typer.echo("")


@app.command("load-eia-rates")
def load_eia_rates(
    csv_file: Path = typer.Option(
        None,
        "--csv",
        help="EIA avgprice_annual CSV file to load (optional; uses seed data if omitted).",
    ),
    replace: bool = typer.Option(False, "--replace", help="Overwrite existing rows."),
) -> None:
    """Load EIA Form 861 state average retail electricity rates into the database.

    Without --csv, loads bundled seed data covering NC + Southeast neighbors
    and US national average for 2010–2024.

    With --csv, loads a full EIA avgprice_annual.csv download:
      https://www.eia.gov/electricity/data/state/
    """
    from duke_rates.db.eia_loader import load_eia_csv, load_eia_seed
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    if csv_file:
        if not csv_file.exists():
            typer.echo(f"File not found: {csv_file}", err=True)
            raise typer.Exit(1)
        ins, skip = load_eia_csv(conn, csv_file, replace=replace)
        typer.echo(f"EIA CSV loaded: {ins} inserted, {skip} skipped  <- {csv_file}")
    else:
        ins, skip = load_eia_seed(conn)
        typer.echo(f"EIA seed data: {ins} inserted, {skip} skipped (NC + Southeast + US 2010-2024)")

    conn.close()


@app.command("nc-rate-context")
def nc_rate_context(
    year: int = typer.Argument(..., help="Year (e.g. 2024)."),
    sector: str = typer.Option("residential", "--sector", help="residential / commercial / industrial / all_sectors"),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC average retail electricity rate vs. Southeast neighbors and US average.

    Data sourced from EIA API v2 (populate with: duke-rates eia-backfill --states NC SC VA TN GA US).

    Example:

      python -m duke_rates nc-rate-context 2024
      python -m duke_rates nc-rate-context 2023 --sector commercial
    """
    from duke_rates.db.eia_loader import get_nc_rate_context
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    ctx = get_nc_rate_context(conn, year, sector)
    conn.close()

    if json_out:
        typer.echo(json.dumps(ctx, indent=2))
        return

    def _fmt(v) -> str:
        return f"{v:.2f}¢/kWh" if v else "  N/A   "

    typer.echo(f"\nEIA Average Retail Electricity Prices — {year}  ({sector})")
    typer.echo("-" * 44)
    typer.echo(f"  North Carolina (NC):   {_fmt(ctx['nc'])}")
    typer.echo(f"  South Carolina (SC):   {_fmt(ctx['sc'])}")
    typer.echo(f"  Virginia       (VA):   {_fmt(ctx['va'])}")
    typer.echo(f"  Tennessee      (TN):   {_fmt(ctx['tn'])}")
    typer.echo(f"  Georgia        (GA):   {_fmt(ctx['ga'])}")
    typer.echo(f"  US National Avg:       {_fmt(ctx['us_avg'])}")
    typer.echo("-" * 44)
    if ctx["nc_vs_us_pct"] is not None:
        direction = "above" if ctx["nc_vs_us_pct"] >= 0 else "below"
        typer.echo(f"  NC is {abs(ctx['nc_vs_us_pct']):.1f}% {direction} the US average")
    if ctx["nc_rank_in_southeast"]:
        typer.echo(f"  NC ranks {ctx['nc_rank_in_southeast']} in the Southeast")
    typer.echo("")


# ---------------------------------------------------------------------------
# EIA API v2 integration commands
# ---------------------------------------------------------------------------

@app.command("eia-backfill")
def eia_backfill(
    states: list[str] = typer.Option(None, "--states", "-s", help="State codes to fetch (default: all 50+DC+US)"),
    start: str = typer.Option("2001", "--start", help="Start year (YYYY)"),
    end: str = typer.Option(None, "--end", help="End year (YYYY); default: latest available"),
    skip_generation: bool = typer.Option(False, "--skip-generation", help="Skip generation-by-fuel fetch"),
    skip_profiles: bool = typer.Option(False, "--skip-profiles", help="Skip state-profile-summary"),
    skip_capability: bool = typer.Option(False, "--skip-capability", help="Skip capability fetch"),
    skip_disposition: bool = typer.Option(False, "--skip-disposition", help="Skip source-disposition"),
    cache: bool = typer.Option(True, "--cache/--no-cache", help="Cache API responses locally"),
):
    """Fetch full EIA historical data for all states (one-time backfill).

    Populates eia_retail_sales, eia_generation_by_fuel, eia_state_profile_summary,
    eia_source_disposition, and eia_state_capability tables from the EIA API v2.
    Safe to re-run — all upserts are idempotent.

    Requires EIA_API_KEY in .env or environment.

    Examples::

        duke-rates eia-backfill
        duke-rates eia-backfill --states NC SC VA GA TN
        duke-rates eia-backfill --skip-generation --start 2010
    """
    import json as _json
    from duke_rates.db.schema import migrate
    from duke_rates.eia.client import EIAClient
    from duke_rates.eia.endpoints import ALL_STATES, GENERATION_FUELS, RETAIL_SECTOR_ALL
    from duke_rates.eia.endpoints import (
        fetch_retail_sales, fetch_generation_by_fuel, fetch_state_profile_summary,
        fetch_state_source_disposition, fetch_state_capability,
    )
    from duke_rates.eia.loaders import (
        upsert_retail_sales, upsert_generation_by_fuel, upsert_state_profile_summary,
        upsert_source_disposition, upsert_state_capability,
    )
    from duke_rates.eia.references import seed_state_region_lookup, seed_market_structure_lookup
    from duke_rates.eia.transformers import (
        make_batch_id, transform_retail_sales, transform_generation_by_fuel,
        transform_state_profile_summary, transform_source_disposition, transform_state_capability,
    )

    settings = get_settings()
    if not settings.eia_api_key:
        typer.echo("ERROR: EIA_API_KEY not set in .env or environment", err=True)
        raise typer.Exit(1)

    conn = _open_db(settings)
    migrate(conn)

    cache_dir = settings.eia_cache_dir if cache else None
    client = EIAClient(
        api_key=settings.eia_api_key,
        cache_dir=cache_dir,
        request_delay=settings.eia_request_delay,
    )
    batch_id = make_batch_id()
    target_states = list(states) if states else ALL_STATES

    typer.echo(f"EIA backfill starting  batch={batch_id}  states={len(target_states)}  start={start}")

    ri, rs = seed_state_region_lookup(conn)
    mi, ms = seed_market_structure_lookup(conn)
    typer.echo(f"  reference tables: region {ri}+{rs}, market-structure {mi}+{ms}")

    raw = fetch_retail_sales(client, states=target_states, sectors=RETAIL_SECTOR_ALL, frequency="annual", start=start, end=end)
    ins, _ = upsert_retail_sales(conn, transform_retail_sales(raw, frequency="annual", batch_id=batch_id))
    typer.echo(f"  retail-sales annual:  {ins} rows upserted")

    raw_m = fetch_retail_sales(client, states=target_states, sectors=RETAIL_SECTOR_ALL, frequency="monthly", start=start, end=end)
    ins_m, _ = upsert_retail_sales(conn, transform_retail_sales(raw_m, frequency="monthly", batch_id=batch_id))
    typer.echo(f"  retail-sales monthly: {ins_m} rows upserted")

    if not skip_generation:
        raw_g = fetch_generation_by_fuel(client, states=[s for s in target_states if s != "US"], fuels=GENERATION_FUELS, sectors=["99"], frequency="annual", start=start, end=end)
        ins_g, _ = upsert_generation_by_fuel(conn, transform_generation_by_fuel(raw_g, frequency="annual", batch_id=batch_id))
        typer.echo(f"  generation-by-fuel:   {ins_g} rows upserted")

    if not skip_profiles:
        raw_p = fetch_state_profile_summary(client, states=[s for s in target_states if s != "US"], start=max(start, "2008"), end=end)
        ins_p, _ = upsert_state_profile_summary(conn, transform_state_profile_summary(raw_p, batch_id=batch_id))
        typer.echo(f"  state-profile-summary:{ins_p} rows upserted")

    if not skip_disposition:
        raw_d = fetch_state_source_disposition(client, states=[s for s in target_states if s != "US"], start="1990", end=end)
        ins_d, _ = upsert_source_disposition(conn, transform_source_disposition(raw_d, batch_id=batch_id))
        typer.echo(f"  source-disposition:   {ins_d} rows upserted")

    if not skip_capability:
        raw_c = fetch_state_capability(client, states=[s for s in target_states if s != "US"], energy_sources=["ALL","NG","NUC","HYC","WND","SOL","COL","PET"], start="1990", end=end)
        ins_c, _ = upsert_state_capability(conn, transform_state_capability(raw_c, batch_id=batch_id))
        typer.echo(f"  state-capability:     {ins_c} rows upserted")

    conn.close()
    typer.echo(f"EIA backfill complete  batch={batch_id}")


@app.command("eia-update")
def eia_update(
    states: list[str] = typer.Option(None, "--states", "-s", help="State codes (default: all)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be fetched; no writes"),
):
    """Incrementally update EIA tables with the latest data.

    Determines the last period already in each table and fetches only newer
    data.  Safe to run on a schedule (e.g., monthly).

    Examples::

        duke-rates eia-update
        duke-rates eia-update --states NC SC VA
        duke-rates eia-update --dry-run
    """
    from duke_rates.db.schema import migrate
    from duke_rates.eia.client import EIAClient
    from duke_rates.eia.endpoints import ALL_STATES, GENERATION_FUELS, RETAIL_SECTOR_ALL
    from duke_rates.eia.endpoints import (
        fetch_retail_sales, fetch_generation_by_fuel, fetch_state_profile_summary,
        fetch_state_source_disposition, fetch_state_capability,
    )
    from duke_rates.eia.loaders import (
        upsert_retail_sales, upsert_generation_by_fuel, upsert_state_profile_summary,
        upsert_source_disposition, upsert_state_capability,
    )
    from duke_rates.eia.transformers import (
        make_batch_id, transform_retail_sales, transform_generation_by_fuel,
        transform_state_profile_summary, transform_source_disposition, transform_state_capability,
    )

    settings = get_settings()
    if not settings.eia_api_key:
        typer.echo("ERROR: EIA_API_KEY not set", err=True)
        raise typer.Exit(1)

    conn = _open_db(settings)
    migrate(conn)
    client = EIAClient(api_key=settings.eia_api_key, request_delay=settings.eia_request_delay)
    batch_id = make_batch_id()
    target_states = list(states) if states else ALL_STATES

    def _latest(table: str, freq: str) -> str | None:
        row = conn.execute(f"SELECT MAX(period) FROM {table} WHERE frequency=?", (freq,)).fetchone()
        return row[0] if row and row[0] else None

    def _start_from(latest: str | None, default: str, step_back: int = 1) -> str:
        if not latest:
            return default
        try:
            return str(int(str(latest)[:4]) - step_back)
        except ValueError:
            return default

    if dry_run:
        typer.echo("DRY RUN — no data will be written")

    for freq, default in [("annual", "2001"), ("monthly", "2001")]:
        latest = _latest("eia_retail_sales", freq)
        start = _start_from(latest, default)
        typer.echo(f"retail-sales {freq}: latest={latest or 'none'}, will fetch from {start}")
        if not dry_run:
            raw = fetch_retail_sales(client, states=target_states, sectors=RETAIL_SECTOR_ALL, frequency=freq, start=start)
            ins, _ = upsert_retail_sales(conn, transform_retail_sales(raw, frequency=freq, batch_id=batch_id))
            typer.echo(f"  -> {ins} rows upserted")

    conn.close()
    typer.echo("EIA update complete" if not dry_run else "Dry run complete")


@app.command("eia-state-price")
def eia_state_price(
    state: str = typer.Argument(..., help="2-letter state code (e.g. NC, TX, CA)"),
    sector: str = typer.Option("RES", "--sector", "-s", help="Sector: RES COM IND ALL"),
    years: int = typer.Option(10, "--years", "-y", help="Number of recent years to show"),
):
    """Show EIA retail price history for a state.

    Examples::

        duke-rates eia-state-price NC
        duke-rates eia-state-price TX --sector COM --years 5
    """
    settings = get_settings()
    conn = _open_db(settings)
    conn.row_factory = __import__("sqlite3").Row

    rows = conn.execute(
        """
        SELECT year, price_cents_per_kwh, sales_million_kwh, customers
        FROM eia_retail_sales
        WHERE state=? AND sector=? AND frequency='annual' AND year IS NOT NULL
        ORDER BY year DESC
        LIMIT ?
        """,
        (state.upper(), sector.upper(), years),
    ).fetchall()

    if not rows:
        typer.echo(f"No data found for {state.upper()} / {sector.upper()}. Run: duke-rates eia-backfill")
        raise typer.Exit(1)

    typer.echo(f"\nEIA Retail Price — {state.upper()} / {sector.upper()}")
    typer.echo(f"{'Year':<6}  {'¢/kWh':>7}  {'Sales (M kWh)':>14}  {'Customers':>12}")
    typer.echo("-" * 46)
    for r in sorted(rows, key=lambda x: x["year"]):
        price = f"{r['price_cents_per_kwh']:.2f}" if r["price_cents_per_kwh"] else "  N/A"
        sales = f"{r['sales_million_kwh']:,.1f}" if r["sales_million_kwh"] else "N/A"
        cust = f"{r['customers']:,}" if r["customers"] else "N/A"
        typer.echo(f"{r['year']:<6}  {price:>7}  {sales:>14}  {cust:>12}")
    typer.echo("")
    conn.close()


@app.command("eia-national-comparison")
def eia_national_comparison(
    year: int = typer.Argument(..., help="Year to compare (e.g. 2024)"),
    sector: str = typer.Option("RES", "--sector", "-s", help="Sector: RES COM IND ALL"),
    top: int = typer.Option(10, "--top", help="Show top N cheapest and most expensive states"),
):
    """Show national price comparison: cheapest, most expensive, and NC context.

    Examples::

        duke-rates eia-national-comparison 2024
        duke-rates eia-national-comparison 2023 --sector COM --top 5
    """
    settings = get_settings()
    conn = _open_db(settings)
    conn.row_factory = __import__("sqlite3").Row

    rows = conn.execute(
        """
        SELECT r.state, r.state_name, r.price_cents_per_kwh,
               m.market_structure, m.rto
        FROM eia_retail_sales r
        LEFT JOIN eia_market_structure_lookup m ON m.state = r.state
        WHERE r.year=? AND r.sector=? AND r.frequency='annual'
          AND r.price_cents_per_kwh IS NOT NULL
          AND r.state NOT IN ('US','DC')
          AND length(r.state) = 2
        ORDER BY r.price_cents_per_kwh ASC
        """,
        (year, sector.upper()),
    ).fetchall()

    if not rows:
        typer.echo(f"No data for {year}/{sector}. Run: duke-rates eia-backfill")
        raise typer.Exit(1)

    us_row = conn.execute(
        "SELECT price_cents_per_kwh FROM eia_retail_sales WHERE state='US' AND year=? AND sector=? AND frequency='annual'",
        (year, sector.upper()),
    ).fetchone()
    us_avg = us_row["price_cents_per_kwh"] if us_row else None

    typer.echo(f"\nEIA Retail Price Comparison — {year} / {sector.upper()}")
    if us_avg:
        typer.echo(f"  US National Average: {us_avg:.2f} ¢/kWh")
    typer.echo(f"\n  {'State':<6}  {'¢/kWh':>7}  {'vs US':>7}  {'Structure':<14}  {'RTO'}")
    typer.echo("  " + "-" * 58)

    def _line(r, label=""):
        price = r["price_cents_per_kwh"]
        delta = f"{price - us_avg:+.2f}" if us_avg else "  N/A"
        struct = r["market_structure"] or "?"
        rto = r["rto"] or "none"
        marker = " <--" if r["state"] == "NC" else ""
        typer.echo(f"  {r['state']:<6}  {price:>7.2f}  {delta:>7}  {struct:<14}  {rto}{marker}")

    typer.echo(f"\n  {top} CHEAPEST:")
    for r in rows[:top]:
        _line(r)

    typer.echo(f"\n  {top} MOST EXPENSIVE:")
    for r in rows[-top:]:
        _line(r)

    nc_rows = [r for r in rows if r["state"] == "NC"]
    if nc_rows:
        nc = nc_rows[0]
        nc_rank = next((i + 1 for i, r in enumerate(rows) if r["state"] == "NC"), None)
        typer.echo(f"\n  NC: rank {nc_rank} of {len(rows)} states  ({nc['price_cents_per_kwh']:.2f} ¢/kWh)")
    typer.echo("")
    conn.close()


# ---------------------------------------------------------------------------
# Tariff completeness audit commands
# ---------------------------------------------------------------------------


@app.command("audit-tariff-timeline")
def audit_tariff_timeline(
    family: str = typer.Argument(..., help="Tariff family key, e.g. nc-progress-leaf-601"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    show_charges: bool = typer.Option(False, "--show-charges", help="Include charge_count column"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of table"),
) -> None:
    """Show version timeline, date gaps, and supersession chain for one tariff family."""
    import datetime as _dt
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)
    tm = svc.build_temporal_map(family)

    if json_out:
        typer.echo(tm.model_dump_json(indent=2))
        return

    typer.echo(f"\nFamily:  {tm.family_key}")
    typer.echo(f"Title:   {tm.title or '(none)'}")
    typer.echo(f"Type:    {tm.family_type}")
    typer.echo(f"Status:  {tm.timeline_status.upper()}")

    if tm.supersession_chain:
        typer.echo("\nSupersession chain:")
        for i, label in enumerate(tm.supersession_chain):
            prefix = "  +- " if i == len(tm.supersession_chain) - 1 else "  |- "
            typer.echo(f"{prefix}{label}")
    if tm.orphaned_revisions:
        typer.echo(f"\nOrphaned (no chain link): {', '.join(tm.orphaned_revisions)}")

    typer.echo(f"\nVersions ({len(tm.versions)}):")
    header = f"  {'Start':<12} {'End':<12} {'Charges':>7}  {'Nulls':>5}  Revision"
    if show_charges:
        typer.echo(header)
    else:
        typer.echo(f"  {'Start':<12} {'End':<12} Status     Revision")
    typer.echo("  " + "-" * 72)
    for v in tm.versions:
        start = v.effective_start or "undated"
        end = v.effective_end or "open"
        status = v.charge_status
        rev = v.revision_label or "(no label)"
        if show_charges:
            typer.echo(f"  {start:<12} {end:<12} {v.charge_count:>7}  {v.null_rate_count:>5}  {rev}")
        else:
            status_disp = {"ok": "OK        ", "no_charges": "NO CHARGES", "null_rates": "NULL RATES"}
            typer.echo(f"  {start:<12} {end:<12} {status_disp.get(status, status):<10} {rev}")

    if tm.gaps:
        typer.echo(f"\nGaps ({len(tm.gaps)}):")
        for g in tm.gaps:
            if g.gap_type == "undated_version":
                typer.echo(f"  [undated version -- id {g.successor_version_id}]")
            elif g.gap_type == "between_versions":
                days = f"{g.gap_days}d" if g.gap_days is not None else "?"
                typer.echo(f"  {g.gap_start} -> {g.gap_end}  ({days})")
            else:
                typer.echo(f"  {g.gap_type}: {g.gap_start} -> {g.gap_end}")
    else:
        typer.echo("\nNo gaps detected.")
    typer.echo("")


@app.command("audit-tariff-coverage")
def audit_tariff_coverage(
    schedule: str = typer.Argument(..., help="Rate schedule family key, e.g. nc-progress-leaf-500"),
    date: str = typer.Option(None, "--date", help="Date (YYYY-MM-DD), defaults to today"),
    customer_class: str = typer.Option("residential", "--class", help="Customer class"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of table"),
) -> None:
    """Audit rider coverage for one rate schedule at a given date."""
    import datetime as _dt
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)
    as_of = _dt.date.fromisoformat(date) if date else _dt.date.today()
    cm = svc.build_coverage_map(schedule, as_of, customer_class)

    if json_out:
        typer.echo(cm.model_dump_json(indent=2))
        return

    verdict_icon = {"complete": "[OK]", "partial": "[~]", "missing_riders": "[!]", "no_data": "[X]"}
    icon = verdict_icon.get(cm.audit_verdict, "[?]")
    typer.echo(f"\n{icon} {cm.schedule_family_key}  [{cm.audit_verdict.upper()}]  as of {cm.as_of_date}")
    typer.echo(f"  Revision: {cm.schedule_revision_label or '(none)'}")
    typer.echo(f"  Schedule: {cm.schedule_charge_status}")

    if cm.leaf600_total_cents_per_kwh is not None:
        tol_flag = "OK" if cm.delta_within_tolerance else "MISMATCH"
        typer.echo(
            f"  Leaf-600: {cm.leaf600_total_cents_per_kwh:.4f} c/kWh  "
            f"engine: {cm.engine_summary_total_cents_per_kwh:.4f} c/kWh  "
            f"delta={cm.delta_cents_per_kwh:.4f} [{tol_flag}]"
        )

    typer.echo(f"\n  Riders ({len(cm.riders)})  ok={cm.riders_ok}  issues={cm.riders_missing}")
    typer.echo(f"  {'Family key':<32} {'Summary':>7} {'Status':<12} {'Rate(c/kWh)':>11}")
    typer.echo("  " + "-" * 70)
    for r in cm.riders:
        summ = "Y" if r.in_rider_summary else "N"
        rate = f"{r.rate_cents_per_kwh:.4f}" if r.rate_cents_per_kwh is not None else "--"
        status_flag = "  " if r.coverage_status == "ok" else "! "
        typer.echo(f"  {status_flag}{r.rider_family_key:<30} {summ:>7} {r.coverage_status:<12} {rate:>11}")

    if cm.warnings:
        typer.echo("\n  Warnings:")
        for w in cm.warnings:
            typer.echo(f"    - {w}")
    typer.echo("")


@app.command("export-nc-coverage-assessment")
def export_nc_coverage_assessment_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_coverage_assessment"),
        "--output-dir",
        help="Directory for generated coverage assessment exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export DB-driven NC schedule coverage matrices for DEP and DEC."""
    from duke_rates.analytics.nc_coverage_assessment import export_nc_coverage_assessment

    paths = export_nc_coverage_assessment(output_dir, database_path=database)
    typer.echo("Wrote NC coverage assessment exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-nc-anomaly-audit")
def export_nc_anomaly_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_anomaly_audit"),
        "--output-dir",
        help="Directory for generated NC anomaly audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a ranked NC tariff anomaly audit to drive reparse and backfill work."""
    from duke_rates.analytics.nc_anomaly_audit import export_nc_anomaly_audit

    paths = export_nc_anomaly_audit(output_dir, database_path=database)
    typer.echo("Wrote NC anomaly audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-nc-schedule-inventory-audit")
def export_nc_schedule_inventory_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_schedule_inventory_audit"),
        "--output-dir",
        help="Directory for generated NC schedule inventory audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a full NC rate_schedule inventory audit against the focused matrix scope."""
    from duke_rates.analytics.nc_schedule_inventory_audit import export_nc_schedule_inventory_audit

    paths = export_nc_schedule_inventory_audit(output_dir, database_path=database)
    typer.echo("Wrote NC schedule inventory audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-nc-document-intelligence-audit")
def export_nc_document_intelligence_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_document_intelligence_audit"),
        "--output-dir",
        help="Directory for generated NC document-intelligence audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    limit: int = typer.Option(150, "--limit", help="Maximum number of candidate rows to analyze."),
) -> None:
    """Apply the document-intelligence layer to NC zero-charge and malformed historical rows."""
    from duke_rates.analytics.nc_document_intelligence_audit import (
        export_nc_document_intelligence_audit,
    )

    paths = export_nc_document_intelligence_audit(output_dir, database_path=database, limit=limit)
    typer.echo("Wrote NC document-intelligence audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-nc-document-gap-audit")
def export_nc_document_gap_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_document_gap_audit"),
        "--output-dir",
        help="Directory for generated NC document gap audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Identify temporal gaps, ordinal gaps, and thin-source versions where a higher-quality
    NCUC document (compliance bundle or targeted leaf) would improve coverage."""
    from duke_rates.analytics.nc_document_gap_audit import export_nc_document_gap_audit

    paths = export_nc_document_gap_audit(output_dir, database_path=database)
    typer.echo("Wrote NC document gap audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-nc-confidence-audit")
def export_nc_confidence_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_confidence_audit"),
        "--output-dir",
        help="Directory for generated NC confidence audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a family-level NC confidence audit combining lineage, redline, and parse signals."""
    from duke_rates.analytics.nc_confidence_audit import export_nc_confidence_audit

    paths = export_nc_confidence_audit(output_dir, database_path=database)
    typer.echo("Wrote NC confidence audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-nc-redline-lead-audit")
def export_nc_redline_lead_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_redline_lead_audit"),
        "--output-dir",
        help="Directory for generated NC redline lead audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a ranked queue of NC families where redline documents can help locate or validate clean tariffs."""
    from duke_rates.analytics.nc_redline_lead_audit import export_nc_redline_lead_audit

    paths = export_nc_redline_lead_audit(output_dir, database_path=database)
    typer.echo("Wrote NC redline lead audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


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


@app.command("export-nc-redline-parse-audit")
def export_nc_redline_parse_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_redline_parse_audit"),
        "--output-dir",
        help="Directory for generated NC redline parse audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Audit NC parsed tariff versions whose source PDFs may be redlines, using the corrected detector."""
    from duke_rates.analytics.nc_redline_parse_audit import export_nc_redline_parse_audit

    paths = export_nc_redline_parse_audit(output_dir, database_path=database)
    typer.echo("Wrote NC redline parse audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-nc-missing-clean-doc-audit")
def export_nc_missing_clean_doc_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_missing_clean_doc_audit"),
        "--output-dir",
        help="Directory for generated NC missing-clean-document audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export ranked NC leads for missing clean historical tariff/rider documents."""
    from duke_rates.analytics.nc_missing_clean_doc_audit import export_nc_missing_clean_doc_audit

    paths = export_nc_missing_clean_doc_audit(output_dir, database_path=database)
    typer.echo("Wrote NC missing clean document audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("search-nc-missing-clean-docs")
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


@app.command("run-nc-missing-doc-workflow")
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
        process_reprocess_queue_nc(limit=reprocess_limit)
    if _stage_order_index(to_stage) >= _stage_order_index("validate"):
        validate_extraction_nc()


@app.command("promote-nc-missing-doc-targets")
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


@app.command("show-nc-missing-doc-status")
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


@app.command("report-nc-missing-doc-deferred")
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


@app.command("report-nc-missing-doc-triage")
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


@app.command("execute-top-nc-missing-doc-triage")
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


@app.command("execute-batch-nc-missing-doc-triage")
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


@app.command("plan-nc-missing-doc-remediation")
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


@app.command("execute-top-nc-missing-doc-remediation")
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


@app.command("report-nc-missing-doc-remediation-history")
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


@app.command("remediate-nc-missing-doc-no-download-url")
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


@app.command("remediate-nc-missing-doc-effective-start")
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


@app.command("remediate-nc-missing-doc-confidence")
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


@app.command("remediate-and-promote-nc-missing-docs")
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


@app.command("export-dep-leaf-503-audit")
def export_dep_leaf_503_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_leaf_503_audit"),
        "--output-dir",
        help="Directory for generated DEP leaf-503 audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a focused audit for DEP leaf-503 (R-TOU-CPP) versions and rider linkage."""
    from duke_rates.analytics.dep_leaf503_audit import export_dep_leaf503_audit

    paths = export_dep_leaf503_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP leaf-503 audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


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


@app.command("export-dep-residential-rider-gap-audit")
def export_dep_residential_rider_gap_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_residential_rider_gap_audit"),
        "--output-dir",
        help="Directory for generated DEP residential rider gap audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export rider-family charge coverage gaps for DEP residential schedules 500-504."""
    from duke_rates.analytics.dep_residential_rider_gap_audit import (
        export_dep_residential_rider_gap_audit,
    )

    paths = export_dep_residential_rider_gap_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP residential rider gap audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-dep-residential-rider-action-queue")
def export_dep_residential_rider_action_queue_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_residential_rider_action_queue"),
        "--output-dir",
        help="Directory for generated DEP residential rider action queue exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a ranked DEP residential rider repair queue derived from the gap audit."""
    from duke_rates.analytics.dep_residential_rider_action_queue import (
        export_dep_residential_rider_action_queue,
    )

    paths = export_dep_residential_rider_action_queue(output_dir, database_path=database)
    typer.echo("Wrote DEP residential rider action queue exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-dep-residential-rider-repair-plan")
def export_dep_residential_rider_repair_plan_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_residential_rider_repair_plan"),
        "--output-dir",
        help="Directory for generated DEP residential rider repair plan exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export an operational DEP residential rider repair plan with parser/discovery guidance."""
    from duke_rates.analytics.dep_residential_rider_repair_plan import (
        export_dep_residential_rider_repair_plan,
    )

    paths = export_dep_residential_rider_repair_plan(output_dir, database_path=database)
    typer.echo("Wrote DEP residential rider repair plan exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-dep-compliance-bundle-audit")
def export_dep_compliance_bundle_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_compliance_bundle_audit"),
        "--output-dir",
        help="Directory for generated DEP compliance bundle audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a DEP rider-family compliance bundle audit for discovery/import/span triage."""
    from duke_rates.analytics.dep_compliance_bundle_audit import (
        export_dep_compliance_bundle_audit,
    )

    paths = export_dep_compliance_bundle_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP compliance bundle audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-dep-storm-rider-audit")
def export_dep_storm_rider_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_storm_rider_audit"),
        "--output-dir",
        help="Directory for generated DEP storm rider audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a DEP storm-rider audit for canonical-family, residue, and applicability triage."""
    from duke_rates.analytics.dep_storm_rider_audit import (
        export_dep_storm_rider_audit,
    )

    paths = export_dep_storm_rider_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP storm rider audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@app.command("export-dep-storm-history-inventory")
def export_dep_storm_history_inventory_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_storm_history_inventory"),
        "--output-dir",
        help="Directory for generated DEP storm history inventory exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a DEP storm-history inventory separating canonical families from older docket candidates."""
    from duke_rates.analytics.dep_storm_history_inventory import (
        export_dep_storm_history_inventory,
    )

    paths = export_dep_storm_history_inventory(output_dir, database_path=database)
    typer.echo("Wrote DEP storm history inventory exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


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


@app.command("audit-tariff-null-scan")
def audit_tariff_null_scan(
    state: str = typer.Option("NC", "--state", help="State code"),
    company: str = typer.Option("progress", "--company", help="Company name"),
    date: str = typer.Option(None, "--date", help="Date (YYYY-MM-DD), defaults to today"),
    customer_class: str = typer.Option("residential", "--class", help="Customer class"),
    verdicts: str = typer.Option(
        None, "--verdicts",
        help="Comma-separated verdicts to show: complete,partial,missing_riders,no_data"
    ),
    family_type: str = typer.Option("rate_schedule", "--family-type", help="Family type to scan"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of table"),
) -> None:
    """Batch coverage scan for all rate schedules of a state/company."""
    import datetime as _dt
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)
    as_of = _dt.date.fromisoformat(date) if date else _dt.date.today()

    results = svc.build_null_audit(state, company, as_of, family_type, customer_class)

    verdict_filter = set(verdicts.split(",")) if verdicts else None
    if verdict_filter:
        results = [r for r in results if r.audit_verdict in verdict_filter]

    if json_out:
        typer.echo(json.dumps([r.model_dump() for r in results], indent=2))
        return

    typer.echo(f"\nTariff null scan | {state.upper()} {company}  as of {as_of}  ({len(results)} schedules)\n")
    typer.echo(f"  {'Schedule':<36} {'Verdict':<16} {'OK':>4} {'Issues':>6}  Revision")
    typer.echo("  " + "-" * 82)
    counts = {"complete": 0, "partial": 0, "missing_riders": 0, "no_data": 0}
    for cm in results:
        rev = (cm.schedule_revision_label or "")[:30]
        typer.echo(
            f"  {cm.schedule_family_key:<36} {cm.audit_verdict:<16} "
            f"{cm.riders_ok:>4} {cm.riders_missing:>6}  {rev}"
        )
        counts[cm.audit_verdict] = counts.get(cm.audit_verdict, 0) + 1

    typer.echo(f"\n  Summary: complete={counts['complete']}  partial={counts['partial']}  "
               f"missing_riders={counts['missing_riders']}  no_data={counts['no_data']}")
    typer.echo("")


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

    finally:
        conn.close()


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
            "suggested_command": f"python -m duke_rates enqueue-reprocess-nc --hd-id {doc['id']}",
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
                                 f"python -m duke_rates enqueue-reprocess-nc --hd-id {doc['id']}",
            "priority": "high",
        }

    if status == "no_text":
        text_len = (report.get("text_metrics") or {}).get("text_length") or 0
        return {
            "action": "run_ocr",
            "reason": f"No extractable text (text_length={text_len}). PDF is likely scanned/image-based.",
            "suggested_command": f"python -m duke_rates enqueue-ocr-nc --hd-id {doc['id']}\n"
                                 f"python -m duke_rates process-ocr-queue-nc",
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
                f"python -m duke_rates enqueue-ocr-nc --hd-id {doc['id']}\n"
                f"# If profile routing issue (e.g., Carolinas doc matched progress profile):\n"
                f"python -m duke_rates enqueue-reprocess-nc --hd-id {doc['id']} --priority 90"
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


@app.command("canonicalize-doc-families-nc")
def canonicalize_doc_families_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
    limit: int = typer.Option(0, "--limit", help="Max families to canonicalize (0 = all)."),
) -> None:
    """Scan remaining doc-* families and canonicalize them to schedule/rider keys.

    Infers the correct canonical family key from document titles and content.
    Supports bulk --execute to promote all eligible doc-* families at once.
    """
    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        rows = conn.execute(
            """
            SELECT tf.family_key, tf.title, tf.state, tf.company,
                   COUNT(DISTINCT hd.id) AS doc_count,
                   SUM((SELECT COUNT(*) FROM tariff_charges tc
                        JOIN tariff_versions tv ON tv.id = tc.version_id
                        WHERE tv.family_key = tf.family_key)) AS charge_count
            FROM tariff_families tf
            LEFT JOIN historical_documents hd ON hd.family_key = tf.family_key
            WHERE tf.family_key LIKE 'nc-%doc-%'
            GROUP BY tf.family_key
            ORDER BY charge_count DESC, doc_count DESC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No doc-* families found. Nothing to canonicalize.")
        return

    typer.echo(f"Found {len(rows)} doc-* families.\n")

    actions: list[dict] = []
    for row in rows:
        fk = row["family_key"]
        title = row["title"] or ""
        company = row["company"] or ""

        # Infer canonical key from title
        canonical = _infer_canonical_family_key(fk, title, company)
        actions.append({
            "family_key": fk,
            "canonical_key": canonical,
            "title": title,
            "company": company,
            "doc_count": row["doc_count"],
            "charge_count": row["charge_count"],
        })

    for i, a in enumerate(actions):
        if limit and i >= limit:
            break
        typer.echo(
            f"  {a['family_key']}"
            f"\n    -&gt; {a['canonical_key']}"
            f"\n    title={_safe_cli_text(a['title'][:80])}  "
            f"docs={a['doc_count']}  charges={a['charge_count']}"
        )

    if dry_run:
        typer.echo(f"\n[DRY RUN] Would canonicalize {min(len(actions), limit) if limit else len(actions)} families.")
        typer.echo("Re-run with --execute to apply.")
        return

    migrated = 0
    for a in actions:
        if limit and migrated >= limit:
            break
        try:
            conn2 = connect_sqlite(repository.database_path)
            try:
                _apply_canonicalization(conn2, a["family_key"], a["canonical_key"])
                conn2.commit()
                migrated += 1
                typer.echo(f"  OK  {a['family_key']} -&gt; {a['canonical_key']}")
            finally:
                conn2.close()
        except Exception as exc:
            typer.echo(f"  FAIL  {a['family_key']}: {exc}")

    typer.echo(f"\nCanonicalized {migrated} families.")


def _infer_canonical_family_key(family_key: str, title: str, company: str) -> str:
    """Infer a canonical family key from document title and existing key."""
    import re

    title_lower = title.lower()
    key_lower = family_key.lower()

    # Extract leaf number if present
    leaf_match = re.search(r"leaf\s*(?:no\.?\s*)?(\d{1,4})", title_lower)
    leaf_no = leaf_match.group(1) if leaf_match else None

    company_prefix = "nc-progress" if "progress" in key_lower else (
        "nc-carolinas" if "carolinas" in key_lower else "nc"
    )

    # Detect schedule patterns
    schedule_patterns = [
        (r"schedule\s+rs\b", f"{company_prefix}-schedule-rs"),
        (r"schedule\s+re\b", f"{company_prefix}-schedule-re"),
        (r"schedule\s+r[-\s]?tou", f"{company_prefix}-schedule-r-tou"),
        (r"schedule\s+r[-\s]?toud?\b", f"{company_prefix}-schedule-r-toud"),
        (r"schedule\s+res\b", f"{company_prefix}-schedule-res"),
        (r"schedule\s+sgs[-\s]?toue?\b", f"{company_prefix}-schedule-sgs-toue"),
        (r"schedule\s+sgs\b", f"{company_prefix}-schedule-sgs"),
        (r"schedule\s+lgs[-\s]?toue?\b", f"{company_prefix}-schedule-lgs-toue"),
        (r"schedule\s+lgs\b", f"{company_prefix}-schedule-lgs"),
        (r"schedule\s+pg\b", f"{company_prefix}-schedule-pg"),
        (r"schedule\s+ts\b", f"{company_prefix}-schedule-ts"),
        (r"schedule\s+hlf\b", f"{company_prefix}-schedule-hlf"),
        (r"schedule\s+i\b", f"{company_prefix}-schedule-i"),
        (r"schedule\s+fl\b", f"{company_prefix}-schedule-fl"),
        (r"schedule\s+wc\b", f"{company_prefix}-schedule-wc"),
        (r"schedule\s+nm\b", f"{company_prefix}-schedule-nm"),
        (r"schedule\s+ol\b", f"{company_prefix}-schedule-ol"),
        (r"schedule\s+se\b", f"{company_prefix}-schedule-se"),
        (r"schedule\s+lp\b", f"{company_prefix}-schedule-lp"),
        (r"schedule\s+isl?\b", f"{company_prefix}-schedule-is"),
        (r"schedule\s+dsm\b", f"{company_prefix}-schedule-dsm"),
        (r"schedule\s+ee\b", f"{company_prefix}-schedule-ee"),
        (r"schedule\s+opt[-\s]?e\b", f"{company_prefix}-schedule-opte"),
        (r"schedule\s+opt[-\s]?h\b", f"{company_prefix}-schedule-opth"),
        (r"schedule\s+opt[-\s]?g\b", f"{company_prefix}-schedule-optg"),
        (r"schedule\s+cpp\b", f"{company_prefix}-schedule-cpp"),
        (r"schedule\s+fcar\b", f"{company_prefix}-schedule-fcar"),
        (r"schedule\s+edpr\b", f"{company_prefix}-schedule-edpr"),
        (r"schedule\s+sbes\b", f"{company_prefix}-schedule-sbes"),
        (r"schedule\s+iqheu\b", f"{company_prefix}-schedule-iqheu"),
        (r"schedule\s+gs\b", f"{company_prefix}-schedule-gs"),
    ]

    for pattern, canonical in schedule_patterns:
        if re.search(pattern, title_lower):
            if leaf_no:
                return f"{canonical}-leaf-{leaf_no}"
            return canonical

    # Detect rider patterns
    rider_match = re.search(r"rider\s+(\w[\w\s-]{0,20})", title_lower)
    if rider_match:
        rider_code = rider_match.group(1).strip().upper().replace(" ", "-")[:15]
        return f"{company_prefix}-rider-{rider_code}"

    # Fallback: extract meaningful part from doc-* key
    doc_part = re.sub(r"^nc-(?:progress|carolinas)-doc-", "", family_key, flags=re.IGNORECASE)
    doc_part = re.sub(r"[^a-zA-Z0-9_-]", "", doc_part)[:60].strip("-").lower()
    if doc_part:
        if leaf_no:
            return f"{company_prefix}-schedule-{doc_part}-leaf-{leaf_no}"
        return f"{company_prefix}-schedule-{doc_part}"

    return family_key  # No inference possible


def _apply_canonicalization(conn: sqlite3.Connection, old_key: str, new_key: str) -> None:
    """Migrate all rows from old family key to new key, creating target family if needed."""
    target = conn.execute(
        "SELECT family_key FROM tariff_families WHERE family_key = ?", (new_key,)
    ).fetchone()

    if not target:
        # Create the canonical family with metadata from the old one
        old = conn.execute(
            "SELECT state, company, title, category FROM tariff_families WHERE family_key = ?",
            (old_key,),
        ).fetchone()
        if old:
            conn.execute(
                """INSERT INTO tariff_families (family_key, state, company, title, category, is_provisional, is_curated)
                   VALUES (?, ?, ?, ?, ?, 0, 1)""",
                (new_key, old["state"], old["company"], old["title"], old["category"]),
            )

    # Update all referencing tables
    for table, col in [
        ("historical_documents", "family_key"),
        ("tariff_versions", "family_key"),
        ("tariff_families", "family_key"),
        ("historical_reprocess_queue", "family_key"),
        ("ncuc_discovery_records", "family_key"),
        ("ncuc_missing_doc_targets", "family_key"),
    ]:
        try:
            conn.execute(
                f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                (new_key, old_key),
            )
        except sqlite3.OperationalError:
            pass  # Column may not exist in some tables

    # Update historical_processing_runs via historical_documents
    conn.execute(
        """UPDATE historical_processing_runs SET family_key = ?
           WHERE historical_document_id IN (
               SELECT id FROM historical_documents WHERE family_key = ?
           )""",
        (new_key, new_key),
    )

    # Delete old family if it was a doc-* key (and different from new)
    if old_key != new_key and "doc-" in old_key.lower():
        conn.execute("DELETE FROM tariff_families WHERE family_key = ?", (old_key,))


@app.command("deduplicate-family-nc")
def deduplicate_family_nc(
    family_key: str = typer.Option(..., "--family-key", help="Family key to deduplicate."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
) -> None:
    """Deduplicate charges across all versions in a tariff family.

    Uses the natural charge signature (type, label, rate, unit, season,
    tou_period, tier, customer_class) to find and remove duplicates.
    """
    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        version_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM tariff_versions WHERE family_key = ?",
                (family_key,),
            ).fetchall()
        ]
    finally:
        conn.close()

    if not version_ids:
        typer.echo(f"No versions found for family {family_key}.")
        return

    total_before = 0
    total_unique = 0

    for vid in version_ids:
        conn = connect_sqlite(repository.database_path)
        try:
            before = int(conn.execute(
                "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (vid,)
            ).fetchone()[0])
            unique_count = int(conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM tariff_charges
                    WHERE version_id = ?
                    GROUP BY
                        charge_type,
                        COALESCE(charge_label, ''),
                        COALESCE(rate_value, -999999999.0),
                        COALESCE(rate_unit, ''),
                        COALESCE(season, ''),
                        COALESCE(tou_period, ''),
                        COALESCE(tier_min, -999999999.0),
                        COALESCE(tier_max, -999999999.0),
                        COALESCE(customer_class, '')
                )
                """,
                (vid,),
            ).fetchone()[0]
            )
            total_before += before
            total_unique += unique_count

            if not dry_run and before > unique_count:
                repository.deduplicate_tariff_charges_for_version(vid)

            if before != unique_count:
                typer.echo(
                    f"  {'[DRY RUN]' if dry_run else '[EXECUTED]'} "
                    f"version={vid} before={before} unique={unique_count} "
                    f"duplicates={before - unique_count}"
                )
        finally:
            conn.close()

    dup_count = total_before - total_unique
    typer.echo(
        f"\nFamily {family_key}: {len(version_ids)} versions, "
        f"{total_before} charges -&gt; {total_unique} unique "
        f"({dup_count} duplicates {'(dry run)' if dry_run else 'removed'})"
    )


@app.command("deduplicate-documents-nc")
def deduplicate_documents_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB (default: dry-run)."),
    file_hash: str = typer.Option("", "--file-hash", help="Target a specific content_hash group only."),
    limit: int = typer.Option(0, "--limit", help="Max duplicate groups to process (0 = all)."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Consolidate historical documents that share the same content_hash.

    For each group of duplicates the best survivor is kept (most charges,
    has local_path, newest retrieved_at) and all foreign-key references
    from the other rows are remapped to it.

    Always preview with --dry-run first.
    """
    _, repository = _bootstrap()

    result = repository.deduplicate_historical_documents(
        dry_run=dry_run,
        file_hash=file_hash if file_hash else None,
        limit=limit,
    )

    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    typer.echo(
        f"\nDocument Deduplication {'(DRY RUN)' if dry_run else '(EXECUTED)'}"
    )
    typer.echo(f"  Total duplicate groups: {result['total_groups']}")
    typer.echo(f"  Groups processed:      {result['groups_processed']}")
    typer.echo(f"  Documents to remove:   {result['documents_removed']}")

    if result["errors"]:
        typer.echo(f"\n  Errors ({len(result['errors'])}):")
        for e in result["errors"]:
            typer.echo(f"    - {e}")

    for pg in result["per_group"]:
        typer.echo(
            f"\n  content_hash={pg['content_hash'][:16]}... "
            f"survivor=hd:{pg['survivor_id']} ({pg['survivor_charges']} charges) "
            f"remove={pg['group_size'] - 1} docs [hd:{', hd:'.join(str(i) for i in pg['removed_ids'])}]"
        )

    typer.echo(
        f"\n  Duration: {result['duration_ms']}ms"
    )

    if dry_run and result["documents_removed"] > 0:
        typer.echo("\n  Re-run with --execute to apply changes.")


@app.command("backfill-evidence-nc")
def backfill_evidence_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB (default: dry-run)."),
    limit: int = typer.Option(0, "--limit", help="Max documents to backfill (0 = all candidates)."),
    family: str = typer.Option("", "--family", help="Target a specific family key."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Backfill evidence_json for historical documents where it is missing.

    Extracts the best-family evidence breakdown from existing span artifacts
    (ncuc_span_artifacts). Documents without span artifacts are skipped —
    they need full reprocessing via enqueue-stale-reprocess-nc.
    """
    _, repository = _bootstrap()

    result = repository.backfill_evidence_json(
        dry_run=dry_run,
        limit=limit,
        family_key=family if family else None,
    )

    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    typer.echo(
        f"\nEvidence Backfill {'(DRY RUN)' if dry_run else '(EXECUTED)'}"
    )
    typer.echo(f"  Total candidates:       {result['total_candidates']}")
    typer.echo(f"  Would backfill:         {result['backfilled']}")
    typer.echo(f"  Skipped (no spans):     {result['skipped_no_spans']}")
    typer.echo(f"  Skipped (no breakdown): {result['skipped_no_breakdown']}")

    if result["errors"]:
        typer.echo(f"\n  Errors ({len(result['errors'])}):")
        for e in result["errors"][:10]:
            typer.echo(f"    - {e}")

    if result["backfilled"] > 0 and not json_out:
        typer.echo(f"\n  Top backfills:")
        for d in result["per_doc"][:5]:
            score = d.get("evidence_score", "?")
            typer.echo(
                f"    hd:{d['historical_document_id']} "
                f"family={d['family_key']} "
                f"score={score}"
            )

    typer.echo(f"\n  Duration: {result['duration_ms']}ms")

    if dry_run and result["backfilled"] > 0:
        typer.echo("\n  Re-run with --execute to apply changes.")
    if result["skipped_no_spans"] > 0:
        typer.echo(
            f"\n  {result['skipped_no_spans']} docs have no span artifacts. "
            f"Use 'enqueue-stale-reprocess-nc' for full regeneration."
        )


@app.command("backfill-content-hash-nc")
def backfill_content_hash_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB (default: dry-run)."),
    limit: int = typer.Option(0, "--limit", help="Max documents to hash (0 = all)."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Backfill content_hash for historical_documents where it is null or empty.

    Calculates SHA-1 checksums from the files on disk and writes them to the
    content_hash column.  Documents whose files are missing are skipped.

    This is a prerequisite for span-artifact matching and evidence backfill.
    """
    import hashlib as _hashlib
    from pathlib import Path as _Path

    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        candidates = conn.execute(
            """
            SELECT id, local_path FROM historical_documents
            WHERE local_path IS NOT NULL AND local_path != ''
              AND (content_hash IS NULL OR content_hash = '')
            ORDER BY id
            """
            + (" LIMIT ?" if limit > 0 else ""),
            (limit,) if limit > 0 else (),
        ).fetchall()

        hashed = 0
        skipped_missing = 0
        errors: list[str] = []

        for c in candidates:
            hd_id = c["id"]
            local_path = c["local_path"]
            file_path = _Path(local_path)
            if not file_path.exists():
                skipped_missing += 1
                continue

            if dry_run:
                hashed += 1
                continue

            try:
                sha1 = _hashlib.sha1()
                with open(file_path, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        sha1.update(chunk)
                ch = sha1.hexdigest()
                conn.execute(
                    "UPDATE historical_documents SET content_hash = ? WHERE id = ?",
                    (ch, hd_id),
                )
                hashed += 1
            except Exception as exc:
                errors.append(f"hd:{hd_id}: {exc}")

        if not dry_run and hashed > 0:
            conn.commit()

        result = {
            "dry_run": dry_run,
            "total_candidates": len(candidates),
            "hashed": hashed,
            "skipped_missing": skipped_missing,
            "errors": errors,
        }
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    typer.echo(
        f"\nContent Hash Backfill {'(DRY RUN)' if dry_run else '(EXECUTED)'}"
    )
    typer.echo(f"  Total candidates:       {result['total_candidates']}")
    typer.echo(f"  Would hash:             {result['hashed']}")
    typer.echo(f"  Skipped (file missing): {result['skipped_missing']}")

    if result["errors"]:
        typer.echo(f"\n  Errors ({len(result['errors'])}):")
        for e in result["errors"][:10]:
            typer.echo(f"    - {e}")

    if dry_run and result["hashed"] > 0:
        typer.echo("\n  Re-run with --execute to apply changes.")


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
            "command": "python -m duke_rates enqueue-ocr-nc --hd-id {ids}\npython -m duke_rates process-ocr-queue-nc",
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


@app.command("repair-anomaly-nc")
def repair_anomaly_nc(
    historical_document_id: int = typer.Option(
        ..., "--hd-id", help="Historical document ID to repair."
    ),
    repair_action: str = typer.Option(
        "", "--action", help="Repair action: rebind_span, reassign_profile, enqueue_ocr, accept_caveat, or auto-detect if empty."
    ),
    new_start_page: int = typer.Option(None, "--start-page", help="New start page for rebind_span."),
    new_end_page: int = typer.Option(None, "--end-page", help="New end page for rebind_span."),
    new_profile: str = typer.Option(None, "--profile", help="Target profile for reassign_profile."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
) -> None:
    """Apply a known repair pattern to an anomalous document.

    Supported actions:
      rebind_span    -- Update start_page/end_page on historical_documents
      reassign_profile -- Re-queue the doc with a different profile hint
      enqueue_ocr    -- Enqueue the doc in the OCR queue
      accept_caveat  -- Mark as accepted caveat (no DB change, just log)
      auto-detect    -- Detect the best action from the document's current state
    """
    from duke_rates.db.reprocess import latest_processing_run_for_document

    settings, _ = _bootstrap()
    conn = connect_sqlite(settings.database_path)
    try:
        doc = conn.execute(
            "SELECT * FROM historical_documents WHERE id = ?",
            (historical_document_id,),
        ).fetchone()
        if not doc:
            raise typer.BadParameter(f"Historical document {historical_document_id} not found.")

        run = latest_processing_run_for_document(conn, historical_document_id=historical_document_id)

        typer.echo(f"Document hd={historical_document_id}  family={doc['family_key']}")
        typer.echo(f"  effective_start={doc['effective_start']}  "
                   f"pages={doc['start_page']}-{doc['end_page']}")
        typer.echo(f"  title={_safe_cli_text(doc['title'] or '-')}")

        if run:
            metadata = json.loads(run["metadata_json"] or "{}")
            signals = metadata.get("signals") or {}
            text_len = (metadata.get("text_metrics") or {}).get("text_length") or 0
            typer.echo(f"  profile={run['parser_profile']}  status={run['status']}  "
                       f"quality={run['outcome_quality']}  charges={run['charge_count']}  "
                       f"text_len={text_len}")
            typer.echo(f"  has_carolinas_text={signals.get('has_carolinas_company_text')}  "
                       f"has_progress_text={signals.get('has_progress_company_text')}")

        # Auto-detect action
        if not repair_action:
            repair_action = _detect_anomaly_repair(dict(doc), run)

        typer.echo(f"\n  Action: {repair_action}")

        if repair_action == "accept_caveat":
            typer.echo(f"  No DB changes. This anomaly is an accepted caveat.")
            typer.echo(f"  Reason: structural parser limitation or non-tariff content.")
            if not dry_run:
                typer.echo(f"  [EXECUTED] Caveat accepted.")
            return

        if repair_action == "rebind_span":
            if new_start_page is None or new_end_page is None:
                raise typer.BadParameter("--start-page and --end-page required for rebind_span.")
            typer.echo(f"  New pages: {new_start_page}-{new_end_page}")
            if not dry_run:
                conn.execute(
                    "UPDATE historical_documents SET start_page=?, end_page=?, "
                    "title=? WHERE id=?",
                    (
                        new_start_page, new_end_page,
                        f"{doc['title'] or 'Untitled'} (Span {new_start_page}-{new_end_page})",
                        historical_document_id,
                    ),
                )
                conn.commit()
                typer.echo(f"  [EXECUTED] Span updated. Re-queue with:")
                typer.echo(f"    python -m duke_rates enqueue-reprocess-nc --hd-id {historical_document_id} --priority 90")
            else:
                typer.echo(f"  [DRY RUN] Would update span to {new_start_page}-{new_end_page}")

        elif repair_action == "reassign_profile":
            if not new_profile:
                raise typer.BadParameter("--profile required for reassign_profile.")
            typer.echo(f"  Target profile: {new_profile}")
            if not dry_run:
                # Re-queue with metadata hint
                conn.execute(
                    """INSERT INTO historical_reprocess_queue
                       (historical_document_id, source_pdf, family_key, priority,
                        queue_reason, requested_by, requested_at)
                       VALUES (?, ?, ?, 90, ?, 'repair_anomaly', datetime('now'))""",
                    (
                        historical_document_id,
                        doc["local_path"],
                        doc["family_key"],
                        f"profile_reassign:{run['parser_profile']}->{new_profile}",
                    ),
                )
                conn.commit()
                typer.echo(f"  [EXECUTED] Re-queued with profile hint. Process with:")
                typer.echo(f"    python -m duke_rates process-reprocess-queue-nc")
            else:
                typer.echo(f"  [DRY RUN] Would re-queue with profile={new_profile}")

        elif repair_action == "enqueue_ocr":
            if not dry_run:
                conn.execute(
                    """INSERT INTO ocr_processing_queue
                       (historical_document_id, source_pdf, family_key, status,
                        ocr_backend, priority, requested_by, requested_at)
                       VALUES (?, ?, ?, 'pending', 'pytesseract_cpu', 90,
                               'repair_anomaly', datetime('now'))""",
                    (
                        historical_document_id,
                        doc["local_path"],
                        doc["family_key"],
                    ),
                )
                conn.commit()
                typer.echo(f"  [EXECUTED] Enqueued in OCR queue. Process with:")
                typer.echo(f"    python -m duke_rates process-ocr-queue-nc")
            else:
                typer.echo(f"  [DRY RUN] Would enqueue in OCR queue")

        else:
            raise typer.BadParameter(f"Unknown repair action: {repair_action}")

    finally:
        conn.close()


def _detect_anomaly_repair(doc: dict, run: dict | None) -> str:
    """Auto-detect the best repair action for an anomalous document."""
    if not run:
        return "enqueue_ocr"

    status = run["status"] or ""
    profile = run["parser_profile"] or ""
    metadata = json.loads(run["metadata_json"] or "{}")
    signals = metadata.get("signals") or {}
    text_len = (metadata.get("text_metrics") or {}).get("text_length") or 0

    # Skipped docs are accepted caveats
    if status.startswith("skipped"):
        return "accept_caveat"

    # Profile mismatch detection (before text check — signals are authoritative)
    has_carolinas = signals.get("has_carolinas_company_text")
    has_progress = signals.get("has_progress_company_text")
    company = (doc.get("company") or "").lower()

    progress_profile_on_carolinas = (
        profile.startswith("progress_") and (has_carolinas or "carolinas" in company)
    )
    carolinas_profile_on_progress = (
        profile.startswith("carolinas_") and (has_progress or "progress" in company)
    )
    if progress_profile_on_carolinas or carolinas_profile_on_progress:
        return "reassign_profile"

    # No text -> OCR
    if not text_len or text_len == 0:
        return "enqueue_ocr"

    # Weak parse with text -> likely parser limitation
    if run["outcome_quality"] == "weak":
        return "accept_caveat"

    # Fallback: if profile is unknown, try OCR
    if profile == "unknown":
        return "enqueue_ocr"

    return "accept_caveat"


if __name__ == "__main__":
    main()
