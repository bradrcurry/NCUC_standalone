from datetime import date

from duke_rates.billing.observations import derive_bill_component_observations
from duke_rates.models.bill import BillLineItem, BillSection, BillStatementData


def test_derive_bill_component_observations_infers_key_residential_components():
    statement = BillStatementData(
        source_path="bill.pdf",
        electric_section=BillSection(
            name="Electric",
            rate_code="RES",
            billing_period_start=date(2025, 12, 17),
            billing_period_end=date(2026, 1, 17),
            line_items=[
                BillLineItem(
                    label="Energy Charge",
                    amount=100.98,
                    quantity=800.0,
                    unit="kWh",
                    rate=0.12623,
                ),
                BillLineItem(
                    label="Energy Charge",
                    amount=58.12,
                    quantity=500.0,
                    unit="kWh",
                    rate=0.11623,
                ),
                BillLineItem(label="Clean Energy Rider", amount=1.81),
                BillLineItem(label="Energy Conservation Credit", amount=-7.96),
                BillLineItem(label="Storm Recovery Charge", amount=4.93),
                BillLineItem(label="Summary of Rider Adjustments", amount=15.99),
                BillLineItem(
                    label="Summary of Rider Adjustments - Dec 17 to Dec 31",
                    amount=4.88,
                    is_subperiod_detail=True,
                    period_start=date(2025, 12, 17),
                    period_end=date(2025, 12, 31),
                ),
            ],
        ),
    )

    observations = derive_bill_component_observations(bill_id=9, statement=statement)
    by_key = {
        (item.component_label, item.period_start, item.period_end): item
        for item in observations
        if item.component_label != "Energy Charge"
    }
    energy_rates = sorted(
        item.inferred_value
        for item in observations
        if item.component_label == "Energy Charge"
    )

    assert energy_rates == [0.11623, 0.12623]

    clean_energy = by_key[("Clean Energy Rider", None, None)]
    assert clean_energy.inferred_unit == "fixed_monthly"
    assert clean_energy.inferred_value == 1.81

    credit = by_key[("Energy Conservation Credit", None, None)]
    assert credit.inferred_unit == "percent_of_energy_charges"
    assert credit.inferred_value == 5.003

    summary_total = by_key[("Summary of Rider Adjustments", None, None)]
    assert summary_total.inferred_unit == "cents_per_kwh"
    assert summary_total.inferred_value == 1.23
    assert summary_total.quantity_basis_kwh == 1300.0

    summary_subperiod = next(
        item
        for item in observations
        if item.component_key == "summary_rider_adjustments"
        and item.period_start == date(2025, 12, 17)
        and item.period_end == date(2025, 12, 31)
    )
    assert summary_subperiod.inferred_unit == "cents_per_kwh"
    assert summary_subperiod.quantity_basis_kwh == 609.375
    assert summary_subperiod.notes
