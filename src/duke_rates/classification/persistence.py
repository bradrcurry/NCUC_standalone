"""Persist ClassificationResult instances to ``document_classifications``.

Idempotent on ``(subject_kind, subject_id, stage, classifier, classifier_version)``.
Re-running a classifier with the same version does not duplicate rows.
A new ``classifier_version`` produces a new row, leaving the prior one
intact for audit; callers can mark prior rows ``superseded_by`` the new one
when overlaying a second-opinion classifier.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from duke_rates.classification.result import ClassificationResult


def record_classification(
    conn: sqlite3.Connection,
    *,
    subject_kind: str,
    subject_id: str,
    stage: str,
    result: ClassificationResult,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Upsert one classification row. Returns the row id.

    The UNIQUE constraint on (subject_kind, subject_id, stage, classifier,
    classifier_version) makes this idempotent — repeated calls update
    confidence/evidence/alternatives but never create duplicates.
    """
    now = datetime.now(UTC).isoformat()
    evidence_json = json.dumps(result.evidence, sort_keys=True) if result.evidence else None
    alternatives_json = (
        json.dumps([list(item) for item in result.alternatives])
        if result.alternatives else None
    )
    merged_meta = dict(result.metadata or {})
    if metadata:
        merged_meta.update(metadata)
    metadata_json = json.dumps(merged_meta, sort_keys=True) if merged_meta else None

    existing = conn.execute(
        """
        SELECT id FROM document_classifications
        WHERE subject_kind = ? AND subject_id = ? AND stage = ?
          AND classifier = ? AND classifier_version = ?
        """,
        (subject_kind, subject_id, stage, result.classifier, result.classifier_version),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE document_classifications
            SET label = ?, confidence = ?, evidence_json = ?,
                alternatives_json = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                result.label, result.confidence, evidence_json,
                alternatives_json, metadata_json, existing["id"],
            ),
        )
        return int(existing["id"])

    cur = conn.execute(
        """
        INSERT INTO document_classifications (
            subject_kind, subject_id, stage, label, confidence,
            classifier, classifier_version, evidence_json, alternatives_json,
            metadata_json, superseded_by, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)
        """,
        (
            subject_kind, subject_id, stage, result.label, result.confidence,
            result.classifier, result.classifier_version, evidence_json,
            alternatives_json, metadata_json, now,
        ),
    )
    return int(cur.lastrowid)


def supersede_prior_classifications(
    conn: sqlite3.Connection,
    *,
    subject_kind: str,
    subject_id: str,
    stage: str,
    superseded_by_id: int,
) -> int:
    """Mark all prior active classifications for a subject+stage as superseded.

    Returns the number of rows updated. Used when a higher-authority
    classifier (e.g. an LLM second opinion or a human review) overrides
    the rule-based decision.
    """
    cur = conn.execute(
        """
        UPDATE document_classifications
        SET superseded_by = ?
        WHERE subject_kind = ? AND subject_id = ? AND stage = ?
          AND id != ? AND superseded_by IS NULL
        """,
        (superseded_by_id, subject_kind, subject_id, stage, superseded_by_id),
    )
    return cur.rowcount or 0
