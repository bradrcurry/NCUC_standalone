"""Regression tests for D3/D4: stale or degraded Docling artifact selection.

``BulkExtractor._load_docling_artifact`` picked the newest ``docling_artifacts``
row for a ``source_pdf`` path without checking ``file_hash``.  Downloads can
overwrite a PDF at the same deterministic path, so extraction could read
Docling text converted from a superseded revision.  The conversion cache also
let a degraded (no-OCR / no-tables) artifact satisfy a full-quality request.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from duke_rates.db.artifact_cache import docling_artifact_is_degraded
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.historical.ncuc.pipeline import docling_backend
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.historical.ncuc.pipeline.stage_versions import DOCLING_BACKEND_VERSION

REV1_TEXT = "Leaf No. 600 Eighth Revised  EDIT-4 Rider (0.249) " * 5
REV2_TEXT = "Leaf No. 600 Ninth Revised  EDIT-4 Rider (0.312) " * 5


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.commit()
    conn.close()
    return path


def _insert(
    db_path: Path,
    *,
    source_pdf: str,
    file_hash: str,
    text: str,
    status: str = "ConversionStatus.SUCCESS",
    backend_version: str = DOCLING_BACKEND_VERSION,
    metadata: dict | None = None,
    accelerator: str = "cuda",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO docling_artifacts (source_pdf, file_hash, backend_version, accelerator,
            pipeline, status, page_count, table_count, plain_text_content, doc_json_content,
            tables_json_content, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'standard', ?, 1, 0, ?, '{}', '[]', ?, 'now', 'now')
        """,
        (source_pdf, file_hash, backend_version, accelerator, status, text,
         json.dumps(metadata or {})),
    )
    conn.commit()
    conn.close()


def test_stale_artifact_for_overwritten_pdf_is_not_used(tmp_path: Path, db_path: Path) -> None:
    pdf = tmp_path / "leaf-600.pdf"
    pdf.write_bytes(b"%PDF-1.4 revision one")
    _insert(db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text=REV1_TEXT)

    # A newer revision is downloaded to the same path; no artifact exists for it yet.
    pdf.write_bytes(b"%PDF-1.4 revision two -- different bytes")
    extractor = BulkExtractor(db_path=str(db_path))

    assert extractor._load_docling_artifact(str(pdf)) is None
    assert extractor._load_docling_text(str(pdf)) is None


def test_artifact_matching_current_hash_wins_over_newer_stale_row(
    tmp_path: Path, db_path: Path
) -> None:
    pdf = tmp_path / "leaf-600.pdf"
    pdf.write_bytes(b"%PDF-1.4 revision two")
    _insert(db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text=REV2_TEXT)
    # A later row (higher id) converted from an older revision of the same path.
    _insert(db_path, source_pdf=str(pdf), file_hash="0" * 64, text=REV1_TEXT)

    text = BulkExtractor(db_path=str(db_path))._load_docling_text(str(pdf))
    assert text == REV2_TEXT


