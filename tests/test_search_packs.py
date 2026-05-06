import json
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.search_packs import ProgressNCSearchPackService
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical_lead import HistoricalLeadRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rider import RiderChargeComponent, RiderData
from duke_rates.models.url_variant import CandidateUrlVariantRecord
from duke_rates.utils.dates import utc_now


def test_search_pack_generation_includes_leads_and_variants(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "leaf-no-605-rider-cpre.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    current_id = repo.upsert_document(
        DiscoveryRecord(
            title="Competitive Procurement of Renewable Energy Rider CPRE",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                "leaf-no-605-rider-cpre.pdf?rev=current"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current-605",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=current_id,
            parser_name="heuristic_rider_parser",
            status=ParseStatus.PARSED,
            rider=RiderData(
                rider_id="nc_progress_cpre",
                state="NC",
                company="progress",
                code="CPRE",
                title="Competitive Procurement of Renewable Energy Rider",
                effective_date="October 1, 2025",
                charge_components=[
                    RiderChargeComponent(
                        bill_label="Summary of Rider Adjustments",
                        value=0.456,
                        unit="cents_per_kwh",
                    )
                ],
            ),
        )
    )
    family_key = "/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-605-rider-cpre.pdf"
    repo.upsert_historical_lead(
        HistoricalLeadRecord(
            family_key=family_key,
            target_leaf_no="605",
            target_code="CPRE",
            target_title="Competitive Procurement of Renewable Energy Rider CPRE",
            family_type="rider_adjustment",
            category="rider",
            source_class="openei_reference",
            provenance_class="reference",
            source_label="openei",
            extracted_url="https://www.duke-energy.com/pdfs/Rider-CPRE-dep.pdf",
            filename="Rider-CPRE-dep.pdf",
            extraction_method="test",
            confidence_score=55.0,
        )
    )
    repo.upsert_candidate_url_variant(
        CandidateUrlVariantRecord(
            family_key=family_key,
            variant_url="https://www.progress-energy.com/pdfs/Rider-CPRE-dep.pdf",
            hostname="www.progress-energy.com",
            path_family="/pdfs/",
            filename="Rider-CPRE-dep.pdf",
            heuristic="legacy_code_map",
            wayback_snapshot_count=3,
            score=72.0,
        )
    )

    packs = ProgressNCSearchPackService(repo).generate_missing_family_packs()

    assert len(packs) == 1
    payload = json.loads(packs[0].payload_json)
    assert payload["leaf_no"] == "605"
    assert payload["code"] == "CPRE"
    assert "Rider-CPRE-dep.pdf" in payload["candidate_filename_patterns"]
    assert payload["top_variants"][0]["wayback_snapshot_count"] == 3

