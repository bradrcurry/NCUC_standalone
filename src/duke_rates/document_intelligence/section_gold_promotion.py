"""Section-level gold promotion (Phase A2 of the ML training roadmap).

The whole-document ``document_type_gold`` table (441 rows, heavily skewed
toward TARIFF_SHEET) is too coarse for compliance bundles — a 339-section
filing becomes one training example instead of 339. This module promotes
high-confidence section-level classifications from ``document_sections``
into a new ``section_type_gold`` table that mirrors the per-PDF gold
schema but operates at the section level.

Promotion criteria (configurable via CLI flags):

  1. The section's ``overall_confidence`` clears a per-section-type
     threshold (rate_schedule needs >=0.75; procedural sections can pass
     at >=0.5 because their evidence weights are structurally lower).
  2. The section's ``section_type`` is not ``unknown``.
  3. At least ``min_classifiers_agreed`` (default 2) classifiers
     agree — the section_aggregator counts as one, and each doc-level
     classifier whose label maps consistently to the section's type
     counts as one more.
  4. The section's type is consistent with the parent document's
     classifier consensus (e.g., a section labeled rate_schedule
     inside a doc the LLM called ORDER_PROCEDURAL is rejected as a
     conflict — and logged to ``section_classification_conflicts``
     for triage).
  5. No active gold row for the same (source_pdf, section_index)
     with a consistent label already exists.

This module is read-mostly. The only writes are INSERTs into
``section_type_gold`` and (optionally) ``section_classification_conflicts``.
It never mutates ``document_sections`` or ``document_classifications``.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §A2 (this
module is part of the wider ML training data roadmap; see the
evaluation in chat log 2026-05-25).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL_SECTION_TYPE_GOLD = """
CREATE TABLE IF NOT EXISTS section_type_gold (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    source_pdf TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    start_page INTEGER NOT NULL,
    end_page INTEGER NOT NULL,

    section_type TEXT NOT NULL,
    schedule_code TEXT,
    rider_code TEXT,
    leaf_numbers_json TEXT NOT NULL DEFAULT '[]',
    effective_start TEXT,
    is_redline INTEGER,
    is_compliance INTEGER,
    is_final INTEGER,
    customer_class TEXT,

    gold_source TEXT NOT NULL,
    classifiers_agreed_json TEXT NOT NULL DEFAULT '[]',
    n_classifiers_agreed INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL,

    evidence_log_json TEXT NOT NULL DEFAULT '[]',
    promoted_at TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_by TEXT,

    superseded_by INTEGER REFERENCES section_type_gold(id) ON DELETE SET NULL
);
"""

# One active row per (source_pdf, section_index). Older rows have
# superseded_by set to the new row's id; new rows have NULL.
_DDL_INDEX_ACTIVE = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_section_gold_active
ON section_type_gold(source_pdf, section_index)
WHERE superseded_by IS NULL;
"""

_DDL_INDEX_TYPE = """
CREATE INDEX IF NOT EXISTS idx_section_gold_type
ON section_type_gold(section_type);
"""

_DDL_INDEX_SOURCE = """
CREATE INDEX IF NOT EXISTS idx_section_gold_source
ON section_type_gold(gold_source);
"""


# Companion table: log rejections caused by classifier disagreement.
# These are the most valuable inputs to active learning — they're
# exactly the cases the LLM adjudicator should look at next.
_DDL_SECTION_CONFLICTS = """
CREATE TABLE IF NOT EXISTS section_classification_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    section_type TEXT NOT NULL,
    doc_consensus_types_json TEXT NOT NULL,
    section_confidence REAL,
    doc_classifier_labels_json TEXT,
    reject_reason TEXT NOT NULL,
    seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved INTEGER NOT NULL DEFAULT 0
);
"""

_DDL_INDEX_CONFLICTS_PDF = """
CREATE INDEX IF NOT EXISTS idx_section_conflicts_pdf
ON section_classification_conflicts(source_pdf, section_index);
"""

_DDL_INDEX_CONFLICTS_UNRESOLVED = """
CREATE INDEX IF NOT EXISTS idx_section_conflicts_unresolved
ON section_classification_conflicts(resolved, seen_at DESC);
"""


