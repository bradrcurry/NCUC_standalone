"""Unit tests for derive_doc_type_from_sections and fetch_section_derived_labels."""

from __future__ import annotations

import sqlite3

import pytest

from duke_rates.document_intelligence.section_derived_labels import (
    derive_doc_type_from_sections,
    fetch_section_derived_labels,
)


class TestDeriveDocType:
    def test_empty_returns_none(self) -> None:
        assert derive_doc_type_from_sections([]) is None
        assert derive_doc_type_from_sections(set()) is None

    def test_unknown_only_returns_none(self) -> None:
        assert derive_doc_type_from_sections(["foo", "bar"]) is None
        assert derive_doc_type_from_sections({"unknown"}) is None

    def test_rate_only_is_tariff_sheet(self) -> None:
        assert derive_doc_type_from_sections(["rate_schedule"]) == "TARIFF_SHEET"

    def test_rate_plus_rider_is_tariff_sheet(self) -> None:
        assert (
            derive_doc_type_from_sections(["rate_schedule", "rider"])
            == "TARIFF_SHEET"
        )

    def test_rate_plus_tc_is_tariff_sheet(self) -> None:
        assert (
            derive_doc_type_from_sections(["rate_schedule", "terms_conditions"])
            == "TARIFF_SHEET"
        )

    def test_rate_plus_procedural_is_compliance_filing(self) -> None:
        assert (
            derive_doc_type_from_sections(["rate_schedule", "procedural"])
            == "COMPLIANCE_FILING"
        )

    def test_three_way_bundle_is_tariff_sheet(self) -> None:
        # rate+rider+T&C is still a tariff document (not procedural).
        assert (
            derive_doc_type_from_sections(
                ["rate_schedule", "rider", "terms_conditions"]
            )
            == "TARIFF_SHEET"
        )

    def test_rate_rider_procedural_is_compliance_filing(self) -> None:
        # Multi-purpose bundle.
        assert (
            derive_doc_type_from_sections(
                ["rate_schedule", "rider", "procedural"]
            )
            == "COMPLIANCE_FILING"
        )

    def test_rider_only(self) -> None:
        assert derive_doc_type_from_sections(["rider"]) == "RIDER"

    def test_rider_plus_tc_is_rider(self) -> None:
        assert (
            derive_doc_type_from_sections(["rider", "terms_conditions"])
            == "RIDER"
        )

    def test_tc_only_is_tariff_sheet(self) -> None:
        # T&C without rate/rider is rare but still tariff content.
        assert (
            derive_doc_type_from_sections(["terms_conditions"]) == "TARIFF_SHEET"
        )

    def test_procedural_only(self) -> None:
        assert (
            derive_doc_type_from_sections(["procedural"]) == "ORDER_PROCEDURAL"
        )

    def test_procedural_plus_cover_is_order_final(self) -> None:
        assert (
            derive_doc_type_from_sections(["procedural", "cover_letter"])
            == "ORDER_FINAL"
        )

    def test_cover_only(self) -> None:
        assert derive_doc_type_from_sections(["cover_letter"]) == "COVER_LETTER"

    def test_accepts_set_or_list(self) -> None:
        # Should accept any iterable.
        assert derive_doc_type_from_sections({"rider"}) == "RIDER"
        assert derive_doc_type_from_sections(("rider",)) == "RIDER"

    def test_unknown_types_are_ignored(self) -> None:
        # Unknown types should be filtered out, not crash.
        assert (
            derive_doc_type_from_sections(["rate_schedule", "garbage"])
            == "TARIFF_SHEET"
        )


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE section_type_gold (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL,
            section_index INTEGER NOT NULL,
            section_type TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.8,
            superseded_by INTEGER REFERENCES section_type_gold(id)
        );
        """
    )
    return conn


class TestFetchSectionDerivedLabels:
    def test_empty_db_returns_empty(self, memory_db: sqlite3.Connection) -> None:
        assert fetch_section_derived_labels(memory_db) == {}

    def test_single_pdf_single_section(
        self, memory_db: sqlite3.Connection
    ) -> None:
        memory_db.execute(
            "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) VALUES(?,?,?,?)",
            ("a.pdf", 0, "rate_schedule", 0.9),
        )
        result = fetch_section_derived_labels(memory_db)
        assert result == {
            "a.pdf": {
                "label": "TARIFF_SHEET",
                "confidence": 0.9,
                "n_sections": 1,
                "section_types": ["rate_schedule"],
                "source": "section_gold",
            }
        }

    def test_multi_section_bundle(self, memory_db: sqlite3.Connection) -> None:
        memory_db.execute(
            "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) VALUES('b.pdf', 0, 'rate_schedule', 0.9)"
        )
        memory_db.execute(
            "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) VALUES('b.pdf', 1, 'procedural', 0.85)"
        )
        result = fetch_section_derived_labels(memory_db)
        assert result["b.pdf"]["label"] == "COMPLIANCE_FILING"
        assert result["b.pdf"]["n_sections"] == 2
        assert set(result["b.pdf"]["section_types"]) == {
            "rate_schedule",
            "procedural",
        }

    def test_confidence_capped_at_0_95(
        self, memory_db: sqlite3.Connection
    ) -> None:
        memory_db.execute(
            "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) VALUES('c.pdf', 0, 'rider', 1.0)"
        )
        result = fetch_section_derived_labels(memory_db)
        assert result["c.pdf"]["confidence"] == 0.95

    def test_superseded_rows_skipped(
        self, memory_db: sqlite3.Connection
    ) -> None:
        # Active row → rider; superseded row → rate_schedule. Only the active
        # row should drive the label.
        memory_db.execute(
            "INSERT INTO section_type_gold(id, source_pdf, section_index, section_type, confidence, superseded_by) VALUES(1, 'd.pdf', 0, 'rate_schedule', 0.9, 2)"
        )
        memory_db.execute(
            "INSERT INTO section_type_gold(id, source_pdf, section_index, section_type, confidence) VALUES(2, 'd.pdf', 0, 'rider', 0.85)"
        )
        result = fetch_section_derived_labels(memory_db)
        assert result["d.pdf"]["label"] == "RIDER"
        assert result["d.pdf"]["n_sections"] == 1

    def test_unrecognized_section_types_skipped(
        self, memory_db: sqlite3.Connection
    ) -> None:
        memory_db.execute(
            "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) VALUES('e.pdf', 0, 'table_of_contents', 0.5)"
        )
        # table_of_contents alone yields None → not in result.
        result = fetch_section_derived_labels(memory_db)
        assert "e.pdf" not in result
