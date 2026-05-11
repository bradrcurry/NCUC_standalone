"""
Sub-document section identity layer (Phase 6A of parsing-architecture refactor).

Tracks page-range sections within a document — which pages belong to which
rate schedule, rider, or other content type — with confidence scores and an
append-only evidence log.

This layer is **read-only output** in Phase 6A: it does not change extraction
behavior. Future phases consume section bundles to improve text selection,
template binding, and extraction routing.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §6.

Usage::

    from duke_rates.document_intelligence.document_sections import (
        DocumentSectionAggregator, ensure_schema,
    )
    ensure_schema(db_path)
    agg = DocumentSectionAggregator(db_path)
    n = agg.populate_all()
    print(f"populated {n} sections")
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL_DOCUMENT_SECTIONS = """
CREATE TABLE IF NOT EXISTS document_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    start_page INTEGER NOT NULL,
    end_page INTEGER NOT NULL,
    section_type TEXT NOT NULL DEFAULT 'unknown',
    schedule_codes_json TEXT NOT NULL DEFAULT '[]',
    rider_codes_json TEXT NOT NULL DEFAULT '[]',
    leaf_numbers_json TEXT NOT NULL DEFAULT '[]',
    detected_titles_json TEXT NOT NULL DEFAULT '[]',
    overall_confidence REAL NOT NULL DEFAULT 0.0,
    evidence_log_json TEXT NOT NULL DEFAULT '[]',
    parent_section_index INTEGER,
    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_pdf, section_index)
);
"""

_DDL_INDEX_SECTIONS_PDF = (
    "CREATE INDEX IF NOT EXISTS idx_document_sections_pdf "
    "ON document_sections(source_pdf);"
)
_DDL_INDEX_SECTIONS_CONFIDENCE = (
    "CREATE INDEX IF NOT EXISTS idx_document_sections_confidence "
    "ON document_sections(overall_confidence DESC);"
)


def ensure_schema(db_path: Path | str) -> None:
    """Create the ``document_sections`` table and its indexes if missing.

    Idempotent — safe to call from any module.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(_DDL_DOCUMENT_SECTIONS)
        conn.execute(_DDL_INDEX_SECTIONS_PDF)
        conn.execute(_DDL_INDEX_SECTIONS_CONFIDENCE)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("document_sections schema bootstrap failed", exc_info=True)


# ---------------------------------------------------------------------------
# Section type enumeration
# ---------------------------------------------------------------------------


class SectionType(StrEnum):
    RATE_SCHEDULE = "rate_schedule"
    RIDER = "rider"
    TERMS_CONDITIONS = "terms_conditions"
    COVER_LETTER = "cover_letter"
    TABLE_OF_CONTENTS = "table_of_contents"
    PROCEDURAL = "procedural"
    UNKNOWN = "unknown"


# Section types that contain rate values (used for text selection)
RATE_SECTION_TYPES: set[SectionType] = {SectionType.RATE_SCHEDULE, SectionType.RIDER}


# ---------------------------------------------------------------------------
# Confidence weights (additive, parallel to document_identity weights)
# ---------------------------------------------------------------------------

