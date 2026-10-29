"""Regression tests for D2: empty Docling pages in progressive OCR.

``_try_docling_ocr`` handed ``convert_pdf_with_docling``'s envelope (keys
``document`` / ``plain_text``) to ``mine_pages_from_docling_artifact``, which
reads ``doc_json_content`` / ``plain_text_content``.  Every Docling OCR page
came back empty, yet the list was returned as a result.  Separately,
``extract_pages_with_progressive_ocr`` labelled its fallback ``best_pages``
with ``attempted[-1]`` rather than the backend that produced them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from duke_rates.historical.ncuc.pipeline import docling_backend, ocr
from duke_rates.historical.ncuc.pipeline.page_miner import _extract_page_features_from_text

FIXTURES = Path(__file__).parent / "fixtures" / "docling"


def _leaf600_envelope() -> dict:
    """The shape ``convert_pdf_with_docling`` returns after a fresh conversion."""
    art = json.loads((FIXTURES / "dep_leaf600_2026-04-01.docling.json").read_text(encoding="utf-8"))
    return {
        "file_hash": art["file_hash"],
        "backend": "docling",
        "accelerator": "cuda",
        "pipeline": "standard",
        "page_count": art["page_count"],
        "conversion_status": "ConversionStatus.SUCCESS",
        "document": art["doc_json_content"],
        "plain_text": art["plain_text_content"],
        "tables": [],
    }


@pytest.fixture
def docling_available(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(docling_backend, "get_docling_unavailable_reason", lambda: None)

    def fake_convert(file_path: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return _leaf600_envelope()

    monkeypatch.setattr(docling_backend, "convert_pdf_with_docling", fake_convert)
    return calls


def test_try_docling_ocr_returns_nonempty_pages(docling_available: list[dict]) -> None:
    pages = ocr._try_docling_ocr("leaf600.pdf")

    assert pages is not None and len(pages) == 5
    assert all(p.text_length > 0 for p in pages)
    assert "(0.249)" in pages[0].text_content  # table cell value (EDIT-4 credit)
    # It is an OCR backend: the conversion must actually enable OCR.
    assert docling_available[0].get("has_scanned_pages") is True


def test_try_docling_ocr_treats_empty_output_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docling_backend, "get_docling_unavailable_reason", lambda: None)
    empty = _leaf600_envelope()
    empty["document"] = {"texts": [], "tables": [], "body": {"children": []}}
    empty["plain_text"] = ""
    monkeypatch.setattr(docling_backend, "convert_pdf_with_docling", lambda *a, **k: empty)

    assert ocr._try_docling_ocr("blank.pdf") is None


def _weak_pages(text: str, n: int = 2) -> list:
    return [_extract_page_features_from_text(text, i + 1) for i in range(n)]


def test_progressive_ocr_attributes_best_pages_to_their_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Weak Tesseract text kept as best must not be labelled as GLM output."""
    monkeypatch.setattr(ocr, "extract_ocr_document_pages", lambda *a, **k: _weak_pages("Leaf 600"))
    monkeypatch.setattr(ocr, "_try_docling_ocr", lambda path: None)
    monkeypatch.setattr(ocr, "_try_glm_ocr", lambda path: _weak_pages("x"))

    pages, backend, attempted = ocr.extract_pages_with_progressive_ocr("scan.pdf")

    assert attempted == [ocr.OCR_BACKEND_PYTESSERACT, ocr.OCR_BACKEND_DOCLING, ocr.OCR_BACKEND_GLM]
    assert pages[0].text_content == "Leaf 600"
    assert backend == ocr.OCR_BACKEND_PYTESSERACT


def test_progressive_ocr_escalates_past_empty_docling_to_glm(monkeypatch: pytest.MonkeyPatch) -> None:
    good = "NC Ninth Revised Leaf No. 600 Summary of Rider Adjustments " * 4
    monkeypatch.setattr(ocr, "extract_ocr_document_pages", lambda *a, **k: [])
    monkeypatch.setattr(ocr, "_try_docling_ocr", lambda path: None)
    monkeypatch.setattr(ocr, "_try_glm_ocr", lambda path: _weak_pages(good))

    pages, backend, _ = ocr.extract_pages_with_progressive_ocr("scan.pdf")

    assert backend == ocr.OCR_BACKEND_GLM
    assert pages and pages[0].text_length > 0


def test_progressive_ocr_tesseract_success_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    good = "Residential Service Schedule RES Basic Customer Charge $14.00 per month " * 3
    monkeypatch.setattr(ocr, "extract_ocr_document_pages", lambda *a, **k: _weak_pages(good))

    def boom(path: str):  # type: ignore[no-untyped-def]
        raise AssertionError("GPU backends should not run when Tesseract text is good")

    monkeypatch.setattr(ocr, "_try_docling_ocr", boom)
    monkeypatch.setattr(ocr, "_try_glm_ocr", boom)

    pages, backend, attempted = ocr.extract_pages_with_progressive_ocr("scan.pdf")

    assert backend == ocr.OCR_BACKEND_PYTESSERACT
    assert attempted == [ocr.OCR_BACKEND_PYTESSERACT]
    assert len(pages) == 2
