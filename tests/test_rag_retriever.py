"""Unit tests for RagRetriever using in-memory fixtures and fake orchestrator."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from duke_rates.document_intelligence.rag_retriever import (
    RagRetriever,
    RetrievalHit,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


class _Role:
    def __init__(self, primary: str) -> None:
        self.primary = primary


class _FakeOrchestrator:
    def __init__(self, vector: list[float], model: str = "mock") -> None:
        self._vector = vector
        self._roles = {"embedding_primary": _Role(model)}

    def embed(self, role: str, text: str) -> list[float]:
        return self._vector


def _to_blob(vec: list[float]) -> bytes:
    return np.array(vec, dtype=np.float32).tobytes()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Build a DB with four sections of varying labels/codes/text.

    a.pdf idx=0  vector=[1,0,0]  gold=rate_schedule  schedule=RES   pages=[1,2]
    b.pdf idx=0  vector=[0,1,0]  knn=rider          schedule=FCAR  pages=[1,1]
    c.pdf idx=0  vector=[0.9,0.1,0]  heuristic=rate_schedule  schedule=R-TOU  pages=[5,7]
    d.pdf idx=0  vector=[0,0,1]  no label                        pages=[1,1]
    """
    db = tmp_path / "rag.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE section_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT NOT NULL,
            section_index INTEGER NOT NULL,
            start_page INTEGER NOT NULL,
            end_page INTEGER NOT NULL,
            embedding_kind TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_version TEXT NOT NULL DEFAULT 'v1',
            vector BLOB NOT NULL,
            text_sample TEXT,
            metadata_json TEXT,
            created_at TEXT
        );
        CREATE TABLE document_sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT,
            section_index INTEGER,
            start_page INTEGER,
            end_page INTEGER,
            section_type TEXT,
            schedule_codes_json TEXT,
            rider_codes_json TEXT,
            leaf_numbers_json TEXT
        );
        CREATE TABLE section_type_gold (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT,
            section_index INTEGER,
            section_type TEXT,
            confidence REAL,
            superseded_by INTEGER
        );
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT,
            subject_id TEXT,
            stage TEXT,
            classifier TEXT,
            classifier_version TEXT,
            label TEXT,
            confidence REAL,
            superseded_by INTEGER
        );
        CREATE TABLE ncuc_page_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT,
            artifact_version TEXT,
            page_number INTEGER,
            text_content TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    # Embeddings
    samples = [
        ("a.pdf", 0, [1.0, 0.0, 0.0], 1, 2),
        ("b.pdf", 0, [0.0, 1.0, 0.0], 1, 1),
        ("c.pdf", 0, [0.9, 0.1, 0.0], 5, 7),
        ("d.pdf", 0, [0.0, 0.0, 1.0], 1, 1),
    ]
    for pdf, idx, vec, sp, ep in samples:
        conn.execute(
            "INSERT INTO section_embeddings(source_pdf, section_index, start_page, end_page, embedding_kind, embedding_model, vector) "
            "VALUES(?,?,?,?,?,?,?)",
            (pdf, idx, sp, ep, "section_text", "mock", _to_blob(vec)),
        )
    # document_sections — also gives metadata
    ds_rows = [
        ("a.pdf", 0, 1, 2, "rate_schedule", '["RES"]', "[]", '["226"]'),
        ("b.pdf", 0, 1, 1, "rider", '["FCAR"]', '["FCAR-2024"]', '["601"]'),
        ("c.pdf", 0, 5, 7, "rate_schedule", '["R-TOU"]', "[]", "[]"),
        ("d.pdf", 0, 1, 1, None, "[]", "[]", "[]"),
    ]
    for pdf, idx, sp, ep, st, sc, rc, ln in ds_rows:
        conn.execute(
            "INSERT INTO document_sections(source_pdf, section_index, start_page, end_page, section_type, schedule_codes_json, rider_codes_json, leaf_numbers_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pdf, idx, sp, ep, st, sc, rc, ln),
        )
    # Gold label for a.pdf (overrides heuristic for that row)
    conn.execute(
        "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) VALUES('a.pdf', 0, 'rate_schedule', 0.95)"
    )
    # KNN label for b.pdf (no gold; KNN should win)
    conn.execute(
        "INSERT INTO document_classifications(subject_kind, subject_id, stage, classifier, classifier_version, label, confidence) "
        "VALUES('document_section', '2', 'section_type', 'section_knn_v1', 'v1', 'rider', 0.88)"
    )
    # Page text — one row per (pdf, page)
    pages = [
        ("a.pdf", 1, "Schedule RES Residential Service first 200 chars of text"),
        ("a.pdf", 2, "Residential rate per kWh details continued from page 1"),
        ("b.pdf", 1, "Fuel Charge Adjustment Rider for the period beginning ..."),
        ("c.pdf", 5, "Residential Time-of-Use Schedule R-TOU"),
        ("c.pdf", 6, "Peak hours and energy charges"),
        ("c.pdf", 7, "Off-peak credit and demand fee"),
        ("d.pdf", 1, "Unknown content with no metadata"),
    ]
    for pdf, page, text in pages:
        conn.execute(
            "INSERT INTO ncuc_page_artifacts(source_pdf, page_number, text_content) VALUES(?,?,?)",
            (pdf, page, text),
        )
    conn.commit()
    conn.close()
    return db


