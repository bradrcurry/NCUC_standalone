from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.progress_nc import (
    ProgressNCHistoricalRecoveryService,
    _leaf_number_from_url,
    _rider_code_from_url,
    _seed_priority,
    _seed_priority_with_context,
)
from duke_rates.models.document import (
    DiscoveryRecord,
    DocumentCategory,
    DocumentKind,
    StoredDocument,
)
from duke_rates.utils.dates import utc_now


def test_leaf_number_from_url_extracts_numeric_leaf() -> None:
    url = (
        "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
        "leaf-no-500-schedule-res.pdf?rev=abc"
    )

    assert _leaf_number_from_url(url) == 500


def test_seed_priority_prefers_core_leaf_schedule_over_summary_page() -> None:
    summary_doc = StoredDocument(
        id=1,
        title="Summary of Rider Adjustments",
        source_page_url="https://www.duke-energy.com/home/billing/rates",
        document_url=(
            "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
            "leaf-no-600-summary.pdf?rev=abc"
        ),
        state="NC",
        company="progress",
        category="rider",
        kind="pdf",
        local_path="summary.pdf",
        content_hash="1",
        retrieved_at="2026-03-15T00:00:00+00:00",
        discovered_at="2026-03-15T00:00:00+00:00",
    )
    residential_doc = StoredDocument(
        id=2,
        title="Residential Service Schedule RES",
        source_page_url="https://www.duke-energy.com/home/billing/rates",
        document_url=(
            "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
            "leaf-no-500-schedule-res.pdf?rev=abc"
        ),
        state="NC",
        company="progress",
        category="rate",
        kind="pdf",
        local_path="res.pdf",
        content_hash="2",
        retrieved_at="2026-03-15T00:00:00+00:00",
        discovered_at="2026-03-15T00:00:00+00:00",
    )

    assert _seed_priority(residential_doc) < _seed_priority(summary_doc)


def test_rider_code_from_url_extracts_rider_code() -> None:
    url = (
        "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
        "leaf-no-601-rider-ba-ry1.pdf?rev=abc"
    )

    assert _rider_code_from_url(url) == "BA"


def test_seed_priority_with_context_targets_missing_rider_gaps() -> None:
    ba_doc = StoredDocument(
        id=1,
        title="Annual Billing Adjustments",
        source_page_url="https://www.duke-energy.com/home/billing/rates",
        document_url=(
            "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
            "leaf-no-601-rider-ba-ry1.pdf?rev=abc"
        ),
        state="NC",
        company="progress",
        category="rider",
        kind="pdf",
        local_path="ba.pdf",
        content_hash="1",
        retrieved_at="2026-03-15T00:00:00+00:00",
        discovered_at="2026-03-15T00:00:00+00:00",
    )
    jaa_doc = StoredDocument(
        id=2,
        title="Joint Agency Asset Rider JAA",
        source_page_url="https://www.duke-energy.com/home/billing/rates",
        document_url=(
            "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
            "leaf-no-602-rider-jaa-ry1.pdf?rev=abc"
        ),
        state="NC",
        company="progress",
        category="rider",
        kind="pdf",
        local_path="jaa.pdf",
        content_hash="2",
        retrieved_at="2026-03-15T00:00:00+00:00",
        discovered_at="2026-03-15T00:00:00+00:00",
    )

    targeted = _seed_priority_with_context(
        ba_doc,
        target_rider_codes={"BA"},
        historical_family_keys=set(),
    )
    existing = _seed_priority_with_context(
        ba_doc,
        target_rider_codes={"BA"},
        historical_family_keys={
            "/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-601-rider-ba-ry1.pdf"
        },
    )
    untargeted = _seed_priority_with_context(
        jaa_doc,
        target_rider_codes={"BA"},
        historical_family_keys=set(),
    )

    assert targeted < untargeted
    assert targeted < existing


def test_seed_documents_can_filter_to_target_leaf_numbers(tmp_path) -> None:
    repo = Repository(tmp_path / "test.db")
    for leaf in ("500", "601"):
        pdf = tmp_path / f"leaf-no-{leaf}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        repo.upsert_document(
            DiscoveryRecord(
                title=f"Leaf {leaf}",
                source_page_url="https://www.duke-energy.com/home/billing/rates",
                document_url=(
                    "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/"
                    f"leaf-no-{leaf}-sample.pdf?rev=abc"
                ),
                state="NC",
                company="progress",
                category=DocumentCategory.RIDER if leaf == "601" else DocumentCategory.RATE,
                kind=DocumentKind.PDF,
                retrieval_timestamp=utc_now(),
                local_path=str(pdf),
                content_hash=leaf,
                status_code=200,
            )
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "test.db",
        manifest_path=tmp_path / "data" / "manifests" / "discovery.jsonl",
    )
    settings.ensure_directories()
    service = ProgressNCHistoricalRecoveryService(settings, repo)
    try:
        selected = service._seed_documents(10, target_leaf_numbers={"601"})
    finally:
        service.close()

    assert len(selected) == 1
    assert selected[0].document_url.endswith("leaf-no-601-sample.pdf?rev=abc")
