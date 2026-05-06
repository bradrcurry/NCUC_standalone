from datetime import datetime

from duke_rates.billing.calculators import IntervalUsagePoint, UsageInput
from duke_rates.billing.engine import BillingEngine
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import EnergyCharge, FixedCharge, RateScheduleData, TOUPeriod
from duke_rates.models.rider import RiderAdjustmentRow, RiderData


def test_billing_engine_estimates_simple_bill() -> None:
    schedule = RateScheduleData(
        tariff_id="nc_progress_rs",
        state="NC",
        company="progress",
        schedule_title="Residential Service",
        fixed_charges=[FixedCharge(label="Customer charge", amount=15.0)],
        energy_charges=[EnergyCharge(label="Energy charge", rate=0.10)],
    )
    estimate = BillingEngine().estimate(schedule, UsageInput(monthly_kwh=1000))
    assert estimate.total == 115.0


def test_billing_engine_applies_block_pricing_for_matching_season() -> None:
    schedule = RateScheduleData(
        tariff_id="nc_progress_res",
        state="NC",
        company="progress",
        schedule_title="Residential Service",
        fixed_charges=[FixedCharge(label="Customer charge", amount=14.0)],
        energy_charges=[
            EnergyCharge(
                label="Kilowatt-Hour Charge",
                rate=0.12623,
                season="October - April",
                block_from=0,
                block_to=800,
            ),
            EnergyCharge(
                label="Kilowatt-Hour Charge",
                rate=0.11623,
                season="October - April",
                block_from=800,
            ),
            EnergyCharge(
                label="Kilowatt-Hour Charge",
                rate=0.12623,
                season="May - September",
                block_from=0,
            ),
        ],
    )

    estimate = BillingEngine().estimate(
        schedule,
        UsageInput(monthly_kwh=1234, service_date=datetime(2025, 12, 16).date()),
    )

    assert estimate.subtotal == 165.42
    assert estimate.line_items[1].amount == 100.98
    assert estimate.line_items[2].amount == 50.44


def test_billing_engine_allocates_tou_interval_usage() -> None:
    schedule = RateScheduleData(
        tariff_id="fl_florida_rst-1",
        state="FL",
        company="florida",
        schedule_title="Residential Service (Optional Time of Use)",
        fixed_charges=[FixedCharge(label="Customer charge", amount=14.27)],
        energy_charges=[
            EnergyCharge(label="On-Peak", rate=0.11032, period="On-Peak"),
            EnergyCharge(label="Off-Peak", rate=0.08172, period="Off-Peak"),
            EnergyCharge(label="Discount", rate=0.04958, period="Discount"),
        ],
        tou_periods=[
            TOUPeriod(
                name="On-Peak",
                months=["calendar months of December through February"],
                weekday_hours="5:00 a.m. to 10:00 a.m.",
            ),
            TOUPeriod(
                name="Discount",
                months=["calendar months of March through November"],
                weekday_hours="12:00 a.m. (midnight) to 6:00 a.m.",
                weekend_hours="12:00 a.m. (midnight) to 6:00 a.m.",
            ),
            TOUPeriod(name="Off-Peak"),
        ],
    )
    usage = UsageInput(
        monthly_kwh=3.0,
        interval_data=[
            IntervalUsagePoint(timestamp=datetime.fromisoformat("2026-01-05T06:00:00"), kwh=1.0),
            IntervalUsagePoint(timestamp=datetime.fromisoformat("2026-01-05T12:00:00"), kwh=1.0),
            IntervalUsagePoint(timestamp=datetime.fromisoformat("2026-03-07T01:00:00"), kwh=1.0),
        ],
    )
    estimate = BillingEngine().estimate(schedule, usage)
    assert estimate.total == 14.51
    assert [item.label for item in estimate.line_items[1:]] == ["On-Peak", "Off-Peak", "Discount"]


def test_billing_engine_applies_historical_ba_rider_adjustment() -> None:
    schedule = RateScheduleData(
        tariff_id="nc_progress_res_2024",
        state="NC",
        company="progress",
        schedule_code="RES",
        schedule_title="Residential Service",
        fixed_charges=[FixedCharge(label="Customer charge", amount=14.0)],
        energy_charges=[EnergyCharge(label="Energy charge", rate=0.11)],
    )
    rider_parse = DocumentParseResult(
        document_id=10,
        parser_name="rider_parser",
        status=ParseStatus.PARSED,
        rider=RiderData(
            rider_id="nc_progress_ba",
            state="NC",
            company="progress",
            code="BA",
            version_code="BA-9",
            title="Annual Billing Adjustments",
            applicable_schedules=["RES"],
            adjustment_rows=[
                RiderAdjustmentRow(
                    rate_class="Residential",
                    net_adjustment_cents_per_kwh=0.543,
                    applicable_schedules=["RES", "R-TOUD"],
                )
            ],
        ),
    )

    estimate = BillingEngine().estimate(
        schedule,
        UsageInput(monthly_kwh=1000),
        rider_parse_results=[rider_parse],
    )

    assert estimate.total == 129.43
    assert any("Annual Billing Adjustments" in item.label for item in estimate.line_items)
