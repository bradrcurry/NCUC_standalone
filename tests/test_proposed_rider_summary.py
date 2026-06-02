"""Tests for the Summary of Rider Adjustments parser.

Fixtures mirror the real line layouts: DEC (E-7 Sub 1329, leaf 99) groups
wrapped names before their values; DEP (E-2 Sub 1380, leaf 600) interleaves
name / value / date and carries dateless subtotals.
"""

from duke_rates.document_intelligence.proposed_rider_summary import (
    RiderSummaryCharge,
    extract_rider_summary_charges,
    is_rider_summary_page,
)


DEC_PAGE = """\
The following is a summary of Rider Adjustments that must be added to the bill.
Residential Schedules RS, RE, ES, RT, RSTC, RETC
cents/kWh
Effective Date
Fuel Cost Adjustment Rider
0.2753
1/1/27
Energy Efficiency Rider
0.5081
1/1/26
BPM Prospective Rider
-0.0075
7/1/25
Production Tax Credits Rider
Pension Cost Rider
Regulatory Asset and Liability Rider
-0.1648
0.0000
-0.0030
1/1/27
1/1/27
1/1/27
TOTAL cents/kWh
0.5265
Lighting Schedules PL, OL, NL
cents/kWh
Effective Date
Fuel Cost Adjustment Rider
0.1458
1/1/27
"""

DEP_PAGE = """\
SUMMARY OF RIDER ADJUSTMENTS
The following is a summary of Rider Adjustments that must be added to the bill.
Residential Service Schedules
cents
/kWh
Effective
 Date
Annual Billing Adjustments Rider BA
Fuel and Fuel-Related Adjustment Rate
0.000
1/1/27
Demand Side Management DSM & EE Rate
0.767
1/1/25
Annual Billing Adjustments Rider BA - Net Adjustment
1.347
EDIT-4 Rider
-0.249
10/1/23
TOTAL cents/kWh
1.768
"""


def _by_label(charges: list[RiderSummaryCharge]) -> dict[str, RiderSummaryCharge]:
    return {c.rider_label: c for c in charges}


def test_detects_summary_body_pages() -> None:
    assert is_rider_summary_page(DEC_PAGE)
    assert is_rider_summary_page(DEP_PAGE)
    # An index row that merely lists the summary as a leaf is not a body page.
    assert not is_rider_summary_page("Leaf 600\nSummary of Rider Adjustments\n")


def test_dec_grouped_names_pair_with_values_and_dates() -> None:
    charges = extract_rider_summary_charges(DEC_PAGE, strategy="dec")
    res = [c for c in charges if c.schedule_group.startswith("Residential")]
    by = _by_label(res)
    # The grouped PTC/PC/RAL block must zip positionally, not collapse.
    assert by["Production Tax Credits Rider"].cents_per_kwh == -0.1648
    assert by["Pension Cost Rider"].cents_per_kwh == 0.0
    assert by["Regulatory Asset and Liability Rider"].cents_per_kwh == -0.0030
    assert by["Regulatory Asset and Liability Rider"].effective_date == "1/1/27"
    assert abs(by["Fuel Cost Adjustment Rider"].rate_value - 0.002753) < 1e-9
    assert by["BPM Prospective Rider"].cents_per_kwh == -0.0075


def test_dec_codes_and_groups_extracted() -> None:
    charges = extract_rider_summary_charges(DEC_PAGE, strategy="dec")
    res = next(c for c in charges if c.schedule_group.startswith("Residential"))
    assert res.schedule_codes == ("RS", "RE", "ES", "RT", "RSTC", "RETC")
    lighting = [c for c in charges if c.schedule_group.startswith("Lighting")]
    assert lighting and lighting[0].schedule_codes == ("PL", "OL", "NL")
    # TOTAL row is not emitted as a rider.
    assert not any("TOTAL" in c.rider_label.upper() for c in charges)


def test_dep_interleaved_with_dateless_subtotal() -> None:
    charges = extract_rider_summary_charges(DEP_PAGE, strategy="dep")
    by = _by_label(charges)
    # Section header with no value of its own is dropped.
    assert "Annual Billing Adjustments Rider BA" not in by
    # Sub-line keeps its value/date.
    assert by["Fuel and Fuel-Related Adjustment Rate"].effective_date == "1/1/27"
    # Dateless subtotal is captured with effective_date == None.
    net = by["Annual Billing Adjustments Rider BA - Net Adjustment"]
    assert net.cents_per_kwh == 1.347
    assert net.effective_date is None
    assert by["EDIT-4 Rider"].cents_per_kwh == -0.249


def test_negative_values_become_credits() -> None:
    charges = extract_rider_summary_charges(DEC_PAGE, strategy="dec")
    ptc = next(c for c in charges if c.rider_label == "Production Tax Credits Rider")
    assert ptc.rate_value < 0
    assert ptc.cents_per_kwh == -0.1648


def test_empty_text_is_safe() -> None:
    assert extract_rider_summary_charges("", strategy="dec") == []
    assert extract_rider_summary_charges("", strategy="dep") == []
    assert not is_rider_summary_page("")
