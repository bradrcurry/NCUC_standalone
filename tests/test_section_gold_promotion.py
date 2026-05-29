"""Tests for the section-level gold promotion logic.

The promotion module is the foundation of the section-level training
corpus. Locking its decision rules with explicit fixtures lets future
work (adding new section_types, adjusting confidence floors) catch
regressions immediately.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from duke_rates.document_intelligence import section_gold_promotion as sgp


# ---------------------------------------------------------------------------
# Schema fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Build a minimal DB with document_sections + document_classifications
    populated and the gold/conflict tables created."""
    db_path = tmp_path / "promotion.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE document_sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL,
            section_index INTEGER NOT NULL,
            start_page INTEGER NOT NULL,
            end_page INTEGER NOT NULL,
            section_type TEXT NOT NULL DEFAULT 'unknown',
            schedule_codes_json TEXT NOT NULL DEFAULT '[]',
            rider_codes_json TEXT NOT NULL DEFAULT '[]',
            leaf_numbers_json TEXT NOT NULL DEFAULT '[]',
            overall_confidence REAL NOT NULL DEFAULT 0.0,
            UNIQUE(source_pdf, section_index)
        );
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            label TEXT NOT NULL,
            confidence REAL NOT NULL,
            classifier TEXT NOT NULL,
            classifier_version TEXT NOT NULL DEFAULT '',
            superseded_by INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()
    sgp.ensure_schema(db_path)
    return db_path


def _insert_section(
    db_path: Path,
    source_pdf: str,
    section_index: int,
    *,
    section_type: str,
    confidence: float,
    schedule_codes: list[str] | None = None,
    rider_codes: list[str] | None = None,
    leaf_numbers: list[str] | None = None,
    start_page: int = 1,
    end_page: int = 5,
) -> None:
    import json as _json
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO document_sections
        (source_pdf, section_index, start_page, end_page, section_type,
         schedule_codes_json, rider_codes_json, leaf_numbers_json,
         overall_confidence)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (source_pdf, section_index, start_page, end_page, section_type,
         _json.dumps(schedule_codes or []),
         _json.dumps(rider_codes or []),
         _json.dumps(leaf_numbers or []),
         confidence),
    )
    conn.commit()
    conn.close()


