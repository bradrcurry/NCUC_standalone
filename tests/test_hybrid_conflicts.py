"""Phase 2C: hybrid merge vouching and conflicting-evidence validators."""

from __future__ import annotations

from types import SimpleNamespace

from duke_rates.document_intelligence.charge_validators import (
    run_charge_validators,
    validate_parse_warnings,
    validate_parser_disagreements,
    validate_printed_precision,
    validate_redline_superseded,
    validate_source_pages,
)
from duke_rates.parse.rider_summary import (
    RiderLineItem,
    RiderRateClassBlock,
    RiderSummaryResult,
    _check_block_totals,
)
from duke_rates.parse.rider_summary_tables import block_residual, merge_with_legacy


def _block(items, total, name="Demand: Medium General Service Schedules"):
    block = RiderRateClassBlock(
        rate_class=name,
        line_items=[*items, RiderLineItem(label="TOTAL cents/kWh", is_total=True)],
        total_cents_per_kwh=total,
    )
    _check_block_totals(block)
    return block


def _result(block):
    return RiderSummaryResult(
        source_pdf="x.pdf", leaf_no="600", effective_date=None, docket_number=None,
        order_date=None, supersedes=None, rate_classes=[block],
    )


def _merge(legacy_items, structured_items, total):
    legacy = _block(legacy_items, total)
    structured = _block(structured_items, total)
    return merge_with_legacy(_result(structured), _result(legacy)).rate_classes[0]


def test_sub_tolerance_table_row_not_added_when_block_already_reconciles():
    # blk13: the text parse misses PIM (printed 0.002); the redline table reads
    # "0.0002". Both blocks reconcile within the 0.002 tolerance, so the TOTAL
    # cannot vouch for the extra row.
    legacy = [RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500)]
    structured = [
        RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500),
        RiderLineItem("Performance Incentive Mechanism Rider PIM", "PIM", cents_per_kwh=0.0002),
    ]
    merged = _merge(legacy, structured, 0.5015)
    assert [i.rider_code for i in merged.line_items if not i.is_total] == ["BA"]
    assert any("cannot vouch" in w and "needs review" in w for w in merged.warnings)
    assert merged.evidence["unvouched_table_rows"][0]["values"][0] == 0.0002


def test_zero_table_row_added_to_reconciled_block():
    legacy = [RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500)]
    structured = [
        RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500),
        RiderLineItem("Earnings Sharing Mechanism Rider ESM", "ESM", cents_per_kwh=0.0),
    ]
    merged = _merge(legacy, structured, 0.5009)  # redline residual, still within tolerance
    assert [i.rider_code for i in merged.line_items if not i.is_total] == ["BA", "ESM"]
    assert not any("not added" in w for w in merged.warnings)


def test_table_row_that_makes_block_reconcile_is_added():
    legacy = [RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500)]
    structured = [
        RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500),
        RiderLineItem("Storm Recovery Rider", "STS", cents_per_kwh=0.250),
    ]
    merged = _merge(legacy, structured, 0.750)
    assert [i.rider_code for i in merged.line_items if not i.is_total] == ["BA", "STS"]
    assert merged.checksum_ok is True
    assert block_residual(merged) < 1e-9


def test_table_row_kept_out_when_block_has_no_total():
    # Without a printed TOTAL nothing vouches for a row only the table found.
    legacy = [RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500)]
    structured = [
        RiderLineItem("Fuel BA", "BA", cents_per_kwh=0.500),
        RiderLineItem("Storm Recovery Rider", "STS", cents_per_kwh=0.250),
    ]
    merged = _merge(legacy, structured, None)
    assert [i.rider_code for i in merged.line_items if not i.is_total] == ["BA"]
    assert any("needs review" in w for w in merged.warnings)


def _charge(label, value, unit="$/kWh", evidence=None, **kw):
    return SimpleNamespace(
        charge_type=kw.get("charge_type", "adjustment"), charge_label=label, rate_value=value,
        rate_unit=unit, customer_class=None, tou_period=None, season="all_year",
        tier_min=None, tier_max=None, effective_start=None, evidence=evidence,
    )


def _cell(text, page=1, unit="cents /kWh"):
    return {"text": text, "page": page, "printed_unit": unit}


def test_printed_precision_outlier_is_review():
    charges = [_charge(f"R{i}", 0.001 * i, evidence={"values": [_cell(f"0.00{i}")]}) for i in range(1, 7)]
    charges.append(_charge("PIM", 0.000002, evidence={"values": [_cell("0.0002")]}))
    findings = validate_printed_precision(charges)
    assert [f.code for f in findings] == ["printed_precision_outlier"]
    assert findings[0].level == "review" and "PIM" in findings[0].detail


def test_printed_precision_needs_enough_values():
    charges = [_charge("A", 0.1, evidence={"values": [_cell("0.10")]}),
               _charge("B", 0.0002, evidence={"values": [_cell("0.0002")]})]
    assert validate_printed_precision(charges) == []


def test_parser_disagreement_is_review():
    ev = {"disagreement": {"legacy": (0.3, None, None), "structured": (0.227, None, None)}}
    findings = validate_parser_disagreements([_charge("Storm", 0.003, evidence=ev)])
    assert findings and findings[0].code == "parser_disagreement" and findings[0].level == "review"


def test_parse_warnings_needing_review_are_surfaced():
    findings = validate_parse_warnings(
        ["RES: table parse rows ['PIM'] not added: printed TOTAL cannot vouch for them; needs review",
         "informational warning"]
    )
    assert [f.code for f in findings] == ["parser_conflict"]


def test_redline_superseded_value_only_on_redlines():
    ev = {"quote": "Storm Recovery Rider 0.300 0.227 12/1/21", "values": [_cell("0.227")]}
    charges = [_charge("Storm", 0.00227, evidence=ev)]
    assert validate_redline_superseded(charges, is_redline=False) == []
    findings = validate_redline_superseded(charges, is_redline=True)
    assert findings and findings[0].code == "redline_superseded_value"


def test_two_value_cells_are_not_a_superseded_value():
    ev = {"quote": "Rider X 0.300 0.150", "values": [_cell("0.300"), _cell("0.150")]}
    assert validate_redline_superseded([_charge("X", 0.003, evidence=ev)], is_redline=True) == []


def test_source_page_disagreement_and_span():
    a = _charge("Fuel", 0.005, evidence={"values": [_cell("0.5", page=2)]})
    b = _charge("Fuel", 0.006, evidence={"values": [_cell("0.6", page=7)]})
    codes = {f.code for f in validate_source_pages([a, b], page_span=(1, 4))}
    assert codes == {"source_page_disagreement", "page_outside_span"}


def test_same_value_on_two_pages_is_not_a_disagreement():
    a = _charge("Fuel", 0.005, evidence={"values": [_cell("0.5", page=2)]})
    b = _charge("Fuel", 0.005, evidence={"values": [_cell("0.5", page=3)]})
    assert validate_source_pages([a, b]) == []


def test_run_charge_validators_wires_new_checks():
    ev = {"disagreement": {"legacy": (1,), "structured": (2,)}}
    codes = {
        f.code
        for f in run_charge_validators(
            [_charge("X", 0.01, evidence=ev)],
            parse_warnings=["x: needs review"],
        )
    }
    assert {"parser_disagreement", "parser_conflict"} <= codes
