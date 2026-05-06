from __future__ import annotations

from pathlib import Path

from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rider import RiderAdjustmentRow, RiderChargeComponent, RiderData
from duke_rates.parse.heuristics import (
    extract_applicable_schedule_codes,
    extract_effective_date,
    extract_rider_adjustment_rows,
    extract_rider_applicability,
    extract_rider_charge_components,
    extract_rider_code,
    extract_rider_title,
    extract_rider_version,
    summarize_text,
)
from duke_rates.parse.normalization import build_tariff_id, normalize_company


def parse_rider_text(
    *,
    document_id: int,
    title: str,
    state: str | None,
    company: str | None,
    text: str,
    raw_text_path: Path | None = None,
) -> DocumentParseResult:
    effective_date = extract_effective_date(text)
    applicability = extract_rider_applicability(text)
    normalized_company = normalize_company(title, text, fallback=company, state=state)
    rider_code = extract_rider_code(title, text)
    rider_title = extract_rider_title(title, text)
    if _prefer_supplied_title(supplied_title=title, extracted_title=rider_title):
        rider_title = title
    rider_version = extract_rider_version(text)
    applicable_schedules = extract_applicable_schedule_codes(text)
    adjustment_rows = extract_rider_adjustment_rows(text)
    charge_components = extract_rider_charge_components(text, rider_code=rider_code)
    if not applicable_schedules:
        derived_schedules: list[str] = []
        for row in adjustment_rows:
            derived_schedules.extend(row.applicable_schedules)
        for component in charge_components:
            derived_schedules.extend(component.applicable_schedules or [])
        applicable_schedules = list(dict.fromkeys(code for code in derived_schedules if code))
    rider = RiderData(
        rider_id=build_tariff_id(state, normalized_company, rider_code, rider_title),
        state=state,
        company=normalized_company,
        code=rider_code,
        version_code=rider_version,
        title=rider_title,
        effective_date=effective_date,
        applicability=applicability,
        charge_description=summarize_text(text),
        formula_based=("formula" in text.lower()) or bool(adjustment_rows),
        applicable_schedules=applicable_schedules,
        adjustment_rows=[
            RiderAdjustmentRow(
                rate_class=row.rate_class,
                fuel_adjustment_cents_per_kwh=row.fuel_adjustment_cents_per_kwh,
                fuel_emf_cents_per_kwh=row.fuel_emf_cents_per_kwh,
                dsm_ee_adjustment_cents_per_kwh=row.dsm_ee_adjustment_cents_per_kwh,
                dsm_ee_emf_cents_per_kwh=row.dsm_ee_emf_cents_per_kwh,
                net_adjustment_cents_per_kwh=row.net_adjustment_cents_per_kwh,
                applicable_schedules=row.applicable_schedules,
            )
            for row in adjustment_rows
        ],
        charge_components=[
            RiderChargeComponent(
                bill_label=component.bill_label,
                rate_class=component.rate_class,
                value=component.value,
                unit=component.unit,
                applicable_schedules=component.applicable_schedules or [],
            )
            for component in charge_components
        ],
        references=applicable_schedules,
    )
    review_flags: list[str] = []
    if not rider.code:
        review_flags.append("No rider code extracted")
    if not rider.applicable_schedules:
        review_flags.append("No applicable schedules extracted")
    if rider.code == "BA" and not rider.adjustment_rows and not rider.charge_components:
        review_flags.append("No rider adjustment rows extracted")
    status = ParseStatus.PARSED if not review_flags else ParseStatus.PARTIAL
    return DocumentParseResult(
        document_id=document_id,
        status=status,
        parser_name="heuristic_rider_parser",
        raw_text_path=str(raw_text_path) if raw_text_path else None,
        rider=rider,
        review_flags=review_flags or ["Rider parsing is heuristic and requires review"],
    )


BAD_EXTRACTED_TITLES = {
    "Electronically Submitted",
    "Before The North Carolina Utilities Commission",
}


def _prefer_supplied_title(*, supplied_title: str, extracted_title: str) -> bool:
    supplied = supplied_title.strip()
    extracted = extracted_title.strip()
    if not supplied or not extracted:
        return False
    if extracted in BAD_EXTRACTED_TITLES:
        return True
    if len(extracted) > 80:
        return True
    extracted_upper = extracted.upper()
    if any(
        marker in extracted_upper
        for marker in ("COMMISSION", "PROCEEDING", "REQUIRE THE COMMISSION", "PUBLIC NOTICE")
    ):
        return True
    if "RIDER" in supplied.upper() and "RIDER" not in extracted_upper and len(extracted) > 45:
        return True
    return False
