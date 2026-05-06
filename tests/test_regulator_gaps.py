from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.regulator_gaps import ProgressNCRegulatorGapService
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.public_notice import PublicNoticeData
from duke_rates.models.rate_schedule import RateScheduleData, TariffReference
from duke_rates.utils.dates import utc_now


def test_regulator_gap_service_surfaces_archive_backed_chain_with_notice_docket(
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "leaf-no-500-schedule-res.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    current_pdf.with_suffix(".pdf.txt").write_text(
        "\n".join(
            [
                "Residential Service Schedule RES",
                "NC Second Revised Leaf No. 500",
                "Superseding NC First Revised Leaf No. 500",
                "Effective for service rendered on and after October 1, 2025",
            ]
        ),
        encoding="utf-8",
    )
    current_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Schedule RES",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-500-schedule-res.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=current_id,
            parser_name="schedule_parser",
            status=ParseStatus.PARSED,
            schedule=RateScheduleData(
                tariff_id="nc_progress_res_current",
                state="NC",
                company="progress",
                schedule_code="RES",
                schedule_title="Residential Service Schedule RES",
                riders=[TariffReference(code="BA", title="Rider BA", role="rider")],
            ),
        )
    )
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=current_id,
            family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
            title="Residential Service Schedule RES",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older",
            archived_url="https://web.archive.org/web/20241118190307/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older",
            snapshot_timestamp=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
            local_path=tmp_path / "historical-res.pdf",
            raw_text_path=tmp_path / "historical-res.pdf.txt",
            content_hash="older",
            content_type="application/pdf",
            revision_label="NC First Revised Leaf No. 500",
            supersedes_label="NC Original Leaf No. 500",
            leaf_no="500",
            effective_start="October 1, 2024",
            effective_end="September 30, 2025",
            retrieved_at=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
            metadata_json='{"source":"wayback"}',
        )
    )
    notice_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key="/-/media/pdfs/for-your-home/rates/public-notices/e-2-sub-1396-dep-pbr-year-2-public-notice.pdf",
            title="Notice of Application for Rider Rate Adjustments and Public Hearing",
            state="NC",
            company="progress",
            category="public_notice",
            kind="pdf",
            canonical_url="https://www.duke-energy.com/notice.pdf",
            archived_url="https://www.duke-energy.com/notice.pdf",
            snapshot_timestamp=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
            local_path=tmp_path / "notice.pdf",
            raw_text_path=tmp_path / "notice.pdf.txt",
            content_hash="notice",
            content_type="application/pdf",
            retrieved_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
    )
    repo.save_historical_parse_result(
        notice_id,
        DocumentParseResult(
            document_id=notice_id,
            parser_name="notice_parser",
            status=ParseStatus.PARSED,
            notice=PublicNoticeData(
                notice_id="notice-1",
                title="Notice of Application for Rider Rate Adjustments and Public Hearing",
                state="NC",
                company="progress",
                docket_numbers=["E-2, Sub 1396"],
                related_rider_codes=["BA"],
                related_schedule_codes=["RES"],
            ),
        ),
    )

    gaps = ProgressNCRegulatorGapService(repo).build_gaps(query="500")

    assert len(gaps) == 1
    assert gaps[0].leaf_no == "500"
    assert gaps[0].evidence_authorities == ["archive"]
    assert gaps[0].suggested_dockets == ["E-2, Sub 1396"]
    assert gaps[0].gap_priority >= 2
