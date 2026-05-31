from __future__ import annotations

from duke_rates.analytics.proposed_financial_context import parse_class_revenue_impact_page


DEC_CLASS_PAGE = """
DUKE ENERGY CAROLINAS, LLC.
Docket No. E-7, Sub 1329
NC RETAIL COST OF SERVICE-PROPOSED-12CP FIRM, A&E AT RETAIL
FINAL ASK: MYRP YEAR 1
For the test year ending December 31, 2024
Dollars in Thousands
Line
Description
Function Allocator
NCSGS
Production
Demand
Production
Energy
Transmission
Dist-Substations
Dist-Primary
Dist-
Transformers
Dist-Sec Serv
Customer
109 PR REV-PROPOSED INCREASE
50,292
16,784
10,843
3,154
1,621
4,393
942
764
11,790
110 ELECTRIC OPERATING REVENUE - PROPOSED
690,934
231,052
148,176
44,583
21,937
60,773
12,761
10,555
161,098
111 CHECK TOTAL
-
"""


DEC_CREDIT_PAGE = """
DUKE ENERGY CAROLINAS, LLC.
Docket No. E-7, Sub 1329
NC RETAIL COST OF SERVICE-PROPOSED-12CP FIRM, A&E AT RETAIL
FINAL ASK: MYRP YEAR 1
Dollars in Thousands
Line
Description
Function Allocator
NCNL
Production
Demand
Production
Energy
Transmission
Dist-Substations
Dist-Primary
Dist-
Transformers
Dist-Sec Serv
Customer
109 PR REV-PROPOSED INCREASE
(14)
(0)
(1)
(2)
(0)
(5)
(2)
(2)
(2)
110 ELECTRIC OPERATING REVENUE - PROPOSED
1,000
100
100
100
100
100
100
100
300
"""


def test_parse_class_revenue_impact_page() -> None:
    row = parse_class_revenue_impact_page(DEC_CLASS_PAGE, source_pdf="sample.pdf", source_page=100)

    assert row is not None
    assert row.utility == "Duke Energy Carolinas"
    assert row.docket_number == "E-7 Sub 1329"
    assert row.scenario_label == "MYRP Rate Year 1"
    assert row.class_code == "NCSGS"
    assert row.class_name == "Small General Service"
    assert row.proposed_increase_millions == 50.292
    assert row.proposed_revenue_millions == 690.934
    assert row.current_revenue_millions == 640.642
    assert row.percent_increase == 7.85
    assert row.source_page == 100


def test_parse_class_revenue_impact_page_handles_credit() -> None:
    row = parse_class_revenue_impact_page(DEC_CREDIT_PAGE)

    assert row is not None
    assert row.class_code == "NCNL"
    assert row.proposed_increase_millions == -0.014
    assert row.current_revenue_millions == 1.014
    assert row.percent_increase == -1.38


def test_parse_class_revenue_impact_page_ignores_zero_increase() -> None:
    text = DEC_CLASS_PAGE.replace("50,292", "-")

    assert parse_class_revenue_impact_page(text) is None
