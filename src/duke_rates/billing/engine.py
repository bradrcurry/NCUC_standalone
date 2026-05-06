from __future__ import annotations

from pydantic import BaseModel, Field

from duke_rates.billing.calculators import (
    BillLineItem,
    UsageInput,
    calculate_demand_charge,
    calculate_energy_charge,
    calculate_fixed_charge,
)
from duke_rates.billing.riders import apply_riders
from duke_rates.billing.season_utils import season_matches
from duke_rates.billing.tou import calculate_tou_energy_charge, summarize_interval_usage
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.models.rate_schedule import EnergyCharge, RateScheduleData


class BillEstimate(BaseModel):
    tariff_id: str
    schedule_title: str
    line_items: list[BillLineItem] = Field(default_factory=list)
    subtotal: float
    total: float
    notes: list[str] = Field(default_factory=list)


class BillingEngine:
    def estimate(
        self,
        schedule: RateScheduleData,
        usage: UsageInput,
        *,
        rider_parse_results: list[DocumentParseResult] | None = None,
    ) -> BillEstimate:
        line_items = []
        line_items.extend(
            calculate_fixed_charge(
                schedule.fixed_charges,
                billing_period_start=usage.billing_period_start,
                billing_period_end=usage.billing_period_end,
            )
        )
        energy_charges = _select_applicable_energy_charges(
            schedule.energy_charges,
            usage.service_date,
        )
        if usage.interval_data and schedule.tou_periods:
            tou_line_items = calculate_tou_energy_charge(
                energy_charges,
                schedule.tou_periods,
                usage,
            )
            if tou_line_items:
                line_items.extend(tou_line_items)
            else:
                line_items.extend(calculate_energy_charge(energy_charges, usage.monthly_kwh))
        else:
            line_items.extend(calculate_energy_charge(energy_charges, usage.monthly_kwh))
        line_items.extend(calculate_demand_charge(schedule.demand_charges, usage.peak_kw))

        subtotal = round(sum(item.amount for item in line_items), 2)
        energy_charge_amount = round(
            sum(
                item.amount
                for item in line_items
                if "charge" in item.label.lower() and "customer" not in item.label.lower()
            ),
            2,
        )
        rider_result = apply_riders(
            subtotal,
            [r.title for r in schedule.riders],
            monthly_kwh=usage.monthly_kwh,
            schedule_code=schedule.schedule_code,
            energy_charge_amount=energy_charge_amount,
            billing_period_start=usage.billing_period_start,
            billing_period_end=usage.billing_period_end,
            rider_parse_results=rider_parse_results,
        )
        interval_summary = summarize_interval_usage(usage)
        line_items.extend(rider_result["line_items"])
        total = round(subtotal + rider_result["adjustment"], 2)

        notes = []
        if schedule.tou_periods and not usage.interval_data:
            notes.append(
                "TOU structure detected; current estimate does not allocate "
                "interval usage by period."
            )
        if len(schedule.fixed_charges) > 1:
            notes.append(
                "Multiple fixed charges were parsed; estimate used the first charge only."
            )
        if any(charge.season for charge in schedule.energy_charges) and not usage.service_date:
            notes.append(
                "Seasonal energy charges were parsed, but no service date was provided."
            )
        if usage.interval_data:
            notes.append(interval_summary["note"])
        if schedule.riders:
            notes.append(rider_result["note"])
        if rider_result.get("used_storm_proration"):
            notes.append(
                "Storm rider mid-period proration uses a linear day-fraction "
                "of monthly kWh (approximation; actual billing uses meter reads)."
            )

        return BillEstimate(
            tariff_id=schedule.tariff_id,
            schedule_title=schedule.schedule_title,
            line_items=line_items,
            subtotal=subtotal,
            total=total,
            notes=notes,
        )


def _select_applicable_energy_charges(
    charges: list[EnergyCharge],
    service_date,
) -> list[EnergyCharge]:
    if not charges:
        return []
    seasonal = [charge for charge in charges if charge.season]
    if not seasonal:
        return charges
    if service_date is None:
        return [charge for charge in charges if not charge.season] or seasonal
    matching = [charge for charge in seasonal if season_matches(charge.season, service_date.month)]
    if matching:
        return matching
    return [charge for charge in charges if not charge.season] or seasonal


# season_matches() is now the shared implementation in billing.season_utils.
