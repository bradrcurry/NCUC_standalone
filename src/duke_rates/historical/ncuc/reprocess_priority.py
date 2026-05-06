from __future__ import annotations

import json
import sqlite3
from typing import Any

from duke_rates.db.reprocess import latest_parse_attempt_for_historical_document


def _safe_json_load(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _safe_json_list(payload: str | None) -> list[Any]:
    if not payload:
        return []
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _latest_processing_run(
    conn: sqlite3.Connection,
    *,
    historical_document_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM historical_processing_runs
        WHERE historical_document_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (historical_document_id,),
    ).fetchone()
    return dict(row) if row else None


def _latest_ocr_queue_row(
    conn: sqlite3.Connection,
    *,
    source_pdf: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ocr_processing_queue
        WHERE source_pdf = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_pdf,),
    ).fetchone()
    return dict(row) if row else None


def _has_version_link(
    conn: sqlite3.Connection,
    *,
    historical_document_id: int,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM tariff_versions
        WHERE historical_document_id = ?
        LIMIT 1
        """,
        (historical_document_id,),
    ).fetchone()
    return row is not None


def _derive_category(
    *,
    queue_reason: str,
    stale_reasons: list[str],
    latest_run: dict[str, Any] | None,
    latest_attempt: dict[str, Any] | None,
    review_flags: list[str],
    has_version_link: bool,
    ocr_queue_row: dict[str, Any] | None,
) -> tuple[str, str]:
    queue_prefix = queue_reason.split(":", 1)[0] if queue_reason else "unknown"
    latest_run_quality = str((latest_run or {}).get("outcome_quality") or "")
    latest_attempt_status = str((latest_attempt or {}).get("status") or "")

    if (
        latest_run_quality == "empty"
        or latest_attempt_status == "empty"
        or "no_charges_extracted" in review_flags
    ):
        return ("empty_parse", "latest extraction produced no usable charges")
    if (
        "ocr_backend_version" in stale_reasons
        or (ocr_queue_row and str(ocr_queue_row.get("status") or "") in {"pending", "running"})
    ):
        return ("ocr_needed", "OCR or OCR-backed artifacts are the gating issue")
    if queue_prefix == "stale_stage":
        return ("stale_stage", "cached artifacts or parser version are stale")
    if queue_prefix == "profile_dependency":
        return ("profile_impact", "parser-profile dependency rule says this doc is affected")
    if latest_run_quality == "strong" and not has_version_link:
        return ("strong_but_unlinked", "latest parse looks strong but the doc is not version-linked")
    if queue_prefix == "needs_review":
        return ("needs_review", "latest review still requires operator attention")
    return ("other", "queued for manual or uncategorized follow-up")


def _priority_weight(category: str) -> int:
    return {
        "empty_parse": 500,
        "ocr_needed": 450,
        "stale_stage": 400,
        "profile_impact": 300,
        "strong_but_unlinked": 200,
        "needs_review": 150,
        "other": 100,
    }.get(category, 0)


def _impact_summary(
    *,
    latest_run: dict[str, Any] | None,
    latest_attempt_metadata: dict[str, Any],
    stale_reasons: list[str],
    impact_reasons: list[str],
    has_version_link: bool,
    ocr_queue_row: dict[str, Any] | None,
) -> list[str]:
    details: list[str] = []
    latest_quality = str((latest_run or {}).get("outcome_quality") or "")
    if latest_quality:
        details.append(f"latest_outcome={latest_quality}")
    charge_count = (latest_run or {}).get("charge_count")
    if charge_count is not None:
        details.append(f"latest_charge_count={charge_count}")
    if stale_reasons:
        details.append(f"stale={','.join(stale_reasons)}")
    if impact_reasons:
        details.append(f"impact={','.join(impact_reasons)}")
    if not has_version_link:
        details.append("version_link_missing")
    if ocr_queue_row:
        details.append(f"ocr_queue={ocr_queue_row.get('status') or 'unknown'}")
    selection = latest_attempt_metadata.get("selection") if isinstance(latest_attempt_metadata, dict) else {}
    if isinstance(selection, dict):
        fallback_triggered_by = selection.get("fallback_triggered_by")
        if fallback_triggered_by:
            details.append(f"fallback_triggered_by={fallback_triggered_by}")
    return details


def build_reprocess_priority_report(
    conn: sqlite3.Connection,
    *,
    status: str | None = "pending",
    limit: int = 50,
) -> dict[str, Any]:
    query = """
        SELECT
            hrq.*,
            hd.company,
            hd.title,
            hd.effective_start,
            hd.content_hash
        FROM historical_reprocess_queue hrq
        LEFT JOIN historical_documents hd
          ON hd.id = hrq.historical_document_id
    """
    params: list[Any] = []
    if status:
        query += " WHERE hrq.status = ?"
        params.append(status)
    query += " ORDER BY hrq.priority DESC, hrq.requested_at ASC"
    rows = conn.execute(query, tuple(params)).fetchall()

    ranked_rows: list[dict[str, Any]] = []
    category_counts: dict[str, int] = {}
    for row in rows:
        queue_row = dict(row)
        metadata = _safe_json_load(queue_row.get("metadata_json"))
        stale_reasons = [
            str(item)
            for item in metadata.get("stale_reasons", [])
            if str(item)
        ]
        impact_reasons = [
            str(item)
            for item in metadata.get("impact_reasons", [])
            if str(item)
        ]
        latest_run = _latest_processing_run(
            conn,
            historical_document_id=int(queue_row["historical_document_id"]),
        )
        latest_attempt = latest_parse_attempt_for_historical_document(
            conn,
            historical_document_id=int(queue_row["historical_document_id"]),
        )
        latest_attempt_metadata = (
            dict((latest_attempt or {}).get("metadata") or {})
            if latest_attempt
            else {}
        )
        review_flags = [
            str(item)
            for item in _safe_json_list((latest_attempt or {}).get("review_flags_json"))
            if str(item)
        ]
        has_version_link = _has_version_link(
            conn,
            historical_document_id=int(queue_row["historical_document_id"]),
        )
        ocr_queue_row = _latest_ocr_queue_row(
            conn,
            source_pdf=str(queue_row.get("source_pdf") or ""),
        )

        category, category_note = _derive_category(
            queue_reason=str(queue_row.get("queue_reason") or ""),
            stale_reasons=stale_reasons,
            latest_run=latest_run,
            latest_attempt=latest_attempt,
            review_flags=review_flags,
            has_version_link=has_version_link,
            ocr_queue_row=ocr_queue_row,
        )
        category_counts[category] = category_counts.get(category, 0) + 1

        rank_score = _priority_weight(category) + int(queue_row.get("priority") or 0)
        if queue_row.get("effective_start"):
            rank_score += 15
        if str((latest_run or {}).get("outcome_quality") or "") == "strong":
            rank_score += 10
        if ocr_queue_row and str(ocr_queue_row.get("status") or "") == "pending":
            rank_score += 5

        ranked_rows.append(
            {
                "queue_id": int(queue_row["id"]),
                "historical_document_id": int(queue_row["historical_document_id"]),
                "family_key": queue_row.get("family_key"),
                "company": queue_row.get("company"),
                "title": queue_row.get("title"),
                "effective_start": queue_row.get("effective_start"),
                "status": queue_row.get("status"),
                "queue_reason": queue_row.get("queue_reason"),
                "stored_priority": int(queue_row.get("priority") or 0),
                "rank_score": rank_score,
                "priority_category": category,
                "priority_note": category_note,
                "latest_outcome_quality": (latest_run or {}).get("outcome_quality"),
                "latest_charge_count": (latest_run or {}).get("charge_count"),
                "latest_parser_profile": (latest_run or {}).get("parser_profile")
                or (latest_attempt or {}).get("parser_profile"),
                "latest_attempt_status": (latest_attempt or {}).get("status"),
                "review_flags": review_flags,
                "stale_reasons": stale_reasons,
                "impact_profile": metadata.get("impact_profile"),
                "impact_reasons": impact_reasons,
                "has_version_link": has_version_link,
                "ocr_queue_status": (ocr_queue_row or {}).get("status"),
                "impact_summary": _impact_summary(
                    latest_run=latest_run,
                    latest_attempt_metadata=latest_attempt_metadata,
                    stale_reasons=stale_reasons,
                    impact_reasons=impact_reasons,
                    has_version_link=has_version_link,
                    ocr_queue_row=ocr_queue_row,
                ),
                "requested_by": queue_row.get("requested_by"),
                "source_pdf": queue_row.get("source_pdf"),
            }
        )

    ranked_rows.sort(
        key=lambda item: (
            -int(item["rank_score"]),
            -int(item["stored_priority"]),
            int(item["queue_id"]),
        )
    )
    visible_rows = ranked_rows[:limit]

    return {
        "summary": {
            "queue_row_count": len(ranked_rows),
            "visible_row_count": len(visible_rows),
            "category_counts": category_counts,
        },
        "rows": visible_rows,
    }
