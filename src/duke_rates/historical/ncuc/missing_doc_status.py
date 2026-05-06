from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from duke_rates.db.parse_review import list_parse_review_queue
from duke_rates.db.reprocess import (
    latest_parse_attempt_for_historical_document,
    latest_processing_run_for_document,
)
from duke_rates.db.repository import Repository


def build_nc_missing_doc_status_report(
    repository: Repository,
    *,
    family_key: str | None = None,
    discovery_record_id: int | None = None,
    historical_document_id: int | None = None,
) -> dict[str, Any]:
    if not any([family_key, discovery_record_id, historical_document_id]):
        raise ValueError("Provide family_key, discovery_record_id, or historical_document_id.")

    with repository._connect() as conn:
        resolved_family_key = family_key or _family_key_from_targets(
            conn,
            repository,
            discovery_record_id=discovery_record_id,
            historical_document_id=historical_document_id,
        )
        discovery_rows = _load_discovery_rows(
            repository,
            conn,
            family_key=resolved_family_key,
            discovery_record_id=discovery_record_id,
            historical_document_id=historical_document_id,
        )
        historical_rows = _load_historical_rows(
            repository,
            conn,
            family_key=resolved_family_key,
            discovery_record_id=discovery_record_id,
            historical_document_id=historical_document_id,
        )
        lead_rows = repository.list_historical_leads(family_key=resolved_family_key) if resolved_family_key else []
        docket_rows = repository.list_regulatory_docket_leads(family_key=resolved_family_key) if resolved_family_key else []
        version_rows = repository.list_tariff_versions(resolved_family_key) if resolved_family_key else []

        historical_status_rows = [
            _build_historical_document_status(conn, row)
            for row in historical_rows
        ]
        discovery_status_rows = [
            _build_discovery_status(conn, row)
            for row in discovery_rows
        ]

        summary = {
            "family_key": resolved_family_key,
            "discovery_record_count": len(discovery_status_rows),
            "historical_lead_count": len(lead_rows),
            "docket_lead_count": len(docket_rows),
            "historical_document_count": len(historical_status_rows),
            "tariff_version_count": len(version_rows),
            "fetched_success_count": sum(
                1 for row in discovery_status_rows if row.get("fetch_status") == "success"
            ),
            "queued_reprocess_count": sum(
                1
                for row in historical_status_rows
                if (row.get("latest_reprocess_queue") or {}).get("status") in {"pending", "running"}
            ),
            "needs_review_count": sum(
                1
                for row in historical_status_rows
                if (row.get("latest_review") or {}).get("outcome") == "needs_review"
            ),
            "versions_with_historical_link_count": sum(
                1 for row in version_rows if row.historical_document_id is not None
            ),
        }

        return {
            "target": {
                "family_key": resolved_family_key,
                "discovery_record_id": discovery_record_id,
                "historical_document_id": historical_document_id,
            },
            "summary": summary,
            "historical_leads": [_historical_lead_summary(row) for row in lead_rows[:20]],
            "docket_leads": [_docket_lead_summary(row) for row in docket_rows[:20]],
            "discovery_records": discovery_status_rows,
            "historical_documents": historical_status_rows,
            "tariff_versions": [_tariff_version_summary(row) for row in version_rows[:50]],
        }


def _family_key_from_targets(
    conn: sqlite3.Connection,
    repository: Repository,
    *,
    discovery_record_id: int | None,
    historical_document_id: int | None,
) -> str | None:
    if historical_document_id is not None:
        row = repository.get_historical_document(historical_document_id)
        return row.family_key if row else None
    if discovery_record_id is not None:
        record = repository.get_ncuc_discovery_record(discovery_record_id)
        if record and record.family_keys:
            return record.family_keys[0]
    return None


def _load_discovery_rows(
    repository: Repository,
    conn: sqlite3.Connection,
    *,
    family_key: str | None,
    discovery_record_id: int | None,
    historical_document_id: int | None,
):
    if discovery_record_id is not None:
        record = repository.get_ncuc_discovery_record(discovery_record_id)
        return [record] if record else []
    if family_key:
        return repository.list_ncuc_discovery_records(family_key=family_key)
    if historical_document_id is not None:
        hist = repository.get_historical_document(historical_document_id)
        if hist and hist.canonical_url:
            rows = conn.execute(
                """
                SELECT id FROM ncuc_discovery_records
                WHERE discovered_url = ? OR download_url = ? OR viewer_url = ?
                ORDER BY id DESC
                """,
                (hist.canonical_url, hist.canonical_url, hist.canonical_url),
            ).fetchall()
            return [
                repository.get_ncuc_discovery_record(int(row["id"]))
                for row in rows
                if row["id"] is not None
            ]
    return []


