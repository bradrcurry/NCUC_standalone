from __future__ import annotations

import json

from duke_rates.models.document import StoredDocument
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.openei_export import OpenEIChargeCandidate, OpenEIExportCandidate
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.models.rate_schedule import RateScheduleData

UTILITY_NAME_MAP = {
    "progress": "Duke Energy Progress, LLC",
    "carolinas": "Duke Energy Carolinas, LLC",
    "florida": "Duke Energy Florida, LLC",
    "indiana": "Duke Energy Indiana, LLC",
    "kentucky": "Duke Energy Kentucky, Inc.",
    "ohio": "Duke Energy Ohio, Inc.",
}
SECTOR_MAP = {
    "residential": "Residential",
    "commercial": "Commercial",
    "industrial": "Industrial",
    "lighting": "Lighting",
}


def build_openei_export_candidate(
    *,
    parse_result: DocumentParseResult,
    source_document: StoredDocument | None = None,
    historical_document: HistoricalDocumentRecord | None = None,
    openei_reference=None,
) -> OpenEIExportCandidate:
    if not parse_result.schedule:
        raise ValueError("OpenEI export candidates currently require a parsed schedule.")
    schedule = parse_result.schedule
    source_kind = "historical" if historical_document else "current"
    source = historical_document or source_document
    if source is None:
        raise ValueError("Provide source_document or historical_document.")

    metadata = _safe_json_load(getattr(source, "metadata_json", None))
    source_parent_url = None
    if source_document:
        source_parent_url = source_document.source_page_url
    elif historical_document:
        source_parent_url = (
            metadata.get("page_url")
            or metadata.get("api_url")
            or metadata.get("current_document_url")
            or metadata.get("source_url")
        )

    sector = _sector_from_schedule(schedule)
    candidate = OpenEIExportCandidate(
        source_kind=source_kind,
        document_id=source.id or source_document.id,  # type: ignore[union-attr]
        title=source.title,
        utility=_utility_name(schedule.company),
        rate_name=schedule.schedule_title,
        schedule_code=schedule.schedule_code,
        sector=sector,
        source_url=_source_url(source_document, historical_document),
        source_parent_url=source_parent_url,
        effective_start=schedule.effective_start.isoformat() if schedule.effective_start else None,
        effective_end=schedule.effective_end.isoformat() if schedule.effective_end else None,
        fixed_charges=[
            OpenEIChargeCandidate(label=charge.label, rate=charge.amount, unit=charge.unit)
            for charge in schedule.fixed_charges
        ],
        energy_charges=[
            OpenEIChargeCandidate(
                label=charge.label,
                rate=charge.rate,
                unit=charge.unit,
                block_from=charge.block_from,
                block_to=charge.block_to,
                period=charge.period,
                season=charge.season,
            )
            for charge in schedule.energy_charges
        ],
        demand_charges=[
            OpenEIChargeCandidate(label=charge.label, rate=charge.rate, unit=charge.unit)
            for charge in schedule.demand_charges
        ],
        rider_codes=sorted({rider.code for rider in schedule.riders if rider.code}),
        tou_detected=bool(
            schedule.tou_periods
            or sum(1 for charge in schedule.energy_charges if charge.period) > 1
        ),
        missing_fields=_missing_fields(schedule),
        notes=_notes(schedule, source_kind),
        submission_guidance=_submission_guidance(schedule, source_kind),
    )

    if openei_reference:
        candidate.openei_label = openei_reference.label
        candidate.openei_uri = openei_reference.uri
        candidate.openei_source_url = openei_reference.source_url
        candidate.openei_start_date = openei_reference.start_date
        candidate.openei_end_date = openei_reference.end_date
        candidate.approved = openei_reference.approved
    return candidate


def _utility_name(company: str | None) -> str | None:
    if not company:
        return None
    return UTILITY_NAME_MAP.get(company.lower(), company)


def _sector_from_schedule(schedule: RateScheduleData) -> str | None:
    if schedule.customer_class:
        key = schedule.customer_class.strip().lower()
        return SECTOR_MAP.get(key, schedule.customer_class.title())
    title = schedule.schedule_title.lower()
    if "residential" in title:
        return "Residential"
    if "lighting" in title:
        return "Lighting"
    if "industrial" in title:
        return "Industrial"
    if "general service" in title or "commercial" in title:
        return "Commercial"
    return None


def _source_url(
    source_document: StoredDocument | None,
    historical_document: HistoricalDocumentRecord | None,
) -> str | None:
    if historical_document:
        return historical_document.canonical_url
    if source_document:
        return source_document.document_url
    return None


def _missing_fields(schedule: RateScheduleData) -> list[str]:
    missing: list[str] = []
    if not schedule.state:
        missing.append("state")
    if not schedule.company:
        missing.append("company")
    if not schedule.schedule_code:
        missing.append("schedule_code")
    if not schedule.effective_start:
        missing.append("effective_start")
    if not schedule.customer_class:
        missing.append("customer_class")
    if not schedule.energy_charges and not schedule.demand_charges:
        missing.append("charge_components")
    return missing


def _notes(schedule: RateScheduleData, source_kind: str) -> list[str]:
    notes = [
        (
            "Candidate export only. OpenEI/USURDB should be treated as a reference sink, "
            "not an authoritative source."
        )
    ]
    if schedule.riders:
        notes.append(
            "Riders are preserved as references only; formula upload mapping "
            "is not implemented."
        )
    if source_kind == "historical":
        notes.append(
            "Historical candidate was derived from archived or imported "
            "historical evidence."
        )
    if schedule.tou_periods:
        notes.append(
            "TOU periods were parsed and should be reviewed before any "
            "manual USURDB entry."
        )
    return notes


def _submission_guidance(schedule: RateScheduleData, source_kind: str) -> list[str]:
    guidance = [
        (
            "Verify the source URL and effective dates against the official "
            "document before submitting to OpenEI."
        ),
        (
            "Preserve the original Duke or regulator source URL in the OpenEI "
            "source/sourceparent fields."
        ),
        (
            "Review block, TOU, and rider structures manually; this export is "
            "a curation aid, not a direct upload payload."
        ),
    ]
    if source_kind == "historical":
        guidance.append(
            "If the source is an archive or regulator filing, note that "
            "provenance clearly in the OpenEI record."
        )
    if not schedule.schedule_code:
        guidance.append("Add a clear rate name/schedule code during manual curation.")
    return guidance


def _safe_json_load(payload: str | None) -> dict:
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
