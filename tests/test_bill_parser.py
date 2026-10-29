from pathlib import Path

from duke_rates.parse.bill_parser import parse_bill_text

SAMPLE_BILL_TEXT = """Page 1 of 4
duke-energy.com
800.452.2777
9101 8064 6213
Your Energy Bill
Service address
 Bill date
Account number
Dec 18, 2025
29 days
For service   Nov 18 - Dec 16
BRAD CURRY
7701 SUMMERCREST DR
APEX NC 27539
Billing summary
 Previous Amount Due
$141.64
      Payment Received Dec 15
-141.64
 Current Lighting Charges
9.30
 Current Electric Charges
188.78
 Taxes
13.86
 Total Amount Due Jan 12
$211.94
Your usage snapshot
Page 3 of 4
9101 8064 6213
Account number 
duke-energy.com
800.452.2777
Street Lighting
Billing period Nov 18 - Dec 16
Description
Quantity
Usage
SV 95HL UG BP3
1
46 kWh
Total
1
46 kWh
Billing details - Lighting
Billing Period - Nov 18 25 to Dec 16 25
Fixture Charge
     SV 95HL UG BP3
          1.000 @ $8.27000000
$8.27
Storm Recovery Charge
0.66
Summary of Rider Adjustments
-0.38
Summary of Rider Adjustments - Nov 18 to Nov 30
0.34
Summary of Rider Adjustments - Dec 01 to Dec 16
0.41
Total Current Charges
$9.30
Your current rate is Street Lighting Service - Residential Subdivisions
(SLR).
Billing details - Electric
Billing Period - Nov 18 25 to Dec 16 25
Meter - 323544050
Basic Customer Charge
$14.00
Energy Charge
     800.000 kWh @ $0.12623000
100.98
Energy Charge
     434.000 kWh @ $0.11623000
50.44
Clean Energy Rider - Nov 18 to Nov 30
0.68
Clean Energy Rider - Dec 01 to Dec 16
1.00
Energy Conservation Credit
-7.57
Storm Recovery Charge
4.64
Summary of Rider Adjustments
9.69
Summary of Rider Adjustments - Nov 18 to Nov 30
6.44
Summary of Rider Adjustments - Dec 01 to Dec 16
8.48
Total Current Charges
$188.78
Your current rate is Residential Service (RES).
For a complete listing of all North Carolina rates and riders, visit
duke-energy.com/rates
Billing details - Taxes
Sales Tax For Utility
$13.86
Billing details - Taxes continued
Total Taxes
$13.86
"""

YEAR_CROSSING_BILL_TEXT = """Page 1 of 3
duke-energy.com
800.452.2777
9101 8064 6213
Your Energy Bill
Service address
 Bill date
Account number
Jan 21, 2026
32 days
For service   Dec 17 - Jan 17
BRAD CURRY
7701 SUMMERCREST DR
APEX NC 27539
Billing summary
 Previous Amount Due
$198.09
      Payment Received Jan 13
-198.09
 Current Electric Charges
198.74
 Taxes
14.57
 Total Amount Due Feb 17
$222.66
Your usage snapshot
Page 3 of 3
Billing details - Electric
Billing Period - Dec 17 25 to Jan 17 26
Meter - 323544050
Basic Customer Charge
$14.00
Energy Charge
     800.000 kWh @ $0.12623000
100.98
Energy Charge
     500.000 kWh @ $0.11623000
58.12
Summary of Rider Adjustments
15.99
Summary of Rider Adjustments - Dec 17 to Dec 31
4.88
Summary of Rider Adjustments - Jan 01 to Jan 17
5.99
Total Current Charges
$198.74
Your current rate is Residential Service (RES).
"""


