from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.bill_relevant_gaps import ProgressNCBillRelevantGapService
from duke_rates.models.bill_observation import BillComponentObservation
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rider import RiderChargeComponent, RiderData
from duke_rates.utils.dates import utc_now


def test_bill_relevant_gap_service_reports_parse_and_historical_status(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "leaf-no-601-rider-ba.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    current_id = repo.upsert_document(
        DiscoveryRecord(
            title="Annual Billing Adjustments",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-601-rider-ba-ry1.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current-601",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=current_id,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="nc_progress_ba",
                state="NC",
                company="progress",
                code="BA",
                title="Annual Billing Adjustments",
                applicable_schedules=["RES", "R-TOUD", "R-TOU", "R-TOU-CPP"],
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Summary of Rider Adjustments",
                        value=1.549,
                        unit="cents_per_kwh",
                    ),
                    RiderChargeComponent(
                        bill_label="Clean Energy Rider",
                        value=1.81,
                        unit="fixed_monthly",
                    ),
                ],
            ),
        )
    )
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=current_id,
            family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-601-rider-ba-ry1.pdf",
            title="Annual Billing Adjustments",
            state="NC",
            company="progress",
            category="rider",
            kind="pdf",
            canonical_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-601-rider-ba-ry1.pdf?rev=older",
            archived_url="https://web.archive.org/web/20250101120000/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-601-rider-ba-ry1.pdf?rev=older",
            snapshot_timestamp=utc_now(),
            local_path=tmp_path / "historical-601.pdf",
            raw_text_path=tmp_path / "historical-601.pdf.txt",
            content_hash="historical-601",
            content_type="application/pdf",
            leaf_no="601",
            effective_start="December 1, 2025",
            effective_end="December 31, 2025",
            retrieved_at=utc_now(),
        )
    )
    historical = repo.list_historical_documents(state="NC", company="progress")[0]
    repo.save_historical_parse_result(
        historical.id or 1,
        DocumentParseResult(
            document_id=historical.id or 1,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="historic_ba",
                state="NC",
                company="progress",
                code="BA",
                title="Annual Billing Adjustments",
            ),
        ),
    )
    repo.replace_bill_component_observations(
        bill_id=1,
        observations=[
            BillComponentObservation(
                bill_id=1,
                source_path="bill.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="clean_energy_rider",
                component_label="Clean Energy Rider",
                amount=1.81,
                confidence=0.95,
            )
        ],
    )

    records = ProgressNCBillRelevantGapService(repo).build_records()

    assert len(records) == 1
    record = records[0]
    assert record.leaf_no == "601"
    assert record.primary_code == "BA"
    assert record.parse_status == "parsed"
    assert record.historical_version_count == 1
    assert record.historical_match_modes == ["leaf", "code"]
    assert record.parsed_component_labels == [
        "Summary of Rider Adjustments",
        "Clean Energy Rider",
    ]
    assert record.observed_component_keys == ["clean_energy_rider"]
    assert "missing_historical_leaf" not in record.gap_flags


def test_bill_relevant_gap_service_counts_code_matched_historical_predecessor(
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "leaf-no-501-r-toud.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    current_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-501-schedule-r-toud.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current-501",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=current_id,
            parser_name="heuristic_schedule_parser",
            status=ParseStatus.PARSED,
            schedule={
                "tariff_id": "nc_progress_r_toud",
                "state": "NC",
                "company": "progress",
                "schedule_code": "R-TOUD",
                "schedule_title": "Residential Service Time-of-Use",
            },
        )
    )
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key="/pdfs/r2-nc-schedule-r-toud-dep.pdf",
            title="Residential Time of Use (R-TOU-36) - Three Phase",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://www.duke-energy.com/pdfs/R2-NC-Schedule-R-TOUD-dep.pdf",
            archived_url="https://web.archive.org/web/20160102000000/https://www.duke-energy.com/pdfs/R2-NC-Schedule-R-TOUD-dep.pdf",
            snapshot_timestamp=utc_now(),
            local_path=tmp_path / "historical-r-toud.pdf",
            raw_text_path=tmp_path / "historical-r-toud.pdf.txt",
            content_hash="historical-r-toud",
            content_type="application/pdf",
            effective_start="2016-01-01",
            effective_end="2017-01-31",
            retrieved_at=utc_now(),
        )
    )
    repo.save_historical_parse_result(
        historical_id,
        DocumentParseResult(
            document_id=historical_id,
            parser_name="heuristic_schedule_parser",
            status=ParseStatus.PARSED,
            schedule={
                "tariff_id": "historic_r_toud",
                "state": "NC",
                "company": "progress",
                "schedule_code": "R-TOUD",
                "schedule_title": "Residential Time of Use",
            },
        ),
    )

    record = ProgressNCBillRelevantGapService(repo).build_records()[0]

    assert record.leaf_no == "501"
    assert record.primary_code == "R-TOUD"
    assert record.historical_version_count == 1
    assert record.historical_match_modes == ["code"]
    assert "missing_historical_leaf" not in record.gap_flags


def test_bill_relevant_gap_service_excludes_same_effective_start_duplicate(
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "leaf-no-501-r-toud.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    current_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-501-schedule-r-toud.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current-501",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=current_id,
            parser_name="heuristic_schedule_parser",
            status=ParseStatus.PARSED,
            schedule={
                "tariff_id": "nc_progress_r_toud",
                "state": "NC",
                "company": "progress",
                "schedule_code": "R-TOUD",
                "schedule_title": "Residential Service Time-of-Use",
                "effective_start": "2025-10-01",
            },
        )
    )
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-501-schedule-r-toud.pdf",
            title="Residential Service Time-of-Use (R-TOUD) - Single Phase",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-501-schedule-r-toud.pdf?rev=current"
            ),
            archived_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-501-schedule-r-toud.pdf?rev=current"
            ),
            snapshot_timestamp=utc_now(),
            local_path=tmp_path / "duplicate-current-r-toud.pdf",
            raw_text_path=tmp_path / "duplicate-current-r-toud.pdf.txt",
            content_hash="duplicate-current-r-toud",
            content_type="application/pdf",
            leaf_no="501",
            effective_start="October 1, 2025",
            retrieved_at=utc_now(),
        )
    )
    repo.save_historical_parse_result(
        historical_id,
        DocumentParseResult(
            document_id=historical_id,
            parser_name="heuristic_schedule_parser",
            status=ParseStatus.PARSED,
            schedule={
                "tariff_id": "duplicate_current_r_toud",
                "state": "NC",
                "company": "progress",
                "schedule_code": "R-TOUD",
                "schedule_title": "Residential Time of Use",
                "effective_start": "2025-10-01",
            },
        ),
    )

    record = ProgressNCBillRelevantGapService(repo).build_records()[0]

    assert record.leaf_no == "501"
    assert record.historical_version_count == 0
    assert "missing_historical_leaf" in record.gap_flags
