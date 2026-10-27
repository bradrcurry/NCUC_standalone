"""Phase 2C: bounded (span, chunked) structured representations for large filings."""

from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from duke_rates.document_intelligence.bounded import (  # noqa: E402
    bounded_representation,
    chunk_ranges,
)


def _pdf(tmp_path: Path, pages: int) -> Path:
    path = tmp_path / "filing.pdf"
    with fitz.open() as doc:
        for n in range(1, pages + 1):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {n} Rider JAA 0.0{n:02d} cents per kWh")
        doc.save(str(path))
    return path


def _docling_for(path: str) -> dict:
    """A minimal Docling document: one text item per page of the excerpt."""
    with fitz.open(path) as doc:
        count = doc.page_count
        texts = [doc[i].get_text().strip() for i in range(count)]
    return {
        "backend_version": "test-docling",
        "document": {
            "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(count)]},
            "furniture": {"children": []},
            "texts": [
                {
                    "self_ref": f"#/texts/{i}",
                    "label": "text",
                    "text": texts[i],
                    "prov": [{"page_no": i + 1, "bbox": {"l": 72, "t": 720, "r": 400, "b": 700, "coord_origin": "BOTTOMLEFT"}}],
                }
                for i in range(count)
            ],
            "tables": [],
            "pages": {str(i + 1): {"size": {"width": 612, "height": 792}} for i in range(count)},
        },
    }


def test_chunk_ranges_cover_the_span_exactly():
    assert chunk_ranges((17, 31), 8) == [(17, 24), (25, 31)]
    assert chunk_ranges((5, 5), 8) == [(5, 5)]


def test_only_the_span_is_converted_and_page_numbers_are_original(tmp_path):
    pdf = _pdf(tmp_path, 40)
    seen = []

    def converter(path):
        with fitz.open(path) as d:
            seen.append(d.page_count)
        return _docling_for(path)

    result = bounded_representation(pdf, (17, 31), chunk_pages=8, converter=converter)
    assert seen == [8, 7]  # never the whole 40-page filing
    rep = result.representation
    assert [p.page_number for p in rep.pages] == list(range(17, 32))
    assert "Page 17 " in rep.pages[0].text and "Page 31 " in rep.pages[-1].text
    assert not result.degraded and rep.document_metadata["bounded"]["chunks"] == [[17, 24], [25, 31]]


def test_failed_chunk_falls_back_to_native_and_is_marked_degraded(tmp_path):
    pdf = _pdf(tmp_path, 20)
    calls = {"n": 0}

    def converter(path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise MemoryError("simulated")
        return _docling_for(path)

    result = bounded_representation(pdf, (1, 20), chunk_pages=8, converter=converter)
    assert result.degraded
    assert result.degraded_chunks == [{"pages": [9, 16], "reason": "out of memory", "fallback": "native"}]
    rep = result.representation
    assert [p.page_number for p in rep.pages] == list(range(1, 21))
    backends = {p.page_number: p.backend.value for p in rep.pages}
    assert backends[9] == backends[16] != backends[1]
    assert rep.document_metadata["mixed_backends"] is True
    assert rep.document_metadata["bounded"]["degraded"] is True


def test_no_fallback_leaves_pages_missing_but_reports_them(tmp_path):
    pdf = _pdf(tmp_path, 10)
    result = bounded_representation(
        pdf, (1, 10), chunk_pages=5, converter=lambda p: None, native_fallback=False
    )
    assert result.representation is None and len(result.degraded_chunks) == 2


def test_native_adapter_parses_only_requested_pages(tmp_path, monkeypatch):
    from duke_rates.document_intelligence.adapters import representation_from_native_pdf

    pdf = _pdf(tmp_path, 30)
    rep = representation_from_native_pdf(pdf, page_range=(12, 14))
    assert [p.page_number for p in rep.pages] == [12, 13, 14]


def test_bulk_extractor_marks_degraded_bounded_input(tmp_path):
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    pdf = _pdf(tmp_path, 20)
    ext = BulkExtractor(str(tmp_path / "x.db"), structured_mode="hybrid")
    ext.bounded_conversion = True
    ext.bounded_converter = lambda path: None  # Docling fails on every chunk
    ext._docling_representation = lambda pdf_path, page_range: None  # nothing cached
    rep = ext._structured_representation({"local_path": str(pdf), "start_page": 3, "end_page": 6})
    assert [p.page_number for p in rep.pages] == [3, 4, 5, 6]
    assert rep.document_metadata["bounded"]["degraded"] is True
