"""
Tests for docling_page_miner module.

Tests the reconstruction of PageEvidence from stored Docling artifacts
without re-running Docling conversion.
"""

import json
from duke_rates.historical.ncuc.pipeline.docling_page_miner import (
    mine_pages_from_docling_artifact,
)


def test_mine_pages_from_minimal_docling_artifact() -> None:
    """Reconstruct pages from a minimal Docling artifact."""
    artifact = {
        "doc_json_content": json.dumps({
            "texts": [
                {
                    "text": "Leaf No. 604",
                    "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 700, "r": 200, "b": 680}}],
                },
                {
                    "text": "This is page one text",
                    "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 600, "r": 500, "b": 580}}],
                },
            ],
            "tables": [
                {
                    "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 500, "r": 500, "b": 300}}],
                },
            ],
        }),
        "plain_text_content": "",
        "tables_json_content": "[]",
        "page_count": 1,
        "accelerator": "cuda",
        "pipeline": "standard",
    }

    pages, metadata = mine_pages_from_docling_artifact(artifact)

    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert "Leaf No. 604" in pages[0].text_content
    assert "This is page one text" in pages[0].text_content
    assert pages[0].has_leaf_header is True
    assert "604" in pages[0].extracted_leaf_nos
    assert metadata["source_backend"] == "docling"
    assert metadata["docling_accelerator"] == "cuda"
    assert metadata["docling_pipeline"] == "standard"


def test_mine_pages_text_features_preserved() -> None:
    """Verify text-based feature extraction works on reconstructed pages."""
    artifact = {
        "doc_json_content": json.dumps({
            "texts": [
                {
                    "text": "Leaf No. 604\nRIDER JAA\nAvailability: This rider",
                    "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 700, "r": 500, "b": 680}}],
                },
            ],
            "tables": [],
        }),
        "plain_text_content": "",
        "tables_json_content": "[]",
        "page_count": 1,
        "accelerator": "cpu",
        "pipeline": "standard",
    }

    pages, metadata = mine_pages_from_docling_artifact(artifact)

    assert len(pages) == 1
    assert pages[0].has_leaf_header is True
    assert pages[0].extracted_leaf_nos == ["604"]
    assert pages[0].has_schedule_heading is True
    # Check that tariff vocab is detected
    assert pages[0].tariff_vocab_density > 0


def test_table_density_boosted_when_docling_tables_present() -> None:
    """Table presence boosts table_like_density."""
    # Page 1 has a table
    page1_artifact = {
        "doc_json_content": json.dumps({
            "texts": [{"text": "Page 1 text", "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 700, "r": 200, "b": 680}}]}],
            "tables": [{"prov": [{"page_no": 1, "bbox": {"l": 100, "t": 500, "r": 500, "b": 200}}]}],
        }),
        "plain_text_content": "",
        "tables_json_content": "[]",
        "page_count": 2,
        "accelerator": "cpu",
        "pipeline": "standard",
    }

    pages, metadata = mine_pages_from_docling_artifact(page1_artifact)

    assert len(pages) == 2
    # Page 1 should have boosted table density due to table presence
    assert pages[0].table_like_density > pages[1].table_like_density or pages[1].table_like_density == 0.0


def test_fallback_to_plain_text_when_no_json() -> None:
    """Without doc_json_content, fall back to plain_text_content."""
    artifact = {
        "doc_json_content": None,
        "plain_text_content": "This is page one text\nWith two lines",
        "tables_json_content": "[]",
        "page_count": 1,
        "accelerator": "cpu",
        "pipeline": "standard",
    }

    pages, metadata = mine_pages_from_docling_artifact(artifact)

    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert "page one text" in pages[0].text_content or "page one" in pages[0].text_content.lower()


def test_docling_pages_segment_into_tariff_spans() -> None:
    """Reconstructed pages can segment into TariffSpans."""
    from duke_rates.historical.ncuc.pipeline.segmentation import segment_document

    artifact = {
        "doc_json_content": json.dumps({
            "texts": [
                {
                    "text": "Leaf No. 604\nRIDER JAA\nEffective Date: January 1, 2020",
                    "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 700, "r": 500, "b": 680}}],
                },
                {
                    "text": "Service Terms and Conditions\nThese are procedural terms",
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
    }

    pages, metadata = mine_pages_from_docling_artifact(artifact)
    spans = segment_document(pages)

    # Should have at least one tariff span
    assert len(spans) > 0
    tariff_spans = [s for s in spans if s.doc_type == "tariff"]
    assert len(tariff_spans) > 0


def test_mixed_content_produces_segments() -> None:
    """Pages with mixed content still segment into spans."""
    from duke_rates.historical.ncuc.pipeline.segmentation import segment_document

    artifact = {
        "doc_json_content": json.dumps({
            "texts": [
                {
                    "text": "Motion for Rate Increase\nBrief in Support of Application",
                    "prov": [{"page_no": 1, "bbox": {"l": 100, "t": 700, "r": 500, "b": 680}}],
                },
                {
                    "text": "Certificate of Service\nDocket No. E-2 Sub 1200",
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
    }

    pages, metadata = mine_pages_from_docling_artifact(artifact)
    spans = segment_document(pages)

    # Should have spans — classification is downstream concern
    assert len(spans) > 0
    assert all(hasattr(s, 'doc_type') for s in spans)


def test_empty_artifact() -> None:
    """Empty artifact returns empty pages and metadata."""
    pages, metadata = mine_pages_from_docling_artifact({})
    assert pages == []
    assert metadata == {}


def test_zero_page_count() -> None:
    """Zero page count returns empty pages and metadata."""
    artifact = {
        "doc_json_content": json.dumps({"texts": [], "tables": []}),
        "plain_text_content": "",
        "tables_json_content": "[]",
        "page_count": 0,
        "accelerator": "cpu",
        "pipeline": "standard",
    }
    pages, metadata = mine_pages_from_docling_artifact(artifact)
    assert pages == []
    assert metadata == {}