def test_degraded_artifact_does_not_satisfy_extraction(tmp_path: Path, db_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 scanned")
    _insert(db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text=REV2_TEXT)
    _insert(
        db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text="degraded " * 30,
        metadata={"degraded_modes": ["page_degraded"], "skipped_pages": []},
    )
    extractor = BulkExtractor(db_path=str(db_path))
    assert extractor._load_docling_text(str(pdf)) == REV2_TEXT

    only_degraded = tmp_path / "scan2.pdf"
    only_degraded.write_bytes(b"%PDF-1.4 scanned two")
    _insert(
        db_path, source_pdf=str(only_degraded), file_hash=_sha(only_degraded), text="x " * 80,
        metadata={"degraded_modes": ["page_degraded"]},
    )
    assert extractor._load_docling_artifact(str(only_degraded)) is None


FIXTURES = Path(__file__).parent / "fixtures" / "docling"


def _resaved_leaf600(tmp_path: Path) -> tuple[Path, dict]:
    """Same Leaf 600 content, different bytes (as a portal re-download produces)."""
    fitz = pytest.importorskip("fitz")
    art = json.loads((FIXTURES / "dep_leaf600_2026-04-01.docling.json").read_text(encoding="utf-8"))
    pdf = tmp_path / "leaf-600.pdf"
    with fitz.open(FIXTURES / "dep_leaf600_2026-04-01.pdf") as doc:
        doc.set_metadata({"title": "re-downloaded copy"})
        doc.save(pdf)
    assert _sha(pdf) != art["file_hash"]
    return pdf, art


def _insert_leaf600(db_path: Path, pdf: Path, art: dict, text: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO docling_artifacts (source_pdf, file_hash, backend_version, accelerator,
            pipeline, status, page_count, table_count, plain_text_content, doc_json_content,
            tables_json_content, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, 'cuda', 'standard', 'ConversionStatus.SUCCESS', ?, 12, ?, '{}', '[]',
                '{}', 'now', 'now')
        """,
        (str(pdf), art["file_hash"], DOCLING_BACKEND_VERSION, art["page_count"], text),
    )
    conn.commit()
    conn.close()


def test_redownload_with_identical_content_keeps_its_artifact(
    tmp_path: Path, db_path: Path
) -> None:
    pdf, art = _resaved_leaf600(tmp_path)
    _insert_leaf600(db_path, pdf, art, art["plain_text_content"])

    row = BulkExtractor(db_path=str(db_path))._load_docling_artifact(str(pdf))
    assert row is not None and row["plain_text_content"] == art["plain_text_content"]


def test_hash_mismatch_with_different_values_is_rejected(tmp_path: Path, db_path: Path) -> None:
    """Same layout and page count, but the rates differ: a different revision."""
    import re

    pdf, art = _resaved_leaf600(tmp_path)
    other_revision = re.sub(r"(\d+\.\d+)", r"\g<1>7", art["plain_text_content"])
    _insert_leaf600(db_path, pdf, art, other_revision)

    assert BulkExtractor(db_path=str(db_path))._load_docling_artifact(str(pdf)) is None


def test_current_backend_version_preferred(tmp_path: Path, db_path: Path) -> None:
    pdf = tmp_path / "leaf.pdf"
    pdf.write_bytes(b"%PDF-1.4 same bytes")
    _insert(db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text=REV2_TEXT)
    _insert(db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text=REV1_TEXT,
            backend_version="docling_v1")

    assert BulkExtractor(db_path=str(db_path))._load_docling_text(str(pdf)) == REV2_TEXT


def test_missing_source_pdf_cannot_be_verified(tmp_path: Path, db_path: Path) -> None:
    ghost = tmp_path / "gone.pdf"
    _insert(db_path, source_pdf=str(ghost), file_hash="f" * 64, text=REV2_TEXT)
    assert BulkExtractor(db_path=str(db_path))._load_docling_artifact(str(ghost)) is None


@pytest.mark.parametrize(
    ("metadata", "degraded"),
    [
        (None, False),
        ({}, False),
        ({"degraded_modes": []}, False),
        ({"degraded_modes": ["chunked", "cpu_fallback"]}, False),
        ({"degraded_modes": ["page_degraded"]}, True),
        ({"degraded_modes": [], "skipped_pages": [3, 4]}, True),
    ],
)
def test_docling_artifact_is_degraded(metadata: dict | None, degraded: bool) -> None:
    assert docling_artifact_is_degraded(metadata) is degraded
    assert docling_artifact_is_degraded(json.dumps(metadata) if metadata is not None else None) is degraded


def test_conversion_cache_rejects_degraded_db_artifact(tmp_path: Path, db_path: Path) -> None:
    """A degraded conversion must not be returned as a cache hit."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    _insert(
        db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text=REV2_TEXT, status="success",
        accelerator="cpu", metadata={"degraded_modes": ["page_degraded"]},
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        hit = docling_backend._load_cached_docling_artifact(
            str(pdf), "cpu", expected_hash=_sha(pdf), conn=conn
        )
    finally:
        conn.close()
    assert hit is None


def test_conversion_cache_returns_full_quality_db_artifact(tmp_path: Path, db_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 y")
    _insert(db_path, source_pdf=str(pdf), file_hash=_sha(pdf), text=REV2_TEXT, status="success",
            accelerator="cpu", metadata={"degraded_modes": []})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        hit = docling_backend._load_cached_docling_artifact(
            str(pdf), "cpu", expected_hash=_sha(pdf), conn=conn
        )
    finally:
        conn.close()
    assert hit is not None and hit["plain_text"] == REV2_TEXT