def _build(db_path: Path, query_vec: list[float]) -> RagRetriever:
    return RagRetriever(
        db_path=db_path,
        orchestrator=_FakeOrchestrator(query_vec),
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestBasicRetrieval:
    def test_top_hit_is_closest_vector(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("residential rates", top_k=1)
        assert len(hits) == 1
        assert hits[0].source_pdf == "a.pdf"

    def test_results_ordered_by_similarity(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("rates", top_k=4)
        sims = [h.similarity for h in hits]
        assert sims == sorted(sims, reverse=True)

    def test_empty_query_returns_empty(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        assert retriever.search("") == []
        assert retriever.search("   ") == []

    def test_top_k_truncates(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("foo", top_k=2)
        assert len(hits) == 2

    def test_hit_carries_metadata(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("rates", top_k=1)
        h = hits[0]
        assert h.section_type == "rate_schedule"
        assert h.section_type_source == "gold"
        assert h.section_type_conf == 0.95
        assert h.schedule_codes == ["RES"]
        assert h.leaf_numbers == ["226"]
        assert "Schedule RES" in h.text_excerpt


class TestLabelPriority:
    def test_gold_overrides_heuristic(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("rates", top_k=4)
        a_hit = next(h for h in hits if h.source_pdf == "a.pdf")
        assert a_hit.section_type_source == "gold"

    def test_knn_used_when_no_gold(self, db_path: Path) -> None:
        retriever = _build(db_path, [0.0, 1.0, 0.0])
        hits = retriever.search("fuel", top_k=4)
        b_hit = next(h for h in hits if h.source_pdf == "b.pdf")
        assert b_hit.section_type == "rider"
        assert b_hit.section_type_source == "predicted"
        assert b_hit.section_type_conf == 0.88

    def test_heuristic_used_when_no_gold_or_knn(self, db_path: Path) -> None:
        retriever = _build(db_path, [0.9, 0.1, 0.0])
        hits = retriever.search("rates", top_k=4)
        c_hit = next(h for h in hits if h.source_pdf == "c.pdf")
        assert c_hit.section_type == "rate_schedule"
        assert c_hit.section_type_source == "heuristic"
        assert c_hit.section_type_conf is None

    def test_none_when_no_label_at_all(self, db_path: Path) -> None:
        retriever = _build(db_path, [0.0, 0.0, 1.0])
        hits = retriever.search("foo", top_k=4)
        d_hit = next(h for h in hits if h.source_pdf == "d.pdf")
        assert d_hit.section_type is None
        assert d_hit.section_type_source == "none"


class TestMetadataFilters:
    def test_section_type_filter(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        # Only rider sections
        hits = retriever.search("foo", section_types=["rider"], top_k=10)
        assert len(hits) == 1
        assert hits[0].source_pdf == "b.pdf"

    def test_schedule_code_filter(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("foo", schedule_code_like="R-TOU", top_k=10)
        assert len(hits) == 1
        assert hits[0].source_pdf == "c.pdf"

    def test_schedule_code_filter_substring_case_insensitive(
        self, db_path: Path
    ) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        # Match "RES" as substring — works case-insensitive
        hits = retriever.search("foo", schedule_code_like="res", top_k=10)
        assert any(h.source_pdf == "a.pdf" for h in hits)

    def test_source_pdf_filter(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("foo", source_pdf_like="b.pdf", top_k=10)
        assert len(hits) == 1
        assert hits[0].source_pdf == "b.pdf"

    def test_min_similarity_filter(self, db_path: Path) -> None:
        retriever = _build(db_path, [0.0, 0.0, 1.0])
        # Query at [0,0,1] — only d.pdf has high similarity; others ~0
        hits = retriever.search("foo", min_similarity=0.5, top_k=10)
        assert len(hits) == 1
        assert hits[0].source_pdf == "d.pdf"

    def test_filter_combination(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        # Rate schedules with RES schedule code → only a.pdf
        hits = retriever.search(
            "foo",
            section_types=["rate_schedule"],
            schedule_code_like="RES",
            top_k=10,
        )
        assert len(hits) == 1
        assert hits[0].source_pdf == "a.pdf"

    def test_no_matches_returns_empty(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search(
            "foo", schedule_code_like="NONEXISTENT", top_k=10
        )
        assert hits == []


class TestCitation:
    def test_citation_includes_schedule_code(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        hits = retriever.search("foo", top_k=1)
        c = hits[0].citation()
        assert "a.pdf" in c
        assert "Sch RES" in c
        assert "p1-2" in c

    def test_citation_falls_back_to_leaf(self, db_path: Path) -> None:
        # We construct a synthetic hit with only a leaf number.
        h = RetrievalHit(
            source_pdf="x/y/test.pdf",
            section_index=0,
            start_page=1,
            end_page=3,
            similarity=0.5,
            section_type="rate_schedule",
            section_type_source="gold",
            section_type_conf=0.9,
            schedule_codes=[],
            rider_codes=[],
            leaf_numbers=["500"],
        )
        c = h.citation()
        assert "leaf 500" in c
        assert "p1-3" in c


class TestCaching:
    def test_reference_loaded_once(self, db_path: Path) -> None:
        retriever = _build(db_path, [1.0, 0.0, 0.0])
        retriever.search("first", top_k=1)
        # After first call, _ref_vectors is set
        first_load = retriever._ref_vectors
        retriever.search("second", top_k=1)
        # Same array object (cache hit)
        assert retriever._ref_vectors is first_load
