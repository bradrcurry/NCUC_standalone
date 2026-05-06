from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.document_intelligence.fingerprinting import HybridDocumentFingerprinter
from duke_rates.document_intelligence.models import DocumentType, ParseLane
from duke_rates.document_intelligence.service import (
    DocumentIntelligenceOrchestrator,
    HistoricalDocumentIntelligenceContext,
)
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "document-intelligence.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.close()
    return db_path


def test_orchestrator_builds_snapshot_and_training_record(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    pdf_path = project_root / "sample.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    orchestrator = DocumentIntelligenceOrchestrator(project_root=project_root)
    snapshot = orchestrator.analyze_historical_document(
        {
            "id": 42,
            "local_path": str(pdf_path),
            "content_hash": "hash-42",
            "company": "progress",
            "state": "NC",
            "family_key": "nc-progress-leaf-503",
            "title": "Rider CPP",
            "effective_start": "2025-01-01",
            "leaf_no": "503",
            "start_page": 1,
            "end_page": 1,
        },
        raw_text=(
            "NC Original Leaf No. 503\n"
            "Rider CPP\n"
            "Effective January 1, 2025\n"
            "Monthly Rate\n"
            "0.1234 $/kWh"
        ),
        page_artifacts=[
            {
                "page_number": 1,
                "text_content": "NC Original Leaf No. 503\nRider CPP\n0.1234 $/kWh",
                "metadata": {"source": "unit-test"},
            }
        ],
        context=HistoricalDocumentIntelligenceContext(
            parser_profile="progress_single_value_rider",
            charge_count=1,
            status="ok",
            errors=[],
        ),
    )

    assert snapshot.fingerprint.doc_type == DocumentType.RIDER
    assert snapshot.fingerprint.parse_lane == ParseLane.DETERMINISTIC
    assert snapshot.extraction.data["rider_code"] == "CPP"
    assert snapshot.training_record.historical_document_id == 42

    training_path = (
        project_root / "data" / "processed" / "document_intelligence" / "training_records.jsonl"
    )
    lines = training_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["family_key"] == "nc-progress-leaf-503"
    assert payload["doc_type"] == "rider"


def test_fingerprinter_skips_commission_orders() -> None:
    representation = {
        "source_pdf": "order.pdf",
        "raw_text": "Order Dated January 1, 2025 before the North Carolina Utilities Commission",
        "title": "Commission Order Approving Tariff",
        "family_key": "nc-carolinas-doc-order",
        "pages": [],
        "document_metadata": {},
    }
    from duke_rates.document_intelligence.models import DocumentRepresentation

    result = HybridDocumentFingerprinter().fingerprint(
        DocumentRepresentation.model_validate(representation)
    )

    assert result.doc_type == DocumentType.COMMISSION_ORDER
    assert result.parse_lane == ParseLane.SKIP


def test_fingerprinter_detects_redline_documents() -> None:
    representation = {
        "source_pdf": "redline.pdf",
        "raw_text": "Redline version showing deleted text and inserted text for Schedule RS",
        "title": "Schedule RS Redline",
        "family_key": "nc-carolinas-doc-rs-redline",
        "pages": [],
        "document_metadata": {},
    }
    from duke_rates.document_intelligence.models import DocumentRepresentation

    result = HybridDocumentFingerprinter().fingerprint(
        DocumentRepresentation.model_validate(representation)
    )

    assert result.doc_type == DocumentType.REDLINE
    assert result.parse_lane == ParseLane.LLM_ASSISTED
    assert "redline_marker" in result.features_detected


def test_bulk_extractor_record_parse_attempt_persists_document_intelligence(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    extractor = BulkExtractor(str(db_path))
    doc = {
        "id": 7,
        "family_key": "nc-progress-leaf-503",
        "company": "progress",
        "title": "Rider CPP",
        "local_path": str(tmp_path / "leaf-503.pdf"),
        "effective_start": "2025-01-01",
        "start_page": 1,
        "end_page": 1,
    }

    parse_attempt_id = extractor.record_parse_attempt(
        doc,
        parser_profile="progress_single_value_rider",
        ranked_candidates=[],
        signals=None,
        charge_count=1,
        status="ok",
        document_intelligence={"fingerprint": {"doc_type": "rider"}},
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT metadata_json FROM parse_attempt_logs WHERE id = ?",
        (parse_attempt_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    metadata = json.loads(row[0])
    assert metadata["historical_document_id"] == 7
    assert metadata["document_intelligence"]["fingerprint"]["doc_type"] == "rider"
