from duke_rates.parse.rider_summary import parse_rider_summary


def test_parse_rider_summary_extracts_dec_residential_leaf_99_block() -> None:
    text = """
    Duke Energy Carolinas, LLC
    NC Sixty-First Revised Leaf No. 99
    SUMMARY OF RIDER ADJUSTMENTS
    The following is a summary of Rider Adjustments that must be added to the bill.

    Residential Schedules RS, RE, ES, RT, RSTC, RETC cents/kWh Effective Date
    Fuel Cost Adjustment Rider 1.2682 1/15/24
    Energy Efficiency Rider 0.3775 1/1/24
    Existing DSM Program Costs Adjustment Rider -0.0027 7/1/24
    BPM Prospective Rider -0.0128 7/1/24
    BPM True-Up Rider -0.0039 7/1/24
    CPRE Rider 0.0143 9/1/23
    EDIT-4 Rider -0.5081 1/15/24
    Regulatory Asset and Liability Rider -0.0009 1/15/24
    Customer Assistance Recovery Rider 0.1246 1/15/24
    Residential Decoupling Mechanism Rider 0.0000 1/15/24
    Earnings Sharing Mechanism Rider 0.0000 1/15/24
    Performance Incentive Mechanism Rider 0.0000 1/15/24
    TOTAL cents/kWh 1.2562
    """

    result = parse_rider_summary(text, source_pdf="dec-leaf99.pdf", leaf_no="99")

    assert len(result.rate_classes) == 1
    residential = result.rate_classes[0]
    assert residential.rate_class == "Residential Schedules"
    assert residential.applicable_schedules == ["RS", "RE", "ES", "RT", "RSTC", "RETC"]
    assert residential.total_cents_per_kwh == 1.2562

    components = {item.rider_code: item.cents_per_kwh for item in residential.line_items if item.rider_code}
    assert components["FCA"] == 1.2682
    assert components["EE"] == 0.3775
    assert components["DSM"] == -0.0027
    assert components["BPM-P"] == -0.0128
    assert components["BPM-T"] == -0.0039
    assert components["CPRE"] == 0.0143
    assert components["EDIT-4"] == -0.5081
    assert components["RAL"] == -0.0009
    assert components["CAR"] == 0.1246
    assert components["RDM"] == 0.0
    assert components["ESM"] == 0.0
    assert components["PIM"] == 0.0


def test_parse_rider_summary_extracts_dep_residential_service_schedules_block() -> None:
    text = """
    Duke Energy Progress, LLC
    NC Eighth Revised Leaf No. 600
    SUMMARY OF RIDER ADJUSTMENTS
    Effective for service rendered on and after January 1, 2026

    Residential Service Schedules
    cents
    /kWh
    Effective
    Date
    Annual Billing Adjustments Rider BA
    Fuel and Fuel-Related Adjustment Rate 0.262 12/1/25
    Fuel and Fuel-Related Adjustment Experience Modification Factor
    (EMF) 0.518 12/1/25
    Demand Side Management DSM & EE Rate 0.769 1/1/26
    Annual Billing Adjustments Rider BA - Net Adjustment 1.549
    EDIT-4 Rider -0.249 10/1/23
    Joint Agency Asset Rider JAA 0.464 12/1/25
    Competitive Procurement of Renewable Energy Rider CPRE 0.001 12/1/25
    Customer Assistance Recovery Rider CAR 0.098 1/1/26
    Residential Decoupling Mechanism Rider RDM 0.232 4/1/25
    Earnings Sharing Mechanism Rider ESM 0.000 4/1/25
    Performance Incentive Mechanism Rider PIM 0.002 4/1/25
    TOTAL cents/kWh 2.097
    """

    result = parse_rider_summary(text, source_pdf="dep-leaf600.pdf", leaf_no="600")

    assert len(result.rate_classes) == 1
    residential = result.rate_classes[0]
    assert residential.rate_class == "Residential Service Schedules"
    assert residential.total_cents_per_kwh == 2.097

    components = {item.rider_code: item.cents_per_kwh for item in residential.line_items if item.rider_code}
    assert components["BA-Fuel"] == 0.262
    assert components["BA-EMF"] == 0.518
    assert components["BA-DSM"] == 0.769
    assert components["BA"] == 1.549
    assert components["EDIT-4"] == -0.249
    assert components["JAA"] == 0.464
    assert components["CPRE"] == 0.001
    assert components["CAR"] == 0.098
    assert components["RDM"] == 0.232
    assert components["ESM"] == 0.0
    assert components["PIM"] == 0.002


