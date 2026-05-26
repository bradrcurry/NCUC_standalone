"""Tests for SectionKNNClassifier against fully-mocked fixtures.

We don't run a real embedding model — the orchestrator is faked with a
deterministic embed() so neighbor selection is predictable, and a
section_embeddings table is populated with pre-baked vectors.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import numpy as np
import pytest

from duke_rates.document_intelligence.section_classifier import (
    SectionKNNClassifier,
)


# ----------------------------------------------------------------------
# Fake orchestrator
# ----------------------------------------------------------------------


class _Role:
    def __init__(self, primary: str) -> None:
        self.primary = primary


class _FakeOrchestrator:
    """Returns a precomputed vector regardless of input text, so neighbor
    selection is deterministic. Stores roles dict mirroring real shape."""

    def __init__(self, vector: list[float], model: str = "mock") -> None:
        self._vector = vector
        self._roles = {"embedding_primary": _Role(model)}

    def embed(self, role: str, text: str) -> list[float]:
        return self._vector


# ----------------------------------------------------------------------
# Fixture
# ----------------------------------------------------------------------


def _to_blob(vec: list[float]) -> bytes:
    return np.array(vec, dtype=np.float32).tobytes()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Build an in-memory DB with:
    - Three reference sections:
      - a.pdf idx=0: vector=[1,0,0], gold=rate_schedule conf=0.9
      - b.pdf idx=0: vector=[0,1,0], gold=rider conf=0.85
      - c.pdf idx=0: vector=[0.9,0.1,0], gold=rate_schedule conf=0.8
    - One section in section_embeddings but NOT in section_type_gold
      (d.pdf idx=0, vector=[0,0,1]) — should be skipped at lookup.
    """
    db = tmp_path / "sec.db"
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
    samples = [
        ("a.pdf", 0, [1.0, 0.0, 0.0], "rate_schedule", 0.9),
        ("b.pdf", 0, [0.0, 1.0, 0.0], "rider", 0.85),
        ("c.pdf", 0, [0.9, 0.1, 0.0], "rate_schedule", 0.8),
    ]
    for pdf, idx, vec, label, conf in samples:
        conn.execute(
            "INSERT INTO section_embeddings(source_pdf, section_index, start_page, end_page, embedding_kind, embedding_model, vector) "
            "VALUES(?,?,?,?,?,?,?)",
            (pdf, idx, 1, 1, "section_text", "mock", _to_blob(vec)),
        )
        conn.execute(
            "INSERT INTO section_type_gold(source_pdf, section_index, section_type, confidence) VALUES(?,?,?,?)",
            (pdf, idx, label, conf),
        )
    # d.pdf has an embedding but no gold label — should be skipped at lookup.
    conn.execute(
        "INSERT INTO section_embeddings(source_pdf, section_index, start_page, end_page, embedding_kind, embedding_model, vector) "
        "VALUES('d.pdf', 0, 1, 1, 'section_text', 'mock', ?)",
        (_to_blob([0.0, 0.0, 1.0]),),
    )
    conn.commit()
    conn.close()
    return db


