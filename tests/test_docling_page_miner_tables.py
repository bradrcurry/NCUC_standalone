"""Regression tests for D1: Docling table cells missing from page-miner output.

``mine_pages_from_docling_artifact`` used to rebuild page text from
``doc_json["texts"]`` only, so table cells (where Duke rider summaries keep
every rate) never reached page features, segmentation, or the OCR fallback.
Measured on the DEP Leaf 600 Ninth Revised (2026-04-01) fixture: 132 of 134
table decimals were absent from the mined text.

Fixtures in ``tests/fixtures/docling`` are frozen exports of real artifacts
(``docling_artifacts`` ids 1755 and 1833) together with their source PDFs; the
pdfplumber text of the same PDF is used as an independent oracle.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from duke_rates.historical.ncuc.pipeline.docling_page_miner import (
    mine_pages_from_docling_artifact,
)
from duke_rates.historical.ncuc.pipeline.ocr_normalization import normalize_ocr_text

FIXTURES = Path(__file__).parent / "fixtures" / "docling"
_DECIMAL = re.compile(r"\d*\.\d+")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.docling.json").read_text(encoding="utf-8"))


def _bbox(t: float, l: float = 72.0, b: float | None = None, r: float = 500.0) -> dict:
    return {"l": l, "t": t, "r": r, "b": t - 12 if b is None else b, "coord_origin": "BOTTOMLEFT"}


def _cell(text: str, row: int, col: int, *, row_span: int = 1, col_span: int = 1) -> dict:
    return {
        "text": text,
        "start_row_offset_idx": row,
        "end_row_offset_idx": row + row_span,
        "start_col_offset_idx": col,
        "end_col_offset_idx": col + col_span,
        "row_span": row_span,
        "col_span": col_span,
    }


def _artifact(doc: dict, page_count: int) -> dict:
    return {
        "doc_json_content": json.dumps(doc),
        "plain_text_content": "",
        "page_count": page_count,
        "accelerator": "cuda",
        "pipeline": "standard",
    }


# ---------------------------------------------------------------------------
# Real Duke artifacts
# ---------------------------------------------------------------------------


def test_leaf600_table_cell_values_appear_on_their_page() -> None:
    art = _load("dep_leaf600_2026-04-01")
    pages, metadata = mine_pages_from_docling_artifact(art)
    doc = art["doc_json_content"]

    assert len(pages) == art["page_count"] == 5
    missing: list[tuple[int, str]] = []
    total = 0
    for table in doc["tables"]:
        page_no = table["prov"][0]["page_no"]
        for row in table["data"]["grid"]:
            for cell in row:
                for value in _DECIMAL.findall(cell["text"]):
                    total += 1
                    if value not in pages[page_no - 1].text_content:
                        missing.append((page_no, value))
    assert total >= 130
    assert missing == []
    assert metadata["docling_table_count_per_page"] == {1: 2, 2: 3, 3: 3, 4: 2, 5: 2}


def test_leaf600_rows_keep_label_value_and_date_together_in_order() -> None:
    pages, _ = mine_pages_from_docling_artifact(_load("dep_leaf600_2026-04-01"))
    lines = pages[0].text_content.splitlines()

    # Each logical row is one line: label, cents/kWh, then effective date.
    edit4 = [ln for ln in lines if ln.startswith("EDIT-4 Rider")]
    assert edit4 and edit4[0].split()[-2:] == ["(0.249)", "10/1/23"]

    # Rows keep the printed order: residential table header precedes its riders,
    # and the residential table precedes the Small General Service table.
    def index_of(prefix: str) -> int:
        return next(i for i, ln in enumerate(lines) if ln.startswith(prefix))

    res = index_of("Residential Service Schedule")
    sgs = index_of("Small General Service Schedule")
    assert res < index_of("EDIT-4 Rider") < sgs


def test_leaf600_values_not_duplicated_and_page_boundaries_hold() -> None:
    pages, _ = mine_pages_from_docling_artifact(_load("dep_leaf600_2026-04-01"))
    # Residential EDIT-4 credit printed once on page 1 only.
    assert pages[0].text_content.count("EDIT-4 Rider (0.249)") == 1
    assert all("Residential Service Schedule" not in p.text_content for p in pages[1:])


def test_leaf600_mined_text_covers_pdfplumber_decimals() -> None:
    pdfplumber = pytest.importorskip("pdfplumber")
    pages, _ = mine_pages_from_docling_artifact(_load("dep_leaf600_2026-04-01"))
    with pdfplumber.open(FIXTURES / "dep_leaf600_2026-04-01.pdf") as pdf:
        oracle = [p.extract_text() or "" for p in pdf.pages]
    missing = [
        (i + 1, v)
        for i, text in enumerate(oracle)
        for v in re.findall(r"\d+\.\d+", text)
        if v not in pages[i].text_content
    ]
    # Allow a tiny residue for values Docling itself misreads.
    assert len(missing) <= 3, missing


def test_leaf500_non_table_text_remains_intact() -> None:
    art = _load("dep_leaf500_2024-10-01")
    pages, _ = mine_pages_from_docling_artifact(art)
    doc = art["doc_json_content"]
    for item in doc["texts"]:
        text = (item.get("text") or "").strip()
        if not text or not item.get("prov"):
            continue
        page_no = item["prov"][0]["page_no"]
        # Page text goes through normalize_ocr_text; compare on that basis.
        expected = " ".join(normalize_ocr_text(text).split())
        assert expected in " ".join(pages[page_no - 1].text_content.split())
    assert pages[0].has_leaf_header is True
    assert "500" in pages[0].extracted_leaf_nos


# ---------------------------------------------------------------------------
# Synthetic layouts
# ---------------------------------------------------------------------------


def test_multi_column_table_preserves_column_relationships() -> None:
    """A TOTAL row with ¢/kWh and $/kW columns stays one ordered row."""
    grid = [
        [_cell("Rider", 0, 0), _cell("cents/kWh", 0, 1), _cell("dollars/kW", 0, 2)],
        [_cell("JAA", 1, 0), _cell("0.031", 1, 1), _cell("1.72", 1, 2)],
        [_cell("TOTAL", 2, 0), _cell("1.3513", 2, 1), _cell("2.278", 2, 2)],
    ]
    doc = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]},
        "texts": [{"text": "Large General Service", "prov": [{"page_no": 1, "bbox": _bbox(700)}]}],
        "tables": [{"prov": [{"page_no": 1, "bbox": _bbox(650, b=500)}], "data": {"grid": grid}}],
    }
    pages, _ = mine_pages_from_docling_artifact(_artifact(doc, 1))
    lines = pages[0].text_content.splitlines()
    assert lines == [
        "Large General Service",
        "Rider cents/kWh dollars/kW",
        "JAA 0.031 1.72",
        "TOTAL 1.3513 2.278",
    ]


def test_spanned_cells_are_emitted_once() -> None:
    grid = [
        [_cell("Summer Rates", 0, 0, col_span=2), _cell("Summer Rates", 0, 0, col_span=2)],
        [_cell("On-Peak", 1, 0), _cell("0.29905", 1, 1)],
    ]
    doc = {
        "body": {"children": [{"$ref": "#/tables/0"}]},
        "texts": [],
        "tables": [{"prov": [{"page_no": 1, "bbox": _bbox(650, b=500)}], "data": {"grid": grid}}],
    }
    pages, _ = mine_pages_from_docling_artifact(_artifact(doc, 1))
    assert pages[0].text_content.splitlines() == ["Summer Rates", "On-Peak 0.29905"]


def test_two_column_page_follows_docling_reading_order() -> None:
    """Side-by-side columns must not be interleaved line by line by bbox.t."""
    texts = [
        {"text": "LEFT heading", "prov": [{"page_no": 1, "bbox": _bbox(700, l=50, r=290)}]},
        {"text": "LEFT body", "prov": [{"page_no": 1, "bbox": _bbox(680, l=50, r=290)}]},
        {"text": "RIGHT heading", "prov": [{"page_no": 1, "bbox": _bbox(701, l=310, r=560)}]},
        {"text": "RIGHT body", "prov": [{"page_no": 1, "bbox": _bbox(681, l=310, r=560)}]},
    ]
    doc = {
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(4)]},
        "texts": texts,
        "tables": [],
    }
    pages, _ = mine_pages_from_docling_artifact(_artifact(doc, 1))
    assert pages[0].text_content.splitlines() == [
        "LEFT heading", "LEFT body", "RIGHT heading", "RIGHT body",
    ]


def test_group_children_and_page_headers_are_included() -> None:
    """Items nested in groups and furniture page headers are not dropped."""
    doc = {
        "body": {"children": [{"$ref": "#/groups/0"}]},
        "groups": [{"children": [{"$ref": "#/texts/1"}, {"$ref": "#/texts/2"}]}],
        "furniture": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [
            {"text": "Leaf No. 602", "prov": [{"page_no": 2, "bbox": _bbox(760)}]},
            {"text": "- first bullet", "prov": [{"page_no": 2, "bbox": _bbox(600)}]},
            {"text": "- second bullet", "prov": [{"page_no": 2, "bbox": _bbox(580)}]},
        ],
        "tables": [],
    }
    pages, _ = mine_pages_from_docling_artifact(_artifact(doc, 2))
    assert pages[0].text_content == ""
    assert pages[1].text_content.splitlines() == [
        "Leaf No. 602", "- first bullet", "- second bullet",
    ]


def test_text_items_parented_to_a_table_are_not_duplicated() -> None:
    grid = [[_cell("Basic Customer Charge", 0, 0), _cell("$14.00", 0, 1)]]
    doc = {
        "body": {"children": [{"$ref": "#/tables/0"}]},
        "texts": [
            {
                "text": "$14.00",
                "parent": {"$ref": "#/tables/0"},
                "prov": [{"page_no": 1, "bbox": _bbox(640)}],
            }
        ],
        "tables": [{"prov": [{"page_no": 1, "bbox": _bbox(650, b=600)}], "data": {"grid": grid}}],
    }
    pages, _ = mine_pages_from_docling_artifact(_artifact(doc, 1))
    assert pages[0].text_content.count("$14.00") == 1