def test_parse_rider_summary_joins_docling_split_section_headers() -> None:
    """Regression: Docling sometimes splits long DEC SUMMARY section headers across
    lines, putting the schedule list and 'cents/kWh' marker on a second line below the
    rate-class name. Without joining, _RATE_CLASS_RE never matches the second header
    and all rows fall into the first (Residential) block. See production doc
    e-7-sub-1307/bb3752b0-...pdf."""
    text = """
    Duke Energy Carolinas, LLC
    SUMMARY OF RIDER ADJUSTMENTS

    Residential Schedules RS, RE, ES, RT, RSTC, RETC cents/kWh Effective Date
    Fuel Cost Adjustment Rider 1.4464 9/1/24
    Energy Efficiency Rider 0.3775 1/1/24
    TOTAL cents/kWh 1.4264

    General Service Schedules SGS, BC, LGS, TS, S, HLF, OPT-V,

    PG, SGSTC cents/kWh Effective Date
    Fuel Cost Adjustment Rider 1.5996 9/1/24
    Energy Efficiency Rider 0.4343 1/1/24
    TOTAL cents/kWh 1.7137
    """

    result = parse_rider_summary(text, source_pdf="dec-leaf99-split.pdf", leaf_no="99")
    assert len(result.rate_classes) == 2

    blocks_by_class = {rc.rate_class: rc for rc in result.rate_classes}
    assert blocks_by_class["Residential Schedules"].total_cents_per_kwh == 1.4264
    assert blocks_by_class["General Service Schedules"].total_cents_per_kwh == 1.7137

    res_components = {
        item.rider_code: item.cents_per_kwh
        for item in blocks_by_class["Residential Schedules"].line_items
        if item.rider_code
    }
    assert res_components["FCA"] == 1.4464
    assert res_components["EE"] == 0.3775

    gs_components = {
        item.rider_code: item.cents_per_kwh
        for item in blocks_by_class["General Service Schedules"].line_items
        if item.rider_code
    }
    assert gs_components["FCA"] == 1.5996
    assert gs_components["EE"] == 0.4343


def test_parse_rider_summary_prefers_leaf_effective_date_over_component_dates() -> None:
    text = """
    RIDER EDIT-3 (NC)
    Effective for service rendered on and after October 1, 2021, the decremental rate for the appropriate rate class.

    North Carolina Fifty-First Revised Leaf No. 99
    Superseding North Carolina Fiftieth Revised Leaf No. 99
    The following is a summary of Rider Adjustments that must be added to the bill calculated on the applicable rate schedule.
    Residential Schedules RS, RE, ES, RT, RSTC, RETC cents/kWh Effective Date
    Fuel Cost Adjustment Rider -0.1014 9/1/21
    Energy Efficiency Rider 0.4771 1/1/22
    EDIT-3 Rider -0.1894 10/1/21
    EDIT-4 Rider -0.4842 10/1/21
    TOTAL cents/kWh -0.3923
    North Carolina Fifty-First Revised Leaf No. 99
    Effective for service rendered on and after January 1, 2021
    """

    result = parse_rider_summary(text, source_pdf="dec-leaf99-2021.pdf", leaf_no="99")

    assert result.effective_date == "January 1, 2021"