def _load_historical_rows(
    repository: Repository,
    conn: sqlite3.Connection,
    *,
    family_key: str | None,
    discovery_record_id: int | None,
    historical_document_id: int | None,
):
    if historical_document_id is not None:
        row = repository.get_historical_document(historical_document_id)
        return [row] if row else []
    if family_key:
        return [row for row in repository.list_historical_documents(state="NC") if row.family_key == family_key]
    if discovery_record_id is not None:
        rows = conn.execute(
            """
            SELECT id
            FROM historical_documents
            WHERE canonical_url = (
                SELECT COALESCE(discovered_url, download_url, viewer_url)
                FROM ncuc_discovery_records
                WHERE id = ?
            )
            ORDER BY id DESC
            """,
            (discovery_record_id,),
        ).fetchall()
        return [
            repository.get_historical_document(int(row["id"]))
            for row in rows
            if row["id"] is not None
        ]
    return []


def _build_discovery_status(conn: sqlite3.Connection, row) -> dict[str, Any]:
    metadata = _loads_json_object(row.metadata_json)
    search_promotion = _assess_search_promotion(row, metadata=metadata)
    linked_historical_ids = [
        int(item["id"])
        for item in conn.execute(
            """
            SELECT id
            FROM historical_documents
            WHERE canonical_url = COALESCE(?, ?, ?)
            ORDER BY id DESC
            """,
            (row.discovered_url, row.download_url, row.viewer_url),
        ).fetchall()
        if item["id"] is not None
    ]
    status = {
        "id": row.id,
        "docket_number": row.docket_number,
        "filing_title": row.filing_title,
        "filing_date": row.filing_date,
        "fetch_status": row.fetch_status,
        "search_confidence_score": getattr(row, "search_confidence_score", None),
        "search_ideality": getattr(row, "search_ideality", None),
        "family_keys": list(row.family_keys),
        "download_url": row.download_url,
        "viewer_url": row.viewer_url,
        "local_path": row.local_path,
        "content_hash": row.content_hash,
        "acquisition_method": row.acquisition_method.value if hasattr(row.acquisition_method, "value") else str(row.acquisition_method),
        "provenance_notes": list(row.provenance_notes),
        "linked_historical_document_ids": linked_historical_ids,
        "search_promotion_assessment": search_promotion,
        "metadata": metadata,
    }
    status["next_action"] = _next_action_for_discovery_status(status)
    status["blocked_reason"] = _blocked_reason_for_discovery_status(status)
    return status


def _build_historical_document_status(conn: sqlite3.Connection, row) -> dict[str, Any]:
    latest_run = latest_processing_run_for_document(conn, historical_document_id=int(row.id))
    latest_attempt = latest_parse_attempt_for_historical_document(conn, historical_document_id=int(row.id))
    latest_review = _latest_review_for_document(conn, historical_document_id=int(row.id), latest_attempt=latest_attempt)
    latest_queue = _latest_reprocess_queue_for_document(conn, historical_document_id=int(row.id))
    ocr_artifact = _latest_ocr_artifact_for_source(conn, source_pdf=str(row.local_path or ""))
    version_row = _latest_tariff_version_for_document(conn, historical_document_id=int(row.id))
    import_promotion = _assess_import_promotion(row)
    status = {
        "id": row.id,
        "family_key": row.family_key,
        "title": row.title,
        "effective_start": row.effective_start,
        "start_page": row.start_page,
        "end_page": row.end_page,
        "local_path": str(row.local_path or ""),
        "canonical_url": row.canonical_url,
        "archived_url": row.archived_url,
        "family_match_score": import_promotion["family_match_score"],
        "evidence_breakdown": import_promotion["evidence_breakdown"],
        "import_promotion_assessment": import_promotion,
        "tariff_version": version_row,
        "latest_processing_run": latest_run,
        "latest_parse_attempt": latest_attempt,
        "latest_review": latest_review,
        "latest_reprocess_queue": latest_queue,
        "latest_ocr_artifact": ocr_artifact,
        "current_stage": _current_stage(version_row, latest_run, latest_review, latest_queue),
    }
    status["next_action"] = _next_action_for_historical_status(status)
    status["blocked_reason"] = _blocked_reason_for_historical_status(status)
    return status


