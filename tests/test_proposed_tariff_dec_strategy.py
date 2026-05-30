"""DEC E-7 specific strategy for proposed-tariff parsing.

DEC application exhibits follow a different layout than DEP:

* The proposed exhibit opens with a tariff index page that lists
  ``LEAF NO. / DESCRIPTION / REVISION NO.`` rows in a fixed order.
* Schedule body pages do **not** carry a bold schedule heading. They begin
  with the AVAILABILITY paragraph and run until the next AVAILABILITY page.
* Rate values appear on their own line beneath a label line, e.g.::

      For the first 6,000 kWh per month, per kWh
      8.4138¢

These tests pin the helpers used by the DEC code path.
"""

from __future__ import annotations

from duke_rates.document_intelligence.proposed_tariff_dec_strategy import (
    extract_dec_split_line_charges,
    parse_dec_exhibit_index,
    parse_dec_rider_catalog,
)


def test_parse_dec_exhibit_index_reads_leaf_code_description_rows() -> None:
    text = """
    Duke Energy Carolinas, LLC
    Rate Year 0 North Carolina Tariffs Proposed for Change
    I.
    RETAIL CLASSIFICATION
    A.
    RESIDENTIAL RATE SCHEDULES
    LEAF NO.
    DESCRIPTION
    REVISION NO.
    11
    RS Residential Service ......................... 61
    13
    RE Residential Service Electric Water Heating and Space Conditioning ............ 62
    15
    RT Residential Service Time Of Use ............ 61
    B.
    GENERAL SERVICE AND INDUSTRIAL RATE SCHEDULES
    21
    SGS Small General Service ............ 37
    29
    LGS Large General Service ............ 36
    """

    entries = parse_dec_exhibit_index(text)

    assert [(e.leaf_no, e.schedule_code, e.description) for e in entries] == [
        (11, "RS", "Residential Service"),
        (13, "RE", "Residential Service Electric Water Heating and Space Conditioning"),
        (15, "RT", "Residential Service Time Of Use"),
        (21, "SGS", "Small General Service"),
        (29, "LGS", "Large General Service"),
    ]


def test_extract_dec_split_line_charges_pairs_label_with_value() -> None:
    text = """
    AVAILABILITY
    Some availability text that should not produce rates.
    Basic Customer Charge
    $ 16.00
    For the first 6,000 kWh per month, per kWh
    8.4138¢
    For all over 6,000 kWh per month, per kWh
    7.3472¢
    For all kWh per month, per kWh
    6.8961¢
    LEAF NO.
    11
    Duke Energy Carolinas, LLC
    """

    charges = extract_dec_split_line_charges(text)

    by_value = {c.rate_value: c for c in charges}
    assert 16.00 in by_value
    assert by_value[16.00].rate_unit == "$/month"
    assert by_value[16.00].charge_type == "fixed"

    assert 0.084138 in by_value
    assert by_value[0.084138].rate_unit == "$/kWh"
    assert by_value[0.084138].charge_type == "energy"

    # Leaf number "11" should not be picked up as a charge — its preceding
    # line ("LEAF NO.") is not a rate-bearing label.
    assert 11 not in by_value


def test_parse_dec_rider_catalog_captures_new_riders_marked_orig() -> None:
    text = """
    RETAIL RIDERS
    IN CONJUNCTION WITH:
    99
    Summary of Rider Adjustments ............ All ............ 57
    60
    FCAR Fuel Cost Adjustment Rider ............ All Retail Schedules ............ 46
    183
    PC Pension Costs Rider ............ All Retail Schedules ............ Orig.
    184
    RAL-2 Regulatory Asset and Liability Rider ............ All Retail Schedules ............ Orig.
    194
    PTC Production Tax Credits Rider ............ All Retail Schedules ............ 2
    E.
    OTHER TARIFFS
    """

    catalog = parse_dec_rider_catalog(text)

    assert [(entry.leaf_no, entry.schedule_code, entry.normalized_name) for entry in catalog] == [
        (183, "PC", "RIDER PC PENSION COSTS"),
        (184, "RAL-2", "RIDER RAL-2 REGULATORY ASSET AND LIABILITY"),
    ]


def test_extract_dec_split_line_charges_skips_bare_thresholds() -> None:
    text = """
    For the first 4,500 kWh per month, per kWh
    For all over 5,500 kWh per month, per kWh
    """

    assert extract_dec_split_line_charges(text) == []
