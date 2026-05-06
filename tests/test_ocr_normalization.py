from __future__ import annotations

from duke_rates.historical.ncuc.pipeline.ocr_normalization import (
    normalize_docling_markdown,
    normalize_ocr_label,
    normalize_ocr_money_line,
    normalize_ocr_text,
)
from duke_rates.historical.ncuc.pipeline.page_miner import _extract_page_features_from_text
from duke_rates.historical.ncuc.pipeline.parser_profiles import CarolinasLightingScheduleProfile


def test_normalize_ocr_text_fixes_common_month_and_rate_artifacts() -> None:
    text = "Effective Julv 1, 2009\nFuel factor S6.75\nCharge lO.3560^ per kWh"

    normalized = normalize_ocr_text(text)

    assert "July 1, 2009" in normalized
    assert "$6.75" in normalized
    assert "10.3560¢ per kWh" in normalized


def test_page_miner_uses_normalized_ocr_text_for_feature_detection() -> None:
    evidence = _extract_page_features_from_text(
        "Leaf No. 60\nSERVICE RENDERED ON OR AFTER Julv 1, 2009\nMonthly senice charge S6.75\n",
        1,
    )

    assert evidence.text_content is not None
    assert "July 1, 2009" in evidence.text_content
    assert "$6.75" in evidence.text_content
    assert evidence.has_effective_date_phrase is True
    assert evidence.tariff_vocab_density > 0.0


def test_parser_profile_uses_shared_ocr_normalization_helpers() -> None:
    profile = CarolinasLightingScheduleProfile()

    assert profile._normalize_ocr_money_line("S6.75 per month") == "$6.75 per month"
    assert profile._normalize_table_label("Floodlighl streel senice") == "Floodlight street service"
    assert normalize_ocr_label("Floodlighl streel senice") == "Floodlight street service"


def test_normalize_docling_markdown_strips_headers() -> None:
    out = normalize_docling_markdown("## MONTHLY RATE\nLED 30 $7.45\n### Sub\nfoo")
    assert "## " not in out
    assert "MONTHLY RATE" in out
    assert "Sub" in out


def test_normalize_docling_markdown_flattens_tables() -> None:
    md = (
        "| Class | Wattage | Charge |\n"
        "|-------|---------|--------|\n"
        "| LED 30 | 30      | $7.45  |\n"
    )
    out = normalize_docling_markdown(md)
    assert "|" not in out
    assert "---" not in out
    assert "LED 30" in out
    assert "$7.45" in out


def test_normalize_docling_markdown_strips_html_comments() -> None:
    out = normalize_docling_markdown("<!-- image -->\nfoo")
    assert "<!--" not in out
    assert "foo" in out


def test_normalize_docling_markdown_idempotent_on_flat_text() -> None:
    flat = "MONTHLY RATE\nLED 30  30  $7.45  10"
    assert normalize_docling_markdown(flat) == flat


def test_render_docling_table_as_markdown_basic_grid() -> None:
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    table = {
        "data": {
            "grid": [
                [{"text": "Class"}, {"text": "Wattage"}, {"text": "Charge"}],
                [{"text": "LED 30"}, {"text": "30"}, {"text": "$7.45"}],
            ]
        }
    }
    out = BulkExtractor._render_docling_table_as_markdown(table)
    # Header + separator + body row
    assert "Class" in out and "Wattage" in out and "Charge" in out
    assert "LED 30" in out and "$7.45" in out
    # Has the separator pattern that normalize_docling_markdown will drop
    assert "---" in out


def test_render_docling_table_handles_pipes_in_cells() -> None:
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    table = {"data": {"grid": [[{"text": "a|b"}, {"text": "c"}]]}}
    out = BulkExtractor._render_docling_table_as_markdown(table)
    # Pipe inside cell text would break the markdown table; replaced with space
    assert "a b" in out
    # Outer pipes still present (they're delimiters)
    assert out.startswith("|")


def test_slice_docling_text_rejects_partial_coverage_artifact(tmp_path) -> None:
    """A1 fix: when docling artifact's prov[].page_no doesn't span the full PDF,
    its page numbers are chunk-relative and unreliable as PDF page indices.
    The slicer must reject such artifacts so callers fall back to pdfplumber.
    """
    import json
    import sqlite3
    import fitz

    # Build a tiny 5-page PDF
    pdf_path = tmp_path / "test.pdf"
    doc = fitz.open()
    for _ in range(5):
        doc.new_page()
    doc.save(str(pdf_path))
    doc.close()

    # DB with a partial-coverage docling artifact (max prov page_no = 1, but PDF has 5)
    db_path = tmp_path / "duke.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE docling_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_pdf TEXT,
            status TEXT,
            page_count INTEGER,
            doc_json_content TEXT,
            plain_text_content TEXT,
            file_hash TEXT, backend_version TEXT, accelerator TEXT,
            json_sidecar_path TEXT, text_sidecar_path TEXT, tables_sidecar_path TEXT,
            tables_json_content TEXT, conversion_confidence REAL, table_count INTEGER,
            metadata_json TEXT, created_at TEXT, updated_at TEXT, pipeline TEXT,
            discovery_record_id INTEGER
        );
    """)
    # Build a doc_json with only 1 page of prov, but PDF has 5 pages — partial chunk
    fake_doc = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [
            {"text": "wrong-content from chunk", "prov": [{"page_no": 1}]}
        ],
        "tables": [],
    }
    conn.execute(
        "INSERT INTO docling_artifacts(source_pdf, status, page_count, doc_json_content, plain_text_content) VALUES (?,?,?,?,?)",
        (str(pdf_path), "success", 5, json.dumps(fake_doc), "wrong-content from chunk"),
    )
    conn.commit()
    conn.close()

    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    ext = BulkExtractor(str(db_path))
    # Request pages 1-2 — slicer must detect that artifact only covers 1/5 pages and refuse
    result = ext._slice_docling_text(str(pdf_path), 1, 2)
    assert result is None, f"expected None for partial-coverage artifact, got {result!r}"
