"""Regression tests for rider-summary section segmentation and unit assignment.

Fixtures in ``tests/fixtures/rider_summary`` are the pdfplumber and PyMuPDF
text layers (pages joined by form feeds) of real Duke summary sheets; see
``MANIFEST.json`` for source paths and SHA-256 hashes.  Expected values below
were read from those text layers and the printed TOTAL rows, not from parser
output.

Defects covered:

* A ``TOTAL`` row never closed a block, so a missed rate-class header merged
  the next class into the previous one (DEP Leaf 600 2026-04-01 under PyMuPDF:
  four merges; blk 304 source: a redline copy and two HP sections appended to
  Industrial; blk 361 source: footers and a service-list line absorbed).
* Values were assigned to units by position: a lone $/kW JAA value closed by
  ``TOTAL dollars/kW`` was stored as cents/kWh (blk 13), and the HP
  "Incremental" cents/kWh column was stored as $/kW.
* ``Large General Service`` received the ``General Service`` schedule list.
* Page footers such as "NC Sixty-Third Revised Leaf No. 99" became 99.0 rows.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from duke_rates.parse.rider_summary import (
    RiderRateClassBlock,
    RiderSummaryResult,
    _lookup_schedules,
    parse_rider_summary,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rider_summary"
EXTRACTORS = ("pdfplumber", "pymupdf")


def _parse(name: str, extractor: str, *, keep_pages: bool = True) -> RiderSummaryResult:
    text = (FIXTURES / f"{name}.{extractor}.txt").read_text(encoding="utf-8")
    if not keep_pages:
        text = text.replace("\f", "\n")
    return parse_rider_summary(text, source_pdf=name)


def _block(result: RiderSummaryResult, prefix: str) -> RiderRateClassBlock:
    matches = [b for b in result.rate_classes if b.rate_class.startswith(prefix)]
    assert len(matches) == 1, [b.rate_class for b in result.rate_classes]
    return matches[0]


def _item(block: RiderRateClassBlock, label_prefix: str):
    matches = [li for li in block.line_items if li.label.startswith(label_prefix) and not li.is_total]
    assert len(matches) == 1, [li.label for li in block.line_items]
    return matches[0]


def _totals(block: RiderRateClassBlock) -> list:
    return [li for li in block.line_items if li.is_total]


def _assert_no_cross_section_merge(result: RiderSummaryResult) -> None:
    for block in result.rate_classes:
        cents_totals = [t for t in _totals(block) if t.cents_per_kwh is not None]
        kw_totals = [t for t in _totals(block) if t.dollars_per_kw is not None]
        assert len(cents_totals) <= 1, (block.rate_class, [t.label for t in cents_totals])
        assert len(kw_totals) <= 1, (block.rate_class, [t.label for t in kw_totals])
        for li in block.line_items:
            assert "Leaf No" not in li.label, (block.rate_class, li.label)
            assert "PO Box" not in li.label and "P.O. Box" not in li.label


# ---------------------------------------------------------------------------
# blk 307 -- DEC Leaf 99 Rev. 63, eff. 2025-01-01 ("Residential" merge)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_blk307_residential_and_general_service_are_separate(extractor: str) -> None:
    result = _parse("dec_rev63_2025-01-01", extractor)
    _assert_no_cross_section_merge(result)

    res = _block(result, "Residential Schedules")
    assert res.applicable_schedules == ["RS", "RE", "ES", "RT", "RSTC", "RETC"]
    assert res.total_cents_per_kwh == pytest.approx(0.1125)
    assert _item(res, "Fuel Cost Adjustment Rider").cents_per_kwh == pytest.approx(0.1779)
    assert _item(res, "Nuclear Production Tax Credits").cents_per_kwh == pytest.approx(-0.0834)
    assert len([li for li in res.line_items if not li.is_total]) == 12
    assert res.checksum_ok is True

    gs = _block(result, "General Service Schedules")
    assert gs.applicable_schedules[0] == "SGS"
    assert gs.total_cents_per_kwh == pytest.approx(0.2487)
    assert _item(gs, "Fuel Cost Adjustment Rider").cents_per_kwh == pytest.approx(0.3551)
    assert gs.checksum_ok is True

    lighting = _block(result, "Lighting Schedules")
    assert lighting.total_cents_per_kwh == pytest.approx(-1.0898)
    assert not any(li.cents_per_kwh == 99.0 for b in result.rate_classes for li in b.line_items)


# ---------------------------------------------------------------------------
# blk 304 -- DEC E-7 Sub 1285 compliance filing (clean pp. 3-4, redline pp. 5-6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor", EXTRACTORS)
@pytest.mark.parametrize("keep_pages", [True, False])
def test_blk304_clean_sections_survive_the_redline_copy(extractor: str, keep_pages: bool) -> None:
    result = _parse("dec_e7_sub1285_2024-01-01", extractor, keep_pages=keep_pages)
    _assert_no_cross_section_merge(result)

    assert _block(result, "Residential Schedules").total_cents_per_kwh == pytest.approx(2.1812)
    assert _block(result, "General Service Schedules").total_cents_per_kwh == pytest.approx(1.8812)
    industrial = _block(result, "Industrial Schedules")
    assert industrial.total_cents_per_kwh == pytest.approx(1.7811)
    assert len([li for li in industrial.line_items if not li.is_total]) == 8
    assert industrial.checksum_ok is True

    # Redline values fuse struck and inserted digits (e.g. EE "0.33893775");
    # they must never land in a block that passes as clean.
    for block in result.rate_classes:
        if any(li.cents_per_kwh in (0.33893775, 0.62424343) for li in block.line_items):
            assert block.checksum_ok is not True, block.rate_class
    assert any("redline" in w.lower() for w in result.parse_warnings)


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_blk304_hp_baseline_incremental_columns_are_both_cents(extractor: str) -> None:
    result = _parse("dec_e7_sub1285_2024-01-01", extractor)
    hp_gs = _block(result, "Schedule HP - General Service")
    assert hp_gs.column_layout == "baseline_incremental"

    fca = _item(hp_gs, "Fuel Cost Adjustment Rider")
    assert fca.cents_per_kwh == pytest.approx(1.7463)
    assert fca.incremental_cents_per_kwh == pytest.approx(0.0)
    assert fca.dollars_per_kw is None
    assert hp_gs.total_cents_per_kwh == pytest.approx(1.8812)
    assert hp_gs.total_incremental_cents_per_kwh == pytest.approx(0.1349)
    assert hp_gs.total_dollars_per_kw is None

    hp_ind = _block(result, "Schedule HP - Industrial")
    assert hp_ind.total_cents_per_kwh == pytest.approx(1.7811)
    assert hp_ind.total_incremental_cents_per_kwh == pytest.approx(0.2018)
    assert all(li.dollars_per_kw is None for b in result.rate_classes for li in b.line_items)


# ---------------------------------------------------------------------------
# blk 13 -- DEP Leaf 600 in E-2 Sub 1300 ($/kW JAA, HP three-column layout)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_blk13_jaa_after_cents_total_is_dollars_per_kw(extractor: str) -> None:
    result = _parse("dep_e2_sub1300_leaf600_redline", extractor)
    lgs = _block(result, "Large General Service Schedules")
    assert "LGS" in lgs.applicable_schedules and "SGS" not in lgs.applicable_schedules

    jaa = _item(lgs, "Joint Agency Asset Rider JAA")
    assert jaa.dollars_per_kw == pytest.approx(1.72)
    assert jaa.cents_per_kwh is None
    assert lgs.total_cents_per_kwh == pytest.approx(1.3513)
    assert lgs.total_dollars_per_kw == pytest.approx(1.72)
    assert _item(lgs, "EDIT-4 Rider").cents_per_kwh == pytest.approx(-0.093)


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_blk13_hp_incremental_column_is_not_dollars_per_kw(extractor: str) -> None:
    result = _parse("dep_e2_sub1300_leaf600_redline", extractor)
    hp = _block(result, "Schedule HP & Schedule LGS-RTP")
    assert hp.column_layout == "baseline_incremental"
    assert hp.applicable_schedules == ["HP", "LGS-RTP"]

    dsm = _item(hp, "Demand Side Management (DSM) Rate")
    assert (dsm.cents_per_kwh, dsm.incremental_cents_per_kwh, dsm.dollars_per_kw) == (
        pytest.approx(0.041), pytest.approx(0.041), None,
    )
    net = _item(hp, "Annual Billing Adjustments Rider BA - Net Adjustment")
    assert (net.cents_per_kwh, net.incremental_cents_per_kwh) == (
        pytest.approx(1.440), pytest.approx(0.274),
    )
    assert hp.total_cents_per_kwh == pytest.approx(1.3513)
    assert hp.total_incremental_cents_per_kwh == pytest.approx(0.278)
    jaa = _item(hp, "Joint Agency Asset Rider JAA")
    assert jaa.dollars_per_kw == pytest.approx(1.72) and jaa.cents_per_kwh is None
    assert hp.total_dollars_per_kw == pytest.approx(1.72)
    # The Outdoor Lighting section that follows is its own block.
    assert _block(result, "Outdoor Lighting Schedules").total_cents_per_kwh == pytest.approx(0.84951)


# ---------------------------------------------------------------------------
# Control -- DEP Leaf 600 Ninth Revised, eff. 2026-04-01 (clean, current layout)
# ---------------------------------------------------------------------------

LEAF600_2026_04_CLASSES = [
    ("Residential Service Schedules", 2.108, None),
    ("Small General Service Schedules", 0.2, None),
    ("Small General Service - Constant Load", 0.203, None),
    ("Non-Demand: Medium General Service Schedules", 0.948, None),
    ("Demand: Medium General Service Schedules", 0.537, 0.92),
    ("Seasonal or Intermittent Service Schedule", 1.453, None),
    ("Large General Service Schedules", 0.856, 3.03),
    ("Schedule HP & Schedule LGS-RTP", 0.856, 3.03),
    ("Outdoor Lighting Schedules", 0.8, None),
    ("Sports Field Lighting Schedule", 0.909, None),
    ("Traffic Signal Schedules", 0.388, None),
]


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_control_leaf600_same_sections_from_both_extractors(extractor: str) -> None:
    result = _parse("dep_leaf600_2026-04-01", extractor)
    _assert_no_cross_section_merge(result)

    got = [(b.rate_class, b.total_cents_per_kwh, b.total_dollars_per_kw) for b in result.rate_classes]
    assert len(got) == len(LEAF600_2026_04_CLASSES), got
    for (name, cents, kw), (exp_name, exp_cents, exp_kw) in zip(got, LEAF600_2026_04_CLASSES):
        assert name.startswith(exp_name)
        assert cents == pytest.approx(exp_cents)
        assert kw == (pytest.approx(exp_kw) if exp_kw is not None else None)
    assert all(b.checksum_ok is True for b in result.rate_classes), [
        (b.rate_class, b.warnings) for b in result.rate_classes if b.checksum_ok is not True
    ]
    assert not any(b.ambiguous_rate_class for b in result.rate_classes)


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_demand_class_keeps_both_unit_totals_in_one_block(extractor: str) -> None:
    """A legitimate class with both a cents/kWh and a dollars/kW TOTAL is not split."""
    result = _parse("dep_leaf600_2026-04-01", extractor)
    demand = _block(result, "Demand: Medium General Service Schedules")
    assert demand.column_layout == "kwh_kw"
    assert len(_totals(demand)) == 2
    jaa = _item(demand, "Joint Agency Asset Rider JAA")
    assert jaa.dollars_per_kw == pytest.approx(0.92) and jaa.cents_per_kwh is None
    assert _item(demand, "EDIT-4 Rider").cents_per_kwh == pytest.approx(-0.145)


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_june_2026_layout_fragmented_hp_header_is_recognized(extractor: str) -> None:
    """Tenth Revised Leaf 600 splits header words ("Incrementa" / "l")."""
    result = _parse("dep_leaf600_2026-06-01", extractor)
    _assert_no_cross_section_merge(result)
    assert len(result.rate_classes) == 11
    assert not any(b.ambiguous_rate_class for b in result.rate_classes)
    assert _block(result, "Residential Service Schedules").total_cents_per_kwh == pytest.approx(3.144)

    hp = _block(result, "Schedule HP & Schedule LGS-RTP")
    assert hp.column_layout == "baseline_incremental"
    fuel = _item(hp, "Fuel and Fuel-Related Adjustment Rate")
    assert (fuel.cents_per_kwh, fuel.incremental_cents_per_kwh, fuel.dollars_per_kw) == (
        pytest.approx(0.361), pytest.approx(0.0), None,
    )
    assert hp.total_cents_per_kwh == pytest.approx(1.397)
    assert hp.total_incremental_cents_per_kwh == pytest.approx(0.242)
    assert hp.total_dollars_per_kw == pytest.approx(3.03)
    assert all(b.checksum_ok is True for b in result.rate_classes)


# ---------------------------------------------------------------------------
# blk 361 -- DEC E-7 Sub 1146 FCA filing (cover letter, service list, HP pages)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extractor", EXTRACTORS)
def test_blk361_summary_sections_without_filing_boilerplate(extractor: str) -> None:
    result = _parse("dec_e7_sub1146_fca_2018", extractor)
    _assert_no_cross_section_merge(result)

    res = _block(result, "Residential Schedules")
    assert res.applicable_schedules == ["RS", "RE", "ES", "RT", "WC"]
    assert res.total_cents_per_kwh == pytest.approx(0.3335)
    assert res.checksum_ok is True
    assert _block(result, "Industrial Schedules").total_cents_per_kwh == pytest.approx(0.4415)
    hp_gs = _block(result, "Schedule HP - General Service")
    assert hp_gs.total_cents_per_kwh == pytest.approx(0.4362)
    assert hp_gs.total_incremental_cents_per_kwh == pytest.approx(0.5127)


# ---------------------------------------------------------------------------
# Synthetic layouts
# ---------------------------------------------------------------------------


def test_missed_header_is_flagged_not_merged() -> None:
    text = "\n".join([
        "Residential Service Schedules /kWh Date",
        "EDIT-4 Rider -0.249 10/1/23",
        "Joint Agency Asset Rider JAA 0.631 12/1/22",
        "TOTAL cents/kWh 0.382",
        "Some Unrecognized Heading Text",
        "EDIT-4 Rider -0.259 10/1/23",
        "Joint Agency Asset Rider JAA 0.439 12/1/22",
        "TOTAL cents/kWh 0.180",
    ])
    result = parse_rider_summary(text)
    assert [b.rate_class for b in result.rate_classes][0] == "Residential Service Schedules"
    res, orphan = result.rate_classes
    assert res.total_cents_per_kwh == pytest.approx(0.382)
    assert [li.cents_per_kwh for li in res.line_items if not li.is_total] == [-0.249, 0.631]
    assert orphan.ambiguous_rate_class is True
    assert orphan.applicable_schedules == []
    assert orphan.total_cents_per_kwh == pytest.approx(0.180)
    assert any("rate class" in w.lower() for w in result.parse_warnings)


def test_single_value_in_two_unit_layout_without_total_is_not_guessed() -> None:
    text = "\n".join([
        "cents dollars Effective",
        "Large General Service Schedules /kWh /kW Date",
        "EDIT-4 Rider -0.093 10/1/23",
        "TOTAL cents/kWh -0.093",
        "Joint Agency Asset Rider JAA 1.72 12/1/24",
    ])
    lgs = parse_rider_summary(text).rate_classes[0]
    jaa = _item(lgs, "Joint Agency Asset Rider JAA")
    assert jaa.cents_per_kwh is None and jaa.dollars_per_kw is None
    assert jaa.unit_ambiguous is True
    assert any("unit" in w.lower() for w in lgs.warnings)


def test_second_value_in_single_cents_column_is_flagged() -> None:
    text = "\n".join([
        "Residential Schedules RS, RE cents/kWh Effective Date",
        "Fuel Cost Adjustment Rider 1.7463 0.0000 9/1/23",
        "TOTAL cents/kWh 1.7463",
    ])
    res = parse_rider_summary(text).rate_classes[0]
    fca = _item(res, "Fuel Cost Adjustment Rider")
    assert fca.unit_ambiguous is True
    assert fca.dollars_per_kw is None


@pytest.mark.parametrize(
    ("rate_class", "expected_first"),
    [
        ("Large General Service Schedules", "LGS"),
        ("Small General Service Schedules", "SGS"),
        ("General Service Schedules", "SGS"),
        ("Residential Service Schedules", "RES"),
    ],
)
def test_lookup_schedules_prefers_most_specific_class(rate_class: str, expected_first: str) -> None:
    assert _lookup_schedules(rate_class)[0] == expected_first