def _latest_review_for_document(
    conn: sqlite3.Connection,
    *,
    historical_document_id: int,
    latest_attempt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if latest_attempt and latest_attempt.get("id") is not None:
        row = conn.execute(
            """
            SELECT *
            FROM parse_review_outcomes
            WHERE parse_attempt_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(latest_attempt["id"]),),
        ).fetchone()
        if row:
            return dict(row)

    hist = conn.execute(
        "SELECT local_path, start_page, end_page FROM historical_documents WHERE id = ?",
        (historical_document_id,),
    ).fetchone()
    if not hist:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM parse_review_outcomes
        WHERE source_pdf = ?
          AND COALESCE(page_start, -1) = COALESCE(?, -1)
          AND COALESCE(page_end, -1) = COALESCE(?, -1)
        ORDER BY id DESC
        LIMIT 1
        """,
        (hist["local_path"], hist["start_page"], hist["end_page"]),
    ).fetchone()
    return dict(row) if row else None


def _latest_reprocess_queue_for_document(conn: sqlite3.Connection, *, historical_document_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM historical_reprocess_queue
        WHERE historical_document_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (historical_document_id,),
    ).fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["metadata"] = _loads_json_object(payload.get("metadata_json"))
    return payload


def _latest_ocr_artifact_for_source(conn: sqlite3.Connection, *, source_pdf: str) -> dict[str, Any] | None:
    if not source_pdf:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM ocr_artifacts
        WHERE source_pdf = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_pdf,),
    ).fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["metadata"] = _loads_json_object(payload.get("metadata_json"))
    return payload


def _latest_tariff_version_for_document(conn: sqlite3.Connection, *, historical_document_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM tariff_versions
        WHERE historical_document_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (historical_document_id,),
    ).fetchone()
    return dict(row) if row else None


def _current_stage(
    version_row: dict[str, Any] | None,
    latest_run: dict[str, Any] | None,
    latest_review: dict[str, Any] | None,
    latest_queue: dict[str, Any] | None,
) -> str:
    if latest_queue and latest_queue.get("status") in {"pending", "running"}:
        return f"queued_for_reprocess:{latest_queue.get('status')}"
    if latest_review and latest_review.get("outcome") == "needs_review":
        return "needs_review"
    if latest_run and latest_run.get("status") == "completed":
        return f"processed:{latest_run.get('outcome_quality') or 'unknown'}"
    if version_row:
        return "registered_for_extraction"
    return "imported_not_linked"


def _next_action_for_discovery_status(status: dict[str, Any]) -> str:
    fetch_status = str(status.get("fetch_status") or "").lower()
    linked_historical_ids = status.get("linked_historical_document_ids") or []
    search_promotion = status.get("search_promotion_assessment") or {}

    if fetch_status in {"pending", "requires_browser"}:
        return "fetch_document"
    if fetch_status == "failed":
        return "retry_fetch_or_manual_portal_review"
    if fetch_status == "success" and not linked_historical_ids:
        return "import_and_mine_document"
    if not search_promotion.get("promotable", False):
        return "review_search_clues"
    return "monitor_linked_document"


def _blocked_reason_for_discovery_status(status: dict[str, Any]) -> str | None:
    fetch_status = str(status.get("fetch_status") or "").lower()
    search_promotion = status.get("search_promotion_assessment") or {}
    reasons = list(search_promotion.get("reasons") or [])
    if fetch_status == "failed":
        return "fetch_failed"
    if reasons:
        return reasons[0]
    return None


def _next_action_for_historical_status(status: dict[str, Any]) -> str:
    queue = status.get("latest_reprocess_queue") or {}
    review = status.get("latest_review") or {}
    latest_run = status.get("latest_processing_run") or {}
    version_row = status.get("tariff_version") or {}
    import_promotion = status.get("import_promotion_assessment") or {}
    run_quality = str(latest_run.get("outcome_quality") or "").lower()
    queue_status = str(queue.get("status") or "").lower()
    review_outcome = str(review.get("outcome") or "").lower()

    if queue_status in {"pending", "running"}:
        return "wait_for_reprocess_completion"
    if not version_row:
        return "bootstrap_tariff_version"
    if not import_promotion.get("promotable", False):
        return "review_family_assignment"
    if review_outcome == "needs_review":
        return "review_parse_output"
    if run_quality in {"empty", "weak"}:
        return "retry_with_better_parser_context"
    if run_quality == "strong":
        return "ready_for_acceptance"
    return "process_document"


