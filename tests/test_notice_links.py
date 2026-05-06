from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.notice_links import ProgressNCNoticeLinkService
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.utils.dates import utc_now


def test_notice_links_match_notice_to_existing_chains(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "leaf-no-500-schedule-res.pdf"
    current_pdf.write_bytes(b"%PDF")
    current_pdf.with_suffix(".pdf.txt").write_text(
        (
            "NC Second Revised Leaf No. 500\n"
            "Effective for service rendered on and after October 1, 2025"
        ),
        encoding="utf-8",
    )
    current_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Schedule RES",
            source_page_url="https://www.duke-energy.com/source",
            document_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=current",
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
            ),
        )
    )
    notice_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key="/-/media/pdfs/for-your-home/bill-inserts-2025/01jan/annual-riders.pdf",
            title="NC Annual Riders Notice – Fuel, REPS, CPRE, DSM/EE, JAAR",
            state="NC",
            company="progress",
            category="public_notice",
            kind="pdf",
            canonical_url="https://www.duke-energy.com/annual-riders.pdf",
            archived_url="https://www.duke-energy.com/annual-riders.pdf",
            snapshot_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            local_path=tmp_path / "annual-riders.pdf",
            raw_text_path=tmp_path / "annual-riders.pdf.txt",
            content_hash="notice",
            content_type="application/pdf",
            direct_status_code=200,
            direct_downloadable=True,
            retrieved_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
    )
    repo.save_historical_parse_result(
        notice_id,
        DocumentParseResult(
            document_id=notice_id,
            parser_name="notice_parser",
            status=ParseStatus.PARSED,
            notice={
                "notice_id": "nc_progress_notice",
                "title": "NC Annual Riders Notice – Fuel, REPS, CPRE, DSM/EE, JAAR",
                "state": "NC",
                "company": "progress",
                "docket_numbers": ["E-2, Sub 1341"],
                "related_rider_codes": ["BA", "CPRE", "JAA"],
                "related_schedule_codes": ["RES"],
                "customer_classes": ["Residential"],
            },
        ),
    )

    links = ProgressNCNoticeLinkService(repo).build_links()

    assert len(links) == 1
    assert links[0].related_schedule_codes == ["RES"]
    assert any(match.title == "Residential Service Schedule RES" for match in links[0].matches)