def ensure_schema(db_path: Path | str) -> None:
    """Create the section_type_gold + conflicts tables if missing."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(_DDL_SECTION_TYPE_GOLD)
            conn.execute(_DDL_INDEX_ACTIVE)
            conn.execute(_DDL_INDEX_TYPE)
            conn.execute(_DDL_INDEX_SOURCE)
            conn.execute(_DDL_SECTION_CONFLICTS)
            conn.execute(_DDL_INDEX_CONFLICTS_PDF)
            conn.execute(_DDL_INDEX_CONFLICTS_UNRESOLVED)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("section_type_gold schema bootstrap failed", exc_info=True)


# ---------------------------------------------------------------------------
# Per-section-type confidence floors
# ---------------------------------------------------------------------------

# The section_aggregator's confidence weights reward leaf/code/rate-value
# matches, so rate-bearing sections naturally reach higher confidence than
# procedural ones. Use type-specific floors so we can promote well-classified
# procedural/cover-letter sections without lowering the bar for rate sections.
TYPE_CONFIDENCE_FLOORS: dict[str, float] = {
    "rate_schedule": 0.75,
    "rider": 0.70,
    "terms_conditions": 0.55,
    "cover_letter": 0.45,
    "table_of_contents": 0.45,
    "procedural": 0.45,
    "unknown": 1.01,  # never promote unknown sections
}

DEFAULT_CONFIDENCE_FLOOR = 0.55


# ---------------------------------------------------------------------------
# Doc-type → section-type consistency map
# ---------------------------------------------------------------------------

# When a doc-level classifier labels the PDF as X, which section_types
# are consistent at the section level? Empty set means "don't constrain"
# (used for ambiguous doc-level labels like UNKNOWN or COMPLIANCE_FILING).
DOC_TO_SECTION_TYPES: dict[str, set[str]] = {
    "TARIFF_SHEET":        {"rate_schedule", "rider", "terms_conditions"},
    "RIDER":               {"rider"},
    "RATE_SCHEDULE":       {"rate_schedule", "terms_conditions"},
    "ORDER_FINAL":         {"procedural", "cover_letter"},
    "ORDER_PROCEDURAL":    {"procedural"},
    "TESTIMONY":           {"procedural"},
    "COVER_LETTER":        {"cover_letter"},
    "CERTIFICATE_OF_SERVICE": {"cover_letter", "procedural"},
    "NOTICE_OF_HEARING":   {"cover_letter", "procedural"},
    "APPLICATION":         {"procedural", "cover_letter"},
    "COMPLIANCE_FILING":   set(),  # bundles can contain anything
    "FERC_ORDER":          {"procedural"},
    "EIA_REPORT":          set(),
    "UNKNOWN":             set(),
}


# ---------------------------------------------------------------------------
# Canonicalization (minimal — Phase A1 will replace this with a full alias map)
# ---------------------------------------------------------------------------

# Stop-word codes that the section_aggregator regex sometimes captures
# but aren't real schedule/rider codes.
_CODE_STOP_WORDS: frozenset[str] = frozenset({
    "CLASS", "DEPENDING", "RIDER", "SCHEDULE", "NORTH", "CAROLINA",
    "ELECTRIC", "PROGRESS", "DUKE", "ENERGY", "NC", "SC",
})


def _canonicalize_code(raw: str | None) -> str | None:
    """Minimal canonicalization. Returns None for empty/stop-word inputs.

    Phase A1 will replace this with a full alias map keyed by company.
    For now: strip, uppercase, drop pure stop-words and lone single
    digits without context, normalize "RIDER FOO"/"FOO RIDER"/"foo rider"
    to "FOO".
    """
    if not raw:
        return None
    s = raw.strip().upper()
    # Strip "RIDER" prefix/suffix
    if s.startswith("RIDER "):
        s = s[len("RIDER "):].strip()
    if s.endswith(" RIDER"):
        s = s[:-len(" RIDER")].strip()
    if not s:
        return None
    # Drop stop-words
    if s in _CODE_STOP_WORDS:
        return None
    # Drop lone single-digit numbers (likely captured by greedy regex)
    if s.isdigit() and len(s) <= 2:
        return None
    return s


def _first_canonical_code(codes_json: str | None) -> str | None:
    """Return the first canonicalized code from a JSON array, or None."""
    if not codes_json:
        return None
    try:
        codes = json.loads(codes_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(codes, list):
        return None
    for c in codes:
        canon = _canonicalize_code(c if isinstance(c, str) else None)
        if canon:
            return canon
    return None


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class PromotionResult:
    """Result of evaluating one section for promotion."""
    source_pdf: str
    section_index: int
    promoted: bool
    section_type: str | None = None
    schedule_code: str | None = None
    rider_code: str | None = None
    confidence: float = 0.0
    n_classifiers_agreed: int = 0
    agreeing_classifiers: list[str] = field(default_factory=list)
    reject_reason: str | None = None
    is_conflict: bool = False  # rejection caused by doc-vs-section disagreement


@dataclass
class PromotionRun:
    """Aggregate result of a batch promotion run."""
    candidates_evaluated: int = 0
    promoted: int = 0
    skipped_already_gold: int = 0
    skipped_low_confidence: int = 0
    skipped_no_consensus: int = 0
    rejected_conflict: int = 0
    rejected_other: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    sample_promotions: list[PromotionResult] = field(default_factory=list)
    sample_conflicts: list[PromotionResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


def _doc_type_consensus(
    doc_classifications: list[dict[str, Any]],
) -> tuple[set[str], list[dict[str, Any]]]:
    """Return (allowed_section_types, raw_labels).

    allowed_section_types is the UNION of acceptable section_types
    across all doc-level classifiers. Empty set means "no constraint"
    (either no active classifications, or all classifiers had
    "don't constrain" mappings).
    """
    allowed: set[str] = set()
    raw_labels: list[dict[str, Any]] = []
    constrained = False
    for c in doc_classifications:
        label = c.get("label") or ""
        raw_labels.append({"classifier": c.get("classifier"), "label": label})
        mapping = DOC_TO_SECTION_TYPES.get(label)
        if mapping is None:
            # Unknown doc-level label; treat as no-constraint
            continue
        if not mapping:
            # Explicitly no constraint
            continue
        constrained = True
        allowed |= mapping
    return (allowed if constrained else set(), raw_labels)


def _classifier_label_consistent(
    classifier_label: str,
    section_type: str,
) -> bool:
    """True iff the doc-level label maps to (or is unconstrained for)
    the section's type."""
    mapping = DOC_TO_SECTION_TYPES.get(classifier_label)
    if mapping is None:
        return False  # unknown label — don't credit
    if not mapping:
        # Unconstrained labels (COMPLIANCE_FILING, UNKNOWN) don't add
        # *positive* evidence but don't contradict either. Treat as
        # "neutral" — not counted as agreeing.
        return False
    return section_type in mapping


