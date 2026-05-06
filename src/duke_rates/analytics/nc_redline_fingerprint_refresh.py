from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from duke_rates.parse.redline_detector import detect_redline

_DETECTOR_VERSION = "native_redline_v2"


@dataclass(frozen=True)
class _DocFingerprintRow:
    id: int | None
    source_pdf: str
    page_start: int | None
    page_end: int | None
    current_is_redline: bool
    current_confidence: float


def refresh_nc_redline_fingerprints(
    database_path: Path | None = None,
    *,
    max_pages: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    db_path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        historical_rows = _load_historical_rows(conn)
        by_slice: dict[tuple[str, int | None, int | None], dict[str, Any]] = {}
        action_counts: Counter[str] = Counter()
        changed_rows: list[dict[str, Any]] = []

        for row in historical_rows:
            source_pdf = str(row["local_path"])
            page_start = int(row["start_page"]) if row["start_page"] is not None else None
            page_end = int(row["end_page"]) if row["end_page"] is not None else None
            slice_key = (source_pdf, page_start, page_end)
            detector = by_slice.get(slice_key)
            if detector is None:
                detector = _run_detector(
                    source_pdf,
                    max_pages=max_pages,
                    start_page=page_start,
                    end_page=page_end,
                )
                by_slice[slice_key] = detector

            current = _load_current_rollup(
                conn,
                source_pdf,
                page_start=page_start,
                page_end=page_end,
            )
            current_is_redline = current.current_is_redline
            current_confidence = current.current_confidence
            new_is_redline = bool(detector["is_redline"])
            new_confidence = round(float(detector["confidence"] or 0.0), 4)

            changed = (
                current_is_redline != new_is_redline
                or round(current_confidence, 4) != new_confidence
            )
            if not changed:
                continue

            action = "detector_error" if detector["error"] else "update"
            action_counts[action] += 1
            updated_review_flags = _update_review_flags(
                _load_review_flags(
                    conn,
                    source_pdf,
                    page_start=page_start,
                    page_end=page_end,
                ),
                is_redline=new_is_redline,
            )
            changed_rows.append(
                {
                    "historical_document_id": int(row["id"]),
                    "source_pdf": source_pdf,
                    "page_start": page_start,
                    "page_end": page_end,
                    "affected_rows": 1 if current.id is not None else 0,
                    "old_is_redline": int(current_is_redline),
                    "new_is_redline": int(new_is_redline),
                    "old_confidence": round(current_confidence, 4),
                    "new_confidence": new_confidence,
                    "signals": detector["signals"],
                    "red_text_samples": detector["red_text_samples"],
                    "strikethrough_samples": detector["strikethrough_samples"],
                    "red_is_index_only": int(bool(detector["red_is_index_only"])),
                    "error": detector["error"],
                }
            )
            if dry_run or detector["error"]:
                continue
            _upsert_fingerprint_row(
                conn,
                row,
                current=current,
                detector=detector,
                review_flags_json=updated_review_flags,
            )

        if not dry_run:
            conn.commit()
        return {
            "database_path": str(db_path),
            "dry_run": dry_run,
            "max_pages": max_pages,
            "source_pdf_count": len(by_slice),
            "changed_fingerprint_rows": len(changed_rows),
            "action_counts": dict(sorted(action_counts.items())),
            "rows": changed_rows,
        }
    finally:
        conn.close()


def _load_historical_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT hd.id, hd.family_key, hd.title, hd.leaf_no, hd.local_path, hd.start_page, hd.end_page
        FROM historical_documents hd
        WHERE hd.state = 'NC'
          AND LOWER(hd.company) IN ('progress', 'carolinas')
          AND hd.local_path IS NOT NULL
          AND TRIM(hd.local_path) <> ''
        ORDER BY hd.local_path, hd.start_page, hd.end_page, hd.id
        """
    ).fetchall()
    return list(rows)


def _load_current_rollup(
    conn: sqlite3.Connection,
    source_pdf: str,
    *,
    page_start: int | None,
    page_end: int | None,
) -> _DocFingerprintRow:
    row = conn.execute(
        """
        SELECT
            id,
            source_pdf,
            page_start,
            page_end,
            COALESCE(is_redline_candidate, 0) AS current_is_redline,
            COALESCE(redline_confidence, 0.0) AS current_confidence
        FROM document_fingerprints
        WHERE source_pdf = ?
          AND (
            (page_start IS ? AND page_end IS ?)
            OR (
                ? IS NULL AND ? IS NULL
                AND page_start IS NULL AND page_end IS NULL
            )
          )
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_pdf, page_start, page_end, page_start, page_end),
    ).fetchone()
    return _DocFingerprintRow(
        id=int(row["id"]) if row and row["id"] is not None else None,
        source_pdf=source_pdf,
        page_start=page_start,
        page_end=page_end,
        current_is_redline=bool(int(row["current_is_redline"] or 0)) if row else False,
        current_confidence=float(row["current_confidence"] or 0.0) if row else 0.0,
    )


