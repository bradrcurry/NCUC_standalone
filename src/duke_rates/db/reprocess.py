from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from duke_rates.historical.ncuc.pipeline.stage_versions import (
    HISTORICAL_BULK_PARSER_VERSION,
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
    PAGE_ARTIFACT_VERSION,
    SPAN_ARTIFACT_VERSION,
    current_stage_versions,
)
from duke_rates.historical.ncuc.pipeline.profile_dependencies import (
    get_parser_profile_impact_rule,
)
from duke_rates.db.parse_review import list_parse_review_queue


def record_historical_processing_run(
    conn: sqlite3.Connection,
    *,
    historical_document_id: int,
    source_pdf: str,
    family_key: str | None,
    content_hash: str | None,
    parser_stage: str,
    parser_profile: str | None,
    parser_version: str,
    processing_mode: str,
    status: str,
    outcome_quality: str | None,
    charge_count: int,
    review_flags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Persist a versioned processing run for a historical extraction attempt."""
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO historical_processing_runs (
            historical_document_id, source_pdf, family_key, content_hash,
            parser_stage, parser_profile, parser_version, processing_mode,
            status, outcome_quality, charge_count, review_flags_json,
            metadata_json, started_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            historical_document_id,
            source_pdf,
            family_key,
            content_hash,
            parser_stage,
            parser_profile,
            parser_version,
            processing_mode,
            status,
            outcome_quality,
            charge_count,
            json.dumps(review_flags or []),
            json.dumps(metadata or {}, sort_keys=True),
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def latest_processing_run_for_document(
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


def latest_parse_attempt_for_historical_document(
    conn: sqlite3.Connection,
    *,
    historical_document_id: int,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT *
        FROM parse_attempt_logs
        WHERE parser_stage = 'historical_bulk'
        ORDER BY id DESC
        """
    ).fetchall()
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        if metadata.get("historical_document_id") == historical_document_id:
            payload = dict(row)
            payload["metadata"] = metadata
            return payload
    return None


def enqueue_historical_reprocess(
    conn: sqlite3.Connection,
    *,
    historical_document_id: int,
    source_pdf: str,
    family_key: str | None,
    queue_reason: str,
    priority: int = 50,
    requested_by: str = "system",
    metadata: dict[str, Any] | None = None,
) -> tuple[int | None, bool]:
    """Queue a historical document for targeted reprocessing if not already pending."""
    existing = conn.execute(
        """
        SELECT id
        FROM historical_reprocess_queue
        WHERE historical_document_id = ?
          AND status IN ('pending', 'running')
        ORDER BY id DESC
        LIMIT 1
        """,
        (historical_document_id,),
    ).fetchone()
    if existing:
        return int(existing["id"]), False

    cur = conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            historical_document_id, source_pdf, family_key, priority,
            queue_reason, requested_by, status, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            historical_document_id,
            source_pdf,
            family_key,
            priority,
            queue_reason,
            requested_by,
            "pending",
            json.dumps(metadata or {}, sort_keys=True),
            datetime.now(UTC).isoformat(),
        ),
    )
    return int(cur.lastrowid), True


def _priority_from_review_row(row: dict[str, Any], default_priority: int) -> int:
    flags = json.loads(row.get("review_flags_json") or "[]")
    profile = str(row.get("parser_profile") or "")
    priority = default_priority
    if "no_charges_extracted" in flags:
        priority = max(priority, 95)
    if "generic_fallback_selected" in flags:
        priority = max(priority, 90)
    if "low_selector_confidence" in flags:
        priority = max(priority, 85)
    if profile == "generic_residential":
        priority = max(priority, 80)
    return priority


