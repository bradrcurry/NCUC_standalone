from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.family_crosswalk import ProgressNCFamilyCrosswalkService
from duke_rates.historical.family_targets import ProgressNCFamilyTarget
from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import RateScheduleData


def test_crosswalk_matches_legacy_r_toue_record_to_leaf_504(tmp_path: Path) -> None:
    service = ProgressNCFamilyCrosswalkService(Repository(tmp_path / "test.db"))
    target = ProgressNCFamilyTarget(
        family_key="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-504-schedule-r-tou.pdf",
        current_document_id=9,
        title="Residential Time-of-Use Energy",
        category="rate",
        family_type="optional_service",
        leaf_no="504",
        code="R-TOU",
        current_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-504-schedule-r-tou.pdf",
        current_path="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-504-schedule-r-tou.pdf",
        current_filename="leaf-no-504-schedule-r-tou.pdf",
        aliases=("Residential Time-of-Use Energy", "R-TOU"),
    )
    parse_result = DocumentParseResult(
        document_id=2,
        parser_name="heuristic_schedule_parser",
        status=ParseStatus.PARSED,
        schedule=RateScheduleData(
            tariff_id="nc_progress_r-tou-21_time-of-use",
            utility="Duke Energy",
            state="NC",
            company="progress",
            schedule_code="R-TOUE-21",
            schedule_title="ALL-Energy TIME-OF-USE",
        ),
    )
    historical = HistoricalDocumentRecord(
        id=2,
        current_document_id=None,
        family_key="/pdfs/pe-ncscheduler-toue.pdf",
        title="ALL-Energy TIME-OF-USE",
        state="NC",
        company="progress",
        category=DocumentCategory.RATE.value,
        kind=DocumentKind.PDF.value,
        canonical_url="https://www.duke-energy.com/pdfs/pe-NCScheduleR-TOUE.pdf",
        archived_url="https://web.archive.org/web/20120906213849/http://www.duke-energy.com:80/pdfs/pe-NCScheduleR-TOUE.pdf",
        snapshot_timestamp=datetime(2012, 9, 6, 21, 38, 49, tzinfo=UTC),
        local_path=tmp_path / "r-toue.pdf",
        raw_text_path=tmp_path / "r-toue.pdf.txt",
        content_hash="hash-r-toue",
        content_type="application/pdf",
        retrieved_at=datetime(2026, 3, 16, 16, 40, 0, tzinfo=UTC),
        parsed_result_json=parse_result.model_dump_json(),
    )

    match = service._match_record(historical, {"504": target})

    assert match is not None
    assert match.new_family_key.endswith("leaf-no-504-schedule-r-tou.pdf")
    assert match.target_leaf_no == "504"
    assert match.matched_code == "R-TOU"
    assert match.basis == "parsed_code"
    assert match.confidence >= 90.0
