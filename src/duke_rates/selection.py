from __future__ import annotations

from duke_rates.billing.calculators import UsageInput
from duke_rates.models.document import StoredDocument
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.parse.heuristics import looks_like_summary_rate_matrix


def is_estimatable_schedule(result: DocumentParseResult) -> bool:
    schedule = result.schedule
    return bool(schedule and (schedule.energy_charges or schedule.demand_charges))


def supports_usage_input(result: DocumentParseResult, usage: UsageInput) -> bool:
    schedule = result.schedule
    if not schedule:
        return False
    if _requires_interval_usage(schedule):
        return bool(usage.interval_data)
    if usage.monthly_kwh > 0 and schedule.energy_charges:
        return True
    if usage.peak_kw is not None and schedule.demand_charges:
        return True
    if usage.interval_data and schedule.energy_charges:
        return True
    return False


def canonical_tariff_key(result: DocumentParseResult) -> str | None:
    schedule = result.schedule
    if not schedule:
        return None
    if schedule.state and schedule.company and schedule.schedule_code:
        return f"{schedule.state}:{schedule.company}:{schedule.schedule_code}".lower()
    return schedule.tariff_id


def estimation_score(document: StoredDocument, result: DocumentParseResult) -> tuple[int, ...]:
    schedule = result.schedule
    if not schedule:
        return (-1,)

    is_summary = _is_summary_document(document, result)
    extra_fixed = max(0, len(schedule.fixed_charges) - 1)
    extra_demand = max(0, len(schedule.demand_charges) - 1)
    extra_energy = max(0, len(schedule.energy_charges) - 2)

    return (
        0 if is_summary else 1,
        1 if result.status == ParseStatus.PARSED else 0,
        1 if schedule.effective_start else 0,
        1 if schedule.customer_class else 0,
        -len(result.review_flags),
        -(extra_fixed + extra_demand + extra_energy),
        len(schedule.fixed_charges) + len(schedule.energy_charges) + len(schedule.demand_charges),
        -document.id,
    )


def _is_summary_document(document: StoredDocument, result: DocumentParseResult) -> bool:
    if "Summary/matrix rate document detected" in result.review_flags:
        return True
    if document.category == "index":
        return True
    schedule = result.schedule
    if not schedule:
        return False
    summary_text = schedule.raw_summary or ""
    return looks_like_summary_rate_matrix(document.title, summary_text)


def _requires_interval_usage(schedule) -> bool:
    periods = {charge.period for charge in schedule.energy_charges if charge.period}
    explicit_tou = any(
        period.weekday_hours or period.weekend_hours or period.months
        for period in schedule.tou_periods
    )
    return len(periods) > 1 or explicit_tou
