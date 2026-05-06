from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from duke_rates.billing.calculators import BillLineItem, IntervalUsagePoint, UsageInput
from duke_rates.billing.holidays import is_duke_holiday
from duke_rates.models.rate_schedule import EnergyCharge, TOUPeriod

DUKE_LOCAL_TZ = ZoneInfo("America/New_York")
MONTH_INDEX = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def summarize_interval_usage(usage: UsageInput) -> dict:
    return {
        "interval_points": len(usage.interval_data),
        "supported": bool(usage.interval_data),
        "note": (
            f"Used {len(usage.interval_data)} interval points for TOU allocation."
            if usage.interval_data
            else "No interval data provided."
        ),
    }


def calculate_tou_energy_charge(
    charges: list[EnergyCharge],
    tou_periods: list[TOUPeriod],
    usage: UsageInput,
) -> list[BillLineItem]:
    if not usage.interval_data or not charges:
        return []

    charge_by_period = {
        charge.period.lower(): charge
        for charge in charges
        if charge.period and charge.rate is not None
    }
    if not charge_by_period:
        return []

    kwh_by_period: dict[str, float] = {period: 0.0 for period in charge_by_period}
    unmatched_kwh = 0.0
    for point in usage.interval_data:
        period = classify_interval_point(point, tou_periods)
        if not period:
            unmatched_kwh += point.kwh
            continue
        charge = charge_by_period.get(period.lower())
        if not charge:
            unmatched_kwh += point.kwh
            continue
        kwh_by_period[period.lower()] += point.kwh

    line_items: list[BillLineItem] = []
    for period_name, kwh in kwh_by_period.items():
        if kwh <= 0:
            continue
        charge = charge_by_period[period_name]
        rate = charge.rate or 0.0
        line_items.append(
            BillLineItem(
                label=charge.label,
                amount=round(kwh * rate, 2),
                details=f"{round(kwh, 4)} kWh @ {rate}",
            )
        )

    if unmatched_kwh > 0:
        line_items.append(
            BillLineItem(
                label="Unallocated TOU usage",
                amount=0.0,
                details=f"{round(unmatched_kwh, 4)} kWh did not match parsed TOU periods",
            )
        )
    return line_items


def classify_interval_point(point: IntervalUsagePoint, tou_periods: list[TOUPeriod]) -> str | None:
    explicit_periods = [
        period for period in tou_periods if period.weekday_hours or period.weekend_hours
    ]
    fallback_periods = [
        period for period in tou_periods if not period.weekday_hours and not period.weekend_hours
    ]

    for period in explicit_periods:
        if _interval_matches_period(point.timestamp, period):
            return period.name

    for period in fallback_periods:
        if period.name.lower() == "off-peak":
            return period.name
    return fallback_periods[0].name if fallback_periods else None


def _interval_matches_period(timestamp: datetime, period: TOUPeriod) -> bool:
    local_timestamp = timestamp.astimezone(DUKE_LOCAL_TZ) if timestamp.tzinfo else timestamp
    if period.months and not any(
        _month_descriptor_matches(local_timestamp.month, value) for value in period.months
    ):
        return False

    is_weekend = local_timestamp.weekday() >= 5 or is_duke_holiday(local_timestamp.date())
    if is_weekend:
        if not period.weekend_hours:
            # Period has no weekend/holiday schedule → does not apply
            return False
        hours = period.weekend_hours
    else:
        hours = period.weekday_hours
    if not hours:
        return False
    time_ranges = _parse_time_ranges(hours)
    if not time_ranges:
        return False
    current = local_timestamp.time()
    return any(start <= current < end for start, end in time_ranges)


def _month_descriptor_matches(month: int, descriptor: str) -> bool:
    lowered = descriptor.lower()
    if "all calendar months" in lowered:
        return True
    if "through" not in lowered:
        return False
    parts = lowered.split("through", maxsplit=1)
    start_name = parts[0].split()[-1]
    end_name = parts[1].strip().split()[0]
    start_month = MONTH_INDEX.get(start_name)
    end_month = MONTH_INDEX.get(end_name)
    if not start_month or not end_month:
        return False
    if start_month <= end_month:
        return start_month <= month <= end_month
    return month >= start_month or month <= end_month


def _parse_time_ranges(value: str) -> list[tuple[time, time]]:
    ranges: list[tuple[time, time]] = []
    normalized = (
        value.replace("(midnight)", "")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace(";", "|")
    )
    for chunk in normalized.split("|"):
        compact = chunk.replace(" ", "")
        if "to" in compact.lower():
            start_raw, end_raw = compact.split("to", maxsplit=1)
        elif "-" in compact:
            start_raw, end_raw = compact.split("-", maxsplit=1)
        else:
            continue
        start = _parse_time(start_raw)
        end = _parse_time(end_raw)
        if start and end:
            ranges.append((start, end))
    return ranges


def _parse_time(value: str) -> time | None:
    candidate = value.lower().replace(".", "")
    for fmt in ("%I:%M%p",):
        try:
            return datetime.strptime(candidate, fmt).time()
        except ValueError:
            continue
    return None
