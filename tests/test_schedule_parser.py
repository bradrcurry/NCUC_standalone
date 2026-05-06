from duke_rates.parse.schedule_parser import parse_schedule_text


def test_schedule_parser_extracts_basic_fields() -> None:
    text = """
    Duke Energy Progress Residential Service Schedule RS
    Effective January 1, 2025
    Basic Customer Charge $14.00 per month
    Energy Charge $0.1200 per kWh
    Available to residential customers.
    Rider NM
    """
    result = parse_schedule_text(
        document_id=1,
        title="Residential Service Schedule RS",
        state="NC",
        company="progress",
        text=text,
    )
    assert result.schedule is not None
    assert result.schedule.schedule_code == "RS"
    assert result.schedule.fixed_charges[0].amount == 14.0
    assert result.schedule.energy_charges[0].rate == 0.12


def test_schedule_parser_handles_nc_leaf_format_without_false_fixed_charge() -> None:
    text = """
    Duke Energy Carolinas, LLC
    SCHEDULE RS
    Effective for service rendered on and after January 1, 2026
    TYPE OF SERVICE
    The Company will furnish 60 Hertz service through one meter.
    RATE
    I.
    Basic Customer Charge per month
    $ 14.00
    II.
    Energy Charge per month, per kWh*
    12.2603¢
    Leaf No. 60
    Fuel Cost Adjustment Rider
    Leaf No. 64
    Existing DSM Program Costs Adjustment Rider
    Leaf No. 194
    NPTC Rider
    A Storm Securitization (STS) Rider charge will be added.
    """
    result = parse_schedule_text(
        document_id=2,
        title="RS",
        state="NC",
        company="carolinas",
        text=text,
    )
    assert result.schedule is not None
    assert result.schedule.fixed_charges[0].amount == 14.0
    assert result.schedule.energy_charges[0].rate == 0.122603
    assert result.schedule.effective_start is not None
    rider_titles = {rider.title for rider in result.schedule.riders}
    rider_codes = {rider.code for rider in result.schedule.riders if rider.code}
    assert "Fuel Cost Adjustment Rider" in rider_titles
    assert "Existing DSM Program Costs Adjustment Rider" in rider_titles
    assert "NPTC" in rider_codes
    assert "STS" in rider_codes
    assert "CHARGE" not in rider_codes
    assert "INCREMENT" not in rider_codes


def test_schedule_parser_handles_progress_block_rates_and_effective_date() -> None:
    text = """
    Duke Energy Progress, LLC
    Effective for service rendered on and after October 1, 2025
    RESIDENTIAL SERVICE
    SCHEDULE RES
    MONTHLY RATE
    Basic Customer Charge:
    $14.00 per month
    Kilowatt-Hour Charge:
    12.623¢ per kWh for the first 800 kWh
    11.623¢ per kWh for the additional kWh
    Leaf No. 601
    Rider BA
    Leaf No. 607 Rider STS
    """
    result = parse_schedule_text(
        document_id=3,
        title="Residential Service Schedule RES",
        state="NC",
        company="progress",
        text=text,
    )
    assert result.schedule is not None
    assert result.schedule.schedule_code == "RES"
    assert result.schedule.effective_start is not None
    assert result.schedule.fixed_charges[0].amount == 14.0
    assert [charge.rate for charge in result.schedule.energy_charges[:2]] == [0.12623, 0.11623]
    rider_codes = {rider.code for rider in result.schedule.riders if rider.code}
    assert {"BA", "STS"} <= rider_codes


def test_schedule_parser_flags_summary_matrix_documents() -> None:
    text = """
    Base Rates by Rate Schedule
    Effective January 2026
    DEF Tariff Base Rates
    schedule
    RS-1 (Win) RS-1 (Sum)
    RST-1
    GS-1
    GST-1
    GS-2
    GSD-1
    Customer Chrg - Unmetered
    $/mo
    10.29
    Energy Chrg - Standard
    ¢/kWh
    8.255
    """
    result = parse_schedule_text(
        document_id=4,
        title="As of February 2026",
        state="FL",
        company="florida",
        text=text,
    )
    assert "Summary/matrix rate document detected" in result.review_flags