def _load_review_flags(
    conn: sqlite3.Connection,
    source_pdf: str,
    *,
    page_start: int | None,
    page_end: int | None,
) -> str:
    row = conn.execute(
        """
        SELECT review_flags_json
        FROM document_fingerprints
        WHERE source_pdf = ?
          AND (
            (page_start IS ? AND page_end IS ?)
            OR (
                ? IS NULL AND ? IS NULL
                AND page_start IS NULL AND page_end IS NULL
            )
          )
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_pdf, page_start, page_end, page_start, page_end),
    ).fetchone()
    return str(row["review_flags_json"] or "[]") if row else "[]"


def _run_detector(
    source_pdf: str,
    *,
    max_pages: int,
    start_page: int | None = None,
    end_page: int | None = None,
) -> dict[str, Any]:
    pdf_path = Path(source_pdf)
    if not pdf_path.exists():
        return {
            "is_redline": False,
            "confidence": 0.0,
            "signals": ["missing_source_pdf"],
            "red_text_samples": [],
            "strikethrough_samples": [],
            "red_is_index_only": False,
            "error": f"Missing PDF: {source_pdf}",
        }
    try:
        result = detect_redline(
            str(pdf_path),
            max_pages=max_pages,
            start_page=start_page,
            end_page=end_page,
        )
        return {
            "is_redline": bool(result.is_redline),
            "confidence": float(result.confidence),
            "signals": list(result.signals),
            "red_text_samples": list(result.red_text_samples),
            "strikethrough_samples": list(result.strikethrough_samples),
            "red_is_index_only": bool(result.red_is_index_only),
            "error": None,
        }
    except Exception as exc:
        return {
            "is_redline": False,
            "confidence": 0.0,
            "signals": [],
            "red_text_samples": [],
            "strikethrough_samples": [],
            "red_is_index_only": False,
            "error": str(exc),
        }


def _schedule_code_for_family_key(family_key: str) -> str | None:
    normalized = family_key.lower()
    if "-schedule-" in normalized:
        return normalized.split("-schedule-", 1)[1].upper()
    if normalized.endswith("-summary"):
        return "SUMMARY_OF_RIDERS"
    return None


def _upsert_fingerprint_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    current: _DocFingerprintRow,
    detector: dict[str, Any],
    review_flags_json: str,
) -> None:
    page_start = int(row["start_page"]) if row["start_page"] is not None else None
    page_end = int(row["end_page"]) if row["end_page"] is not None else None
    params = (
        int(bool(detector["is_redline"])),
        round(float(detector["confidence"] or 0.0), 4),
        _DETECTOR_VERSION,
        json.dumps(list(detector["signals"]), sort_keys=True),
        json.dumps(list(detector["red_text_samples"][:5]), sort_keys=True),
        json.dumps(list(detector["strikethrough_samples"][:5]), sort_keys=True),
        int(bool(detector["red_is_index_only"])),
        review_flags_json,
    )
    if current.id is not None:
        conn.execute(
            """
            UPDATE document_fingerprints
            SET is_redline_candidate = ?,
                redline_confidence = ?,
                redline_detector_version = ?,
                redline_signals_json = ?,
                red_text_samples_json = ?,
                strikethrough_samples_json = ?,
                red_is_index_only = ?,
                review_flags_json = ?
            WHERE id = ?
            """,
            params + (current.id,),
        )
        return

    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO document_fingerprints (
            source_pdf, docket_dir, page_start, page_end, leaf_no, schedule_code, title,
            text_length, line_count, numeric_line_count, has_table_rows, has_rider_summary,
            is_redline_candidate, redline_confidence, redline_detector_version,
            redline_signals_json, red_text_samples_json, strikethrough_samples_json,
            red_is_index_only, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(row["local_path"]),
            None,
            page_start,
            page_end,
            row["leaf_no"],
            _schedule_code_for_family_key(str(row["family_key"] or "")),
            row["title"],
            0,
            0,
            0,
            0,
            0,
            *params[:-1],
            review_flags_json,
            json.dumps(
                {
                    "family_key": row["family_key"],
                    "historical_document_id": int(row["id"]),
                    "redline_refresh_created": True,
                },
                sort_keys=True,
            ),
            now,
        ),
    )


def _update_review_flags(review_flags_json: str | None, *, is_redline: bool) -> str:
    try:
        flags = list(json.loads(review_flags_json or "[]"))
    except json.JSONDecodeError:
        flags = []
    filtered = [flag for flag in flags if flag != "redline_candidate"]
    if is_redline:
        filtered.append("redline_candidate")
    return json.dumps(filtered, sort_keys=True)


__all__ = ["refresh_nc_redline_fingerprints"]
