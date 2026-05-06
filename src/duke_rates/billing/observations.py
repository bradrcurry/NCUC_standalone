from __future__ import annotations

import re
from datetime import date

from duke_rates.models.bill import BillLineItem, BillSection, BillStatementData
from duke_rates.models.bill_observation import BillComponentObservation


def derive_bill_component_observations(
    *,
    bill_id: int,
    statement: BillStatementData,
) -> list[BillComponentObservation]:
    observations: list[BillComponentObservation] = []
    for section in (statement.electric_section, statement.lighting_section, statement.tax_section):
        if section is None:
            continue
        observations.extend(
            _derive_section_observations(
                bill_id=bill_id,
                source_path=statement.source_path,
                statement=statement,
                section=section,
            )
        )
    return observations


def _derive_section_observations(
    *,
    bill_id: int,
    source_path: str,
    statement: BillStatementData,
    section: BillSection,
) -> list[BillComponentObservation]:
    billed_kwh = _derive_billed_kwh(statement) if section.name == "Electric" else None
    total_days = _days_between(section.billing_period_start, section.billing_period_end)
    energy_charge_amount = sum(
        item.amount or 0.0
        for item in section.line_items
        if _canonical_label(item.label) == "energy_charge" and not item.is_subperiod_detail
    )

    observations: list[BillComponentObservation] = []
    for item in section.line_items:
        observation = _build_observation(
            bill_id=bill_id,
            source_path=source_path,
            section=section,
            item=item,
            billed_kwh=billed_kwh,
            total_days=total_days,
            energy_charge_amount=energy_charge_amount,
        )
        if observation is not None:
            observations.append(observation)
    return observations


def _build_observation(
    *,
    bill_id: int,
    source_path: str,
    section: BillSection,
    item: BillLineItem,
    billed_kwh: float | None,
    total_days: int | None,
    energy_charge_amount: float,
) -> BillComponentObservation | None:
    if item.amount is None:
        return None

    component_key = _canonical_label(item.label)
    days_in_period = _line_item_days(item, section)
    quantity_basis_kwh: float | None = None
    inferred_unit: str | None = None
    inferred_value: float | None = None
    confidence = 0.5
    notes: list[str] = []

    if item.quantity is not None and item.rate is not None and item.unit == "kWh":
        quantity_basis_kwh = item.quantity
        inferred_unit = "dollars_per_kwh"
        inferred_value = item.rate
        confidence = 0.99
    elif component_key in {"storm_recovery_charge", "summary_rider_adjustments"} and billed_kwh:
        quantity_basis_kwh = item.quantity if item.quantity is not None else _allocate_kwh(
            billed_kwh=billed_kwh,
            total_days=total_days,
            item=item,
            section=section,
        )
        if quantity_basis_kwh:
            inferred_unit = "cents_per_kwh"
            inferred_value = round(item.amount * 100.0 / quantity_basis_kwh, 3)
            confidence = 0.6 if item.is_subperiod_detail else 0.85
            if item.is_subperiod_detail:
                notes.append("Derived using day-based kWh allocation across the billing period.")
    elif component_key == "energy_conservation_credit" and energy_charge_amount:
        inferred_unit = "percent_of_energy_charges"
        inferred_value = round(abs(item.amount) * 100.0 / energy_charge_amount, 3)
        confidence = 0.95
    elif component_key in {"clean_energy_rider", "customer_charge"}:
        inferred_unit = "fixed_monthly"
        if item.is_subperiod_detail and days_in_period and total_days:
            inferred_value = round(item.amount * total_days / days_in_period, 2)
            confidence = 0.75
            notes.append("Derived monthly equivalent from subperiod amount using day proration.")
        else:
            inferred_value = round(item.amount, 2)
            confidence = 0.95
    elif component_key == "sales_tax" and energy_charge_amount:
        inferred_unit = "percent_of_taxable_amount"
        inferred_value = round(item.amount * 100.0 / energy_charge_amount, 3)
        confidence = 0.5

    return BillComponentObservation(
        bill_id=bill_id,
        source_path=source_path,
        section_name=section.name,
        rate_code=section.rate_code,
        component_key=component_key,
        component_label=item.label,
        amount=round(item.amount, 2),
        service_start=section.billing_period_start,
        service_end=section.billing_period_end,
        period_start=item.period_start,
        period_end=item.period_end,
        days_in_period=days_in_period,
        quantity_basis_kwh=quantity_basis_kwh,
        inferred_unit=inferred_unit,
        inferred_value=inferred_value,
        confidence=confidence,
        notes=notes,
    )


def _allocate_kwh(
    *,
    billed_kwh: float,
    total_days: int | None,
    item: BillLineItem,
    section: BillSection,
) -> float:
    if not item.is_subperiod_detail:
        return round(billed_kwh, 3)
    days_in_period = _line_item_days(item, section)
    if not days_in_period or not total_days:
        return round(billed_kwh, 3)
    return round(billed_kwh * days_in_period / total_days, 3)


def _line_item_days(item: BillLineItem, section: BillSection) -> int | None:
    start = item.period_start or section.billing_period_start
    end = item.period_end or section.billing_period_end
    return _days_between(start, end)


def _days_between(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days + 1


def _derive_billed_kwh(statement: BillStatementData) -> float:
    electric = statement.electric_section
    if not electric:
        return 0.0
    quantities = [
        item.quantity or 0.0
        for item in electric.line_items
        if _canonical_label(item.label) == "energy_charge" and item.unit == "kWh"
    ]
    return round(sum(quantities), 3)


def _canonical_label(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    if "customer charge" in normalized:
        return "customer_charge"
    if "energy charge" in normalized or "kilowatt hour charge" in normalized:
        return "energy_charge"
    if "clean energy rider" in normalized:
        return "clean_energy_rider"
    if "energy conservation credit" in normalized:
        return "energy_conservation_credit"
    if "storm recovery charge" in normalized:
        return "storm_recovery_charge"
    if "summary of rider adjustments" in normalized:
        return "summary_rider_adjustments"
    if "annual billing adjustments" in normalized:
        return "annual_billing_adjustments"
    if "sales tax" in normalized:
        return "sales_tax"
    return normalized.replace(" ", "_")