def enqueue_reprocess_candidates_from_review_queue(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    priority: int = 70,
    requested_by: str = "system",
    family_key: str | None = None,
    parser_profile: str | None = None,
    source_pdf: str | None = None,
) -> dict[str, Any]:
    """Queue historical documents whose latest parse review still needs review."""
    inserted = 0
    skipped = 0
    queued_ids: list[int] = []

    for row in list_parse_review_queue(
        conn,
        limit=limit,
        family_key=family_key,
        parser_profile=parser_profile,
        source_pdf=source_pdf,
    ):
        metadata = json.loads(row.get("metadata_json") or "{}")
        historical_document_id = metadata.get("historical_document_id")
        if not historical_document_id:
            skipped += 1
            continue

        queue_id, did_insert = enqueue_historical_reprocess(
            conn,
            historical_document_id=int(historical_document_id),
            source_pdf=str(row.get("source_pdf") or ""),
            family_key=metadata.get("family_key"),
            queue_reason=f"needs_review:{row.get('parser_profile') or 'unknown'}",
            priority=_priority_from_review_row(row, priority),
            requested_by=requested_by,
            metadata={
                "parse_attempt_id": row.get("parse_attempt_id"),
                "review_outcome_id": row.get("review_outcome_id"),
                "parser_profile": row.get("parser_profile"),
                "review_flags": json.loads(row.get("review_flags_json") or "[]"),
            },
        )
        if did_insert and queue_id is not None:
            inserted += 1
            queued_ids.append(queue_id)
        else:
            skipped += 1

    return {
        "inserted": inserted,
        "skipped": skipped,
        "queue_ids": queued_ids,
    }


def enqueue_specific_historical_documents(
    conn: sqlite3.Connection,
    *,
    historical_document_ids: list[int],
    priority: int = 70,
    requested_by: str = "system",
    queue_reason: str = "manual_requeue",
    metadata_by_id: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Queue one or more historical documents directly by id."""
    inserted = 0
    skipped = 0
    queue_ids: list[int] = []
    missing_ids: list[int] = []

    for historical_document_id in historical_document_ids:
        row = conn.execute(
            """
            SELECT id, family_key, local_path
            FROM historical_documents
            WHERE id = ?
            """,
            (historical_document_id,),
        ).fetchone()
        if row is None:
            missing_ids.append(int(historical_document_id))
            skipped += 1
            continue
        queue_id, did_insert = enqueue_historical_reprocess(
            conn,
            historical_document_id=int(row["id"]),
            source_pdf=str(row["local_path"] or ""),
            family_key=row["family_key"],
            queue_reason=queue_reason,
            priority=priority,
            requested_by=requested_by,
            metadata={
                "historical_document_id": int(row["id"]),
                **((metadata_by_id or {}).get(int(row["id"])) or {}),
            },
        )
        if did_insert and queue_id is not None:
            inserted += 1
            queue_ids.append(queue_id)
        else:
            skipped += 1

    return {
        "inserted": inserted,
        "skipped": skipped,
        "queue_ids": queue_ids,
        "missing_ids": missing_ids,
    }


def list_historical_reprocess_queue(
    conn: sqlite3.Connection,
    *,
    status: str | None = "pending",
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = """
        SELECT *
        FROM historical_reprocess_queue
    """
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY priority DESC, requested_at ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def claim_next_historical_reprocess(
    conn: sqlite3.Connection,
    *,
    requested_status: str = "pending",
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM historical_reprocess_queue
        WHERE status = ?
        ORDER BY priority DESC, requested_at ASC
        LIMIT 1
        """,
        (requested_status,),
    ).fetchone()
    if not row:
        return None

    conn.execute(
        """
        UPDATE historical_reprocess_queue
        SET status = 'running', started_at = ?
        WHERE id = ?
        """,
        (datetime.now(UTC).isoformat(), row["id"]),
    )
    claimed = conn.execute(
        "SELECT * FROM historical_reprocess_queue WHERE id = ?",
        (row["id"],),
    ).fetchone()
    return dict(claimed) if claimed else None