def _blocked_reason_for_historical_status(status: dict[str, Any]) -> str | None:
    queue = status.get("latest_reprocess_queue") or {}
    review = status.get("latest_review") or {}
    latest_run = status.get("latest_processing_run") or {}
    import_promotion = status.get("import_promotion_assessment") or {}
    queue_status = str(queue.get("status") or "").lower()
    review_outcome = str(review.get("outcome") or "").lower()
    run_quality = str(latest_run.get("outcome_quality") or "").lower()
    reasons = list(import_promotion.get("reasons") or [])

    if queue_status == "failed":
        return str(queue.get("error_message") or "reprocess_failed")
    if reasons:
        return reasons[0]
    if review_outcome == "needs_review":
        return "needs_review"
    if run_quality in {"empty", "weak"}:
        return f"processed_{run_quality}"
    return None


def _assess_search_promotion(row, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    ideality_rank = {"skip": 0, "possible": 1, "probable": 2, "ideal": 3}
    current_ideality = str(getattr(row, "search_ideality", None) or "possible").lower()
    current_rank = ideality_rank.get(current_ideality, 1)
    confidence = float(getattr(row, "search_confidence_score", None) or 0.0)
    required_rank = ideality_rank["probable"]
    threshold = 45.0
    if current_rank < required_rank:
        reasons.append(f"ideality_below_threshold:{current_ideality}")
    if confidence < threshold:
        reasons.append(f"confidence_below_threshold:{confidence:.2f}")
    if not (
        getattr(row, "download_url", None)
        or getattr(row, "viewer_url", None)
        or getattr(row, "attachment_url", None)
    ):
        reasons.append("no_downloadable_url")
    audit_context = ((metadata or {}).get("audit_context") if isinstance(metadata, dict) else {}) or {}
    if isinstance(audit_context, dict):
        priority_band = str(audit_context.get("priority_band") or "").lower()
        if priority_band == "high" and current_rank >= ideality_rank["possible"] and confidence >= max(20.0, threshold - 10.0):
            reasons = [reason for reason in reasons if not reason.startswith("confidence_below_threshold:")]
    return {
        "promotable": not reasons,
        "reasons": reasons,
        "search_confidence_score": confidence,
        "search_ideality": current_ideality,
    }


def _assess_import_promotion(row) -> dict[str, Any]:
    reasons: list[str] = []
    evidence = _loads_json_object(getattr(row, "evidence_json", None))
    family_key = str(getattr(row, "family_key", None) or "").strip()
    family_match_score = round(sum(_coerce_float(value) for value in evidence.values()), 4)
    effective_start = str(getattr(row, "effective_start", None) or "").strip()
    local_path = getattr(row, "local_path", None)
    threshold = 24.0
    if not family_key:
        reasons.append("no_family_key")
    if not local_path:
        reasons.append("no_local_path")
    if _looks_like_provisional_family_key(family_key):
        reasons.append("provisional_family_key")
    if family_match_score < threshold:
        reasons.append(f"family_match_below_threshold:{family_match_score:.2f}")
    if not effective_start and family_match_score < max(35.0, threshold + 8.0):
        reasons.append("missing_effective_start_for_weak_match")
    return {
        "promotable": not reasons,
        "reasons": reasons,
        "family_match_score": family_match_score,
        "evidence_breakdown": evidence,
        "effective_start": getattr(row, "effective_start", None),
        "start_page": getattr(row, "start_page", None),
        "end_page": getattr(row, "end_page", None),
    }


def _coerce_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _looks_like_provisional_family_key(family_key: str | None) -> bool:
    normalized = str(family_key or "").strip().lower()
    return "-doc-" in normalized or normalized.endswith("-doc")


def _loads_json_object(payload: Any) -> dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        loaded = json.loads(str(payload))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _historical_lead_summary(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "extracted_title": row.extracted_title,
        "docket_number": row.docket_number,
        "effective_start": row.effective_start,
        "confidence_score": row.confidence_score,
        "extraction_method": row.extraction_method,
        "source_class": row.source_class,
        "source_label": row.source_label,
        "notes": list(row.notes),
    }


def _docket_lead_summary(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "docket_number": row.docket_number,
        "utility": row.utility,
        "proceeding_type": row.proceeding_type,
        "date_start": row.date_start,
        "contains_tariff_text": row.contains_tariff_text,
        "confidence_score": row.confidence_score,
        "notes": list(row.notes),
    }


def _tariff_version_summary(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "historical_document_id": row.historical_document_id,
        "effective_start": row.effective_start,
        "effective_end": row.effective_end,
        "revision_label": row.revision_label,
        "supersedes_label": row.supersedes_label,
        "docket_number": row.docket_number,
        "confidence_score": row.confidence_score,
    }


__all__ = ["build_nc_missing_doc_status_report"]