def _confidence_floor_for(section_type: str) -> float:
    return TYPE_CONFIDENCE_FLOORS.get(section_type, DEFAULT_CONFIDENCE_FLOOR)


def evaluate_section(
    section: dict[str, Any],
    doc_classifications: list[dict[str, Any]],
    existing_gold: dict[str, Any] | None,
    *,
    min_classifiers_agreed: int = 2,
    min_section_confidence_override: float | None = None,
) -> PromotionResult:
    """Pure evaluation — no DB writes, no side effects.

    Inputs:
      section: row from document_sections (must include section_type,
        overall_confidence, schedule_codes_json, rider_codes_json,
        leaf_numbers_json, start_page, end_page).
      doc_classifications: active rows from document_classifications
        for this section's source_pdf at stage='document_type'.
      existing_gold: active row from section_type_gold for the same
        (source_pdf, section_index), or None.
    """
    source_pdf = section["source_pdf"]
    section_index = section["section_index"]
    section_type = section.get("section_type") or "unknown"
    section_conf = float(section.get("overall_confidence") or 0.0)

    result = PromotionResult(
        source_pdf=source_pdf,
        section_index=section_index,
        promoted=False,
        section_type=section_type,
        confidence=section_conf,
    )

    # Gate 1: type must be promotable
    if section_type == "unknown":
        result.reject_reason = "section_type=unknown"
        return result

    # Gate 2: confidence floor
    floor = min_section_confidence_override
    if floor is None:
        floor = _confidence_floor_for(section_type)
    if section_conf < floor:
        result.reject_reason = f"section_confidence={section_conf:.2f} < floor={floor:.2f}"
        return result

    # Gate 3: consistency with doc-level consensus
    allowed_types, raw_labels = _doc_type_consensus(doc_classifications)
    if allowed_types and section_type not in allowed_types:
        result.reject_reason = (
            f"section_type={section_type} not in doc_consensus="
            f"{sorted(allowed_types)} (labels={raw_labels})"
        )
        result.is_conflict = True
        return result

    # Gate 4: classifier agreement count
    agreeing = ["section_aggregator_v1"]
    for c in doc_classifications:
        label = c.get("label") or ""
        if _classifier_label_consistent(label, section_type):
            agreeing.append(c.get("classifier") or "unknown_classifier")

    if len(agreeing) < min_classifiers_agreed:
        result.reject_reason = (
            f"only_{len(agreeing)}_classifiers_agreed"
            f"_need_{min_classifiers_agreed}"
        )
        return result

    # Gate 5: idempotency — already in gold with the same label
    if existing_gold is not None:
        if existing_gold.get("section_type") == section_type:
            result.reject_reason = "already_gold_consistent"
            return result
        # Different label — this is a re-promotion (we'll supersede)

    # Compute promotion confidence (min of section confidence and
    # the strongest agreeing doc-level classifier's confidence)
    doc_conf_max = 0.0
    for c in doc_classifications:
        if (c.get("classifier") or "") in agreeing:
            cc = float(c.get("confidence") or 0.0)
            doc_conf_max = max(doc_conf_max, cc)
    if doc_conf_max == 0.0:
        # Only section_aggregator was the agreeing party; use its conf
        promotion_conf = section_conf
    else:
        promotion_conf = min(section_conf, doc_conf_max)

    # Canonicalize codes
    schedule_code = (
        _first_canonical_code(section.get("schedule_codes_json"))
        if section_type == "rate_schedule" else None
    )
    rider_code = (
        _first_canonical_code(section.get("rider_codes_json"))
        if section_type == "rider" else None
    )

    result.promoted = True
    result.confidence = promotion_conf
    result.n_classifiers_agreed = len(agreeing)
    result.agreeing_classifiers = agreeing
    result.schedule_code = schedule_code
    result.rider_code = rider_code
    return result


