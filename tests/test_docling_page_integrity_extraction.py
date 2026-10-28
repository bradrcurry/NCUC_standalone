"""Phase 2E M1: structured extraction never reads wrong-page Docling structure.

Reproduces the chunked-artifact defect found in the Phase 2E reconciliation:
``convert_pdf_safe``'s chunk fallback stored only the last chunk of a 106-page
compliance filing (artifact 1661, e-2-sub-1300-sept2023, sha256 b61e2940...),
numbered 1..6. Its "page 1" is a Leaf 600 rider-summary redline from PDF pages
~100-106; the real page 1 is the Index of Tariffs. A leaf-600 span on page 1
(hd 2275) read 23 rider charges from it, each citing page 1.

The fixtures here are synthetic PDFs plus the committed Leaf 600 Docling
document; no production data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import fitz
import pytest

from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.document_intelligence import page_integrity
from duke_rates.document_intelligence.models import (
    DocumentRepresentation,
    LayoutBlock,
    NormalizationBackend,
    PageRepresentation,
)
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.historical.ncuc.pipeline.candidate_runs import disable_recorders, to_candidate_charge
from duke_rates.historical.ncuc.pipeline.rate_extractor import ExtractedCharge

ROOT = Path(__file__).resolve().parents[1]
LEAF600 = json.loads(
    (ROOT / "tests/fixtures/docling/dep_leaf600_2026-04-01.docling.json").read_text(encoding="utf-8")
)
CHUNKED = {"degraded_modes": ["chunked"], "skipped_pages": [], "used_chunking": True}


def _pdf(path: Path, pages: int, first_page_text: str | None = None) -> Path:
    doc = fitz.open()
    for i in range(1, pages + 1):
        page = doc.new_page()
        text = first_page_text if (i == 1 and first_page_text) else f"Index of Tariffs page {i}\nLeaf {500 + i}"
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()
    return path


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "scratch.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.commit()
    conn.close()
    return path


def _insert(db: Path, pdf: Path, doc_json: dict, *, page_count: int, metadata: dict | None = None,
            status: str = "ConversionStatus.SUCCESS") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO docling_artifacts (source_pdf, file_hash, backend_version, accelerator,
            pipeline, status, page_count, table_count, plain_text_content, doc_json_content,
            tables_json_content, metadata_json, created_at, updated_at)
        VALUES (?, ?, 'docling_v2', 'cuda', 'standard', ?, ?, ?, ?, ?, '[]', ?, 'now', 'now')
        """,
        (str(pdf), hashlib.sha256(pdf.read_bytes()).hexdigest(), status, page_count,
         len(doc_json.get("tables") or []), LEAF600["plain_text_content"], json.dumps(doc_json),
         json.dumps(metadata or {})),
    )
    conn.commit()
    conn.close()


def _text_doc(pages_with_items: list[int], all_pages: list[int]) -> dict:
    return {
        "pages": {str(p): {"page_no": p, "size": {"width": 612, "height": 792}} for p in all_pages},
        "texts": [
            {"self_ref": f"#/texts/{i}", "label": "text", "text": f"Docling text on page {p}",
             "prov": [{"page_no": p, "bbox": {"l": 72, "t": 720, "r": 300, "b": 700, "coord_origin": "BOTTOMLEFT"}}]}
            for i, p in enumerate(pages_with_items)
        ],
        "tables": [],
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(len(pages_with_items))]},
    }


def _leaf600_doc(pdf: Path, start: int, end: int) -> dict:
    return {
        "id": 1, "family_key": "nc-progress-leaf-600", "local_path": str(pdf),
        "effective_start": "2026-04-01", "start_page": start, "end_page": end,
        "title": "Leaf No. 600 Summary of Rider Adjustments", "company": "progress",
    }


def _extract(db: Path, doc: dict, mode: str = "hybrid"):
    ext = BulkExtractor(str(db), structured_mode=mode)
    disable_recorders(ext)
    return ext.extract_charges_from_document(doc)


LEAF600_PAGE1 = (
    "Duke Energy Progress, LLC (North Carolina Only)\nNC Ninth Revised Leaf No. 600\n"
    "SUMMARY OF RIDER ADJUSTMENTS"
)