def complete_historical_reprocess(
    conn: sqlite3.Connection,
    *,
    queue_id: int,
    status: str,
    latest_run_id: int | None = None,
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    existing = conn.execute(
        "SELECT metadata_json FROM historical_reprocess_queue WHERE id = ?",
        (queue_id,),
    ).fetchone()
    merged_metadata = json.loads(existing["metadata_json"] or "{}") if existing else {}
    merged_metadata.update(metadata or {})
    conn.execute(
        """
        UPDATE historical_reprocess_queue
        SET status = ?, latest_run_id = ?, error_message = ?, metadata_json = ?,
            completed_at = ?
        WHERE id = ?
        """,
        (
            status,
            latest_run_id,
            error_message,
            json.dumps(merged_metadata, sort_keys=True),
            datetime.now(UTC).isoformat(),
            queue_id,
        ),
    )


def find_stale_historical_documents(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    family_key: str | None = None,
) -> list[dict[str, Any]]:
    """Identify historical documents whose cached stages are missing or stale."""
    query = """
        SELECT id, family_key, company, local_path, content_hash, effective_start
        FROM historical_documents
        WHERE local_path IS NOT NULL
    """
    params: list[Any] = []
    if family_key:
        query += " AND family_key = ?"
        params.append(family_key)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    docs = conn.execute(query, tuple(params)).fetchall()

    stale: list[dict[str, Any]] = []
    stage_versions = current_stage_versions()
    for row in docs:
        reasons: list[str] = []
        metadata: dict[str, Any] = {"stage_versions": stage_versions}
        source_pdf = row["local_path"]
        content_hash = row["content_hash"]

        page_row = conn.execute(
            """
            SELECT artifact_version, metadata_json
            FROM ncuc_page_artifacts
            WHERE source_pdf = ? AND file_hash IS ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (source_pdf, content_hash),
        ).fetchone()
        if not page_row:
            reasons.append("page_artifact_missing")
        else:
            if page_row["artifact_version"] != PAGE_ARTIFACT_VERSION:
                reasons.append("page_artifact_version")
            page_metadata = json.loads(page_row["metadata_json"] or "{}")
            if (
                page_metadata.get("artifact_source") == "ocr"
                and page_metadata.get("ocr_backend_version") != OCR_BACKEND_VERSION
            ):
                reasons.append("ocr_backend_version")
            if (
                page_metadata.get("artifact_source") == "ocr"
                and page_metadata.get("ocr_normalization_version") != OCR_NORMALIZATION_VERSION
            ):
                reasons.append("ocr_normalization_version")

        span_row = conn.execute(
            """
            SELECT artifact_version
            FROM ncuc_span_artifacts
            WHERE source_pdf = ? AND file_hash IS ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (source_pdf, content_hash),
        ).fetchone()
        if not span_row:
            reasons.append("span_artifact_missing")
        elif span_row["artifact_version"] != SPAN_ARTIFACT_VERSION:
            reasons.append("span_artifact_version")

        run = latest_processing_run_for_document(
            conn,
            historical_document_id=int(row["id"]),
        )
        if not run:
            reasons.append("parser_run_missing")
        elif run["parser_version"] != HISTORICAL_BULK_PARSER_VERSION:
            reasons.append("parser_version")

        if row["effective_start"] is None and set(reasons) <= {
            "page_artifact_missing",
            "span_artifact_missing",
            "parser_run_missing",
        }:
            continue

        if not reasons:
            continue

        priority = 60
        if "ocr_backend_version" in reasons:
            priority = max(priority, 95)
        if "ocr_normalization_version" in reasons:
            priority = max(priority, 92)
        if "parser_version" in reasons:
            priority = max(priority, 90)
        if "page_artifact_missing" in reasons or "span_artifact_missing" in reasons:
            priority = max(priority, 85)

        stale.append(
            {
                "historical_document_id": int(row["id"]),
                "family_key": row["family_key"],
                "company": row["company"],
                "source_pdf": source_pdf,
                "content_hash": content_hash,
                "effective_start": row["effective_start"],
                "priority": priority,
                "reasons": reasons,
                "metadata": metadata,
            }
        )

    return stale


def enqueue_stale_historical_documents(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    requested_by: str = "system",
    family_key: str | None = None,
) -> dict[str, Any]:
    """Queue historical documents whose cached stages are stale vs current versions."""
    inserted = 0
    skipped = 0
    queue_ids: list[int] = []
    for row in find_stale_historical_documents(conn, limit=limit, family_key=family_key):
        queue_id, did_insert = enqueue_historical_reprocess(
            conn,
            historical_document_id=row["historical_document_id"],
            source_pdf=row["source_pdf"],
            family_key=row["family_key"],
            queue_reason=f"stale_stage:{','.join(row['reasons'])}",
            priority=row["priority"],
            requested_by=requested_by,
            metadata=row["metadata"] | {"stale_reasons": row["reasons"]},
        )
        if did_insert and queue_id is not None:
            inserted += 1
            queue_ids.append(queue_id)
        else:
            skipped += 1
    return {"inserted": inserted, "skipped": skipped, "queue_ids": queue_ids}


def find_profile_impacted_historical_documents(
    conn: sqlite3.Connection,
    *,
    parser_profile: str,
    limit: int = 100,
    family_key: str | None = None,
) -> list[dict[str, Any]]:
    """Identify historical documents likely affected by a parser-profile change."""
    rule = get_parser_profile_impact_rule(parser_profile)

    query = """
        SELECT
            hd.id,
            hd.family_key,
            hd.company,
            hd.local_path,
            hd.content_hash,
            hd.effective_start,
            run.id AS latest_run_id,
            run.parser_profile AS latest_parser_profile,
            run.parser_version AS latest_parser_version
        FROM historical_documents hd
        LEFT JOIN historical_processing_runs run
          ON run.id = (
            SELECT r2.id
            FROM historical_processing_runs r2
            WHERE r2.historical_document_id = hd.id
            ORDER BY r2.id DESC
            LIMIT 1
          )
        WHERE hd.local_path IS NOT NULL
    """
    params: list[Any] = []
    if family_key:
        query += " AND hd.family_key = ?"
        params.append(family_key)
    query += " ORDER BY hd.id DESC"

    rows = conn.execute(query, tuple(params)).fetchall()
    impacted: list[dict[str, Any]] = []
    for row in rows:
        latest_attempt = latest_parse_attempt_for_historical_document(
            conn,
            historical_document_id=int(row["id"]),
        )
        attempt_metadata = latest_attempt.get("metadata", {}) if latest_attempt else {}
        effective_signals = dict(attempt_metadata.get("signals", {}))
        local_path = str(row["local_path"] or "").replace("/", "\\").lower()
        effective_signals.setdefault("is_current_progress_pdf", "data\\raw\\nc\\progress\\" in local_path)
        reasons = rule.match_reasons(
            family_key=row["family_key"],
            company=row["company"],
            latest_parser_profile=row["latest_parser_profile"],
            candidate_profiles=attempt_metadata.get("candidate_profiles"),
            signals=effective_signals,
        )
        if not reasons:
            continue

        priority = 82
        if "latest_parser_profile" in reasons:
            priority = max(priority, 90)
        if "family_key" in reasons:
            priority = max(priority, 88)
        if "family_prefix" in reasons:
            priority = max(priority, 85)
        if "candidate_profile" in reasons:
            priority = max(priority, 91)
        if "candidate_reason" in reasons:
            priority = max(priority, 93)
        if "signal_match" in reasons:
            priority = max(priority, 87)

        impacted.append(
            {
                "historical_document_id": int(row["id"]),
                "family_key": row["family_key"],
                "company": row["company"],
                "source_pdf": row["local_path"],
                "content_hash": row["content_hash"],
                "effective_start": row["effective_start"],
                "priority": priority,
                "reasons": reasons,
                "metadata": {
                    "impact_profile": rule.parser_profile,
                    "impact_rule": rule.to_metadata(),
                    "latest_run_id": row["latest_run_id"],
                    "latest_parser_profile": row["latest_parser_profile"],
                    "latest_parser_version": row["latest_parser_version"],
                    "latest_parse_attempt_id": latest_attempt["id"] if latest_attempt else None,
                    "latest_candidate_profiles": attempt_metadata.get("candidate_profiles", []),
                    "latest_signals": effective_signals,
                },
            }
        )
        if len(impacted) >= limit:
            break

    return impacted


def enqueue_profile_impacted_historical_documents(
    conn: sqlite3.Connection,
    *,
    parser_profile: str,
    limit: int = 100,
    requested_by: str = "system",
    family_key: str | None = None,
) -> dict[str, Any]:
    """Queue historical documents affected by a parser-profile dependency rule."""
    inserted = 0
    skipped = 0
    queue_ids: list[int] = []
    for row in find_profile_impacted_historical_documents(
        conn,
        parser_profile=parser_profile,
        limit=limit,
        family_key=family_key,
    ):
        queue_id, did_insert = enqueue_historical_reprocess(
            conn,
            historical_document_id=row["historical_document_id"],
            source_pdf=row["source_pdf"],
            family_key=row["family_key"],
            queue_reason=f"profile_dependency:{parser_profile}:{','.join(row['reasons'])}",
            priority=row["priority"],
            requested_by=requested_by,
            metadata=row["metadata"] | {"impact_reasons": row["reasons"]},
        )
        if did_insert and queue_id is not None:
            inserted += 1
            queue_ids.append(queue_id)
        else:
            skipped += 1
    return {"inserted": inserted, "skipped": skipped, "queue_ids": queue_ids}
