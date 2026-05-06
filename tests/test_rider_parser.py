from duke_rates.parse.rider_parser import parse_rider_text


def test_rider_parser_extracts_historical_ba_rider_details() -> None:
    text = """
    RIDER BA-9
    ANNUAL BILLING ADJUSTMENTS
    RIDER BA-9

    APPLICABILITY - RATES INCLUDED IN TARIFF CHARGES
    The rates shown below are included in the monthly rate provision.

    Residential
    -0.054 0.171 0.405 0.021 0.543
    Applicable to Schedules: RES, R-TOUD, R-TOUE & R-TOU

    Small General Service
    -0.205 0.379 0.350 0.009 0.533
    Applicable to Schedules: SGS, SGS-TOUE, TSF & TSS

    Effective December 1, 2014
    """
    result = parse_rider_text(
        document_id=1,
        title="RR1 NC Rider BA dep",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.status == "parsed"
    assert result.rider.code == "BA"
    assert result.rider.version_code == "BA-9"
    assert result.rider.title == "Annual Billing Adjustments"
    assert result.rider.effective_date == "December 1, 2014"
    assert result.rider.adjustment_rows[0].rate_class == "Residential"
    assert result.rider.adjustment_rows[0].net_adjustment_cents_per_kwh == 0.543
    assert {"RES", "R-TOUD", "R-TOUE", "R-TOU", "SGS", "SGS-TOUE"} <= set(
        result.rider.applicable_schedules
    )


def test_rider_parser_limits_ba_lighting_row_to_schedule_codes() -> None:
    text = """
    RIDER BA-9
    ANNUAL BILLING ADJUSTMENTS
    RIDER BA-9

    Lighting
    -0.699 0.563 0.102 0.010 -0.024
    Applicable to Schedules: ALS, SLS, SLR &
    SFLS

    * Billing Adjustment Factors, shown above, includes a North Carolina regulatory fee.
    Billing Adjustment Factors Description:
    The Fuel and Fuel-Related Adjustment Rate is adjusted annually.
    """
    result = parse_rider_text(
        document_id=2,
        title="RR1 NC Rider BA dep",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    lighting = result.rider.adjustment_rows[0]
    assert lighting.rate_class == "Lighting"
    assert lighting.net_adjustment_cents_per_kwh == -0.024
    assert lighting.applicable_schedules == ["ALS", "SLS", "SLR", "SFLS"]


def test_rider_parser_extracts_current_bill_components() -> None:
    text = """
    NC Sixth Revised Leaf No. 601
    Effective for service rendered on and after January 1, 2026
    ANNUAL BILLING ADJUSTMENTS
    RIDER BA

    Residential
    Applicable to
    Schedules:
    RES, R-TOUD, R-TOU, &
    R-TOU-CPP
    .262
    0.518
    0.663
    0.106
    1.549

    APPLICABILITY - CLEAN ENERGY PORTFOLIO STANDARD CHARGES
    Residential
    $ 1.75 per month
    $ 0.06 per month
    $ 1.81 per month
    """
    result = parse_rider_text(
        document_id=3,
        title="Annual Billing Adjustments",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.rider.adjustment_rows[0].net_adjustment_cents_per_kwh == 1.549
    labels = {component.bill_label: component for component in result.rider.charge_components}
    assert labels["Summary of Rider Adjustments"].value == 1.549
    assert labels["Clean Energy Rider"].value == 1.81
    assert {"RES", "R-TOUD", "R-TOU", "R-TOU-CPP"} <= set(result.rider.applicable_schedules)


def test_rider_parser_extracts_current_progress_residential_summary_components() -> None:
    text = """
    NC Third Revised Leaf No. 602
    Effective for bills rendered on and after December 1, 2025
    JOINT AGENCY ASSET RIDER JAA

    APPLICABILITY
    The rates shown below are not included in the MONTHLY RATE provision.

    MONTHLY RATE
    The incremental rider for each rate class as follows:
    Rate Class
    Applicable Schedule(s)
    Incremental Rate
    Non-Demand Rate Class (dollars per kilowatt-hour)
    Residential
    RES, R-TOUD, R-TOU, R-TOU-CPP
    0.00464
    Small General Service
    SGS, SGS-TOUE
    0.00223
    """
    result = parse_rider_text(
        document_id=4,
        title="Joint Agency Asset Rider JAA",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.status == "parsed"
    assert result.rider.code == "JAA"
    assert result.rider.applicable_schedules == ["RES", "R-TOUD", "R-TOU", "R-TOU-CPP"]
    assert result.rider.charge_components[0].bill_label == "Summary of Rider Adjustments"
    assert result.rider.charge_components[0].value == 0.464


def test_rider_parser_infers_code_from_known_rider_title() -> None:
    text = """
    Duke Energy Progress, LLC Compliance Tariffs
    Effective for service rendered on and after January 1, 2017
    """
    result = parse_rider_text(
        document_id=5,
        title="Energy Efficiency Rider",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.rider.code == "EE"


def test_rider_parser_extracts_dsm_notice_component() -> None:
    text = """
    Demand Side Management Rider
    Effective January 1, 2018
    General Service customers would see a DSM rider decrease of 0.010 cents per kWh.
    """
    result = parse_rider_text(
        document_id=6,
        title="Demand Side Management Rider",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.rider.code == "DSM"
    assert result.rider.charge_components[0].bill_label == "Demand Side Management Rider"
    assert result.rider.charge_components[0].value == -0.01
    assert result.rider.charge_components[0].rate_class == "General Service"


def test_rider_parser_extracts_reps_fixed_monthly_component() -> None:
    text = """
    REPS Rider
    Effective February 1, 2017
    The appropriate monthly REPS riders per customer account, excluding the regulatory fee,
    that would have been collected during the Billing Period are $1.26 for residential accounts,
    $10.65 for general service accounts, and $67.98 for industrial accounts.
    """
    result = parse_rider_text(
        document_id=7,
        title="REPS Rider",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.rider.code == "REPS"
    assert result.rider.charge_components[0].bill_label == "REPS Rider"
    assert result.rider.charge_components[0].value == 1.26
    assert result.rider.charge_components[0].unit == "fixed_monthly"


def test_rider_parser_extracts_reps_emf_fixed_monthly_component() -> None:
    text = """
    REPS EMF Rider
    Effective February 1, 2017
    The appropriate monthly REPS EMF riders per customer account,
    excluding the North Carolina regulatory fee, to be charged (credited)
    to customers during the billing period are $0.01 for residential accounts.
    """
    result = parse_rider_text(
        document_id=8,
        title="REPS EMF Rider",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.rider.charge_components[0].bill_label == "REPS EMF Rider"
    assert result.rider.charge_components[0].value == 0.01


def test_rider_parser_prefers_supplied_title_when_excerpt_heading_is_noise() -> None:
    text = """
    Electronically submitted
    REPS Experience Modification Factor (EMF) to true-up any over or under-recovery.
    The appropriate monthly REPS EMF riders per customer account are $0.01 for residential accounts.
    """
    result = parse_rider_text(
        document_id=9,
        title="REPS EMF Rider",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.rider is not None
    assert result.rider.title == "REPS EMF Rider"
