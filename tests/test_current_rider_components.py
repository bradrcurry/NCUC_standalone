from datetime import date, datetime

from duke_rates.billing.calculators import UsageInput
from duke_rates.billing.engine import BillingEngine
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import EnergyCharge, FixedCharge, RateScheduleData
from duke_rates.models.rider import RiderChargeComponent, RiderData


def test_billing_engine_applies_current_bill_components() -> None:
    schedule = RateScheduleData(
        tariff_id="nc_progress_res",
        state="NC",
        company="progress",
        schedule_code="RES",
        schedule_title="Residential Service",
        fixed_charges=[FixedCharge(label="Basic Customer Charge", amount=14.0)],
        energy_charges=[
            EnergyCharge(label="Kilowatt-Hour Charge", rate=0.12623, block_from=0, block_to=800),
            EnergyCharge(label="Kilowatt-Hour Charge", rate=0.11623, block_from=800),
        ],
    )
    rider_results = [
        DocumentParseResult(
            document_id=1,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="summary",
                code="SUMMARY",
                title="Summary of Rider Adjustments",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Summary of Rider Adjustments",
                        value=2.097,
                        unit="cents_per_kwh",
                    )
                ],
            ),
        ),
        DocumentParseResult(
            document_id=2,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="sts",
                code="STS",
                title="Storm Securitization Rider STS",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Storm Recovery Charge",
                        value=0.216,
                        unit="cents_per_kwh",
                    )
                ],
            ),
        ),
        DocumentParseResult(
            document_id=3,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="sts2",
                code="STS-2",
                title="Storm Securitization Rider STS-2",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Storm Recovery Charge",
                        value=0.166,
                        unit="cents_per_kwh",
                    )
                ],
            ),
        ),
        DocumentParseResult(
            document_id=4,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="ba",
                code="BA",
                title="Annual Billing Adjustments",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Clean Energy Rider",
                        value=1.81,
                        unit="fixed_monthly",
                    )
                ],
            ),
        ),
        DocumentParseResult(
            document_id=5,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="recd",
                code="RECD",
                title="Energy Conservation Discount",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Energy Conservation Credit",
                        value=5.0,
                        unit="percent_of_energy_charges",
                    )
                ],
            ),
        ),
    ]

    estimate = BillingEngine().estimate(
        schedule,
        UsageInput(monthly_kwh=2405, service_date=datetime(2026, 2, 16).date()),
        rider_parse_results=rider_results,
    )

    labels: dict[str, float] = {}
    for item in estimate.line_items:
        labels[item.label] = round(labels.get(item.label, 0.0) + item.amount, 2)
    assert labels["Summary of Rider Adjustments"] == 50.43
    assert labels["Storm Recovery Charge"] == 9.18
    assert labels["Clean Energy Rider"] == 1.81
    assert labels["Energy Conservation Credit"] == -14.38


def test_billing_engine_prorates_stacked_storm_riders_across_effective_dates() -> None:
    schedule = RateScheduleData(
        tariff_id="nc_progress_res",
        state="NC",
        company="progress",
        schedule_code="RES",
        schedule_title="Residential Service",
        fixed_charges=[FixedCharge(label="Basic Customer Charge", amount=14.0)],
        energy_charges=[EnergyCharge(label="Kilowatt-Hour Charge", rate=0.12623)],
    )
    rider_results = [
        DocumentParseResult(
            document_id=1,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            raw_text_path="leaf-no-607",
            rider=RiderData(
                rider_id="sts_607",
                code="STS",
                title="Storm Recovery Rider",
                effective_date="July 1, 2025",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Storm Recovery Charge",
                        value=0.21,
                        unit="cents_per_kwh",
                    )
                ],
            ),
        ),
        DocumentParseResult(
            document_id=2,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            raw_text_path="leaf-no-613",
            rider=RiderData(
                rider_id="sts_613",
                code="STS",
                title="Storm Securitization Rider STS-2",
                effective_date="November 1, 2025",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Storm Recovery Charge",
                        value=0.166,
                        unit="cents_per_kwh",
                    )
                ],
            ),
        ),
    ]

    estimate = BillingEngine().estimate(
        schedule,
        UsageInput(
            monthly_kwh=813,
            service_date=date(2025, 11, 17),
            billing_period_start=date(2025, 10, 18),
            billing_period_end=date(2025, 11, 17),
        ),
        rider_parse_results=rider_results,
    )

    labels: dict[str, float] = {}
    for item in estimate.line_items:
        labels[item.label] = round(labels.get(item.label, 0.0) + item.amount, 2)
    assert labels["Storm Recovery Charge"] == 2.45


def test_billing_engine_prorates_short_partial_period_fixed_components() -> None:
    schedule = RateScheduleData(
        tariff_id="nc_progress_res",
        state="NC",
        company="progress",
        schedule_code="RES",
        schedule_title="Residential Service",
        fixed_charges=[FixedCharge(label="Basic Customer Charge", amount=14.0)],
        energy_charges=[EnergyCharge(label="Kilowatt-Hour Charge", rate=0.12623)],
    )
    rider_results = [
        DocumentParseResult(
            document_id=1,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="ba",
                code="BA",
                title="Annual Billing Adjustments",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Clean Energy Rider",
                        value=1.52,
                        unit="fixed_monthly",
                    )
                ],
            ),
        ),
    ]

    estimate = BillingEngine().estimate(
        schedule,
        UsageInput(
            monthly_kwh=821,
            service_date=date(2025, 4, 16),
            billing_period_start=date(2025, 3, 31),
            billing_period_end=date(2025, 4, 16),
        ),
        rider_parse_results=rider_results,
    )

    labels: dict[str, float] = {}
    for item in estimate.line_items:
        labels[item.label] = round(labels.get(item.label, 0.0) + item.amount, 2)
    assert labels["Basic Customer Charge"] == 7.93
    assert labels["Clean Energy Rider"] == 0.86
