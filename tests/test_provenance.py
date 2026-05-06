from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.historical.provenance import ProgressNCProvenanceService, derive_source_provenance
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.utils.dates import utc_now


def test_derive_source_provenance_prefers_explicit_metadata() -> None:
    row = HistoricalDocumentRecord(
        id=1,
        current_document_id=None,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
        title="Imported NCUC Residential Service Schedule RES",
        state="NC",
        company="progress",
        category="rate",
        kind="pdf",
        canonical_url="local-file://ncuc-order.pdf",
        archived_url="local-file://ncuc-order.pdf",
        snapshot_timestamp=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
        local_path=Path("ncuc-order.pdf"),
        content_hash="abc",
        retrieved_at=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
        metadata_json=(
            '{"source_label":"ncuc-manual","source_authority":"regulator",'
            '"source_type":"ncuc","docket_number":"E-2, Sub 1300"}'
        ),
    )

    provenance = derive_source_provenance(row)

    assert provenance.authority == "regulator"
    assert provenance.source_type == "ncuc"
    assert provenance.docket_number == "E-2, Sub 1300"
    assert provenance.confidence_rank == 90


def test_progress_nc_provenance_service_builds_chain_coverage(tmp_path: Path) -> None:
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
            source_page_url="https://www.duke-energy.com/home/billing/rates",
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
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=current_id,
            family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
            title="Residential Service Schedule RES",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="local-file://ncuc-order.pdf",
            archived_url="local-file://ncuc-order.pdf",
            snapshot_timestamp=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
            local_path=tmp_path / "historical-res.pdf",
            raw_text_path=tmp_path / "historical-res.pdf.txt",
            content_hash="older",
            content_type="application/pdf",
            revision_label="NC First Revised Leaf No. 500",
            supersedes_label="NC Original Leaf No. 500",
            leaf_no="500",
            effective_start="October 1, 2024",
            effective_end="September 30, 2025",
            retrieved_at=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
            metadata_json=(
                '{"source_label":"ncuc-manual","source_authority":"regulator",'
                '"source_type":"ncuc","docket_number":"E-2, Sub 1300"}'
            ),
        )
    )

    coverage = ProgressNCProvenanceService(repo).build_chain_coverage(query="500")

    assert len(coverage) == 1
    assert coverage[0].family_key.endswith("leaf-no-500-schedule-res.pdf")
    assert coverage[0].authorities == ["regulator"]
    assert coverage[0].dockets == ["E-2, Sub 1300"]
