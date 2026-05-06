from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.citation_miner import HistoricalCitationMiner
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rider import RiderChargeComponent, RiderData
from duke_rates.utils.dates import utc_now


def test_manual_lead_ingest_persists_lead_anchor_and_docket(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    settings = Settings(data_dir=tmp_path / "data", database_path=tmp_path / "test.db")
    current_pdf = tmp_path / "leaf-no-602-rider-jaa.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    current_id = repo.upsert_document(
        DiscoveryRecord(
            title="Joint Agency Asset Rider JAA",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-602-rider-jaa.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current-602",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=current_id,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="nc_progress_jaa",
                state="NC",
                company="progress",
                code="JAA",
                title="Joint Agency Asset Rider",
                effective_date="October 1, 2025",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Summary of Rider Adjustments",
                        value=0.321,
                        unit="cents_per_kwh",
                    )
                ],
            ),
        )
    )

    leads = HistoricalCitationMiner(settings, repo).ingest_manual_lead(
        family_query="602",
        source_class="search_engine",
        provenance_class="external",
        source_label="manual-google",
        source_location="notes.txt",
        source_url="https://starw1.ncuc.gov/example/view.pdf",
        text=(
            "See docket E-2, Sub 1380 and attachment "
            "https://www.duke-energy.com/pdfs/Rider-JAA-dep.pdf effective "
            "December 1, 2024"
        ),
        title="Search note for JAA",
        docket_number="E-2, Sub 1380",
    )

    assert len(leads) == 1
    stored = repo.list_historical_leads()
    assert len(stored) == 1
    assert stored[0].target_leaf_no == "602"
    assert stored[0].target_code == "JAA"
    assert stored[0].docket_number == "E-2, Sub 1380"
    assert repo.list_regulatory_docket_leads()[0].docket_number == "E-2, Sub 1380"
    assert repo.list_evidence_anchors()[0].anchor_value == "December 1, 2024"

