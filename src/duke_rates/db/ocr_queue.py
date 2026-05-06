from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from duke_rates.historical.ncuc.pipeline.stage_versions import (
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
)
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf
from duke_rates.models.pipeline import PipelineRoute


DEFAULT_OCR_BACKEND = "ocrmypdf_tesseract"


def upsert_ocr_artifact(
    conn: sqlite3.Connection,
    *,
    discovery_record_id: int | None,
    source_pdf: str,
    file_hash: str | None,
    backend: str,
    status: str,
    text_sidecar_path: str | None,
    pages_sidecar_path: str | None,
    page_count: int,
    ocr_confidence: float | None,
    metadata: dict[str, Any] | None = None,
) -> int:
    existing = conn.execute(
        """
        SELECT id
        FROM ocr_artifacts
        WHERE source_pdf = ? AND file_hash IS ? AND backend = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_pdf, file_hash, backend),
    ).fetchone()
    now = datetime.now(UTC).isoformat()
    payload = (
        discovery_record_id,
        source_pdf,
        file_hash,
        backend,
        status,
        text_sidecar_path,
        pages_sidecar_path,
        page_count,
        ocr_confidence,
        json.dumps(metadata or {}, sort_keys=True),
        now,
        now,
    )
    if existing:
        conn.execute(
            """
            UPDATE ocr_artifacts
            SET discovery_record_id=?, source_pdf=?, file_hash=?, backend=?, status=?,
                text_sidecar_path=?, pages_sidecar_path=?, page_count=?, ocr_confidence=?,
                metadata_json=?, created_at=?, updated_at=?
            WHERE id = ?
            """,
            payload + (existing["id"],),
        )
        return int(existing["id"])

    cur = conn.execute(
        """
        INSERT INTO ocr_artifacts (
            discovery_record_id, source_pdf, file_hash, backend, status,
            text_sidecar_path, pages_sidecar_path, page_count, ocr_confidence,
            metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        payload,
    )
    return int(cur.lastrowid)


def enqueue_ocr_queue_item(
    conn: sqlite3.Connection,
    *,
    discovery_record_id: int | None,
    source_pdf: str,
    file_hash: str | None,
    backend: str,
    priority: int,
    ocr_confidence: float | None,
    structure_complexity: float | None,
    gpu_candidate: bool,
    metadata: dict[str, Any] | None = None,
) -> tuple[int | None, bool]:
    existing = conn.execute(
        """
        SELECT id
        FROM ocr_processing_queue
        WHERE source_pdf = ?
          AND status IN ('pending', 'running')
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_pdf,),
    ).fetchone()
    if existing:
        return int(existing["id"]), False

    cur = conn.execute(
        """
        INSERT INTO ocr_processing_queue (
            discovery_record_id, source_pdf, file_hash, backend, priority, status,
            ocr_confidence, structure_complexity, gpu_candidate, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            discovery_record_id,
            source_pdf,
            file_hash,
            backend,
            priority,
            "pending",
            ocr_confidence,
            structure_complexity,
            int(gpu_candidate),
            json.dumps(metadata or {}, sort_keys=True),
            datetime.now(UTC).isoformat(),
        ),
    )
    return int(cur.lastrowid), True


def enqueue_ocr_candidates(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    backend: str = DEFAULT_OCR_BACKEND,
    requested_by: str = "system",
    force_rescan: bool = False,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, local_path, content_hash, filing_title, fetch_status
        FROM ncuc_discovery_records
        WHERE fetch_status = 'success' AND local_path IS NOT NULL
        ORDER BY filing_date DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    inserted = 0
    skipped = 0
    queue_ids: list[int] = []
    for row in rows:
        source_pdf = str(row["local_path"] or "")
        if not source_pdf or not Path(source_pdf).exists():
            skipped += 1
            continue
        triage = triage_pdf(source_pdf)
        if triage.route_recommendation != PipelineRoute.OCR_REQUIRED:
            skipped += 1
            continue
        if not force_rescan:
            artifact = conn.execute(
                """
                SELECT id
                FROM ocr_artifacts
                WHERE source_pdf = ? AND file_hash IS ? AND backend = ? AND status = 'completed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (source_pdf, row["content_hash"], backend),
            ).fetchone()
            if artifact:
                skipped += 1
                continue

        priority = 80
        if triage.gpu_ocr_candidate:
            priority = 95
        elif triage.ocr_confidence_score >= 0.85:
            priority = 90

        queue_id, did_insert = enqueue_ocr_queue_item(
            conn,
            discovery_record_id=row["id"],
            source_pdf=source_pdf,
            file_hash=row["content_hash"],
            backend=backend,
            priority=priority,
            ocr_confidence=triage.ocr_confidence_score,
            structure_complexity=triage.structure_complexity_score,
            gpu_candidate=triage.gpu_ocr_candidate,
            metadata={
                "requested_by": requested_by,
                "triage_flags": triage.triage_flags,
                "filing_title": row["filing_title"],
                "ocr_backend_version": OCR_BACKEND_VERSION,
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
            },
        )
        if did_insert and queue_id is not None:
            inserted += 1
            queue_ids.append(queue_id)
        else:
            skipped += 1

    return {"inserted": inserted, "skipped": skipped, "queue_ids": queue_ids}


def list_ocr_queue(
    conn: sqlite3.Connection,
    *,
    status: str | None = "pending",
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM ocr_processing_queue"
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY priority DESC, requested_at ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def claim_next_ocr_queue_item(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Claim the highest-priority pending OCR queue item.

    Wraps the read+update in BEGIN IMMEDIATE so concurrent workers cannot
    both observe the same pending row. The transaction holds a write lock
    from the SELECT through the UPDATE, then commits before returning.
    """
    # In WAL mode SQLite serializes writers; BEGIN IMMEDIATE acquires the
    # write lock up front so the SELECT sees a stable view that the matching
    # UPDATE will land in. Without this, two workers can both read the same
    # row before either UPDATEs it.
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT *
            FROM ocr_processing_queue
            WHERE status = 'pending'
            ORDER BY priority DESC, requested_at ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE ocr_processing_queue
            SET status = 'running', started_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (datetime.now(UTC).isoformat(), row["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM ocr_processing_queue WHERE id = ?",
            (row["id"],),
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return dict(claimed) if claimed else None


def complete_ocr_queue_item(
    conn: sqlite3.Connection,
    *,
    queue_id: int,
    status: str,
    latest_artifact_id: int | None = None,
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    existing = conn.execute(
        "SELECT metadata_json FROM ocr_processing_queue WHERE id = ?",
        (queue_id,),
    ).fetchone()
    merged = json.loads(existing["metadata_json"] or "{}") if existing else {}
    merged.update(metadata or {})
    conn.execute(
        """
        UPDATE ocr_processing_queue
        SET status = ?, latest_artifact_id = ?, error_message = ?, metadata_json = ?,
            completed_at = ?
        WHERE id = ?
        """,
        (
            status,
            latest_artifact_id,
            error_message,
            json.dumps(merged, sort_keys=True),
            datetime.now(UTC).isoformat(),
            queue_id,
        ),
    )
