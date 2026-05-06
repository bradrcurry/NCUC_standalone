"""
Integration tests for the Docling bridge pipeline:
  mine_pages_from_docling_artifact()
  → NcucPipelineImporter.mine_discovery_record_spans_with_pages()
  → HistoricalDocumentRecord creation with all importer guardrails
  → BulkExtractor.process_document()
  → parse_attempt_logs / document_fingerprints / parse_review_outcomes

These tests do NOT call Docling or touch disk PDFs.
All PageEvidence is constructed synthetically.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.importer import NcucPipelineImporter
from duke_rates.historical.ncuc.pipeline.docling_page_miner import (
    mine_pages_from_docling_artifact,
)
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    DOCLING_PAGE_MINER_VERSION,
)
from duke_rates.models.ncuc import (
    NcucDiscoveryRecord,
    NcucFetchStatus,
    NcucFilingClassification,
)
from duke_rates.models.pipeline import PageEvidence
from duke_rates.models.tariff import TariffFamilyRecord


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path) -> tuple[Repository, str, Settings]:
    db_path = tmp_path / "test_bridge.db"
    repo = Repository(db_path)
    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    return repo, str(db_path), settings


def _seed_family(repo: Repository, family_key: str, leaf: str, code: str = "RES") -> None:
    """Insert a minimal tariff family so the importer can match spans."""
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key=family_key,
            state="NC",
            company="progress",
            tariff_identifier=f"leaf-{leaf}",
            schedule_code=code,
            family_type="rate_schedule",
            title=f"Progress Service Leaf {leaf}",
        )
    )


def _seed_tariff_version(db_path: str, doc_id: int, family_key: str, source_pdf: str) -> int:
    """Insert a tariff_versions row so BulkExtractor.process_document() can proceed."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.execute(
        """
        INSERT INTO tariff_versions
            (historical_document_id, family_key, effective_start, source_type, source_pdf, created_at)
        VALUES (?, ?, '2022-01-01', 'docling_bridge', ?, datetime('now'))
        """,
        (doc_id, family_key, source_pdf),
    )
    version_id = cur.lastrowid
    conn.commit()
    conn.close()
    return version_id


def _make_record(
    tmp_path: Path,
    *,
    disc_id: int = 1,
    leaf_hint: str | None = None,
    file_hash: str = "testhash",
) -> NcucDiscoveryRecord:
    pdf_path = tmp_path / f"docling_test_{disc_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    return NcucDiscoveryRecord(
        id=disc_id,
        local_path=str(pdf_path),
        content_hash=file_hash,
        discovered_url=f"docling://{pdf_path}",
        filing_title=f"Duke Energy Progress Tariff Leaf {leaf_hint}" if leaf_hint else "Duke Energy Progress Tariff Filing",
        filing_date="2022-01-01",
        docket_number="E-2 Sub 1220",
        utility="Duke Energy Progress",
        filing_classification=NcucFilingClassification.TARIFF_SHEETS,
        fetch_status=NcucFetchStatus.SUCCESS,
        fetched_at=datetime.now(UTC),
    )


def _make_tariff_pages(leaf: str, n_pages: int = 2) -> list[PageEvidence]:
    """Return synthetic PageEvidence that looks like a tariff sheet for `leaf`."""
    pages = []
    for i in range(1, n_pages + 1):
        pages.append(
            PageEvidence(
                page_number=i,
                text_length=300,
                text_content=(
                    f"Leaf No. {leaf}\nRESIDENTIAL SERVICE\n"
                    f"Effective January 1, 2022\n"
                    f"Rate per kWh: $0.1234\n"
                ) if i == 1 else f"Page {i} continuation for Leaf {leaf}",
                has_leaf_header=(i == 1),
                extracted_leaf_nos=[leaf] if i == 1 else [],
                tariff_vocab_density=0.06,
                procedural_vocab_density=0.01,
            )
        )
    return pages


# ---------------------------------------------------------------------------
# Test 1: mine_discovery_record_spans_with_pages() creates HistoricalDocumentRecords
# ---------------------------------------------------------------------------

