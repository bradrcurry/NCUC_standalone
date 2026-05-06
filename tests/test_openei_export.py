from datetime import UTC, date, datetime
from pathlib import Path

from duke_rates.external.openei import OpenEIRateReference
from duke_rates.external.openei_export import build_openei_export_candidate
from duke_rates.models.document import StoredDocument
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import (
    EnergyCharge,
    FixedCharge,
    RateScheduleData,
    TariffReference,
)


def test_build_openei_export_candidate_for_current_schedule() -> None:
    source_document = StoredDocument(
        id=110,
        title="Residential Service Schedule RES",
        source_page_url="https://www.duke-energy.com/home/billing/rates",
        document_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=current",
        state="NC",
        company="progress",
        category="rate",
        kind="pdf",
        local_path=Path("leaf-no-500-schedule-res.pdf"),
        content_hash="abc",
        retrieved_at=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
        discovered_at=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
    )
    parse_result = DocumentParseResult(
        document_id=110,
        parser_name="schedule_parser",
        status=ParseStatus.PARSED,
        schedule=RateScheduleData(
            tariff_id="nc_progress_res",
            state="NC",
            company="progress",
            schedule_code="RES",
            schedule_title="Residential Service Schedule RES",
            customer_class="residential",
            effective_start=date(2025, 10, 1),
            fixed_charges=[FixedCharge(label="Basic Customer Charge", amount=14.0)],
            energy_charges=[EnergyCharge(label="Energy Charge", rate=0.12623)],
            riders=[TariffReference(code="BA", title="Rider BA", role="rider")],
        ),
    )

    candidate = build_openei_export_candidate(
        parse_result=parse_result,
        source_document=source_document,
    )

    assert candidate.utility == "Duke Energy Progress, LLC"
    assert candidate.rate_name == "Residential Service Schedule RES"
    assert candidate.schedule_code == "RES"
    assert candidate.sector == "Residential"
    assert candidate.source_parent_url == "https://www.duke-energy.com/home/billing/rates"
    assert candidate.rider_codes == ["BA"]
    assert candidate.missing_fields == []


def test_build_openei_export_candidate_for_historical_schedule_with_reference() -> None:
    historical_document = HistoricalDocumentRecord(
        id=7,
        current_document_id=110,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
        title="Residential Service Schedule RES",
        state="NC",
        company="progress",
        category="rate",
        kind="pdf",
        canonical_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older",
        archived_url="https://web.archive.org/web/20241118190307/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older",
        snapshot_timestamp=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
        local_path=Path("historical-res.pdf"),
        raw_text_path=Path("historical-res.pdf.txt"),
        content_hash="older",
        content_type="application/pdf",
        direct_status_code=403,
        direct_downloadable=False,
        revision_label="NC First Revised Leaf No. 500",
        supersedes_label="NC Original Leaf No. 500",
        leaf_no="500",
        effective_start="October 1, 2024",
        effective_end="September 30, 2025",
        retrieved_at=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
        metadata_json=(
            '{"source":"wayback","current_document_url":"https://www.duke-energy.com/home/billing/rates"}'
        ),
    )
    parse_result = DocumentParseResult(
        document_id=7,
        parser_name="schedule_parser",
        status=ParseStatus.PARSED,
        schedule=RateScheduleData(
            tariff_id="nc_progress_res_2024",
            state="NC",
            company="progress",
            schedule_code="RES",
            schedule_title="Residential Service Schedule RES",
            effective_start=date(2024, 10, 1),
            energy_charges=[EnergyCharge(label="Energy Charge", rate=0.12119)],
        ),
    )
    openei_reference = OpenEIRateReference(
        label="678abac33d12e18b730b0663",
        name="RST-1",
        utility="Progress Energy Florida Inc",
        uri="https://apps.openei.org/IURDB/rate/view/678abac33d12e18b730b0663",
        source_url="https://www.duke-energy.com/rate.pdf",
        start_date="2025-01-01",
        approved=True,
    )

    candidate = build_openei_export_candidate(
        parse_result=parse_result,
        historical_document=historical_document,
        openei_reference=openei_reference,
    )

    assert candidate.source_kind == "historical"
    assert candidate.source_url == historical_document.canonical_url
    assert candidate.openei_label == "678abac33d12e18b730b0663"
    assert "customer_class" in candidate.missing_fields
    assert candidate.submission_guidance
