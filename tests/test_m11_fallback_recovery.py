"""Phase 2E M1.1: native-fallback extraction defects found by the M1 contamination probe.

Fixtures in ``tests/fixtures/m11`` are the source pages, cut unchanged from the
NCUC filings (page numbers below are the original PDF's):

* ``dep_jaa6_e7sub1243_p146.pdf`` - Rider JAA-6, p.146 of the E-7 Sub 1243 filing
  (hd 7724; the same sheet is hd 3687 p.154 and hd 5365 p.78).
* ``dep_leaf600_e2sub1300_p96-101.pdf`` - DEP NC First Revised Leaf No. 600,
  pp.96-101 of the E-2 Sub 1300 Sept-2023 bundle (hd 7265).
* ``dec_leaf99_e7sub1026_amended_p9.pdf`` - DEC Amended Fifteenth Revised Leaf
  No. 99, p.9 (hd 536), an OCR'd scan.

Expected values are transcribed from those pages, not from any parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from duke_rates.document_intelligence.adapters import (
    _Cell,
    _merge_wrapped_labels,
    representation_from_native_pdf,
)
from duke_rates.document_intelligence.table_api import CellRef
from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    ProgressJaaRiderProfile,
    ProgressRiderAdjustmentMatrixProfile,
)
from duke_rates.parse.class_rate_tables import parse_class_rate_tables
from duke_rates.parse.rider_summary import checksum_score, parse_rider_summary_from_pdf
from duke_rates.parse.rider_summary_tables import (
    _is_inline_header,
    parse_rider_summary_from_representation,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "m11"
JAA_PDF = FIXTURES / "dep_jaa6_e7sub1243_p146.pdf"
LEAF600_PDF = FIXTURES / "dep_leaf600_e2sub1300_p96-101.pdf"
LEAF99_PDF = FIXTURES / "dec_leaf99_e7sub1026_amended_p9.pdf"

# Rider JAA-6 (printed in dollars; Non-Demand per kWh, Demand per kW).
JAA6 = {
    ("adjustment", "JAA Rate - Residential", "$/kWh"): 0.00482,
    ("adjustment", "JAA Rate - Small General Service", "$/kWh"): 0.00462,
    ("adjustment", "JAA Rate - Medium General Service", "$/kWh"): 0.00408,
    ("adjustment", "JAA Rate - Seasonal and Intermittent", "$/kWh"): 0.00651,
    ("adjustment", "JAA Rate - Traffic Signal", "$/kWh"): 0.00277,
    ("adjustment", "JAA Rate - Outdoor Lighting", "$/kWh"): 0.0,
    ("demand", "JAA Rate - Medium General Service", "$/kW"): 1.51,
    ("demand", "JAA Rate - Large General Service", "$/kW"): 1.57,
}

# Leaf 600 p.101: Traffic Signal Schedules, a cents/kWh-only section.
TRAFFIC_SIGNAL_CENTS = {
    "BA-Fuel": 0.000, "BA-EMF": 0.450, "BA-DSM": 0.042, "BA-EE": 0.424, "BA": 0.916,
    "EDIT-4": -0.191, "JAA": 0.333, "CPRE": 0.013, "ESM": 0.000, "PIM": 0.000,
}

# Leaf 99 p.9: four rate classes that share every rider except Cost of Removal.
LEAF99_P9 = {
    # rate class (as parsed) -> (Cost of Removal cents/kWh, TOTAL cents/kWh)
    "General Service Schedules SGS. BC. SGST": (-0.0694, 0.0483),
    "General Sen ice Schedule": (-0.0492, 0.0685),  # LGS; "Service" OCR'd as "Sen ice"
    "General Service Schedules OPT-G. OPT-E": (-0.0390, 0.0787),
    "General Serv ice Schedule": (-0.0295, 0.0882),  # OPT-H
}


def _structured_doc(family_key: str, rep, *, local_path: str = "") -> dict:
    return {
        "family_key": family_key,
        "local_path": local_path,
        "company": "progress",
        "_structured_mode": "hybrid",
        "_representation_loader": lambda: rep,
    }


def _key(charge):
    return (charge.charge_type, charge.charge_label, charge.rate_unit)


# ---------------------------------------------------------------------------
# JAA (hd 7724 / 3687 / 5365): a wrapped class name split the native table
# ---------------------------------------------------------------------------


def _cell(text, x0, x1, top):
    return _Cell(text, x0, x1, top, top + 11, [{"text": text, "x0": x0, "x1": x1, "top": top,
                                                "bottom": top + 11}])


def test_wrapped_class_name_joins_its_value_line():
    lines = [
        [_cell("Seasonal and Intermittent", 91, 203, 327.1), _cell("SI", 248, 257, 327.1)],
        [_cell("Service", 91, 124, 339.9), _cell("0.00651", 448, 484, 339.9)],
    ]
    merged = _merge_wrapped_labels(lines)
    assert len(merged) == 1
    assert [c.text for c in merged[0]] == ["Seasonal and Intermittent Service", "SI", "0.00651"]


def test_separate_rows_are_not_joined():
    # Normal row spacing (6pt gap), and a next line that fills the same column.
    spaced = [
        [_cell("Medium General Service", 91, 201, 310.1), _cell("CH-TOUE", 248, 340, 310.1)],
        [_cell("Traffic Signal Service", 91, 188, 327.1), _cell("0.00277", 448, 484, 327.1)],
    ]
    assert len(_merge_wrapped_labels(spaced)) == 2
    overlapping = [
        [_cell("Seasonal and Intermittent", 91, 203, 327.1), _cell("SI", 248, 257, 327.1)],
        [_cell("Service", 91, 124, 339.9), _cell("SIX", 250, 270, 339.9),
         _cell("0.00651", 448, 484, 339.9)],
    ]
    assert len(_merge_wrapped_labels(overlapping)) == 2


def test_native_jaa_table_is_one_table_with_every_class():
    rep = representation_from_native_pdf(JAA_PDF)
    assert len(rep.pages[0].tables) == 1
    rows = parse_class_rate_tables(rep)
    assert len(rows) == 8 and all(r.unit is not None for r in rows)


def test_jaa_profile_recovers_all_eight_rates_from_native_structure():
    rep = representation_from_native_pdf(JAA_PDF)
    charges = ProgressJaaRiderProfile().extract(_structured_doc("nc-progress-leaf-602", rep), "")
    assert {_key(c): c.rate_value for c in charges} == pytest.approx(JAA6)
    assert len(charges) == len(JAA6)
    assert all(c.evidence and c.evidence["page"] == 1 for c in charges)


def test_jaa_structured_read_never_drops_classes_the_text_parse_finds():
    rep = representation_from_native_pdf(JAA_PDF)
    # Keep only the first three body rows: the pre-fix split-table shape.
    table = rep.pages[0].tables[0]
    table.rows = table.rows[:5]
    table.cells = [c for c in table.cells if c.row_index < 5]
    table.row_count = 5
    text = rep.pages[0].text
    charges = ProgressJaaRiderProfile().extract(_structured_doc("nc-progress-leaf-602", rep), text)
    assert {_key(c): c.rate_value for c in charges} == pytest.approx(JAA6)
    with_evidence = {c.charge_label for c in charges if c.evidence}
    assert with_evidence == {
        "JAA Rate - Residential", "JAA Rate - Small General Service",
        "JAA Rate - Medium General Service",
    }


# ---------------------------------------------------------------------------
# Leaf 600 (hd 7265): fixed x bands put cents/kWh values in the $/kW column
# ---------------------------------------------------------------------------


def test_coordinate_parser_takes_columns_from_the_printed_headers():
    summary = parse_rider_summary_from_pdf(str(LEAF600_PDF), leaf_no="600")
    blocks = {b.rate_class: b for b in summary.rate_classes}
    traffic = blocks["Traffic Signal Schedules"]
    items = {i.rider_code: i for i in traffic.line_items
             if not i.is_section_header and not i.is_total}
    assert {code: items[code].cents_per_kwh for code in TRAFFIC_SIGNAL_CENTS} == pytest.approx(
        TRAFFIC_SIGNAL_CENTS
    )
    assert all(items[code].dollars_per_kw is None for code in TRAFFIC_SIGNAL_CENTS)
    assert traffic.total_cents_per_kwh == pytest.approx(1.071)
    assert traffic.total_dollars_per_kw is None


def test_coordinate_parser_keeps_demand_and_incremental_columns():
    summary = parse_rider_summary_from_pdf(str(LEAF600_PDF), leaf_no="600")
    blocks = {b.rate_class: b for b in summary.rate_classes}
    lgs = blocks["Large General Service Schedules"]
    jaa = next(i for i in lgs.line_items if i.rider_code == "JAA" and not i.is_total)
    assert jaa.dollars_per_kw == pytest.approx(1.59) and jaa.cents_per_kwh is None
    assert (lgs.total_cents_per_kwh, lgs.total_dollars_per_kw) == pytest.approx((1.284, 1.59))
    hp = blocks["Schedule HP & Schedule LGS-RTP"]
    emf = next(i for i in hp.line_items if i.rider_code == "BA-EMF")
    assert (emf.cents_per_kwh, emf.incremental_cents_per_kwh) == pytest.approx((0.899, 0.0))
    assert hp.total_incremental_cents_per_kwh == pytest.approx(0.478)
    assert hp.total_dollars_per_kw == pytest.approx(1.59)


def test_every_leaf600_block_total_reconciles():
    summary = parse_rider_summary_from_pdf(str(LEAF600_PDF), leaf_no="600")
    assert len(summary.rate_classes) == 11
    assert checksum_score(summary) == (11, 0)


def test_leaf600_profile_emits_energy_units_for_energy_only_classes():
    doc = {"family_key": "nc-progress-leaf-600", "local_path": str(LEAF600_PDF)}
    charges = ProgressRiderAdjustmentMatrixProfile().extract(doc, "")
    traffic = [c for c in charges if c.charge_label.startswith("Traffic Signal Schedules")]
    assert traffic and all(c.rate_unit == "$/kWh" for c in traffic)
    total = next(c for c in traffic if c.charge_type == "adjustment_total")
    assert total.rate_value == pytest.approx(0.01071)
    residential_kw = [c for c in charges
                      if c.charge_label.startswith("Residential") and c.rate_unit == "$/kW"]
    assert residential_kw == []


# ---------------------------------------------------------------------------
# Leaf 99 (hd 536): OCR'd inline headers merged four rate classes into one
# ---------------------------------------------------------------------------


def _row(*texts):
    return [CellRef("t", 1, 0, i, t) for i, t in enumerate(texts)]


def test_inline_header_tolerates_unreadable_header_cells():
    assert _is_inline_header(_row("General Sen ice Schedule LGS", "cents/kWh", "EfTeclivc", "Date"))
    assert _is_inline_header(_row("General Serv ice Schedule OPT-H", "ccnts/k\\Vh", "Effective",
                                  "Date"))
    # Not a header: digits, no rate class, or nothing recognizable.
    assert not _is_inline_header(_row("Cost of Removal Rider", "-0.0694", "9/25/13", ""))
    assert not _is_inline_header(_row("Fuel Cost Adjustment Rider", "ccnts/k\\Vh", "Effective"))
    assert not _is_inline_header(_row("General Service Schedule", "ccnts/k\\Vh", "EfTeclivc"))


def test_ocr_damaged_inline_headers_open_their_own_rate_classes():
    rep = representation_from_native_pdf(LEAF99_PDF)
    summary = parse_rider_summary_from_representation(rep, leaf_no="99")
    blocks = {b.rate_class: b for b in summary.rate_classes}
    assert set(blocks) == set(LEAF99_P9)
    for name, (removal, total) in LEAF99_P9.items():
        block = blocks[name]
        rows = [i for i in block.line_items if not i.is_total and not i.is_section_header]
        assert len(rows) == 8, name
        cor = next(i for i in rows if i.label == "Cost of Removal Rider")
        assert cor.cents_per_kwh == pytest.approx(removal), name
        assert block.total_cents_per_kwh == pytest.approx(total), name
        assert all(i.evidence["page"] == 1 for i in rows)
    opt_g = blocks["General Service Schedules OPT-G. OPT-E"]
    assert any("unreadable" in w for w in opt_g.warnings)


# ---------------------------------------------------------------------------
# The five documents, read from the archived source PDFs (skipped without them)
# ---------------------------------------------------------------------------

_JAA_DOCS = [
    ("data/historical/ncuc/e-7-sub-1243/4e0c2c78-3f6f-47b5-8ae0-25aed12fe82f.pdf", 143, 146),
    ("data/historical/ncuc/e-2-sub-1300-sept2023/7f394a37-a20d-4da5-b97d-0035c9d9c0c0.pdf", 154, 154),
    ("data/historical/ncuc/e-2-sub-1275/9c355332-9bfe-4790-8175-140f128a27ea.pdf", 75, 78),
]


@pytest.mark.parametrize("path, first, last", _JAA_DOCS, ids=["hd7724", "hd3687", "hd5365"])
def test_archived_jaa_documents_recover_all_rates(path, first, last):
    pdf = ROOT / path
    if not pdf.exists():
        pytest.skip("archived source PDF not present")
    rep = representation_from_native_pdf(pdf, page_range=(first, last))
    charges = ProgressJaaRiderProfile().extract(_structured_doc("nc-progress-leaf-602", rep), "")
    assert {_key(c): c.rate_value for c in charges} == pytest.approx(JAA6)
    assert all(first <= c.evidence["page"] <= last for c in charges)


def test_archived_leaf600_bundle_hd7265():
    pdf = ROOT / "data/historical/ncuc/e-2-sub-1300-sept2023/7b9171ff-48d4-46dd-82eb-43bd1ca2cd3d.pdf"
    if not pdf.exists():
        pytest.skip("archived source PDF not present")
    summary = parse_rider_summary_from_pdf(str(pdf), leaf_no="600")
    assert checksum_score(summary) == (11, 0)


def test_archived_leaf99_hd536_page9():
    pdf = ROOT / ("data/raw/historical/ncuc/e-7/e-7-nodate-duke-energy-carolinas-llc-s-revisions-to-"
                  "rate-compliance-filing-of-approved-tar.pdf")
    if not pdf.exists():
        pytest.skip("archived source PDF not present")
    rep = representation_from_native_pdf(pdf, page_range=(9, 9))
    summary = parse_rider_summary_from_representation(rep, leaf_no="99")
    totals = {b.rate_class: b.total_cents_per_kwh for b in summary.rate_classes}
    assert totals == pytest.approx({k: v[1] for k, v in LEAF99_P9.items()})
