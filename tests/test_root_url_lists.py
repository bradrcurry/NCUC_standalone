from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.root_url_lists import ProgressNCRootUrlListService
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.models.rider import RiderData
from duke_rates.utils.dates import utc_now


def _seed_current_targets(tmp_path: Path, repo: Repository) -> None:
    r_toud_pdf = tmp_path / "leaf-no-503-schedule-r-toud.pdf"
    r_toud_pdf.write_bytes(b"%PDF-1.4\n")
    r_toud_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Time-of-Use Schedule R-TOUD",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-503-schedule-r-toud.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(r_toud_pdf),
            content_hash="current-503",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=r_toud_id,
            parser_name="heuristic_schedule_parser",
            status=ParseStatus.PARSED,
            schedule=RateScheduleData(
                tariff_id="nc_progress_r-toud",
                utility="Duke Energy Progress",
                state="NC",
                company="progress",
                schedule_code="R-TOUD",
                schedule_title="Residential Service Time-of-Use Schedule R-TOUD",
            ),
        )
    )

    reps_pdf = tmp_path / "leaf-no-604-rider-reps.pdf"
    reps_pdf.write_bytes(b"%PDF-1.4\n")
    reps_id = repo.upsert_document(
        DiscoveryRecord(
            title="REPS Rider",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-604-rider-reps.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(reps_pdf),
            content_hash="current-604",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=reps_id,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="nc_progress_reps",
                state="NC",
                company="progress",
                code="REPS",
                title="REPS Rider",
                effective_date="October 1, 2025",
            ),
        )
    )


def test_root_url_list_preview_finds_useful_tariff_urls(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    _seed_current_targets(tmp_path, repo)
    matches = tmp_path / "matches.txt"
    matches.write_text(
        "\n".join(
            [
                "Found 2 matches.",
                (
                    "Family: 503/R-TOUD -> URL: "
                    "http://www.progress-energy.com:80/aboutenergy/rates/NCScheduleR-TOUD.pdf "
                    "(Timestamp: 20091007150254)"
                ),
                (
                    "Family: 604/REPS -> URL: "
                    "https://www.progress-energy.com/assets/www/docs/company/NC_Rider_REPS.pdf "
                    "(Timestamp: 20150513105735)"
                ),
            ]
        ),
        encoding="utf-8",
    )

    leads = ProgressNCRootUrlListService(repo, project_root=tmp_path).preview_leads(
        file_paths=[matches],
        missing_only=False,
    )

    assert len(leads) == 2
    assert leads[0].extracted_url
    assert any(lead.target_leaf_no == "503" for lead in leads)
    assert any(lead.target_leaf_no == "604" for lead in leads)


def test_root_url_list_preview_filters_noisy_false_matches(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    _seed_current_targets(tmp_path, repo)
    strict_matches = tmp_path / "strict_matches.txt"
    strict_matches.write_text(
        (
            "607/STS -> "
            "http://www.progress-energy.com:80/custservice/carbusiness/outdoorlight/"
            "Masterpiece%20Fixtures%20and%20Posts.pdf (20030814135825)\n"
        ),
        encoding="utf-8",
    )

    leads = ProgressNCRootUrlListService(repo, project_root=tmp_path).preview_leads(
        file_paths=[strict_matches],
        missing_only=False,
    )

    assert leads == []


def test_root_url_list_import_persists_scored_lead(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    _seed_current_targets(tmp_path, repo)
    matches = tmp_path / "matches.txt"
    matches.write_text(
        (
            "Family: 503/R-TOUD -> URL: "
            "http://www.duke-energy.com:80/pdfs/pe-NCScheduleR-TOUD.pdf "
            "(Timestamp: 20120907015145)\n"
        ),
        encoding="utf-8",
    )

    stored = ProgressNCRootUrlListService(repo, project_root=tmp_path).import_leads(
        file_paths=[matches],
        missing_only=False,
        min_score=50.0,
    )

    assert len(stored) == 1
    rows = repo.list_historical_leads()
    assert len(rows) == 1
    assert rows[0].source_class == "root_url_list"
    assert rows[0].target_leaf_no == "503"
    assert rows[0].filename == "pe-NCScheduleR-TOUD.pdf"
