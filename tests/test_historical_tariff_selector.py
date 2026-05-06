from datetime import UTC, date, datetime
from pathlib import Path

from duke_rates.billing.calculators import UsageInput
from duke_rates.billing.engine import BillingEngine
from duke_rates.db.repository import Repository
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.public_notice import PublicNoticeData
from duke_rates.models.rate_schedule import (
    EnergyCharge,
    FixedCharge,
    RateScheduleData,
    TariffReference,
)
from duke_rates.models.rider import RiderAdjustmentRow
from duke_rates.utils.dates import utc_now


def test_progress_nc_historical_tariff_selector_picks_covered_version(tmp_path: Path) -> None:
    repo, current_id, historical_id = _seed_progress_res_history(tmp_path)

    selector = ProgressNCHistoricalTariffSelector(repo)
    historical = selector.select_schedule(schedule_code="RES", service_date=date(2024, 10, 15))
    current = selector.select_schedule(schedule_code="RES", service_date=date(2025, 10, 15))

    assert historical.version.source_kind == "historical"
    assert historical.version.document_id == historical_id
    assert historical.schedule.effective_start == date(2024, 10, 1)
    assert historical.schedule.effective_end == date(2025, 9, 30)

    assert current.version.source_kind == "current"
    assert current.version.document_id == current_id
    assert current.schedule.effective_start == date(2025, 10, 1)
    assert current.schedule.effective_end is None


def test_progress_nc_historical_tariff_selector_matches_schedule_family_code(
    tmp_path: Path,
) -> None:
    repo, _, historical_id = _seed_progress_res_history(tmp_path)
    selector = ProgressNCHistoricalTariffSelector(repo)

    historical = selector.select_schedule(schedule_code="RES", service_date=date(2024, 10, 15))

    assert historical.version.document_id == historical_id


def test_progress_nc_historical_tariff_selector_supports_bill_estimation(tmp_path: Path) -> None:
    repo, _, _ = _seed_progress_res_history(tmp_path)

    selection = ProgressNCHistoricalTariffSelector(repo).select_schedule(
        schedule_code="RES",
        service_date=date(2024, 11, 15),
    )
    estimate = BillingEngine().estimate(selection.schedule, UsageInput(monthly_kwh=1000))

    assert estimate.total == 124.0
    assert estimate.tariff_id == "nc_progress_res_2024"

    estimate_with_riders = BillingEngine().estimate(
        selection.schedule,
        UsageInput(monthly_kwh=1000),
        rider_parse_results=[
            rider.parse_result for rider in selection.riders if rider.parse_result
        ],
    )

    assert estimate_with_riders.total == 129.43


def test_progress_nc_historical_tariff_selector_includes_riders_and_notice_links(
    tmp_path: Path,
) -> None:
    repo, _, _ = _seed_progress_res_history(tmp_path)

    selection = ProgressNCHistoricalTariffSelector(repo).select_schedule(
        schedule_code="RES",
        service_date=date(2024, 11, 15),
    )

    assert [rider.code for rider in selection.riders] == ["BA", "RDM"]
    assert selection.riders[0].version.source_kind == "historical"
    assert selection.riders[0].version.family_key.endswith("leaf-no-601-rider-ba-ry1.pdf")
    assert selection.riders[1].version.family_key.endswith("leaf-no-608-rider-rdm-ry1.pdf")
    assert selection.supporting_notices
    assert selection.supporting_notices[0].title == "NC Annual Riders Notice"
    assert selection.unresolved_rider_codes == []