# ---------------------------------------------------------------------------
# Representation selection
# ---------------------------------------------------------------------------


def test_chunk_relative_artifact_is_rejected_with_a_reason(tmp_path):
    pdf = _pdf(tmp_path / "bundle.pdf", 106)
    db = _db(tmp_path)
    _insert(db, pdf, LEAF600["doc_json_content"], page_count=106, metadata=CHUNKED, status="success")
    ext = BulkExtractor(str(db), structured_mode="hybrid")
    rep, verdict = ext._docling_selection(str(pdf), (1, 3))
    assert rep is None
    assert verdict["status"] == "unverifiable" and verdict["reason"] == "chunk_relative_numbering"
    assert verdict["file_hash_match"] is True and verdict["pdf_page_count"] == 106
    assert ext._docling_representation(str(pdf), (1, 3)) is None


def test_complete_artifact_keeps_original_page_numbers(tmp_path):
    pdf = _pdf(tmp_path / "leaf600.pdf", 5)
    db = _db(tmp_path)
    _insert(db, pdf, LEAF600["doc_json_content"], page_count=5)
    ext = BulkExtractor(str(db), structured_mode="hybrid")
    whole, verdict = ext._docling_selection(str(pdf), None)
    assert verdict["status"] == "valid" and whole.pages[0].page_number == 1
    assert whole.document_metadata["page_integrity"]["numbering"] == "absolute"
    bounded, _ = ext._docling_selection(str(pdf), (2, 3))
    assert {p.page_number for p in bounded.pages} <= {2, 3}


def test_hybrid_falls_back_to_native_and_docling_mode_does_not(tmp_path):
    pdf = _pdf(tmp_path / "bundle.pdf", 106)
    db = _db(tmp_path)
    _insert(db, pdf, LEAF600["doc_json_content"], page_count=106, metadata=CHUNKED, status="success")
    doc = {"local_path": str(pdf), "start_page": 1, "end_page": 3}

    diag: dict = {}
    rep = BulkExtractor(str(db), structured_mode="hybrid")._structured_representation(doc, diag)
    assert rep.normalizer_backend == NormalizationBackend.NATIVE_PDF
    assert [p.page_number for p in rep.pages] == [1, 2, 3]
    assert "Index of Tariffs page 2" in rep.pages[1].text
    assert diag["docling_fallback_reason"] == "chunk_relative_numbering"

    diag = {}
    assert BulkExtractor(str(db), structured_mode="docling")._structured_representation(doc, diag) is None
    assert diag["docling_fallback_reason"] == "chunk_relative_numbering"


def test_span_beyond_the_chunk_no_longer_suppresses_native(tmp_path):
    # Before M1 an empty (not None) Docling representation skipped the native read.
    pdf = _pdf(tmp_path / "bundle.pdf", 106)
    db = _db(tmp_path)
    _insert(db, pdf, LEAF600["doc_json_content"], page_count=106, metadata=CHUNKED, status="success")
    rep = BulkExtractor(str(db), structured_mode="hybrid")._structured_representation(
        {"local_path": str(pdf), "start_page": 53, "end_page": 56}
    )
    assert rep.normalizer_backend == NormalizationBackend.NATIVE_PDF
    assert [p.page_number for p in rep.pages] == [53, 54, 55, 56]


def test_valid_artifact_with_an_empty_slice_falls_back_in_hybrid(tmp_path):
    pdf = _pdf(tmp_path / "doc.pdf", 4)
    db = _db(tmp_path)
    _insert(db, pdf, _text_doc([1], [1, 2, 3, 4]), page_count=4)
    doc = {"local_path": str(pdf), "start_page": 3, "end_page": 3}
    diag: dict = {}
    rep = BulkExtractor(str(db), structured_mode="hybrid")._structured_representation(doc, diag)
    assert rep.normalizer_backend == NormalizationBackend.NATIVE_PDF
    assert diag["docling_fallback_reason"] == "empty_slice"
    assert diag["docling_page_integrity"]["status"] == "valid"


