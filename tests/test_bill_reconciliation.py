from datetime import date

from duke_rates.billing.reconciliation import _aggregate_actual_line_items, derive_billed_kwh
from duke_rates.models.bill import BillLineItem as ActualBillLineItem
from duke_rates.models.bill import BillSection, BillStatementData


def test_derive_billed_kwh_sums_energy_rows() -> None:
    statement = BillStatementData(
        source_path="bill.pdf",
        electric_section=BillSection(
            name="Electric",
            line_items=[
                ActualBillLineItem(
                    label="Energy Charge",
                    quantity=800.0,
                    unit="kWh",
                    amount=100.98,
                ),
                ActualBillLineItem(
                    label="Energy Charge",
                    quantity=434.0,
                    unit="kWh",
                    amount=50.44,
                ),
                ActualBillLineItem(
                    label="Clean Energy Rider - Nov 18 to Nov 30",
                    amount=0.68,
                    is_subperiod_detail=True,
                    period_start=date(2025, 11, 18),
                    period_end=date(2025, 11, 30),
                ),
            ],
        ),
    )

    assert derive_billed_kwh(statement) == 1234.0


def test_derive_billed_kwh_includes_split_period_energy_rows() -> None:
    statement = BillStatementData(
        source_path="bill.pdf",
        electric_section=BillSection(
            name="Electric",
            line_items=[
                ActualBillLineItem(
                    label="Energy Charge",
                    quantity=379.0,
                    unit="kWh",
                    amount=45.93,
                    is_subperiod_detail=True,
                    period_start=date(2025, 9, 18),
                    period_end=date(2025, 9, 30),
                ),
                ActualBillLineItem(
                    label="Energy Charge",
                    quantity=421.0,
                    unit="kWh",
                    amount=53.14,
                    is_subperiod_detail=True,
                    period_start=date(2025, 10, 1),
                    period_end=date(2025, 10, 17),
                ),
                ActualBillLineItem(
                    label="Energy Charge",
                    quantity=76.0,
                    unit="kWh",
                    amount=8.83,
                ),
            ],
        ),
    )

    assert derive_billed_kwh(statement) == 876.0


def test_aggregate_actual_line_items_prefers_base_total_over_subperiod_breakouts() -> None:
    totals = _aggregate_actual_line_items(
        [
            ActualBillLineItem(label="Summary of Rider Adjustments", amount=9.69),
            ActualBillLineItem(
                label="Summary of Rider Adjustments",
                amount=6.44,
                is_subperiod_detail=True,
            ),
            ActualBillLineItem(
                label="Summary of Rider Adjustments",
                amount=8.48,
                is_subperiod_detail=True,
            ),
            ActualBillLineItem(
                label="Clean Energy Rider",
                amount=0.68,
                is_subperiod_detail=True,
            ),
            ActualBillLineItem(
                label="Clean Energy Rider",
                amount=1.00,
                is_subperiod_detail=True,
            ),
        ]
    )

    assert totals["summary_rider_adjustments"]["amount"] == 24.61
    assert totals["clean_energy_rider"]["amount"] == 1.68
