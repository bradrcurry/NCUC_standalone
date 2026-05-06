from __future__ import annotations

from pathlib import Path

from duke_rates.models.parse_result import (
    DocumentParseResult,
    ParsedField,
    ParseStatus,
    SourceSnippet,
)
from duke_rates.models.rate_schedule import (
    DemandCharge,
    EnergyCharge,
    FixedCharge,
    RateScheduleData,
    TariffReference,
    TOUPeriod,
)
from duke_rates.parse.heuristics import (
    ChargeMatch,
    ELIGIBILITY_RE,
    FIXED_CHARGE_RE,
    extract_demand_charge_matches,
    extract_effective_date,
    extract_energy_charge_matches,
    extract_first,
    extract_matches,
    extract_rider_references,
    extract_schedule_code,
    extract_schedule_title,
    extract_tou_periods,
    has_tou,
    likely_customer_class,
    looks_like_summary_rate_matrix,
    summarize_text,
)
from duke_rates.parse.normalization import (
    build_tariff_id,
    normalize_company,
    parse_effective_date,
)


def parse_schedule_text(
    *,
    document_id: int,
    title: str,
    state: str | None,
    company: str | None,
    text: str,
    raw_text_path: Path | None = None,
) -> DocumentParseResult:
    schedule_code = extract_schedule_code(title, text)
    effective_date = extract_effective_date(text)
    eligibility = extract_first(ELIGIBILITY_RE, text)
    schedule_title = extract_schedule_title(title, text)

    fixed_matches = _filter_fixed_matches(extract_matches(FIXED_CHARGE_RE, text, label="customer charge"))
    energy_matches = extract_energy_charge_matches(text)
    demand_matches = extract_demand_charge_matches(text)
    rider_references = extract_rider_references(text)
    tou_matches = extract_tou_periods(text)

    normalized_company = normalize_company(
        title,
        text,
        fallback=company,
        state=state,
    )

    fixed_charges = [
        FixedCharge(label=match.label, amount=match.rate) for match in fixed_matches[:3]
    ]
    energy_charges = [
        EnergyCharge(
            label=match.label,
            rate=_normalize_energy_rate(match.rate),
            period=match.period or _infer_period_from_label(match.label),
            season=match.season,
            block_from=match.block_from,
            block_to=match.block_to,
        )
        for match in energy_matches[:6]
    ]
    demand_charges = [
        DemandCharge(label=match.label, rate=match.rate) for match in demand_matches[:4]
    ]
    schedule = RateScheduleData(
        tariff_id=build_tariff_id(state, normalized_company, schedule_code, schedule_title),
        state=state,
        company=normalized_company,
        schedule_code=schedule_code,
        schedule_title=schedule_title,
        customer_class=likely_customer_class(text),
        effective_start=parse_effective_date(effective_date),
        fixed_charges=fixed_charges,
        energy_charges=energy_charges,
        demand_charges=demand_charges,
        tou_periods=[
            TOUPeriod(
                name=str(match["name"]),
                months=[str(value) for value in match["months"]],
                weekday_hours=str(match["weekday_hours"]) if match["weekday_hours"] else None,
                weekend_hours=str(match["weekend_hours"]) if match["weekend_hours"] else None,
            )
            for match in tou_matches
        ]
        or _fallback_tou_periods(energy_charges, text),
        riders=[
            TariffReference(code=reference.code, title=reference.title, role="rider")
            for reference in rider_references
        ],
        eligibility=eligibility,
        raw_summary=summarize_text(text),
    )

    extracted_fields = []
    if schedule_code:
        extracted_fields.append(
            ParsedField(
                name="schedule_code",
                value=schedule_code,
                confidence=0.6,
                source_snippet=SourceSnippet(label="schedule_code", text=schedule_code),
            )
        )
    if effective_date:
        extracted_fields.append(
            ParsedField(
                name="effective_date",
                value=effective_date,
                confidence=0.7,
                source_snippet=SourceSnippet(label="effective_date", text=effective_date),
            )
        )

    review_flags: list[str] = []
    if not schedule.energy_charges:
        review_flags.append("No energy charge extracted")
    if not schedule.fixed_charges:
        review_flags.append("No fixed charge extracted")
    if not schedule.schedule_code:
        review_flags.append("No schedule code extracted")
    if looks_like_summary_rate_matrix(title, text):
        review_flags.append("Summary/matrix rate document detected")

    status = ParseStatus.PARSED if len(review_flags) <= 1 else ParseStatus.PARTIAL
    return DocumentParseResult(
        document_id=document_id,
        status=status,
        parser_name="heuristic_schedule_parser",
        raw_text_path=str(raw_text_path) if raw_text_path else None,
        extracted_fields=extracted_fields,
        review_flags=review_flags,
        schedule=schedule,
    )


def _normalize_energy_rate(rate: float) -> float:
    if 1.0 < rate <= 100.0:
        return round(rate / 100.0, 6)
    return rate


def _infer_period_from_label(label: str) -> str | None:
    lowered = label.lower()
    if "critical peak" in lowered:
        return "Critical Peak"
    if "super off peak" in lowered:
        return "Super Off-Peak"
    if "off peak" in lowered:
        return "Off-Peak"
    if "on peak" in lowered:
        return "On-Peak"
    if "discount" in lowered:
        return "Discount"
    return None


def _fallback_tou_periods(energy_charges: list[EnergyCharge], text: str) -> list[TOUPeriod]:
    if any(charge.period for charge in energy_charges):
        seen: list[str] = []
        for charge in energy_charges:
            if charge.period and charge.period not in seen:
                seen.append(charge.period)
        return [TOUPeriod(name=period) for period in seen]
    if has_tou(text):
        return [TOUPeriod(name="TOU detected")]
    return []


def _filter_fixed_matches(matches: list[ChargeMatch]) -> list[ChargeMatch]:
    if not matches:
        return matches
    by_label: dict[str, list[ChargeMatch]] = {}
    for match in matches:
        by_label.setdefault(match.label.lower(), []).append(match)

    filtered = []
    for label_matches in by_label.values():
        substantial = [match for match in label_matches if match.rate >= 5.0]
        chosen = substantial or label_matches
        chosen.sort(key=lambda match: match.rate, reverse=True)
        filtered.append(chosen[0])
    return filtered
