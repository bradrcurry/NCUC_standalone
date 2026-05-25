"""Tests for the new label_source branching in EmbeddingKNNClassifier.

We don't exercise the full classify() path here (that requires real PDFs and
an embedding model). Instead we test:
- constructor validation of label_source values
- _lookup_labels routing across the three modes against an in-memory DB
- classifier_version bumps to v2 when label_source != rule_v1
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from duke_rates.document_intelligence.embedding_classifier import (
    LABEL_SOURCE_RULE_V1,
    LABEL_SOURCE_SECTION_GOLD,
    LABEL_SOURCE_SECTION_GOLD_OR_RULE,
    EmbeddingKNNClassifier,
)


class _FakeOrchestrator:
    """Minimal stand-in. We only need the constructor to accept it."""

    pass


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a sqlite DB with section_type_gold + historical_documents +
    document_classifications populated for two PDFs:

    - a.pdf: has section gold (rate_schedule) + rule_v1 label COVER_LETTER
      → section_gold says TARIFF_SHEET; rule_v1 says COVER_LETTER.
    - b.pdf: NO section gold, but has rule_v1 label TARIFF_SHEET.
    - c.pdf: nothing.
    """
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_path TEXT NOT NULL
        );
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            classifier TEXT NOT NULL,
            label TEXT NOT NULL,
            confidence REAL NOT NULL,
            superseded_by INTEGER
        );
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
    # a.pdf: hd_id=1, rule_v1=COVER_LETTER, section_gold=rate_schedule
    conn.execute("INSERT INTO historical_documents(id, local_path) VALUES(1, 'a.pdf')")
    conn.execute(
        "INSERT INTO document_classifications(subject_kind, subject_id, stage, classifier, label, confidence) "
        "VALUES('historical_document', '1', 'document_type', 'rule_document_type_v1', 'COVER_LETTER', 0.7)"
    )
    conn.execute(
        "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) "
        "VALUES('a.pdf', 0, 'rate_schedule', 0.9)"
    )
    # b.pdf: hd_id=2, rule_v1=TARIFF_SHEET, NO section gold
    conn.execute("INSERT INTO historical_documents(id, local_path) VALUES(2, 'b.pdf')")
    conn.execute(
        "INSERT INTO document_classifications(subject_kind, subject_id, stage, classifier, label, confidence) "
        "VALUES('historical_document', '2', 'document_type', 'rule_document_type_v1', 'TARIFF_SHEET', 0.8)"
    )
    # c.pdf: nothing
    conn.execute("INSERT INTO historical_documents(id, local_path) VALUES(3, 'c.pdf')")
    conn.commit()
    conn.close()
    return db


def _build_clf(
    db_path: Path, label_source: str
) -> EmbeddingKNNClassifier:
    clf = EmbeddingKNNClassifier(
        db_path=db_path,
        orchestrator=_FakeOrchestrator(),
        label_source=label_source,
    )
    # Stub the ref-PDF cache so _lookup_labels has something to work with.
    clf._ref_pdfs = ["a.pdf", "b.pdf", "c.pdf"]
    return clf


class TestConstructorValidation:
    def test_rejects_invalid_label_source(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid label_source"):
            EmbeddingKNNClassifier(
                db_path=tmp_path / "x.db",
                orchestrator=_FakeOrchestrator(),
                label_source="garbage",
            )

    def test_accepts_all_three(self, tmp_path: Path) -> None:
        for src in (
            LABEL_SOURCE_RULE_V1,
            LABEL_SOURCE_SECTION_GOLD,
            LABEL_SOURCE_SECTION_GOLD_OR_RULE,
        ):
            EmbeddingKNNClassifier(
                db_path=tmp_path / "x.db",
                orchestrator=_FakeOrchestrator(),
                label_source=src,
            )

    def test_default_is_rule_v1_for_back_compat(self, tmp_path: Path) -> None:
        clf = EmbeddingKNNClassifier(
            db_path=tmp_path / "x.db", orchestrator=_FakeOrchestrator()
        )
        assert clf._label_source == LABEL_SOURCE_RULE_V1


class TestClassifierVersion:
    def test_rule_v1_returns_v1(self, db_path: Path) -> None:
        clf = _build_clf(db_path, LABEL_SOURCE_RULE_V1)
        assert clf._classifier_version() == "v1"

    def test_section_gold_returns_v2(self, db_path: Path) -> None:
        clf = _build_clf(db_path, LABEL_SOURCE_SECTION_GOLD)
        assert clf._classifier_version() == "v2"

    def test_section_gold_or_rule_returns_v2(self, db_path: Path) -> None:
        clf = _build_clf(db_path, LABEL_SOURCE_SECTION_GOLD_OR_RULE)
        assert clf._classifier_version() == "v2"


class TestLookupLabels:
    def test_rule_v1_mode_reads_rule_classifier(self, db_path: Path) -> None:
        clf = _build_clf(db_path, LABEL_SOURCE_RULE_V1)
        result = clf._lookup_labels(np.array([0, 1, 2]))
        assert result[0]["label"] == "COVER_LETTER"  # a.pdf, rule says cover
        assert result[0]["label_source"] == "rule_v1"
        assert result[1]["label"] == "TARIFF_SHEET"  # b.pdf
        assert result[1]["label_source"] == "rule_v1"
        # c.pdf has no rule_v1 row
        assert result[2]["label"] == "UNKNOWN"
        assert result[2]["label_source"] == "rule_v1_missing"

    def test_section_gold_mode_reads_section_only(self, db_path: Path) -> None:
        clf = _build_clf(db_path, LABEL_SOURCE_SECTION_GOLD)
        result = clf._lookup_labels(np.array([0, 1, 2]))
        # a.pdf has section gold → derived TARIFF_SHEET
        assert result[0]["label"] == "TARIFF_SHEET"
        assert result[0]["label_source"] == "section_gold"
        # b.pdf has no section gold → UNKNOWN (does NOT fall back)
        assert result[1]["label"] == "UNKNOWN"
        assert result[1]["label_source"] == "section_gold_missing"
        # c.pdf has nothing → UNKNOWN
        assert result[2]["label"] == "UNKNOWN"
        assert result[2]["label_source"] == "section_gold_missing"

    def test_section_gold_or_rule_prefers_section_falls_back(
        self, db_path: Path
    ) -> None:
        clf = _build_clf(db_path, LABEL_SOURCE_SECTION_GOLD_OR_RULE)
        result = clf._lookup_labels(np.array([0, 1, 2]))
        # a.pdf has section gold → TARIFF_SHEET (overrides rule's COVER_LETTER!)
        assert result[0]["label"] == "TARIFF_SHEET"
        assert result[0]["label_source"] == "section_gold"
        # b.pdf has no section gold → falls back to rule's TARIFF_SHEET
        assert result[1]["label"] == "TARIFF_SHEET"
        assert result[1]["label_source"] == "rule_v1"
        # c.pdf has nothing → UNKNOWN via rule_v1_missing
        assert result[2]["label"] == "UNKNOWN"
        assert result[2]["label_source"] == "rule_v1_missing"

    def test_empty_indices_returns_empty(self, db_path: Path) -> None:
        clf = _build_clf(db_path, LABEL_SOURCE_SECTION_GOLD_OR_RULE)
        assert clf._lookup_labels(np.array([], dtype=int)) == []
