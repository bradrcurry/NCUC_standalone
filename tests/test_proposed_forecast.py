"""Tests for the proposed load/customer forecast extractor.

Fixtures mirror the real page layouts: clean DEC-style tables, a Gross-to-Net
decomposition with parenthesized credits, and a DEP-style peak table whose
digits are rendered in the Calibri/Coptic subset that ``_degarble`` repairs.
"""

from duke_rates.analytics.proposed_forecast import (
    SCHEMAS,
    _degarble,
    classify_table,
    parse_forecast_page,
)


def _years(*rows: str) -> str:
    return "\n".join(rows)


RETAIL_SALES = """\
DEC Spring 2025 Forecast - Forecasted Retail Sales (GWHs)
Year
Residential
GWh
Commercial
GWh
Industrial
GWh
Other
GWh
Retail Sales
2025
30,825
30,535
20,842
286
82,488
2026
31,307
31,104
21,741
284
84,437
2027
31,634
33,310
22,934
283
88,161
2028
31,693
37,090
24,198
282
93,263
2029
31,611
41,196
24,831
281
97,918
2030
31,751
44,536
25,482
280
102,048
"""

GROSS_TO_NET = """\
DEC Spring 2025 Forecast - Gross to Net Sales (GWHs)
Year
Gross Retail Sales
Energy Efficiency
Rooftop Solar
Electric Vehicles
Voltage Control
Economic Development
Net Retail Sales
2025
82,384
(195)
(24)
30
(234)
528
82,488
2026
83,676
(568)
(75)
105
(318)
1,618
84,437
2027
84,296
(959)
(123)
208
(337)
5,076
88,161
2028
84,601
(1,360)
(168)
334
(353)
10,208
93,263
2029
84,672
(1,769)
(214)
491
(356)
15,094
97,918
2030
85,018
(2,159)
(262)
685
(359)
19,124
102,048
"""

# DEP peak page: digits rendered in the Coptic subset (U+03EC..U+03F5, U+0355).
def _garble(s: str) -> str:
    table = {str(i): chr(0x03EC + i) for i in range(10)}
    table[","] = chr(0x0355)
    return "".join(table.get(ch, ch) for ch in s)


PEAK_GARBLED = (
    "\nYear\nWinter\nSummer\n"
    + "\n".join(
        _garble(row)
        for row in [
            "2025", "14,361", "12,761",
            "2026", "14,312", "12,739",
            "2027", "14,596", "13,276",
            "2028", "14,778", "13,498",
            "2029", "15,001", "13,700",
            "2030", "15,210", "13,902",
        ]
    )
)


def test_degarble_restores_coptic_digits() -> None:
    assert _degarble(_garble("14,361")) == "14,361"
    assert _degarble("82,488") == "82,488"  # clean text untouched


def test_retail_sales_parses_and_reconciles() -> None:
    table_type, rows = parse_forecast_page(RETAIL_SALES)
    assert table_type == "retail_sales"
    by_year_seg = {(y, s): v for y, s, v in rows}
    assert by_year_seg[(2025, "Residential")] == 30825
    assert by_year_seg[(2025, "Total")] == 82488
    # Components sum to the Total in every year.
    for year in range(2025, 2031):
        comp = sum(
            by_year_seg[(year, s)]
            for s in ("Residential", "Commercial", "Industrial", "Other")
        )
        assert abs(comp - by_year_seg[(year, "Total")]) <= 2


def test_gross_to_net_handles_credits_and_identity() -> None:
    table_type, rows = parse_forecast_page(GROSS_TO_NET)
    assert table_type == "gross_to_net"
    by = {(y, s): v for y, s, v in rows}
    assert by[(2025, "Energy Efficiency")] == -195  # parenthesized credit
    assert by[(2025, "Economic Development")] == 528
    for year in range(2025, 2031):
        recon = sum(
            by[(year, s)]
            for s in SCHEMAS["gross_to_net"]
            if s != "Net Retail Sales"
        )
        assert abs(recon - by[(year, "Net Retail Sales")]) <= 2


def test_peak_table_degarbles_and_parses() -> None:
    table_type, rows = parse_forecast_page(PEAK_GARBLED)
    assert table_type == "peak"
    by = {(y, s): v for y, s, v in rows}
    assert by[(2025, "Winter")] == 14361
    assert by[(2025, "Summer")] == 12761
    assert by[(2030, "Winter")] == 15210


def test_short_year_run_is_not_a_forecast() -> None:
    # A dense financial schedule may contain a couple of years but never a long
    # sequential run — it must not be classified as a forecast table.
    text = "Rate Base\n2024\n26,391,197\n2025\n12,403,863\n"
    table_type, rows = parse_forecast_page(text)
    assert table_type is None
    assert rows == []
    # classify_table directly: fewer than 6 sequential years -> None.
    assert classify_table(text, [(2024, [1.0]), (2025, [2.0])]) is None