# ---------------------------------------------------------------------------
# DB access helpers
# ---------------------------------------------------------------------------


def _fetch_candidate_sections(
    conn: sqlite3.Connection,
    *,
    section_types: list[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(section_types))
    sql = f"""
        SELECT
            id,
            source_pdf,
            section_index,
            start_page,
            end_page,
            section_type,
            schedule_codes_json,
            rider_codes_json,
            leaf_numbers_json,
            overall_confidence
        FROM document_sections
        WHERE section_type IN ({placeholders})
        ORDER BY overall_confidence DESC, source_pdf, section_index
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql, tuple(section_types))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def _fetch_doc_classifications(
    conn: sqlite3.Connection,
    source_pdf: str,
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT classifier, label, confidence, classifier_version
        FROM document_classifications
        WHERE subject_kind = 'document'
          AND subject_id = ?
          AND stage = 'document_type'
          AND superseded_by IS NULL
        """,
        (source_pdf,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def _fetch_active_gold(
    conn: sqlite3.Connection,
    source_pdf: str,
    section_index: int,
) -> dict[str, Any] | None:
    cur = conn.execute(
        """
        SELECT id, section_type, schedule_code, rider_code, confidence
        FROM section_type_gold
        WHERE source_pdf = ? AND section_index = ? AND superseded_by IS NULL
        """,
        (source_pdf, section_index),
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def _insert_gold(
    conn: sqlite3.Connection,
    section: dict[str, Any],
    promotion: PromotionResult,
    *,
    gold_source: str,
    promoted_by: str | None,
    evidence_log: list[dict[str, Any]] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO section_type_gold (
            source_pdf, section_index, start_page, end_page,
            section_type, schedule_code, rider_code, leaf_numbers_json,
            gold_source, classifiers_agreed_json, n_classifiers_agreed,
            confidence, evidence_log_json, promoted_by
        ) VALUES (?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?)
        """,
        (
            section["source_pdf"],
            section["section_index"],
            section["start_page"],
            section["end_page"],
            promotion.section_type,
            promotion.schedule_code,
            promotion.rider_code,
            section.get("leaf_numbers_json") or "[]",
            gold_source,
            json.dumps(promotion.agreeing_classifiers),
            promotion.n_classifiers_agreed,
            promotion.confidence,
            json.dumps(evidence_log or []),
            promoted_by,
        ),
    )
    new_id = int(cur.lastrowid or 0)
    cur.close()
    return new_id


def _supersede_existing(
    conn: sqlite3.Connection,
    old_id: int,
    new_id: int,
) -> None:
    conn.execute(
        "UPDATE section_type_gold SET superseded_by = ? WHERE id = ?",
        (new_id, old_id),
    )


def _insert_conflict(
    conn: sqlite3.Connection,
    section: dict[str, Any],
    promotion: PromotionResult,
    doc_labels: list[dict[str, Any]],
) -> None:
    conn.execute(
        """
        INSERT INTO section_classification_conflicts (
            source_pdf, section_index, section_type,
            doc_consensus_types_json, section_confidence,
            doc_classifier_labels_json, reject_reason
        ) VALUES (?,?,?, ?,?, ?,?)
        """,
        (
            section["source_pdf"],
            section["section_index"],
            promotion.section_type or "",
            json.dumps([]),  # could store allowed_types here if we re-compute
            float(section.get("overall_confidence") or 0.0),
            json.dumps(doc_labels),
            promotion.reject_reason or "",
        ),
    )


# ---------------------------------------------------------------------------
# Batch promotion entry point
# ---------------------------------------------------------------------------

DEFAULT_PROMOTABLE_TYPES = (
    "rate_schedule",
    "rider",
    "terms_conditions",
    "cover_letter",
    "procedural",
)


def promote_sections(
    db_path: Path | str,
    *,
    section_types: list[str] | None = None,
    min_classifiers_agreed: int = 2,
    min_section_confidence_override: float | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    gold_source: str = "auto_promotion",
    promoted_by: str | None = None,
    log_conflicts: bool = True,
) -> PromotionRun:
    """Batch-promote eligible sections from document_sections to gold.

    Returns aggregate counts plus a small sample of promotions and
    conflicts for the operator to spot-check. Idempotent — running
    twice does not double-promote.

    Parameters
    ----------
    db_path : path to the SQLite DB.
    section_types : restrict candidate set to these section_types.
        Default: all common types except 'unknown'.
    min_classifiers_agreed : minimum count of agreeing classifiers
        (section_aggregator counts as 1).
    min_section_confidence_override : override the per-type floor with
        a single global threshold. Useful for one-off experiments;
        leave None for production runs.
    limit : cap candidates evaluated.
    dry_run : when True, no INSERTs are performed (default for safety).
    gold_source : value written to section_type_gold.gold_source.
    promoted_by : free-text label for who/what triggered this run.
    log_conflicts : when True, rejected-as-conflict candidates are
        written to section_classification_conflicts for triage.
    """
    ensure_schema(db_path)
    db_path = str(db_path)
    section_types = list(section_types or DEFAULT_PROMOTABLE_TYPES)
    run = PromotionRun()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sections = _fetch_candidate_sections(
            conn, section_types=section_types, limit=limit,
        )
        run.candidates_evaluated = len(sections)

        for section in sections:
            classifications = _fetch_doc_classifications(conn, section["source_pdf"])
            existing = _fetch_active_gold(conn, section["source_pdf"], section["section_index"])
            promotion = evaluate_section(
                section,
                classifications,
                existing,
                min_classifiers_agreed=min_classifiers_agreed,
                min_section_confidence_override=min_section_confidence_override,
            )

            if promotion.promoted:
                if not dry_run:
                    # The partial unique index allows only one active
                    # row per (source_pdf, section_index). If we have
                    # an existing row to supersede, mark it inactive
                    # FIRST (with a sentinel) so the new insert
                    # doesn't collide; then fix the sentinel with
                    # the real new_id after insert.
                    if existing is not None:
                        conn.execute(
                            "UPDATE section_type_gold SET superseded_by = -1 WHERE id = ?",
                            (existing["id"],),
                        )
                    new_id = _insert_gold(
                        conn,
                        section,
                        promotion,
                        gold_source=gold_source,
                        promoted_by=promoted_by,
                    )
                    if existing is not None:
                        _supersede_existing(conn, existing["id"], new_id)
                run.promoted += 1
                run.by_type[promotion.section_type or "unknown"] = (
                    run.by_type.get(promotion.section_type or "unknown", 0) + 1
                )
                if len(run.sample_promotions) < 5:
                    run.sample_promotions.append(promotion)
            else:
                reason = promotion.reject_reason or "unknown"
                if reason == "already_gold_consistent":
                    run.skipped_already_gold += 1
                elif reason.startswith("section_confidence="):
                    run.skipped_low_confidence += 1
                elif reason.startswith("only_") and "classifiers_agreed" in reason:
                    run.skipped_no_consensus += 1
                elif promotion.is_conflict:
                    run.rejected_conflict += 1
                    if log_conflicts and not dry_run:
                        _, raw_labels = _doc_type_consensus(classifications)
                        _insert_conflict(conn, section, promotion, raw_labels)
                    if len(run.sample_conflicts) < 5:
                        run.sample_conflicts.append(promotion)
                else:
                    run.rejected_other += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return run