def _seed_progress_res_history(tmp_path: Path) -> tuple[Repository, int, int]:
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
                fixed_charges=[FixedCharge(label="Customer charge", amount=15.0)],
                energy_charges=[EnergyCharge(label="Energy charge", rate=0.11)],
                riders=[
                    TariffReference(code="BA", title="Rider BA", role="rider"),
                    TariffReference(code="RDM", title="Rider RDM", role="rider"),
                ],
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
                fixed_charges=[FixedCharge(label="Customer charge", amount=14.0)],
                energy_charges=[EnergyCharge(label="Energy charge", rate=0.11)],
                riders=[
                    TariffReference(code="BA", title="Rider BA", role="rider"),
                    TariffReference(code="RDM", title="Rider RDM", role="rider"),
                ],
            ),
        ),
    )
    _seed_historical_rider(
        repo=repo,
        current_document_id=current_id,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-601-rider-ba-ry1.pdf",
        title="Annual Billing Adjustments",
        archived_url=(
            "https://web.archive.org/web/20241118190307/https://www.duke-energy.com/"
            "-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-601-rider-ba-ry1.pdf?rev=old"
        ),
        revision_label="NC Fifth Revised Leaf No. 601",
        effective_start="October 1, 2024",
        code="BA",
        filename="historical-ba.pdf",
    )
    _seed_historical_rider(
        repo=repo,
        current_document_id=current_id,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-608-rider-rdm-ry1.pdf",
        title="Residential Decoupling Mechanism Rider RDM",
        archived_url=(
            "https://web.archive.org/web/20241118190307/https://www.duke-energy.com/"
            "-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-608-rider-rdm-ry1.pdf?rev=old"
        ),
        revision_label="NC Original Leaf No. RDM 608",
        effective_start="October 1, 2023",
        code="RDM",
        filename="historical-rdm.pdf",
    )
    _seed_notice(repo, current_id)
    return repo, current_id, historical_id


def _seed_historical_rider(
    *,
    repo: Repository,
    current_document_id: int,
    family_key: str,
    title: str,
    archived_url: str,
    revision_label: str,
    effective_start: str,
    code: str,
    filename: str,
) -> None:
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=current_document_id,
            family_key=family_key,
            title=title,
            state="NC",
            company="progress",
            category="rider",
            kind="pdf",
            canonical_url=f"https://www.duke-energy.com{family_key}?rev=current",
            archived_url=archived_url,
            snapshot_timestamp=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
            local_path=Path(filename),
            raw_text_path=Path(f"{filename}.txt"),
            content_hash=f"{code.lower()}-hash",
            content_type="application/pdf",
            direct_status_code=403,
            direct_downloadable=False,
            revision_label=revision_label,
            leaf_no=family_key.split("leaf-no-", maxsplit=1)[1].split("-", maxsplit=1)[0].upper(),
            effective_start=effective_start,
            retrieved_at=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
        )
    )
    repo.save_historical_parse_result(
        historical_id,
        DocumentParseResult(
            document_id=historical_id,
            parser_name="rider_parser",
            status=ParseStatus.PARSED,
            rider={
                "rider_id": f"nc_progress_{code.lower()}",
                "state": "NC",
                "company": "progress",
                "code": code,
                "title": title,
                "effective_date": effective_start,
                "applicable_schedules": ["RES"] if code == "BA" else [],
                "adjustment_rows": (
                    [
                        RiderAdjustmentRow(
                            rate_class="Residential",
                            net_adjustment_cents_per_kwh=0.543,
                            applicable_schedules=["RES"],
                        ).model_dump(mode="json")
                    ]
                    if code == "BA"
                    else []
                ),
            },
        ),
    )


def _seed_notice(repo: Repository, current_document_id: int) -> None:
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=current_document_id,
            family_key=(
                "/-/media/pdfs/for-your-home/bill-inserts-2025/01jan/"
                "annual-riders-notice.pdf"
            ),
            title="NC Annual Riders Notice",
            state="NC",
            company="progress",
            category="public_notice",
            kind="pdf",
            canonical_url="https://www.duke-energy.com/annual-riders-notice.pdf",
            archived_url=(
                "https://web.archive.org/web/20250115120000/https://www.duke-energy.com/"
                "annual-riders-notice.pdf"
            ),
            snapshot_timestamp=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
            local_path=Path("annual-riders-notice.pdf"),
            raw_text_path=Path("annual-riders-notice.pdf.txt"),
            content_hash="notice-hash",
            content_type="application/pdf",
            direct_status_code=403,
            direct_downloadable=False,
            retrieved_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
    )
    repo.save_historical_parse_result(
        historical_id,
        DocumentParseResult(
            document_id=historical_id,
            parser_name="notice_parser",
            status=ParseStatus.PARSED,
            notice=PublicNoticeData(
                notice_id="progress-nc-annual-riders-2025",
                title="NC Annual Riders Notice",
                state="NC",
                company="progress",
                docket_numbers=["E-2, Sub 1354"],
                related_rider_codes=["BA", "RDM"],
                related_schedule_codes=["RES"],
            ),
        ),
    )
