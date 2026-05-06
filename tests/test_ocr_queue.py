from __future__ import annotations

import json
from datetime import UTC, datetime

from duke_rates.db.ocr_queue import (
    DEFAULT_OCR_BACKEND,
    claim_next_ocr_queue_item,
    complete_ocr_queue_item,
    enqueue_ocr_candidates,
    upsert_ocr_artifact,
)
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.pipeline.ocr import (
    OCR_BACKEND_AUTO,
    OCR_BACKEND_OCRMYPDF,
    OCR_BACKEND_PYTESSERACT,
    _compute_file_hash,
    _ocr_pages_sidecar_path,
    _ocr_text_sidecar_path,
    extract_ocr_document_pages,
    get_ocr_backend_unavailable_reason,
    load_ocr_sidecar_payload,
    summarize_ocr_payload,
)
from duke_rates.historical.ncuc.pipeline.stage_versions import OCR_NORMALIZATION_VERSION
from duke_rates.models.pipeline import PageEvidence, PipelineRoute


def test_extract_ocr_document_pages_uses_cached_sidecar(tmp_path) -> None:
    pdf_path = tmp_path / "ocr-source.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    file_hash = _compute_file_hash(str(pdf_path))

    cached_page = PageEvidence(
        page_number=1,
        text_length=48,
        text_content="Leaf No. 500\nResidential Service\nCustomer Charge",
        has_leaf_header=True,
        extracted_leaf_nos=["500"],
    )
    _ocr_text_sidecar_path(str(pdf_path)).write_text(
        cached_page.text_content or "",
        encoding="utf-8",
    )
    _ocr_pages_sidecar_path(str(pdf_path)).write_text(
        json.dumps(
            {
                "file_hash": file_hash,
                "backend": "pytesseract_cpu",
                "page_count": 1,
                "pages": [cached_page.model_dump(mode="json")],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    pages = extract_ocr_document_pages(str(pdf_path))

    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert pages[0].has_leaf_header is True
    assert pages[0].extracted_leaf_nos == ["500"]


def test_get_ocr_backend_unavailable_reason_rejects_unknown_backend() -> None:
    reason = get_ocr_backend_unavailable_reason("unknown_backend")
    assert reason == "OCR backend unsupported: unknown_backend"


def test_extract_ocr_document_pages_falls_back_to_pytesseract_when_ocrmypdf_unavailable(tmp_path, monkeypatch) -> None:
    pdf_path = tmp_path / "fallback.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        "duke_rates.historical.ncuc.pipeline.ocr.get_ocr_backend_unavailable_reason",
        lambda backend=OCR_BACKEND_AUTO: (
            "missing ocrmypdf"
            if backend == OCR_BACKEND_OCRMYPDF
            else None
        ),
    )
    monkeypatch.setattr(
        "duke_rates.historical.ncuc.pipeline.ocr._extract_pages_with_pytesseract",
        lambda _path, max_pages=None: (
            ["Leaf No. 500\nCustomer Charge"],
            [
                PageEvidence(
                    page_number=1,
                    text_length=29,
                    text_content="Leaf No. 500\nCustomer Charge",
                    has_leaf_header=True,
                    extracted_leaf_nos=["500"],
                )
            ],
        ),
    )

    pages = extract_ocr_document_pages(str(pdf_path), backend=OCR_BACKEND_AUTO, force=True)

    assert len(pages) == 1
    payload = json.loads(_ocr_pages_sidecar_path(str(pdf_path)).read_text(encoding="utf-8"))
    assert payload["backend"] == OCR_BACKEND_PYTESSERACT
    assert payload["metadata"]["requested_backend"] == OCR_BACKEND_AUTO
    assert payload["metadata"]["attempted_backends"] == [OCR_BACKEND_OCRMYPDF, OCR_BACKEND_PYTESSERACT]


def test_summarize_ocr_payload_reports_backend_provenance(tmp_path) -> None:
    pdf_path = tmp_path / "summary.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    _ocr_pages_sidecar_path(str(pdf_path)).write_text(
        json.dumps(
            {
                "file_hash": "abc",
                "backend": OCR_BACKEND_OCRMYPDF,
                "backend_version": "v-test",
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                "page_count": 2,
                "metadata": {
                    "attempted_backends": [OCR_BACKEND_OCRMYPDF],
                },
                "pages": [
                    PageEvidence(page_number=1, text_length=10, text_content="1234567890").model_dump(mode="json"),
                    PageEvidence(page_number=2, text_length=20, text_content="x" * 20).model_dump(mode="json"),
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = summarize_ocr_payload(load_ocr_sidecar_payload(str(pdf_path)))

    assert summary["selected_backend"] == OCR_BACKEND_OCRMYPDF
    assert summary["attempted_backends"] == [OCR_BACKEND_OCRMYPDF]
    assert summary["ocr_normalization_version"] == OCR_NORMALIZATION_VERSION
    assert summary["page_count"] == 2
    assert summary["avg_text_length"] == 15.0
    assert summary["max_text_length"] == 20


def test_ocr_queue_enqueue_and_completion(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "needs-ocr.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    file_hash = _compute_file_hash(str(pdf_path))

    discovery_id = conn.execute(
        """
        INSERT INTO ncuc_discovery_records (
            docket_number, utility, filing_title, filing_date, fetch_status,
            local_path, content_hash, created_at, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "E-2",
            "Duke Energy Progress",
            "Scanned compliance filing",
            "2024-01-15",
            "success",
            str(pdf_path),
            file_hash,
            now,
            now,
        ),
    ).lastrowid
    conn.commit()

    monkeypatch.setattr(
        "duke_rates.db.ocr_queue.triage_pdf",
        lambda _path: type(
            "Triage",
            (),
            {
                "route_recommendation": PipelineRoute.OCR_REQUIRED,
                "ocr_confidence_score": 0.91,
                "structure_complexity_score": 0.42,
                "gpu_ocr_candidate": False,
                "triage_flags": ["ocr_required_high_confidence"],
            },
        )(),
    )

    report = enqueue_ocr_candidates(conn, limit=10, requested_by="test-suite")
    conn.commit()
    assert report["inserted"] == 1
    assert DEFAULT_OCR_BACKEND == OCR_BACKEND_OCRMYPDF

    claimed = claim_next_ocr_queue_item(conn)
    conn.commit()
    assert claimed is not None
    assert claimed["discovery_record_id"] == discovery_id
    assert claimed["status"] == "running"
    assert claimed["backend"] == OCR_BACKEND_OCRMYPDF

    _ocr_text_sidecar_path(str(pdf_path)).write_text("OCR text", encoding="utf-8")
    _ocr_pages_sidecar_path(str(pdf_path)).write_text(
        json.dumps(
            {
                "file_hash": file_hash,
                "backend": "pytesseract_cpu",
                "page_count": 1,
                "pages": [
                    PageEvidence(
                        page_number=1,
                        text_length=8,
                        text_content="OCR text",
                    ).model_dump(mode="json")
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    artifact_id = upsert_ocr_artifact(
        conn,
        discovery_record_id=discovery_id,
        source_pdf=str(pdf_path),
        file_hash=file_hash,
        backend="pytesseract_cpu",
        status="completed",
        text_sidecar_path=str(_ocr_text_sidecar_path(str(pdf_path))),
        pages_sidecar_path=str(_ocr_pages_sidecar_path(str(pdf_path))),
        page_count=1,
        ocr_confidence=0.91,
        metadata={"queued_by": "test-suite"},
    )
    complete_ocr_queue_item(
        conn,
        queue_id=int(claimed["id"]),
        status="completed",
        latest_artifact_id=artifact_id,
        metadata={"page_count": 1},
    )
    conn.commit()

    queue_row = conn.execute(
        """
        SELECT status, latest_artifact_id, metadata_json
        FROM ocr_processing_queue
        WHERE id = ?
        """,
        (claimed["id"],),
    ).fetchone()
    assert queue_row is not None
    assert queue_row["status"] == "completed"
    assert queue_row["latest_artifact_id"] == artifact_id
    assert json.loads(queue_row["metadata_json"])["page_count"] == 1

    artifact_row = conn.execute(
        """
        SELECT discovery_record_id, status, text_sidecar_path, pages_sidecar_path, page_count
        FROM ocr_artifacts
        WHERE id = ?
        """,
        (artifact_id,),
    ).fetchone()
    assert artifact_row is not None
    assert artifact_row["discovery_record_id"] == discovery_id
    assert artifact_row["status"] == "completed"
    assert artifact_row["page_count"] == 1
    conn.close()