def test_schedule_parser_extracts_tou_leaf_energy_periods() -> None:
    text = """
    EFFECTIVE: January 1, 2026
    RATE SCHEDULE RST-1
    RESIDENTIAL SERVICE
    OPTIONAL TIME OF USE RATE
    Customer Charge:
    $ 14.27
    Energy and Demand Charges:
    Non-Fuel Energy Charges:
    11.032¢ per On-Peak kWh
    8.172¢ per Off-Peak kWh
    4.958¢ per Discount kWh
    Rating Periods:
    (a) On-Peak Periods -
    For the calendar months of December through February,
    Monday through Friday:
    5:00 a.m. to 10:00 a.m.
    (b) Discount Periods -
    For the calendar months of March through November,
    Every day, including weekends and holidays
    12:00 a.m. (midnight) to 6:00 a.m.
    (c) Off-Peak Periods -
    The designated Off-Peak Periods shall be all periods other than the designated
    On-Peak and Discount Periods.
    """
    result = parse_schedule_text(
        document_id=5,
        title="Residential Service (Optional Time of Use)",
        state="FL",
        company="florida",
        text=text,
    )
    assert result.schedule is not None
    assert result.schedule.schedule_code == "RST-1"
    assert result.schedule.demand_charges == []
    assert [charge.period for charge in result.schedule.energy_charges[:3]] == [
        "On-Peak",
        "Off-Peak",
        "Discount",
    ]
    assert [charge.rate for charge in result.schedule.energy_charges[:3]] == [
        0.11032,
        0.08172,
        0.04958,
    ]
    assert result.schedule.tou_periods[0].name == "On-Peak"
    assert result.schedule.tou_periods[0].months == [
        "calendar months of December through February"
    ]


def test_schedule_parser_extracts_historical_multi_hyphen_code_and_heading() -> None:
    text = """
    R-1
    RESIDENTIAL SERVICE
    SCHEDULE RES-28
    MONTHLY RATE
    Basic Customer Charge:
    $11.13 per month
    Kilowatt-Hour Charge:
    10.180¢ per kWh
    """
    result = parse_schedule_text(
        document_id=6,
        title="R1 NC Schedule RES dep",
        state="NC",
        company="progress",
        text=text,
    )
    assert result.schedule is not None
    assert result.schedule.schedule_code == "RES-28"
    assert result.schedule.schedule_title == "Residential Service"


def test_schedule_parser_handles_progress_tou_cpp_leaf_without_bogus_generic_rates() -> None:
    text = """
    Duke Energy Progress, LLC
    NC Second Revised Leaf No. 503
    Effective for service rendered on and after October 1, 2025
    RESIDENTIAL SERVICE
    TIME OF USE WITH CRITICAL PEAK PRICING
    SCHEDULE R-TOU-CPP
    MONTHLY RATE
    I.
    For Single-Phase Service:
    A. Basic Customer Charge:
    $14.00
    B. kWh Energy Charge:
    1. 41.002¢ per Critical Peak kWh
    2. 21.952¢ per On-Peak kWh
    3. 11.000¢ per Off-Peak kWh
    4. 8.274¢ per Discount kWh
    DETERMINATION OF ON-PEAK, OFF-PEAK, AND DISCOUNT HOURS
    Applicable Days
    Summer Hours
    May – September
    Non-Summer Hours
    October – April
    On-Peak Period:
    Monday – Friday
    excluding Holidays*
    6:00 pm – 9:00 pm
    6:00 am – 9:00 am
    Discount Period:
    All days
    including Holidays*
    1:00 am – 6:00 am
    1:00 am – 3:00 am
    11:00 am – 4:00 pm
    Off-Peak Period:
    All hours that are not On-Peak
    or Discount Hours
    """
    result = parse_schedule_text(
        document_id=7,
        title="Leaf 503 Schedule R-TOU-CPP",
        state="NC",
        company="progress",
        text=text,
    )

    assert result.schedule is not None
    assert result.schedule.schedule_code == "R-TOU-CPP"
    assert [charge.amount for charge in result.schedule.fixed_charges] == [14.0]
    assert {charge.period for charge in result.schedule.energy_charges} == {
        "Critical Peak",
        "On-Peak",
        "Off-Peak",
        "Discount",
    }
    assert all(charge.rate < 0.5 for charge in result.schedule.energy_charges)
    on_peak = [
        period for period in result.schedule.tou_periods if period.name == "On-Peak"
    ]
    assert len(on_peak) == 2
    assert on_peak[0].weekday_hours == "6:00 pm – 9:00 pm"
    assert on_peak[1].weekday_hours == "6:00 am – 9:00 am"