def test_seam_creates_historical_document(tmp_path: Path) -> None:
    """
    mine_discovery_record_spans_with_pages() should create at least one
    HistoricalDocumentRecord when given tariff-like pages and a matching family.
    """
    repo, db_path, settings = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "500")

    record = _make_record(tmp_path, disc_id=1, leaf_hint="500")
    pages = _make_tariff_pages("500", n_pages=2)

    importer = NcucPipelineImporter(settings, repo)
    created_ids = importer.mine_discovery_record_spans_with_pages(
        record,
        pages,
        page_artifact_version=DOCLING_PAGE_MINER_VERSION,
        page_metadata={"source_backend": "docling"},
    )

    assert len(created_ids) >= 1, "Expected at least one historical document to be created"

    conn = connect(Path(db_path))
    row = conn.execute(
        "SELECT family_key, state, company, start_page FROM historical_documents WHERE id = ?",
        (created_ids[0],),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["family_key"] == "nc-progress-leaf-500"
    assert row["state"] == "NC"
    assert row["company"] == "progress"


# ---------------------------------------------------------------------------
# Test 2: page artifacts are saved with DOCLING_PAGE_MINER_VERSION
# ---------------------------------------------------------------------------

def test_seam_saves_page_artifacts_with_docling_version(tmp_path: Path) -> None:
    """
    Page artifacts saved during mine_discovery_record_spans_with_pages()
    must use DOCLING_PAGE_MINER_VERSION, not PAGE_ARTIFACT_VERSION.
    """
    from duke_rates.db.artifact_cache import load_page_artifacts

    repo, db_path, settings = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-501", "501")

    record = _make_record(tmp_path, disc_id=2, leaf_hint="501", file_hash="hash-501")
    pages = _make_tariff_pages("501", n_pages=1)

    importer = NcucPipelineImporter(settings, repo)
    importer.mine_discovery_record_spans_with_pages(
        record,
        pages,
        page_artifact_version=DOCLING_PAGE_MINER_VERSION,
        page_metadata={"source_backend": "docling"},
    )

    conn = connect(Path(db_path))
    rows = conn.execute(
        "SELECT artifact_version FROM ncuc_page_artifacts WHERE source_pdf = ?",
        (record.local_path,),
    ).fetchall()
    conn.close()

    assert len(rows) >= 1
    assert all(r["artifact_version"] == DOCLING_PAGE_MINER_VERSION for r in rows), (
        f"Expected all page artifacts to use {DOCLING_PAGE_MINER_VERSION!r}, got: "
        f"{[r['artifact_version'] for r in rows]}"
    )


# ---------------------------------------------------------------------------
# Test 3: generic provisional family keys are filtered out (guardrail)
# ---------------------------------------------------------------------------

def test_seam_skips_generic_provisional_family(tmp_path: Path) -> None:
    """
    If the only matching family is a generic provisional key (e.g. NC-PROGRESS-DOC-XXX),
    the importer guardrail should either skip it or replace it with a proper provisional.
    Either way, no document with a generic key should appear.
    """
    from duke_rates.historical.ncuc.importer import _is_generic_provisional_family_key

    repo, db_path, settings = _make_repo(tmp_path)
    # Only seed a generic provisional family — no real leaf family
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-doc-TYPEOFSERVICE",
            state="NC",
            company="progress",
            tariff_identifier="doc-TYPEOFSERVICE",
            schedule_code=None,
            family_type="rate_schedule",
            title="Generic Doc Type Of Service",
        )
    )

    record = _make_record(tmp_path, disc_id=3)
    pages = _make_tariff_pages("999", n_pages=2)

    importer = NcucPipelineImporter(settings, repo)
    created_ids = importer.mine_discovery_record_spans_with_pages(
        record,
        pages,
        page_artifact_version=DOCLING_PAGE_MINER_VERSION,
    )

    conn = connect(Path(db_path))
    for doc_id in created_ids:
        row = conn.execute(
            "SELECT family_key FROM historical_documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if row:
            assert not _is_generic_provisional_family_key(row["family_key"]), (
                f"Expected no generic provisional family_key, got: {row['family_key']}"
            )
    conn.close()


# ---------------------------------------------------------------------------
# Test 4: Full bridge — Docling JSON → pages → importer → historical document
# ---------------------------------------------------------------------------

def test_full_bridge_docling_json_to_historical_document(tmp_path: Path) -> None:
    """
    End-to-end: Docling artifact JSON → mine_pages_from_docling_artifact()
    → mine_discovery_record_spans_with_pages() → HistoricalDocumentRecord.

    Verifies the two-step flow is wired correctly.
    """
    import json as _json

    repo, db_path, settings = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-604", "604", code="JAA")

    # Construct a synthetic Docling artifact with tariff text on page 1
    artifact = {
        "doc_json_content": _json.dumps({
            "texts": [
                {
                    "text": "Leaf No. 604\nRIDER JAA\nEffective January 1, 2022\nRate: $0.001 per kWh",
                    "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 700, "r": 500, "b": 680}}],
                },
                {
                    "text": "Continuation of Rider JAA terms and conditions.",
                    "prov": [{"page_no": 2, "bbox": {"l": 100, "t": 700, "r": 500, "b": 680}}],
                },
            ],
            "tables": [],
        }),
        "plain_text_content": "",
        "tables_json_content": "[]",
        "page_count": 2,
        "accelerator": "cpu",
        "pipeline": "standard",
        "file_hash": "hash-604",
    }

    pages, page_metadata = mine_pages_from_docling_artifact(artifact)
    assert len(pages) == 2, "Expected 2 pages from artifact"

    record = _make_record(tmp_path, disc_id=4, leaf_hint="604", file_hash="hash-604")
    importer = NcucPipelineImporter(settings, repo)
    created_ids = importer.mine_discovery_record_spans_with_pages(
        record,
        pages,
        page_artifact_version=DOCLING_PAGE_MINER_VERSION,
        page_metadata=page_metadata,
    )

    assert len(created_ids) >= 1, "Full Docling bridge should create at least one historical document"

    conn = connect(Path(db_path))
    row = conn.execute(
        "SELECT family_key, start_page, end_page FROM historical_documents WHERE id = ?",
        (created_ids[0],),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["family_key"] == "nc-progress-leaf-604"
    assert row["start_page"] is not None


# ---------------------------------------------------------------------------
# Test 5: BulkExtractor.process_document() creates parse_attempt_log entry
# ---------------------------------------------------------------------------

def test_bridge_creates_parse_attempt_log(tmp_path: Path) -> None:
    """
    After mine_discovery_record_spans_with_pages() creates a historical doc,
    BulkExtractor.process_document() must create a parse_attempt_logs row.
    """
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    repo, db_path, settings = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "500")

    record = _make_record(tmp_path, disc_id=5, leaf_hint="500", file_hash="hash-500-pal")
    pages = _make_tariff_pages("500", n_pages=2)

    importer = NcucPipelineImporter(settings, repo)
    created_ids = importer.mine_discovery_record_spans_with_pages(
        record,
        pages,
        page_artifact_version=DOCLING_PAGE_MINER_VERSION,
        page_metadata={"source_backend": "docling"},
    )
    assert created_ids, "Importer must create at least one historical document"

    extractor = BulkExtractor(db_path=db_path)
    for doc_id in created_ids:
        doc = extractor.get_document_for_extraction(doc_id)
        if doc:
            _seed_tariff_version(db_path, doc_id, doc["family_key"], doc["local_path"])
            extractor.process_document(doc)

    conn = connect(Path(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM parse_attempt_logs WHERE source_pdf IN ({})".format(
            ",".join("?" * len(created_ids))
        ),
        [record.local_path] * len(created_ids),
    ).fetchone()[0]
    conn.close()

    assert count >= 1, (
        f"Expected at least one parse_attempt_logs row for {record.local_path!r}, got {count}"
    )


# ---------------------------------------------------------------------------
# Test 6: BulkExtractor.process_document() creates document_fingerprints entry
# ---------------------------------------------------------------------------

def test_bridge_creates_document_fingerprint(tmp_path: Path) -> None:
    """
    After process_document(), a document_fingerprints row must exist.
    """
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    repo, db_path, settings = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "500")

    record = _make_record(tmp_path, disc_id=6, leaf_hint="500", file_hash="hash-500-fp")
    pages = _make_tariff_pages("500", n_pages=2)

    importer = NcucPipelineImporter(settings, repo)
    created_ids = importer.mine_discovery_record_spans_with_pages(
        record,
        pages,
        page_artifact_version=DOCLING_PAGE_MINER_VERSION,
        page_metadata={"source_backend": "docling"},
    )
    assert created_ids

    extractor = BulkExtractor(db_path=db_path)
    for doc_id in created_ids:
        doc = extractor.get_document_for_extraction(doc_id)
        if doc:
            _seed_tariff_version(db_path, doc_id, doc["family_key"], doc["local_path"])
            extractor.process_document(doc)

    conn = connect(Path(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM document_fingerprints WHERE source_pdf = ?",
        (record.local_path,),
    ).fetchone()[0]
    conn.close()

    assert count >= 1, (
        f"Expected at least one document_fingerprints row for {record.local_path!r}, got {count}"
    )


# ---------------------------------------------------------------------------
# Test 7: parse_review_outcomes row is visible after full bridge
# ---------------------------------------------------------------------------

def test_bridge_creates_parse_review_outcome(tmp_path: Path) -> None:
    """
    After the full bridge + extraction, a parse_review_outcomes row must exist.
    This is what parse-review-queue and parse-review-summary read.
    """
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    repo, db_path, settings = _make_repo(tmp_path)
    _seed_family(repo, "nc-progress-leaf-500", "500")

    record = _make_record(tmp_path, disc_id=7, leaf_hint="500", file_hash="hash-500-pro")
    pages = _make_tariff_pages("500", n_pages=2)

    importer = NcucPipelineImporter(settings, repo)
    created_ids = importer.mine_discovery_record_spans_with_pages(
        record,
        pages,
        page_artifact_version=DOCLING_PAGE_MINER_VERSION,
        page_metadata={"source_backend": "docling"},
    )
    assert created_ids

    extractor = BulkExtractor(db_path=db_path)
    for doc_id in created_ids:
        doc = extractor.get_document_for_extraction(doc_id)
        if doc:
            _seed_tariff_version(db_path, doc_id, doc["family_key"], doc["local_path"])
            extractor.process_document(doc)

    conn = connect(Path(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM parse_review_outcomes WHERE source_pdf = ?",
        (record.local_path,),
    ).fetchone()[0]
    conn.close()

    assert count >= 1, (
        f"Expected at least one parse_review_outcomes row for {record.local_path!r}, got {count}"
    )