def test_partial_coverage_falls_back_in_hybrid_and_is_degraded_in_docling_mode(tmp_path):
    pdf = _pdf(tmp_path / "doc.pdf", 10)
    db = _db(tmp_path)
    # Whole-document conversion that lost pages 4-6.
    pages = [1, 2, 3, 7, 8, 9, 10]
    _insert(db, pdf, _text_doc(pages, pages), page_count=10)
    doc = {"local_path": str(pdf), "start_page": 2, "end_page": 5}
    diag: dict = {}
    rep = BulkExtractor(str(db), structured_mode="hybrid")._structured_representation(doc, diag)
    assert rep.normalizer_backend == NormalizationBackend.NATIVE_PDF
    assert diag["docling_fallback_reason"] == "span_partially_covered"
    diag = {}
    rep = BulkExtractor(str(db), structured_mode="docling")._structured_representation(doc, diag)
    assert [p.page_number for p in rep.pages] == [2, 3]  # true numbers, never relabeled
    assert diag["degraded"] is True and "4-5" in diag["degraded_reason"]


def test_valid_artifact_without_tables_is_kept(tmp_path):
    pdf = _pdf(tmp_path / "doc.pdf", 3)
    db = _db(tmp_path)
    _insert(db, pdf, _text_doc([1, 2, 3], [1, 2, 3]), page_count=3)
    diag: dict = {}
    rep = BulkExtractor(str(db), structured_mode="hybrid")._structured_representation(
        {"local_path": str(pdf), "start_page": 2, "end_page": 2}, diag
    )
    assert rep.normalizer_backend == NormalizationBackend.DOCLING
    assert [p.page_number for p in rep.pages] == [2] and not rep.pages[0].tables
    assert "docling_fallback_reason" not in diag


# ---------------------------------------------------------------------------
# End to end: the Leaf 600 contamination
# ---------------------------------------------------------------------------


def _contaminated_bundle(tmp_path):
    pdf = _pdf(tmp_path / "1b72e8de.pdf", 106, first_page_text=LEAF600_PAGE1)
    db = _db(tmp_path)
    _insert(db, pdf, LEAF600["doc_json_content"], page_count=106, metadata=CHUNKED, status="success")
    return pdf, db


def test_fixture_reproduces_wrong_page_contamination_without_the_guard(tmp_path, monkeypatch):
    """Pre-M1 behavior: the chunk's Leaf 600 tables become charges citing page 1."""
    pdf, db = _contaminated_bundle(tmp_path)
    original = page_integrity.assess_docling_page_integrity

    def trust_everything(doc_json, **kw):
        verdict = original(doc_json, **{**kw, "metadata": None, "artifact_page_count": None,
                                        "pdf_page_count": 5})
        return page_integrity.PageIntegrity(**{**verdict.__dict__, "pdf_page_count": 106})

    monkeypatch.setattr(page_integrity, "assess_docling_page_integrity", trust_everything)
    charges, *_rest, selection = _extract(db, _leaf600_doc(pdf, 1, 1))
    assert len(charges) >= 20
    assert selection["structured_input"]["backend"] == "docling"
    assert all(BulkExtractor._evidence_pages(c.evidence) == {1} for c in charges)


def test_leaf600_contamination_is_blocked_in_hybrid(tmp_path):
    pdf, db = _contaminated_bundle(tmp_path)
    charges, profile, _c, status, _s, _t, selection = _extract(db, _leaf600_doc(pdf, 1, 1))
    structured = selection["structured_input"]
    assert structured["backend"] == "native_pdf"
    assert structured["docling_fallback_reason"] == "chunk_relative_numbering"
    assert structured["docling_page_integrity"]["status"] == "unverifiable"
    # No charge carries Docling evidence, and none cites a page outside the span.
    for charge in charges:
        assert (charge.evidence or {}).get("backend") != "docling"
        assert BulkExtractor._evidence_pages(charge.evidence) <= {1}
    # The leaked rider values (BA-Fuel 0.262 c/kWh etc.) are not produced.
    assert not any(abs(c.rate_value - 0.00262) < 1e-9 for c in charges)


def test_leaf600_contamination_is_blocked_in_docling_mode(tmp_path):
    pdf, db = _contaminated_bundle(tmp_path)
    charges, *_rest, selection = _extract(db, _leaf600_doc(pdf, 1, 1), mode="docling")
    assert selection["structured_input"]["backend"] is None
    assert not any((c.evidence or {}).get("backend") == "docling" for c in charges)