def test_parse_bill_text_extracts_statement_and_sections() -> None:
    statement = parse_bill_text(SAMPLE_BILL_TEXT, source_path=Path("bill.pdf"))

    assert statement.account_number == "9101 8064 6213"
    assert statement.customer_name == "BRAD CURRY"
    assert statement.bill_date is not None
    assert statement.bill_date.isoformat() == "2025-12-18"
    assert statement.due_date is not None
    assert statement.due_date.isoformat() == "2026-01-12"
    assert statement.service_start is not None
    assert statement.service_start.isoformat() == "2025-11-18"
    assert statement.service_end is not None
    assert statement.service_end.isoformat() == "2025-12-16"
    assert statement.billing_summary.total_amount_due == 211.94
    assert statement.billing_summary.current_electric_charges == 188.78
    assert statement.billing_summary.current_lighting_charges == 9.30

    assert statement.electric_section is not None
    assert statement.electric_section.rate_code == "RES"
    assert statement.electric_section.meter_number == "323544050"
    assert statement.electric_section.total_current_charges == 188.78
    assert statement.electric_section.line_items[0].label == "Basic Customer Charge"
    assert statement.electric_section.line_items[1].quantity == 800.0
    assert statement.electric_section.line_items[1].unit == "kWh"
    assert statement.electric_section.line_items[1].rate == 0.12623
    assert any(
        item.label == "Energy Conservation Credit"
        for item in statement.electric_section.line_items
    )

    split_rider = next(
        item
        for item in statement.electric_section.line_items
        if item.label == "Summary of Rider Adjustments" and item.is_subperiod_detail
    )
    assert split_rider.period_start is not None
    assert split_rider.period_start.isoformat() == "2025-11-18"
    assert split_rider.period_end is not None
    assert split_rider.period_end.isoformat() == "2025-11-30"

    assert statement.lighting_section is not None
    assert statement.lighting_section.rate_code == "SLR"
    assert statement.lighting_section.line_items[0].detail == "SV 95HL UG BP3"

    assert statement.tax_section is not None
    assert statement.tax_section.line_items[0].label == "Sales Tax For Utility"


def test_parse_bill_text_handles_year_crossing_subperiods() -> None:
    statement = parse_bill_text(YEAR_CROSSING_BILL_TEXT, source_path=Path("bill.pdf"))

    assert statement.electric_section is not None
    split_items = [
        item
        for item in statement.electric_section.line_items
        if item.label == "Summary of Rider Adjustments" and item.is_subperiod_detail
    ]
    assert len(split_items) == 2
    assert split_items[0].period_start is not None
    assert split_items[0].period_start.isoformat() == "2025-12-17"
    assert split_items[0].period_end is not None
    assert split_items[0].period_end.isoformat() == "2025-12-31"
    assert split_items[1].period_start is not None
    assert split_items[1].period_start.isoformat() == "2026-01-01"
    assert split_items[1].period_end is not None
    assert split_items[1].period_end.isoformat() == "2026-01-17"