WEIGHT_SECTION_LEAF_MATCH: float = 0.25
WEIGHT_SECTION_CODE_MATCH: float = 0.25
WEIGHT_SECTION_TYPE_CLEAR: float = 0.20
WEIGHT_SECTION_SPAN_AGREE: float = 0.15
WEIGHT_SECTION_RATE_VALUES: float = 0.15


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class SectionBundle:
    source_pdf: str
    section_index: int
    start_page: int
    end_page: int
    section_type: SectionType = SectionType.UNKNOWN
    schedule_codes: list[str] = field(default_factory=list)
    rider_codes: list[str] = field(default_factory=list)
    leaf_numbers: list[str] = field(default_factory=list)
    detected_titles: list[str] = field(default_factory=list)
    overall_confidence: float = 0.0
    evidence_log: list[dict[str, Any]] = field(default_factory=list)
    parent_section_index: int | None = None

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    @property
    def is_rate_relevant(self) -> bool:
        return self.section_type in RATE_SECTION_TYPES

    def to_persistence_tuple(self) -> tuple[Any, ...]:
        return (
            self.source_pdf,
            self.section_index,
            self.start_page,
            self.end_page,
            self.section_type.value if isinstance(self.section_type, SectionType) else str(self.section_type),
            json.dumps(self.schedule_codes),
            json.dumps(self.rider_codes),
            json.dumps(self.leaf_numbers),
            json.dumps(self.detected_titles),
            self.overall_confidence,
            json.dumps(self.evidence_log, default=str),
            self.parent_section_index,
            datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SectionBundle:
        """Build a SectionBundle from a database row."""

        def _json_list(raw: str | None) -> list[str]:
            try:
                val = json.loads(raw or "[]")
                return val if isinstance(val, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        def _json_list_of_dicts(raw: str | None) -> list[dict[str, Any]]:
            try:
                val = json.loads(raw or "[]")
                return val if isinstance(val, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        st = row["section_type"]
        try:
            section_type = SectionType(st) if st else SectionType.UNKNOWN
        except ValueError:
            section_type = SectionType.UNKNOWN

        return cls(
            source_pdf=row["source_pdf"],
            section_index=row["section_index"],
            start_page=row["start_page"],
            end_page=row["end_page"],
            section_type=section_type,
            schedule_codes=_json_list(row["schedule_codes_json"]),
            rider_codes=_json_list(row["rider_codes_json"]),
            leaf_numbers=_json_list(row["leaf_numbers_json"]),
            detected_titles=_json_list(row["detected_titles_json"]),
            overall_confidence=float(row["overall_confidence"] or 0.0),
            evidence_log=_json_list_of_dicts(row["evidence_log_json"]),
            parent_section_index=row["parent_section_index"],
        )


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------


def fetch_sections(
    db_path: Path | str,
    source_pdf: str,
    *,
    min_confidence: float | None = None,
) -> list[SectionBundle]:
    """Return all sections for a document, optionally filtered by confidence."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if min_confidence is not None:
            rows = conn.execute(
                """SELECT * FROM document_sections
                   WHERE source_pdf = ? AND overall_confidence >= ?
                   ORDER BY section_index""",
                (source_pdf, min_confidence),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM document_sections
                   WHERE source_pdf = ?
                   ORDER BY section_index""",
                (source_pdf,),
            ).fetchall()
        return [SectionBundle.from_row(r) for r in rows]
    finally:
        conn.close()


def fetch_rate_sections(
    db_path: Path | str,
    source_pdf: str,
    *,
    min_confidence: float | None = None,
) -> list[SectionBundle]:
    """Return only rate-relevant sections (rate_schedule, rider) sorted by confidence desc."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rate_types = ",".join(f"'{t.value}'" for t in RATE_SECTION_TYPES)
        if min_confidence is not None:
            rows = conn.execute(
                f"""SELECT * FROM document_sections
                    WHERE source_pdf = ? AND section_type IN ({rate_types})
                    AND overall_confidence >= ?
                    ORDER BY overall_confidence DESC""",
                (source_pdf, min_confidence),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT * FROM document_sections
                    WHERE source_pdf = ? AND section_type IN ({rate_types})
                    ORDER BY overall_confidence DESC""",
                (source_pdf,),
            ).fetchall()
        return [SectionBundle.from_row(r) for r in rows]
    finally:
        conn.close()


def fetch_sections_summary(db_path: Path | str) -> dict[str, Any]:
    """Return aggregate statistics for the document_sections table."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM document_sections").fetchone()
        type_counts = conn.execute(
            """SELECT section_type, COUNT(*) AS n
               FROM document_sections
               GROUP BY section_type
               ORDER BY n DESC"""
        ).fetchall()
        confidence_buckets = conn.execute(
            """SELECT
                 CASE
                   WHEN overall_confidence >= 0.85 THEN 'high'
                   WHEN overall_confidence >= 0.50 THEN 'mid'
                   ELSE 'low'
                 END AS bucket,
                 COUNT(*) AS n
               FROM document_sections
               GROUP BY bucket
               ORDER BY bucket"""
        ).fetchall()
        doc_count = conn.execute(
            "SELECT COUNT(DISTINCT source_pdf) AS n FROM document_sections"
        ).fetchone()
        return {
            "total_sections": total["n"] if total else 0,
            "total_documents": doc_count["n"] if doc_count else 0,
            "type_distribution": {r["section_type"]: r["n"] for r in type_counts},
            "confidence_histogram": {r["bucket"]: r["n"] for r in confidence_buckets},
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------


def upsert_section(db_path: Path | str, bundle: SectionBundle) -> int:
    """Insert or update a section row. Returns the row id."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""DELETE FROM document_sections
                        WHERE source_pdf = ? AND section_index = ?""",
                     (bundle.source_pdf, bundle.section_index))
        conn.execute(
            """INSERT INTO document_sections
               (source_pdf, section_index, start_page, end_page, section_type,
                schedule_codes_json, rider_codes_json, leaf_numbers_json,
                detected_titles_json, overall_confidence, evidence_log_json,
                parent_section_index, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            bundle.to_persistence_tuple(),
        )
        conn.commit()
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return row_id
    finally:
        conn.close()


def delete_sections_for_pdf(db_path: Path | str, source_pdf: str) -> int:
    """Delete all sections for a document. Returns count of deleted rows."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "DELETE FROM document_sections WHERE source_pdf = ?", (source_pdf,)
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