def test_accepted_candidates_keep_correct_source_pages(tmp_path):
    # The same Leaf 600 document, correctly converted as a whole 5-page PDF.
    pdf = _pdf(tmp_path / "leaf600.pdf", 5, first_page_text=LEAF600_PAGE1)
    db = _db(tmp_path)
    _insert(db, pdf, LEAF600["doc_json_content"], page_count=5)
    charges, profile, _c, _st, _s, _t, selection = _extract(db, _leaf600_doc(pdf, 1, 5))
    assert selection["structured_input"]["docling_page_integrity"]["status"] == "valid"
    assert "rejected_evidence" not in selection["structured_input"]
    docling_charges = [c for c in charges if (c.evidence or {}).get("backend") == "docling"]
    assert len(docling_charges) >= 20
    for charge in docling_charges:
        candidate = to_candidate_charge(charge, source_pdf_path=str(pdf), source_pdf_sha256=None,
                                        profile=profile)
        assert candidate.page_number == charge.evidence["page"]
        assert 1 <= candidate.page_number <= 5


# ---------------------------------------------------------------------------
# Provenance guard
# ---------------------------------------------------------------------------


def _rep(pages: list[int]) -> DocumentRepresentation:
    return DocumentRepresentation(
        source_pdf="x.pdf",
        raw_text="",
        normalizer_backend=NormalizationBackend.DOCLING,
        pages=[
            PageRepresentation(page_number=p, text="t", blocks=[LayoutBlock(block_type="text", text="t", page_number=p)])
            for p in pages
        ],
    )


def _charge(evidence):
    return ExtractedCharge(
        charge_type="rider", charge_label="EDIT-4", rate_value=-0.00249, rate_unit="$/kWh",
        season="all_year", source_snippet="", confidence_score=0.9, evidence=evidence,
    )


def test_evidence_citing_pages_outside_the_representation_is_dropped():
    good = _charge({"page": 2, "values": [{"page": 2, "text": "(0.249)"}]})
    nested = _charge({"header": {"page": 2}, "values": [{"page": 9, "text": "(0.249)"}]})
    orphan = _charge({"page": 1})
    rejected = BulkExtractor._reject_unverified_evidence([good, nested], _rep([2, 3]))
    assert good.evidence is not None and nested.evidence is None
    assert rejected == [{"charge_label": "EDIT-4", "rate_value": -0.00249, "pages": [9]}]
    # Without any verified representation, no page can be claimed.
    assert BulkExtractor._reject_unverified_evidence([orphan], None)[0]["pages"] == [1]
    assert orphan.evidence is None


@pytest.mark.parametrize(
    "structured, codes",
    [
        ({"backend": "native_pdf", "docling_fallback_reason": "chunk_relative_numbering",
          "docling_page_integrity": {"artifact_id": 1661}}, {"docling_artifact_unusable": "info"}),
        ({"backend": "native_pdf", "docling_fallback_reason": "no_artifact"}, {}),
        ({"backend": "docling", "rejected_evidence": [{"pages": [9]}]},
         {"structured_evidence_rejected": "warning"}),
    ],
)
def test_candidate_runs_record_artifact_fallback_findings(migrated, structured, codes):
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import DocumentExtraction
    from duke_rates.historical.ncuc.pipeline.candidate_runs import record_candidate_run

    conn, ids, path, pdf = migrated
    extraction = DocumentExtraction(
        doc={"id": ids["hd_id"], "family_key": "nc-progress-leaf-500", "local_path": str(pdf),
             "effective_start": "2024-10-01", "start_page": None, "end_page": None},
        version_id=ids["version_id"], structured_mode="hybrid", charges=[],
        parser_profile="test_profile", ranked_candidates=[], status="empty", signals=None,
        text_metrics={"full_text": "", "text_source": "stub"},
        selection_details={"structured_input": {"mode": "hybrid", **structured}},
    )
    result = record_candidate_run(path, extraction, created_by="pytest")
    # seal_run stores producer "review" findings as warnings that need a person.
    found = {i["code"]: i["level"] for i in result["run_issues"]
             if i["code"] in ("docling_artifact_unusable", "structured_evidence_rejected",
                              "degraded_structured_input")}
    assert found == codes


from test_extraction_provenance import db, migrated  # noqa: E402,F401  (fixtures)
