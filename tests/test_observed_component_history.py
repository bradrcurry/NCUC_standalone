from datetime import date

from duke_rates.historical.observed_components import (
    ProgressNCObservedComponentHistoryService,
)
from duke_rates.models.bill_observation import BillComponentObservation


def test_observed_component_history_prefers_split_periods_and_normalizes_units():
    service = ProgressNCObservedComponentHistoryService(
        [
            BillComponentObservation(
                bill_id=9,
                source_path="bill9.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="clean_energy_rider",
                component_label="Clean Energy Rider",
                amount=1.68,
                service_start=date(2025, 11, 18),
                service_end=date(2025, 12, 16),
                inferred_unit="fixed_monthly",
                inferred_value=1.68,
                confidence=0.8,
            ),
            BillComponentObservation(
                bill_id=9,
                source_path="bill9.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="clean_energy_rider",
                component_label="Clean Energy Rider",
                amount=0.68,
                service_start=date(2025, 11, 18),
                service_end=date(2025, 12, 16),
                period_start=date(2025, 11, 18),
                period_end=date(2025, 11, 30),
                inferred_unit="fixed_monthly",
                inferred_value=1.52,
                confidence=0.75,
            ),
            BillComponentObservation(
                bill_id=9,
                source_path="bill9.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="clean_energy_rider",
                component_label="Clean Energy Rider",
                amount=1.00,
                service_start=date(2025, 11, 18),
                service_end=date(2025, 12, 16),
                period_start=date(2025, 12, 1),
                period_end=date(2025, 12, 16),
                inferred_unit="fixed_monthly",
                inferred_value=1.81,
                confidence=0.75,
            ),
            BillComponentObservation(
                bill_id=10,
                source_path="bill10.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="storm_recovery_charge",
                component_label="Storm Recovery Charge",
                amount=2.45,
                service_start=date(2025, 10, 18),
                service_end=date(2025, 11, 17),
                inferred_unit="dollars_per_kwh",
                inferred_value=0.00301,
                confidence=0.8,
            ),
        ]
    )

    clean_energy = service.build_series(component_key="clean_energy_rider", rate_code="RES")
    assert len(clean_energy) == 2
    assert clean_energy[0].normalized_value == 1.52
    assert clean_energy[0].start_date == date(2025, 11, 18)
    assert clean_energy[0].end_date == date(2025, 11, 30)
    assert clean_energy[1].normalized_value == 1.81
    assert clean_energy[1].start_date == date(2025, 12, 1)
    assert clean_energy[1].end_date == date(2025, 12, 16)

    storm = service.build_series(component_key="storm_recovery_charge", rate_code="RES")
    assert len(storm) == 1
    assert storm[0].normalized_unit == "cents_per_kwh"
    assert storm[0].normalized_value == 0.301


def test_select_entry_prefers_covering_or_nearest_recent_history():
    service = ProgressNCObservedComponentHistoryService(
        [
            BillComponentObservation(
                bill_id=8,
                source_path="bill8.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="storm_recovery_charge",
                component_label="Storm Recovery Charge",
                amount=2.45,
                service_start=date(2025, 10, 18),
                service_end=date(2025, 11, 17),
                inferred_unit="cents_per_kwh",
                inferred_value=0.301,
                confidence=0.8,
            ),
            BillComponentObservation(
                bill_id=10,
                source_path="bill10.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="storm_recovery_charge",
                component_label="Storm Recovery Charge",
                amount=4.93,
                service_start=date(2025, 12, 17),
                service_end=date(2026, 1, 17),
                inferred_unit="cents_per_kwh",
                inferred_value=0.379,
                confidence=0.8,
            ),
        ]
    )

    selected = service.select_entry(
        component_key="storm_recovery_charge",
        rate_code="RES",
        target_start=date(2025, 11, 18),
        target_end=date(2025, 12, 16),
        exclude_bill_id=9,
    )

    assert selected is not None
    assert selected.normalized_value == 0.379


def test_select_entry_can_pick_nearest_recent_history():
    service = ProgressNCObservedComponentHistoryService(
        [
            BillComponentObservation(
                bill_id=10,
                source_path="bill10.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="summary_rider_adjustments",
                component_label="Summary of Rider Adjustments",
                amount=15.99,
                service_start=date(2026, 1, 1),
                service_end=date(2026, 1, 17),
                inferred_unit="cents_per_kwh",
                inferred_value=0.867,
                confidence=0.8,
            )
        ]
    )

    selected = service.select_entry(
        component_key="summary_rider_adjustments",
        rate_code="RES",
        target_start=date(2025, 12, 1),
        target_end=date(2025, 12, 16),
        exclude_bill_id=9,
    )

    assert selected is not None
    assert selected.normalized_value == 0.867