# 2026-03-20 R-TOU-CPP bill (pages 1, 3 and 4). The electric section runs off
# page 3 and resumes under "Billing details - Electric continued" on page 4 with
# Storm Recovery, Summary of Rider Adjustments and the section total. Before the
# fix the parser stopped at page 3, so the stored statement omitted $31.99 and
# had no total.
TOU_CONTINUED_BILL_TEXT = """Page 1 of 4
duke-energy.com
800.452.2777
9101 8064 6213
Your Energy Bill
Service address
 Bill date
Account number
Mar 20, 2026
29 days
For service   Feb 17 - Mar 17
BRAD CURRY
7701 SUMMERCREST DR
APEX NC 27539
Billing summary
 Previous Amount Due
$605.70
      Payment Received Mar 17
-605.70
 Current Lighting Charges
9.41
 Current Electric Charges
177.97
 Taxes
13.12
 Total Amount Due Apr 14
$200.50
Your usage snapshot
Page 3 of 4
Billing details - Lighting
Billing Period - Feb 17 26 to Mar 17 26
Fixture Charge
     SV 95HL UG BP3
          1.000 @ $8.27000000
$8.27
Storm Recovery Charge
0.77
Summary of Rider Adjustments
0.37
Total Current Charges
$9.41
Your current rate is Street Lighting Service - Residential Subdivisions
(SLR).
Billing details - Electric
Billing Period - Feb 17 26 to Mar 17 26
Meter - 323544050
Basic Customer Charge
$14.00
Critical Peak Energy Charge
     0.000 kWh @ $0.41002000
0.00
On-Peak Energy Charge
     91.573 kWh @ $0.21952000
20.10
Off-Peak Energy Charge
     650.145 kWh @ $0.11000000
71.52
Discount Energy Charge
     548.865 kWh @ $0.08274000
45.41
Clean Energy Rider
1.81
Energy Conservation Credit
-6.86
Your current rate is Residential Time of Use Critical Pk Pricing (R-
TOU-CPP).
For a complete listing of all North Carolina rates and riders, visit
duke-energy.com/rates
A rider is a mechanism used to recover costs or credit customers for
programs, purchases or regional policy initiatives that are outside
of standard base rates. The Summary of Rider Adjustments line
item found in the Billing Details includes, but is not limited to:
fuel- related costs, demand-side management, energy efficiency
program costs, generation assets, and the competitive procurement
of renewable energy. For additional detail, please visit duke-
energy.com/rates to view the Summary of Rider Adjustments tariff
found within the Index of Rate Schedules tab. Each of the individual
rider tariffs are located under the Retail Riders heading.

Page 4 of 4
9101 8064 6213
Account number
duke-energy.com
800.452.2777
Billing details - Electric continued
Storm Recovery Charge
$4.93
Summary of Rider Adjustments
27.06
Total Current Charges
$177.97
Billing details - Taxes
Sales Tax For Utility
$13.12
Total Taxes
$13.12
"""


def test_parse_bill_text_follows_continued_electric_section() -> None:
    statement = parse_bill_text(TOU_CONTINUED_BILL_TEXT, source_path=Path("2026-03-20.pdf"))

    electric = statement.electric_section
    assert electric is not None
    assert electric.rate_code == "R-TOU-CPP"
    assert electric.total_current_charges == 177.97
    amounts = {item.label: item.amount for item in electric.line_items}
    assert amounts["Storm Recovery Charge"] == 4.93
    assert amounts["Summary of Rider Adjustments"] == 27.06
    assert amounts["Energy Conservation Credit"] == -6.86
    assert len(electric.line_items) == 9
    # Independent checks: the printed lines sum to the printed total, and the
    # section total equals the front-page "Current Electric Charges".
    assert round(sum(item.amount for item in electric.line_items), 2) == 177.97
    assert statement.billing_summary.current_electric_charges == electric.total_current_charges

    # The continuation must not leak into neighbouring sections.
    assert statement.lighting_section is not None
    assert statement.lighting_section.total_current_charges == 9.41
    assert statement.tax_section is not None
    assert statement.tax_section.total_current_charges == 13.12
    assert [item.label for item in statement.tax_section.line_items] == ["Sales Tax For Utility"]


def test_parse_bill_text_does_not_duplicate_lines_when_continuation_is_contiguous() -> None:
    # Without the page-break boilerplate between them, the main block would run
    # into the continuation; each line must still be parsed once.
    text = TOU_CONTINUED_BILL_TEXT.replace(
        TOU_CONTINUED_BILL_TEXT[
            TOU_CONTINUED_BILL_TEXT.index("For a complete listing") : TOU_CONTINUED_BILL_TEXT.index(
                "Billing details - Electric continued"
            )
        ],
        "",
    )
    statement = parse_bill_text(text, source_path=Path("2026-03-20.pdf"))

    electric = statement.electric_section
    assert electric is not None
    assert [item.label for item in electric.line_items].count("Storm Recovery Charge") == 1
    assert electric.total_current_charges == 177.97