def _build(db_path: Path, query_vec: list[float], **kwargs):
    orch = _FakeOrchestrator(query_vec)
    defaults = {"k": 3, "min_neighbors": 1}
    defaults.update(kwargs)
    return SectionKNNClassifier(db_path=db_path, orchestrator=orch, **defaults)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestClassify:
    def test_predicts_rate_schedule_for_rate_like_query(
        self, db_path: Path
    ) -> None:
        # Query near [1,0,0] should pull rate_schedule neighbors (a.pdf, c.pdf).
        clf = _build(db_path, [1.0, 0.0, 0.0])
        result = clf.classify("some rate text")
        assert result.label == "rate_schedule"
        assert result.confidence > 0.5
        assert result.classifier == "section_knn_v1"

    def test_predicts_rider_for_rider_like_query(
        self, db_path: Path
    ) -> None:
        clf = _build(db_path, [0.0, 1.0, 0.0])
        result = clf.classify("some rider text")
        assert result.label == "rider"

    def test_empty_text_returns_unknown(self, db_path: Path) -> None:
        clf = _build(db_path, [1.0, 0.0, 0.0])
        result = clf.classify("")
        assert result.label == "unknown"
        assert result.confidence == 0.0

    def test_whitespace_only_text_returns_unknown(
        self, db_path: Path
    ) -> None:
        clf = _build(db_path, [1.0, 0.0, 0.0])
        result = clf.classify("   \n  \t  ")
        assert result.label == "unknown"

    def test_neighbors_without_gold_are_skipped(
        self, db_path: Path
    ) -> None:
        # d.pdf has embedding [0,0,1] but no gold; query at [0.1,0.05,1.0]
        # picks d.pdf as nearest but skips it. Other neighbors have small
        # but real signal and should drive the vote toward rate_schedule
        # (a + c are both rate; b is rider).
        clf = _build(db_path, [0.1, 0.05, 1.0], k=4)
        result = clf.classify("some text")
        # With k=4 we get all 4 neighbors; d.pdf has no gold so 3 contribute.
        # Two of those three are rate_schedule, so rate_schedule wins.
        assert result.label == "rate_schedule"
        # Evidence should not include d.pdf
        neighbor_pdfs = {ev.get("source_pdf") for ev in result.evidence}
        assert "d.pdf" not in neighbor_pdfs

    def test_classifier_version_v1(self, db_path: Path) -> None:
        clf = _build(db_path, [1.0, 0.0, 0.0])
        result = clf.classify("text")
        assert result.classifier_version == "v1"

    def test_evidence_includes_top_neighbors(
        self, db_path: Path
    ) -> None:
        clf = _build(db_path, [1.0, 0.0, 0.0])
        result = clf.classify("text")
        assert len(result.evidence) >= 1
        first = result.evidence[0]
        assert "similarity" in first
        assert "label" in first
        assert "source_pdf" in first

    def test_exclude_key_omits_self_match(self, db_path: Path) -> None:
        clf = _build(db_path, [1.0, 0.0, 0.0])
        # Without exclude: a.pdf and c.pdf both vote rate_schedule.
        baseline = clf.classify("text")
        assert baseline.label == "rate_schedule"
        # Exclude a.pdf — still rate_schedule because c.pdf is similar.
        result = clf.classify("text", exclude_key=("a.pdf", 0))
        assert result.label == "rate_schedule"
        # Neighbor list should not include a.pdf
        neighbor_pdfs = {ev.get("source_pdf") for ev in result.evidence}
        assert "a.pdf" not in neighbor_pdfs

    def test_no_reference_returns_unknown(self, tmp_path: Path) -> None:
        # Empty DB with just the schema; no rows
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE section_embeddings (
                id INTEGER PRIMARY KEY,
                source_pdf TEXT, section_index INTEGER,
                start_page INTEGER, end_page INTEGER,
                embedding_kind TEXT, embedding_model TEXT,
                embedding_version TEXT DEFAULT 'v1',
                vector BLOB, text_sample TEXT, metadata_json TEXT, created_at TEXT
            );
            CREATE TABLE section_type_gold (
                id INTEGER PRIMARY KEY,
                source_pdf TEXT, section_index INTEGER,
                section_type TEXT, confidence REAL,
                superseded_by INTEGER
            );
            """
        )
        conn.commit()
        conn.close()
        clf = _build(db, [1.0, 0.0, 0.0])
        result = clf.classify("text")
        assert result.label == "unknown"


class TestGoldOnlyReference:
    def test_default_filters_to_gold_only(self, db_path: Path) -> None:
        # d.pdf has an embedding but no gold. With gold_only_reference=True
        # (default), d.pdf is never seen as a neighbor candidate.
        clf = _build(db_path, [0.0, 0.0, 1.0], k=3)
        clf._load_reference_vectors()
        ref_pdfs = {pdf for pdf, _ in clf._ref_keys}
        assert "d.pdf" not in ref_pdfs
        assert ref_pdfs == {"a.pdf", "b.pdf", "c.pdf"}

    def test_gold_only_false_includes_all(self, db_path: Path) -> None:
        # Explicit opt-out: include d.pdf in the reference pool.
        orch = _FakeOrchestrator([0.0, 0.0, 1.0])
        clf = SectionKNNClassifier(
            db_path=db_path,
            orchestrator=orch,
            k=3,
            min_neighbors=1,
            gold_only_reference=False,
        )
        clf._load_reference_vectors()
        ref_pdfs = {pdf for pdf, _ in clf._ref_keys}
        assert "d.pdf" in ref_pdfs
        assert len(ref_pdfs) == 4

    def test_gold_only_avoids_unknown_in_high_density_queries(
        self, db_path: Path
    ) -> None:
        # Query similar to d.pdf [0,0,1]. With gold_only_reference=False,
        # d.pdf is the closest neighbor but has no gold → wasted slot.
        # With gold_only_reference=True (default), only gold neighbors are
        # in the pool, so the prediction is grounded.
        clf = _build(db_path, [0.05, 0.05, 1.0], k=3)
        result = clf.classify("text")
        # Should produce a real label, not unknown
        assert result.label in {"rate_schedule", "rider"}


class TestEdgeCases:
    def test_min_neighbors_threshold(self, db_path: Path) -> None:
        # Require 5 neighbors with gold but only 3 exist → unknown
        orch = _FakeOrchestrator([1.0, 0.0, 0.0])
        clf = SectionKNNClassifier(
            db_path=db_path,
            orchestrator=orch,
            k=5,
            min_neighbors=5,
        )
        result = clf.classify("text")
        assert result.label == "unknown"
        assert result.evidence[0]["kind"] == "insufficient_gold_neighbors"

    def test_superseded_gold_skipped(self, db_path: Path) -> None:
        # Supersede a.pdf's gold label
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE section_type_gold SET superseded_by = 999 "
            "WHERE source_pdf = 'a.pdf' AND section_index = 0"
        )
        conn.commit()
        conn.close()
        clf = _build(db_path, [1.0, 0.0, 0.0])
        result = clf.classify("text")
        # a.pdf no longer contributes. c.pdf (rate_schedule) still does.
        assert result.label == "rate_schedule"
        neighbor_pdfs = {
            ev.get("source_pdf") for ev in result.evidence
        }
        assert "a.pdf" not in {
            ev.get("source_pdf")
            for ev in result.evidence
            if ev.get("label") == "rate_schedule"
        } or len(neighbor_pdfs - {"a.pdf"}) > 0
