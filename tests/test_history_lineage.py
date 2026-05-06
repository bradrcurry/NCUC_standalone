from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.lineage import ProgressNCLineageService, _load_current_text
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.utils.dates import utc_now


def test_progress_nc_lineage_builds_current_and_historical_chain(tmp_path: Path) -> None:
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
            source_page_url="https://www.duke-energy.com/source",
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
            ),
        )
    )
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=current_id,
            family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
            title="Residential Service Schedule RES",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-500-schedule-res.pdf?rev=older"
            ),
            archived_url=(
                "https://web.archive.org/web/20241118190307/https://www.duke-energy.com/"
                "-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older"
            ),
            snapshot_timestamp=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
            local_path=tmp_path / "historical-res.pdf",
            raw_text_path=tmp_path / "historical-res.pdf.txt",
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
        )
    )
    repo.save_historical_parse_result(
        historical_id,
        DocumentParseResult(
            document_id=historical_id,
            parser_name="schedule_parser",
            status=ParseStatus.PARSED,
            schedule=RateScheduleData(
                tariff_id="nc_progress_res_2024",
                state="NC",
                company="progress",
                schedule_code="RES",
                schedule_title="Residential Service Schedule RES",
            ),
        ),
    )

    chains = ProgressNCLineageService(repo).build_chains(query="500", recovered_only=True)

    assert len(chains) == 1
    assert chains[0].leaf_no == "500"
    assert len(chains[0].versions) == 2
    assert chains[0].versions[0].source_kind == "current"
    assert chains[0].versions[0].revision_label == "NC Second Revised Leaf No. 500"
    assert chains[0].versions[1].source_kind == "historical"
    assert chains[0].versions[1].revision_label == "NC First Revised Leaf No. 500"


def test_load_current_text_handles_placeholder_path() -> None:
    assert _load_current_text(Path(".")) == ""
