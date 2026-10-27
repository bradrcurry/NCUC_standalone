"""Phase 2B: structured document representations, table-aware parsing, evidence."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from duke_rates.document_intelligence.adapters import (
    RepresentationCache,
    iter_tables,
    representation_fingerprint,
    representation_from_docling,
    representation_from_json,
    representation_from_native_pdf,
    representation_from_ocr_text,
    representation_to_json,
)
from duke_rates.document_intelligence.evidence import locate_value_evidence
from duke_rates.document_intelligence.models import (
    DocumentRepresentation,
    ExtractedTable,
    LayoutBlock,
    PageRepresentation,
    TableCell,
)
from duke_rates.document_intelligence.table_api import classify_header_text, table_view
from duke_rates.parse.canonical import Basis
from duke_rates.parse.class_rate_tables import parse_class_rate_tables
from duke_rates.parse.rider_summary import RiderLineItem, RiderRateClassBlock, RiderSummaryResult
from duke_rates.parse.rider_summary_tables import (
    merge_with_legacy,
    parse_rider_summary_from_representation,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/docling/dep_leaf600_2026-04-01.docling.json"
BENCH_DOCLING = ROOT / "bench/docling"


def _fixture_rep(**kwargs) -> DocumentRepresentation:
    envelope = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return representation_from_docling(
        envelope["doc_json_content"],
        source_pdf="leaf-600.pdf",
        file_sha256=envelope["file_hash"],
        backend_version=envelope["backend_version"],
        **kwargs,
    )


def _bench_rep(doc_id: str) -> DocumentRepresentation:
    with gzip.open(BENCH_DOCLING / f"{doc_id}.docling.json.gz", "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    return representation_from_docling(
        envelope["document"],
        source_pdf=doc_id,
        file_sha256=envelope["file_sha256"],
        backend_version=envelope["backend_version"],
    )


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def test_docling_adapter_keeps_pages_tables_cells_and_coordinates():
    rep = _fixture_rep()
    assert [p.page_number for p in rep.pages] == [1, 2, 3, 4, 5]
    assert rep.coordinates_available is True and rep.backend_version == "docling_v2"
    table = rep.pages[0].tables[0]
    assert table.table_id == "docling:#/tables/0"
    assert table.rows[0] == ["Residential Service Schedules", "cents /kWh", "Effective Date"]
    assert table.rows[-1] == ["TOTAL cents/kWh", "2.108", ""]
    cell = next(c for c in table.cells if c.text == "2.108")
    assert cell.row_index == 13 and cell.column_index == 1
    # Converted to a top-left origin: y grows down the page, inside the page.
    assert cell.bbox.coordinate_space == "pdf_top_left"
    assert 0 < cell.bbox.y0 < cell.bbox.y1 < rep.pages[0].height
    assert table.bbox.y0 < cell.bbox.y0  # the table's top is above its last row
    header = next(b for b in rep.pages[0].blocks if b.block_type == "page_header")
    assert header.metadata["furniture"] is True
    orders = [
        b.metadata["reading_order"] for b in rep.pages[0].blocks if not b.metadata["furniture"]
    ]
    assert orders == sorted(orders)


def test_docling_adapter_page_range_and_offset():
    only_two = _fixture_rep(page_range=(2, 2))
    assert [p.page_number for p in only_two.pages] == [2]
    shifted = _fixture_rep(page_offset=16)
    assert [p.page_number for p in shifted.pages] == [17, 18, 19, 20, 21]


def test_representation_serialization_is_reproducible(tmp_path):
    rep = _fixture_rep()
    again = representation_from_json(representation_to_json(rep))
    assert representation_fingerprint(again) == representation_fingerprint(rep)
    cache = RepresentationCache(tmp_path)
    first = cache.put(rep, "docling").read_bytes()
    second = cache.put(rep, "docling").read_bytes()
    assert first == second  # gzip without a timestamp
    assert cache.get(rep.file_hash, "docling", "docling_v2") is not None
    assert cache.get("0" * 64, "docling", "docling_v2") is None
    assert cache.get(rep.file_hash, "docling", "docling_v2", {"ocr": True}) is None


def test_native_adapter_rebuilds_tables_from_geometry():
    rep = representation_from_native_pdf(
        ROOT / "bench/pdfs/dep_leaf600_2026-04-01.pdf", page_range=(1, 1)
    )
    assert rep.coordinates_available and rep.backend_version.startswith("pdfplumber-")
    table = rep.pages[0].tables[0]
    assert table.metadata["header_rows"] == 3
    assert table.rows[1][0] == "Residential Service Schedules"
    labels = [r[0] for r in table.rows]
    # A wrapped label is joined with its continuation line.
    assert "Fuel and Fuel-Related Adjustment Experience Modification Factor (EMF)" in labels
    assert all(c.bbox is not None for c in table.cells)
    view = table_view(table)
    units = [c.unit.canonical.value for c in view.value_columns if c.unit]
    assert units == ["$/kWh"]


def test_ocr_adapter_has_no_coordinates():
    rep = representation_from_ocr_text(
        "Rider A\n\n0.300 cents\fpage two",
        source_pdf="x.pdf",
        file_sha256=None,
        ocr_backend="tesseract",
    )
    assert rep.coordinates_available is False
    assert [p.page_number for p in rep.pages] == [1, 2]
    assert all(b.bbox is None for p in rep.pages for b in p.blocks)


# ---------------------------------------------------------------------------
# Table API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "role", "canonical", "qualifier"),
    [
        ("cents /kWh", "value", "$/kWh", None),
        ("dollars /kW", "value", "$/kW", None),
        ("cents /kWh Baseline", "value", "$/kWh", "baseline"),
        ("cents /kWh Incrementa l", "value", "$/kWh", "incremental"),
        ("Effectiv e Date", "effective_date", None, None),
        ("Applicable Schedule(s)", "other", None, None),
        ("", "empty", None, None),
    ],
)
def test_classify_header_text(header, role, canonical, qualifier):
    got_role, unit, got_qualifier = classify_header_text(header)
    assert got_role == role
    assert (unit.canonical.value if unit else None) == canonical
    assert got_qualifier == qualifier


def _table(rows: list[list[str]], table_id="t1", page=1) -> ExtractedTable:
    return ExtractedTable(
        page_number=page,
        table_id=table_id,
        rows=rows,
        row_count=len(rows),
        column_count=len(rows[0]),
        headers=rows[0],
        cells=[
            TableCell(text=t, row_index=r, column_index=c)
            for r, row in enumerate(rows)
            for c, t in enumerate(row)
        ],
    )


def test_numbers_under_a_header_without_unit_are_flagged():
    view = table_view(
        _table([["Residential", "Rate", "Effective"], ["Rider X", "0.100", "1/1/26"]])
    )
    roles = {c.index: c for c in view.columns}
    # Numeric column, but its header names no unit: a value column with no unit.
    assert roles[1].role == "value" and roles[1].unit is None
    assert any("names no unit" in w for w in view.warnings)
    assert roles[2].role == "effective_date"


# ---------------------------------------------------------------------------
# Structured rider summary
# ---------------------------------------------------------------------------


def test_structured_rider_summary_from_docling_fixture():
    summary = parse_rider_summary_from_representation(_fixture_rep(), leaf_no="600")
    blocks = {b.rate_class: b for b in summary.rate_classes}
    res = blocks["Residential Service Schedules"]
    assert res.checksum_ok is True and res.total_cents_per_kwh == pytest.approx(2.108)
    assert res.applicable_schedules == ["RES", "R-TOUD", "R-TOU", "R-TOU-CPP"]
    fuel = next(i for i in res.line_items if i.rider_code == "BA-Fuel")
    assert fuel.cents_per_kwh == pytest.approx(0.262) and fuel.effective_date == "12/1/25"
    assert fuel.indented is True  # BA sub-item: not a TOTAL component
    ev = fuel.evidence
    assert ev["page"] == 1 and ev["table_id"] == "docling:#/tables/0" and ev["row"] == 2
    assert ev["values"][0]["text"] == "0.262" and ev["values"][0]["bbox"] is not None
    assert ev["values"][0]["column_header"] == "cents /kWh"
    hp = next(b for b in summary.rate_classes if b.rate_class.startswith("Schedule HP"))
    assert hp.column_layout == "baseline_incremental"
    assert hp.total_incremental_cents_per_kwh is not None
    reconciled = sum(1 for b in summary.rate_classes if b.checksum_ok)
    assert reconciled >= 9


def test_redline_fused_digits_are_left_unset_for_review():
    rows = [["Residential Service Schedules", "cents /kWh", "Effective Date"]]
    rows += [
        [f"Rider {code}", value, "4/1/25"]
        for code, value in (("RDM", "0.232"), ("ESM", "0.000"), ("PIM", "0.0002"), ("CAR", "0.098"))
    ]
    rows += [["TOTAL cents/kWh", "0.332", ""]]
    rep = DocumentRepresentation(
        source_pdf="x",
        pages=[
            PageRepresentation(
                page_number=1, text="SUMMARY OF RIDER ADJUSTMENTS", tables=[_table(rows)]
            )
        ],
    )
    block = parse_rider_summary_from_representation(rep).rate_classes[0]
    pim = next(i for i in block.line_items if i.label == "Rider PIM")
    assert pim.cents_per_kwh is None and pim.unit_ambiguous
    assert any("possible fused redline digits" in w for w in block.warnings)
    assert block.checksum_ok is False  # an unset value can never reconcile silently


def _block(name, items, total, ok):
    return RiderRateClassBlock(
        rate_class=name,
        applicable_schedules=["RES"],
        line_items=items,
        total_cents_per_kwh=total,
        checksum_ok=ok,
        column_layout="kwh",
    )


def _summary(*blocks):
    return RiderSummaryResult(
        source_pdf="x",
        leaf_no="600",
        effective_date=None,
        docket_number=None,
        order_date=None,
        supersedes=None,
        rate_classes=list(blocks),
    )


def test_merge_keeps_text_value_when_parsers_disagree_and_flags_it():
    legacy = _summary(
        _block(
            "Residential Service Schedules",
            [
                RiderLineItem("Rider PIM", rider_code="PIM", cents_per_kwh=0.002),
                RiderLineItem("Rider CAR", rider_code="CAR", cents_per_kwh=0.098),
            ],
            0.1,
            True,
        )
    )
    structured = _summary(
        _block(
            "Residential Service Schedules",
            [
                RiderLineItem(
                    "Rider PIM", rider_code="PIM", cents_per_kwh=0.0002, evidence={"page": 1}
                ),
                RiderLineItem(
                    "Rider CAR",
                    rider_code="CAR",
                    cents_per_kwh=0.098,
                    evidence={"page": 1, "row": 3},
                ),
                RiderLineItem(
                    "Rider ESM", rider_code="ESM", cents_per_kwh=0.0, evidence={"page": 1}
                ),
            ],
            0.1,
            True,
        )
    )
    merged = merge_with_legacy(structured, legacy).rate_classes[0]
    items = {i.rider_code: i for i in merged.line_items}
    assert items["PIM"].cents_per_kwh == 0.002
    assert items["PIM"].evidence["disagreement"]["structured"][0] == 0.0002
    assert items["CAR"].evidence == {"page": 1, "row": 3}  # agreement: table evidence attached
    assert items["ESM"].cents_per_kwh == 0.0  # extra top-level row still reconciles: added
    assert merged.evidence["selected_from"] == "item_merge"
    assert any("needs review" in w for w in merged.warnings)


def test_merge_takes_the_only_reconciling_parse():
    legacy = _summary(
        _block(
            "Residential Service Schedules",
            [RiderLineItem("Rider CAR", rider_code="CAR", cents_per_kwh=0.5)],
            0.098,
            False,
        )
    )
    structured = _summary(
        _block(
            "Residential Service Schedules",
            [
                RiderLineItem(
                    "Rider CAR", rider_code="CAR", cents_per_kwh=0.098, evidence={"page": 2}
                )
            ],
            0.098,
            True,
        )
    )
    merged = merge_with_legacy(structured, legacy).rate_classes[0]
    assert merged.line_items[0].cents_per_kwh == 0.098
    assert merged.evidence["selected_from"] == "structured"


def test_merge_never_adds_sub_items_the_total_cannot_vouch_for():
    legacy = _summary(
        _block(
            "Residential Service Schedules",
            [RiderLineItem("Rider CAR", rider_code="CAR", cents_per_kwh=0.098)],
            0.098,
            True,
        )
    )
    structured = _summary(
        _block(
            "Residential Service Schedules",
            [
                RiderLineItem("Rider CAR", rider_code="CAR", cents_per_kwh=0.098),
                RiderLineItem("Fuel Rate", rider_code="BA-Fuel", cents_per_kwh=0.3, indented=True),
            ],
            0.098,
            True,
        )
    )
    merged = merge_with_legacy(structured, legacy).rate_classes[0]
    assert [i.rider_code for i in merged.line_items] == ["CAR"]


# ---------------------------------------------------------------------------
# Class-rate tables (JAA)
# ---------------------------------------------------------------------------


def test_jaa_class_rate_table_units_come_from_section_rows():
    rows = parse_class_rate_tables(_bench_rep("hd1882_jaa_rev3"))
    assert len(rows) == 8
    energy = [r for r in rows if r.unit.basis == Basis.ENERGY]
    demand = [r for r in rows if r.unit.basis == Basis.DEMAND]
    assert [(r.class_label, r.value) for r in demand] == [
        ("Medium General Service", Decimal("0.92")),
        ("Large General Service", Decimal("3.03")),
    ]
    assert energy[0].class_label == "Residential" and energy[0].value == Decimal("0.00464")
    assert energy[0].unit.factor == 1  # dollars per kilowatt-hour, printed
    assert energy[0].evidence["page"] == 1 and energy[0].evidence["bbox"] is not None


def test_class_rate_rows_without_a_unit_are_reported():
    rep = DocumentRepresentation(
        source_pdf="x",
        pages=[
            PageRepresentation(
                page_number=1, tables=[_table([["Rate Class", "Rate"], ["Residential", "0.216"]])]
            )
        ],
    )
    (row,) = parse_class_rate_tables(rep)
    assert row.unit is None and row.warnings


# ---------------------------------------------------------------------------
# Evidence locator
# ---------------------------------------------------------------------------


def _block_rep(*texts):
    return DocumentRepresentation(
        source_pdf="x",
        pages=[
            PageRepresentation(
                page_number=1,
                blocks=[LayoutBlock(block_type="text", text=t, page_number=1) for t in texts],
            )
        ],
    )


def test_locator_finds_a_unique_value_and_converts_cents():
    rep = _block_rep(
        "existing storm cost recovery charges of 0.321 cents per kWh for Small General Service",
        "Basic Customer Charge $14.00 per month",
    )
    ev = locate_value_evidence(
        rep, value=0.00321, unit="$/kWh", label="Storm Recovery - Small General"
    )
    assert ev["page"] == 1 and ev["printed_value_text"] == "0.321"
    assert "0.321 cents" in ev["text"]


def test_locator_refuses_ambiguous_locations():
    rep = _block_rep("Alpha program 0.250", "Bravo program 0.250")
    assert locate_value_evidence(rep, value=0.25, unit="$/month", label="Something else") is None
    found = locate_value_evidence(rep, value=0.25, unit="$/month", label="Alpha Rider")
    assert found["text"] == "Alpha program 0.250" and found["alternatives"] == 1
    assert locate_value_evidence(rep, value=9.99, unit="$/month", label="Alpha Rider") is None


def test_iter_tables_follows_reading_order():
    rep = _fixture_rep(page_range=(2, 2))
    ids = [t.table_id for t in iter_tables(rep)]
    assert ids == sorted(ids, key=lambda i: int(i.rsplit("/", 1)[1]))
