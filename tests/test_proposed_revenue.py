from __future__ import annotations

from duke_rates.analytics.proposed_revenue import parse_proposed_revenue_adjustment_page


DEP_TEXT = """
SUMMARY OF PROPOSED REVENUE ADJUSTMENTS
(Dollars in Millions)
Traditional Base Rate Revenue Requirement
619.5
$
[1]
(13.7)
$
[2]
(163.6)
$
[3]
(40.1)
$
[4]
(1.2)
$
[5]
401.0
$
Rate Year 1 - Incremental Revenue Requirement for MYRP Projects
127.4
[6]
127.4
$
3
Rate Year 1 - Total (L1 + L2)
746.9
$
(13.7)
$
(163.6)
$
(40.1)
$
(1.2)
$
528.3
$
Rate Year 2 - Incremental Revenue Requirement for MYRP Projects
200.3
$
[7]
200.3
$
5
Cumulative Rate year 2 Revenue Increase (L3 + L4)
947.2
$
(13.7)
$
(163.6)
$
(40.1)
$
(1.2)
$
728.6
$
"""


DEC_TEXT = """
SUMMARY OF PROPOSED REVENUE ADJUSTMENTS
(Dollars in Millions)
Traditional Base Rate Revenue Requirement
597.5
$
[1]
(1.7)
$
[2]
(0.8)
$
[3]
595.0
$
Rate Year 1 - Incremental Revenue Requirement for MYRP Projects
132.0
[4]
132.0
$
3
Rate Year 1 - Total (L1 + L2)
729.5
$
(1.7)
$
(0.8)
$
727.0
$
Rate Year 2 - Incremental Revenue Requirement for MYRP Projects
274.9
$
274.9
$
5
Cumulative Rate year 2 Revenue Increase (L3 + L4)
1,004.4
$
(1.7)
$
(0.8)
$
1,001.9
$
"""


def test_parse_dep_revenue_adjustment_summary() -> None:
    rows = parse_proposed_revenue_adjustment_page(
        DEP_TEXT,
        utility="Duke Energy Progress",
        docket_number="E-2 Sub 1380",
    )

    assert len(rows) == 5
    traditional = rows[0]
    assert traditional.base_rates_millions == 619.5
    assert traditional.over_amortization_rider_millions == -13.7
    assert traditional.jaar_rider_millions == -163.6
    assert traditional.ptc_rider_millions == -40.1
    assert traditional.bpm_rider_millions == -1.2
    assert traditional.total_impact_millions == 401.0

    ry1_increment = rows[1]
    assert ry1_increment.base_rates_millions == 127.4
    assert ry1_increment.total_impact_millions == 127.4
    assert ry1_increment.ptc_rider_millions is None


def test_parse_dec_revenue_adjustment_summary() -> None:
    rows = parse_proposed_revenue_adjustment_page(
        DEC_TEXT,
        utility="Duke Energy Carolinas",
        docket_number="E-7 Sub 1329",
    )

    assert len(rows) == 5
    traditional = rows[0]
    assert traditional.base_rates_millions == 597.5
    assert traditional.over_amortization_rider_millions == -1.7
    assert traditional.edpr_rider_millions == -0.8
    assert traditional.total_impact_millions == 595.0

    cumulative = rows[-1]
    assert cumulative.base_rates_millions == 1004.4
    assert cumulative.total_impact_millions == 1001.9
