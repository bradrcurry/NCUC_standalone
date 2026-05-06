from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.inbox import (
    ProgressNCHistoricalInboxService,
    parse_history_inbox_manifest,
)
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.public_notice import PublicNoticeData
from duke_rates.models.rate_schedule import RateScheduleData, TariffReference
from duke_rates.utils.dates import utc_now


def test_generate_regulator_manifest_creates_jsonl_targets(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    _seed_res_gap(repo, tmp_path)

    service = ProgressNCHistoricalInboxService(settings, repo)
    output = tmp_path / "inbox" / "regulator_targets.jsonl"

    count = service.generate_regulator_manifest(output_path=output, query="500")

    assert count == 1
    entries = parse_history_inbox_manifest(output)
    assert len(entries) == 1
    assert entries[0].title == "Residential Service Schedule RES"
    assert entries[0].source_authority == "regulator"
    assert "E-2, Sub 1396" in entries[0].candidate_dockets


def test_import_manifest_imports_local_file_entries(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    manifest = tmp_path / "inbox" / "import.jsonl"
    pdf_path = tmp_path / "inbox" / "ncuc-order.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4\n")
    manifest.write_text(
        (
            '{"title":"NCUC Imported Residential Service Schedule RES",'
            '"category":"rate","source_label":"ncuc-manual",'
            '"source_authority":"regulator","source_type":"ncuc",'
            '"file":"ncuc-order.pdf","docket_number":"E-2, Sub 1300"}\n'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "duke_rates.historical.manual_import.extract_pdf_text",
        lambda path: "\n".join(
            [
                "Duke Energy Progress, LLC",
                "NC First Revised Leaf No. 500",
                "Superseding NC Original Leaf No. 500",
                "Effective for service rendered from October 1, 2024 through September 30, 2025",
                "RESIDENTIAL SERVICE",
                "SCHEDULE RES",
            ]
        ),
    )

    imported = ProgressNCHistoricalInboxService(settings, repo).import_manifest(manifest)

    assert len(imported) == 1
    assert imported[0].revision_label == "NC First Revised Leaf No. 500"
    assert imported[0].effective_start == "October 1, 2024"


def test_export_manifest_csv_and_markdown(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    manifest = tmp_path / "inbox" / "targets.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest_entry = (
        '{"title":"Residential Service Schedule RES","category":"rate",'
        '"source_label":"ncuc-manual","source_authority":"regulator",'
        '"source_type":"ncuc","file":"res.pdf",'
        '"docket_number":"E-2, Sub 1300",'
        '"candidate_dockets":["E-2, Sub 1300"],"leaf_no":"500",'
        '"family_key":"/-/media/pdfs/.../leaf-no-500-schedule-res.pdf",'
        '"notes":["gap_priority=3"]}\n'
    )
    manifest.write_text(
        manifest_entry,
        encoding="utf-8",
    )
    service = ProgressNCHistoricalInboxService(settings, repo)
    csv_output = tmp_path / "inbox" / "targets.csv"
    md_output = tmp_path / "inbox" / "targets.md"

    csv_count = service.export_manifest_csv(manifest_path=manifest, output_path=csv_output)
    md_count = service.export_manifest_markdown(manifest_path=manifest, output_path=md_output)

    assert csv_count == 1
    assert md_count == 1
    assert "Residential Service Schedule RES" in csv_output.read_text(encoding="utf-8")
    assert "| Residential Service Schedule RES |" in md_output.read_text(encoding="utf-8")


def _seed_res_gap(repo: Repository, tmp_path: Path) -> None:
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
                riders=[TariffReference(code="BA", title="Rider BA", role="rider")],
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
            canonical_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older",
            archived_url="https://web.archive.org/web/20241118190307/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older",
            snapshot_timestamp=utc_now(),
            local_path=tmp_path / "historical-res.pdf",
            raw_text_path=tmp_path / "historical-res.pdf.txt",
            content_hash="older",
            content_type="application/pdf",
            revision_label="NC First Revised Leaf No. 500",
            supersedes_label="NC Original Leaf No. 500",
            leaf_no="500",
            effective_start="October 1, 2024",
            effective_end="September 30, 2025",
            retrieved_at=utc_now(),
            metadata_json='{"source":"wayback"}',
        )
    )
    notice_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key="/-/media/pdfs/for-your-home/rates/public-notices/e-2-sub-1396-dep-pbr-year-2-public-notice.pdf",
            title="Notice of Application for Rider Rate Adjustments and Public Hearing",
            state="NC",
            company="progress",
            category="public_notice",
            kind="pdf",
            canonical_url="https://www.duke-energy.com/notice.pdf",
            archived_url="https://www.duke-energy.com/notice.pdf",
            snapshot_timestamp=utc_now(),
            local_path=tmp_path / "notice.pdf",
            raw_text_path=tmp_path / "notice.pdf.txt",
            content_hash="notice",
            content_type="application/pdf",
            retrieved_at=utc_now(),
        )
    )
    repo.save_historical_parse_result(
        notice_id,
        DocumentParseResult(
            document_id=notice_id,
            parser_name="notice_parser",
            status=ParseStatus.PARSED,
            notice=PublicNoticeData(
                notice_id="notice-1",
                title="Notice of Application for Rider Rate Adjustments and Public Hearing",
                state="NC",
                company="progress",
                docket_numbers=["E-2, Sub 1396"],
                related_rider_codes=["BA"],
                related_schedule_codes=["RES"],
            ),
        ),
    )
