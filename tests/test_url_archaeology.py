from pathlib import Path
from types import SimpleNamespace

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.family_targets import ProgressNCFamilyTarget
from duke_rates.historical.url_archaeology import (
    ProgressNCUrlArchaeologyService,
    _legacy_filename_candidates,
    _variant_specs,
)
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical_lead import HistoricalLeadRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.utils.dates import utc_now


def test_legacy_filename_candidates_include_expected_progress_nc_patterns() -> None:
    target = ProgressNCFamilyTarget(
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-501-schedule-r-toud.pdf",
        current_document_id=1,
        title="Residential Service Time-of-Use Schedule R-TOUD",
        category="rate",
        family_type="optional_service",
        leaf_no="501",
        code="R-TOUD",
        current_url=(
            "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
            "leaf-no-501-schedule-r-toud.pdf?rev=current"
        ),
        current_path="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-501-schedule-r-toud.pdf",
        current_filename="leaf-no-501-schedule-r-toud.pdf",
    )

    candidates = dict(_legacy_filename_candidates(target))

    assert "R2-NC-Schedule-R-TOUD-dep.pdf" in candidates
    assert "pe-NCScheduleR-TOUD.pdf" in candidates


def test_variant_specs_prioritize_exact_lead_urls() -> None:
    target = ProgressNCFamilyTarget(
        family_key="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-571-schedule-sls.pdf",
        current_document_id=1,
        title="Street Lighting Service",
        category="rate",
        family_type="optional_service",
        leaf_no="571",
        code=None,
        current_url=(
            "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/"
            "leaf-no-571-schedule-sls.pdf"
        ),
        current_path="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-571-schedule-sls.pdf",
        current_filename="leaf-no-571-schedule-sls.pdf",
    )
    lead = HistoricalLeadRecord(
        id=99,
        family_key=target.family_key,
        target_leaf_no="571",
        target_code=None,
        target_title=target.title,
        family_type=target.family_type,
        category=target.category,
        source_class="root_url_list",
        provenance_class="reference",
        source_label="matches",
        extracted_url="http://www.duke-energy.com/pdfs/pe-NCScheduleSLS.pdf",
        filename="pe-NCScheduleSLS.pdf",
        extraction_method="root_url_list_import",
    )

    specs = _variant_specs(target, [lead])

    assert specs[0][0] == "http://www.duke-energy.com/pdfs/pe-NCScheduleSLS.pdf"
    assert specs[0][2] == "lead_exact_url"


def test_generate_variants_for_family_includes_root_url_list_leads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    settings = Settings(data_dir=tmp_path / "data", database_path=tmp_path / "test.db")

    current_pdf = tmp_path / "leaf-no-571-schedule-sls.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Street Lighting Service",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/"
                "leaf-no-571-schedule-sls.pdf"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current-571",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=doc_id,
            parser_name="heuristic_schedule_parser",
            status=ParseStatus.PARSED,
            schedule=RateScheduleData(
                tariff_id="nc_progress_sls",
                utility="Duke Energy Progress",
                state="NC",
                company="progress",
                schedule_code="SLS",
                schedule_title="Street Lighting Service",
            ),
        )
    )
    repo.upsert_historical_lead(
        HistoricalLeadRecord(
            family_key="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-571-schedule-sls.pdf",
            target_leaf_no="571",
            target_code=None,
            target_title="Street Lighting Service",
            family_type="optional_service",
            category="rate",
            source_class="root_url_list",
            provenance_class="reference",
            source_label="matches",
            extracted_url="http://www.duke-energy.com/pdfs/pe-NCScheduleSLS.pdf",
            filename="pe-NCScheduleSLS.pdf",
            extraction_method="root_url_list_import",
            confidence_score=90.0,
        )
    )

    service = ProgressNCUrlArchaeologyService(settings, repo)

    def fake_evaluate(variant):
        if variant.variant_url == "http://www.duke-energy.com/pdfs/pe-NCScheduleSLS.pdf":
            variant.score = 99.0
        else:
            variant.score = 10.0
        return variant

    monkeypatch.setattr(service, "_evaluate_variant", fake_evaluate)
    try:
        variants = service.generate_variants_for_family("571", max_variants=5)
    finally:
        service.close()

    assert any(
        v.variant_url == "http://www.duke-energy.com/pdfs/pe-NCScheduleSLS.pdf"
        for v in variants
    )


def test_recover_family_continues_after_variant_failure(monkeypatch, tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    settings = Settings(data_dir=tmp_path / "data", database_path=tmp_path / "test.db")

    current_pdf = tmp_path / "leaf-no-571-schedule-sls.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\n")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Street Lighting Service",
            source_page_url="https://www.duke-energy.com/home/billing/rates",
            document_url=(
                "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/"
                "leaf-no-571-schedule-sls.pdf"
            ),
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="current-571",
            status_code=200,
        )
    )
    repo.save_parse_result(
        DocumentParseResult(
            document_id=doc_id,
            parser_name="heuristic_schedule_parser",
            status=ParseStatus.PARSED,
            schedule=RateScheduleData(
                tariff_id="nc_progress_sls",
                utility="Duke Energy Progress",
                state="NC",
                company="progress",
                schedule_code="SLS",
                schedule_title="Street Lighting Service",
            ),
        )
    )

    service = ProgressNCUrlArchaeologyService(settings, repo)
    variants = service.generate_variants_for_family("571", max_variants=2)
    assert len(variants) >= 1

    calls = {"count": 0}

    sentinel = SimpleNamespace(id=None)

    def fake_recover_variant(target, variant, *, from_year):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return sentinel

    monkeypatch.setattr(service, "_recover_variant", fake_recover_variant)
    try:
        recovered = service.recover_family("571", from_year=2003, max_variants=2)
    finally:
        service.close()

    assert recovered == [sentinel]