def _insert_classification(
    db_path: Path,
    source_pdf: str,
    *,
    classifier: str,
    label: str,
    confidence: float = 0.9,
    stage: str = "document_type",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO document_classifications
        (subject_kind, subject_id, stage, label, confidence, classifier, classifier_version)
        VALUES ('document', ?, ?, ?, ?, ?, 'v1')""",
        (source_pdf, stage, label, confidence, classifier),
    )
    conn.commit()
    conn.close()


def _count_gold(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM section_type_gold WHERE superseded_by IS NULL"
    ).fetchone()[0]
    conn.close()
    return int(n)


def _count_conflicts(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM section_classification_conflicts"
    ).fetchone()[0]
    conn.close()
    return int(n)


# ---------------------------------------------------------------------------
# Pure-function tests (no DB)
# ---------------------------------------------------------------------------


class TestCanonicalize:
    def test_rider_prefix_stripped(self):
        assert sgp._canonicalize_code("RIDER EDIT-4") == "EDIT-4"

    def test_rider_suffix_stripped(self):
        assert sgp._canonicalize_code("EMF rider") == "EMF"

    def test_stop_words_rejected(self):
        assert sgp._canonicalize_code("CLASS") is None
        assert sgp._canonicalize_code("DEPENDING") is None
        assert sgp._canonicalize_code("RIDER") is None

    def test_lone_single_digit_rejected(self):
        assert sgp._canonicalize_code("4") is None
        assert sgp._canonicalize_code("10") is None

    def test_real_code_passes(self):
        assert sgp._canonicalize_code("EDIT-4") == "EDIT-4"
        assert sgp._canonicalize_code("emf") == "EMF"

    def test_empty_input(self):
        assert sgp._canonicalize_code(None) is None
        assert sgp._canonicalize_code("") is None
        assert sgp._canonicalize_code("   ") is None

    def test_first_canonical_from_list(self):
        # First valid code wins; stop-words skipped
        assert sgp._first_canonical_code('["CLASS", "EDIT-4", "DEPENDING"]') == "EDIT-4"

    def test_first_canonical_handles_garbage(self):
        assert sgp._first_canonical_code(None) is None
        assert sgp._first_canonical_code("not json") is None
        assert sgp._first_canonical_code('{"not": "a list"}') is None


class TestDocTypeConsensus:
    def test_no_classifications_unconstrained(self):
        allowed, raw = sgp._doc_type_consensus([])
        assert allowed == set()
        assert raw == []

    def test_tariff_sheet_constrains(self):
        allowed, _ = sgp._doc_type_consensus(
            [{"classifier": "x", "label": "TARIFF_SHEET", "confidence": 0.9}]
        )
        assert allowed == {"rate_schedule", "rider", "terms_conditions"}

    def test_compliance_filing_unconstrains(self):
        # COMPLIANCE_FILING explicitly maps to empty -> no constraint
        allowed, _ = sgp._doc_type_consensus(
            [{"classifier": "x", "label": "COMPLIANCE_FILING", "confidence": 0.9}]
        )
        assert allowed == set()

    def test_unknown_label_treated_as_unconstrained(self):
        allowed, _ = sgp._doc_type_consensus(
            [{"classifier": "x", "label": "WHO_KNOWS", "confidence": 0.9}]
        )
        assert allowed == set()

    def test_multiple_classifiers_union(self):
        allowed, _ = sgp._doc_type_consensus([
            {"classifier": "a", "label": "TARIFF_SHEET", "confidence": 0.9},
            {"classifier": "b", "label": "ORDER_PROCEDURAL", "confidence": 0.8},
        ])
        # Union: tariff types + procedural
        assert allowed == {"rate_schedule", "rider", "terms_conditions", "procedural"}


# ---------------------------------------------------------------------------
# End-to-end promotion tests
# ---------------------------------------------------------------------------


class TestPromoteSections:
    def test_high_confidence_rate_schedule_promoted(self, seeded_db: Path):
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
            schedule_codes=["RES-1"], leaf_numbers=["41"],
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="TARIFF_SHEET")

        run = sgp.promote_sections(seeded_db, dry_run=False)

        assert run.promoted == 1
        assert run.by_type == {"rate_schedule": 1}
        assert _count_gold(seeded_db) == 1

    def test_low_confidence_section_skipped(self, seeded_db: Path):
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.4,  # below 0.75 floor
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")

        run = sgp.promote_sections(seeded_db, dry_run=False)
        assert run.promoted == 0
        assert run.skipped_low_confidence == 1
        assert _count_gold(seeded_db) == 0

    def test_doc_section_conflict_logged(self, seeded_db: Path):
        # Section says rate_schedule, doc-level says ORDER_PROCEDURAL
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="ORDER_PROCEDURAL")

        run = sgp.promote_sections(seeded_db, dry_run=False)

        assert run.promoted == 0
        assert run.rejected_conflict == 1
        assert _count_gold(seeded_db) == 0
        assert _count_conflicts(seeded_db) == 1

    def test_insufficient_classifiers_skipped(self, seeded_db: Path):
        # High-confidence section but ZERO doc-level classifiers
        # section_aggregator counts as 1 — need 2 by default — fail
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
        )
        # No classifications inserted

        run = sgp.promote_sections(seeded_db, dry_run=False)
        assert run.promoted == 0
        assert run.skipped_no_consensus == 1

    def test_min_classifiers_one_passes(self, seeded_db: Path):
        # With min_classifiers_agreed=1, section_aggregator alone suffices
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
        )
        run = sgp.promote_sections(
            seeded_db, dry_run=False, min_classifiers_agreed=1,
        )
        assert run.promoted == 1

    def test_idempotent(self, seeded_db: Path):
        # Run promote_sections twice; second run should be a no-op
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
            schedule_codes=["RES-1"],
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="TARIFF_SHEET")

        run1 = sgp.promote_sections(seeded_db, dry_run=False)
        run2 = sgp.promote_sections(seeded_db, dry_run=False)

        assert run1.promoted == 1
        assert run2.promoted == 0
        assert run2.skipped_already_gold == 1
        assert _count_gold(seeded_db) == 1  # still one active row

    def test_dry_run_no_writes(self, seeded_db: Path):
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="TARIFF_SHEET")

        run = sgp.promote_sections(seeded_db, dry_run=True)

        assert run.promoted == 1  # counted
        assert _count_gold(seeded_db) == 0  # but not written

    def test_unknown_section_type_skipped(self, seeded_db: Path):
        # 'unknown' isn't in DEFAULT_PROMOTABLE_TYPES, so the candidate
        # query filters it out entirely — candidates_evaluated stays 0.
        # If the operator passes section_types=['unknown'], the gate
        # inside evaluate_section catches it via rejected_other.
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="unknown", confidence=0.95,
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")
        # Default types: should never reach the candidate set
        run = sgp.promote_sections(seeded_db, dry_run=False)
        assert run.candidates_evaluated == 0
        assert run.promoted == 0
        # Explicit override: unknown should be rejected at the gate
        run2 = sgp.promote_sections(
            seeded_db, dry_run=False, section_types=["unknown"],
        )
        assert run2.candidates_evaluated == 1
        assert run2.promoted == 0
        assert run2.rejected_other == 1

    def test_schedule_code_canonicalized_on_promote(self, seeded_db: Path):
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
            # Order: stop-word, then real code — canonical should pick real
            schedule_codes=["CLASS", "RES-1"],
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="TARIFF_SHEET")

        sgp.promote_sections(seeded_db, dry_run=False)

        conn = sqlite3.connect(seeded_db)
        row = conn.execute(
            "SELECT schedule_code FROM section_type_gold WHERE source_pdf='doc1.pdf'"
        ).fetchone()
        conn.close()
        assert row[0] == "RES-1"

    def test_rider_code_canonicalized(self, seeded_db: Path):
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rider", confidence=0.9,
            rider_codes=["RIDER EDIT-4"],
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="RIDER")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="RIDER")

        sgp.promote_sections(seeded_db, dry_run=False)

        conn = sqlite3.connect(seeded_db)
        row = conn.execute(
            "SELECT rider_code FROM section_type_gold WHERE source_pdf='doc1.pdf'"
        ).fetchone()
        conn.close()
        assert row[0] == "EDIT-4"

    def test_type_specific_floor_procedural_at_0_5(self, seeded_db: Path):
        # Procedural floor is 0.45 — 0.5 should pass
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="procedural", confidence=0.5,
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="ORDER_PROCEDURAL")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="ORDER_PROCEDURAL")

        run = sgp.promote_sections(seeded_db, dry_run=False)
        assert run.promoted == 1

    def test_filter_by_section_types(self, seeded_db: Path):
        # Insert one rate_schedule and one rider; filter to rider only
        _insert_section(seeded_db, "doc1.pdf", 0,
                        section_type="rate_schedule", confidence=0.9)
        _insert_section(seeded_db, "doc1.pdf", 1,
                        section_type="rider", confidence=0.9,
                        rider_codes=["EMF"])
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="TARIFF_SHEET")

        run = sgp.promote_sections(
            seeded_db, dry_run=False, section_types=["rider"],
        )
        assert run.candidates_evaluated == 1
        assert run.promoted == 1
        assert run.by_type == {"rider": 1}

    def test_superseded_on_relabel(self, seeded_db: Path):
        # First promote as rate_schedule, then change the section type
        # to rider and re-promote — old gold should be superseded.
        _insert_section(
            seeded_db, "doc1.pdf", 0,
            section_type="rate_schedule", confidence=0.9,
        )
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="rule_v1", label="TARIFF_SHEET")
        _insert_classification(seeded_db, "doc1.pdf",
                               classifier="embedding_knn_v1", label="TARIFF_SHEET")
        run1 = sgp.promote_sections(seeded_db, dry_run=False)
        assert run1.promoted == 1

        # Now update the section to "rider"
        conn = sqlite3.connect(seeded_db)
        conn.execute(
            "UPDATE document_sections SET section_type='rider', "
            "rider_codes_json='[\"EMF\"]' "
            "WHERE source_pdf='doc1.pdf' AND section_index=0"
        )
        # Also update classifiers to consistent with rider
        conn.execute(
            "UPDATE document_classifications SET label='RIDER' "
            "WHERE subject_id='doc1.pdf'"
        )
        conn.commit()
        conn.close()

        run2 = sgp.promote_sections(seeded_db, dry_run=False)
        assert run2.promoted == 1

        # One active row, one superseded
        conn = sqlite3.connect(seeded_db)
        active = conn.execute(
            "SELECT COUNT(*) FROM section_type_gold WHERE superseded_by IS NULL"
        ).fetchone()[0]
        superseded = conn.execute(
            "SELECT COUNT(*) FROM section_type_gold WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        assert active == 1
        assert superseded == 1


class TestSchemaIdempotency:
    def test_ensure_schema_runs_twice(self, tmp_path: Path):
        db = tmp_path / "schema.db"
        sgp.ensure_schema(db)
        sgp.ensure_schema(db)
        # Verify tables exist
        conn = sqlite3.connect(db)
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "section_type_gold" in names
        assert "section_classification_conflicts" in names
