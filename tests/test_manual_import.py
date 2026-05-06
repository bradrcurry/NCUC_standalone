from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.manual_import import ProgressNCHistoricalImportService


def test_progress_nc_historical_import_service_imports_local_pdf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    pdf_path = tmp_path / "ncuc-order.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

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

    service = ProgressNCHistoricalImportService(settings, repo)
    try:
        record = service.import_document(
            title="NCUC Imported Residential Service Schedule RES",
            category="rate",
            source_label="ncuc-manual",
            local_file=pdf_path,
            docket_number="E-2, Sub 1300",
        )
    finally:
        service.close()

    assert record.title == "NCUC Imported Residential Service Schedule RES"
    assert record.category == "rate"
    assert record.revision_label == "NC First Revised Leaf No. 500"
    assert record.effective_start == "October 1, 2024"
    assert record.parsed_result_json is not None


def test_progress_nc_historical_import_service_prefers_local_file_over_source_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    pdf_path = tmp_path / "ncuc-rider.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        "duke_rates.historical.manual_import.extract_pdf_text",
        lambda path: "\n".join(
            [
                "Duke Energy Progress, LLC",
                "NC Original Leaf No. 640",
                "Clean Power Rate Enhancement Rider",
                "RIDER CPRE",
                "Effective for service rendered on and after December 1, 2020",
            ]
        ),
    )

    service = ProgressNCHistoricalImportService(settings, repo)
    try:
        def _boom(url):  # pragma: no cover - should never execute
            raise AssertionError("HTTP fetch should not run when local_file is provided")

        monkeypatch.setattr(service.client, "get", _boom)
        record = service.import_document(
            title="Clean Power Rate Enhancement Rider",
            category="rider",
            source_label="ncuc-edocket",
            source_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=test",
            local_file=pdf_path,
            docket_number="E-2, Sub 1254",
        )
    finally:
        service.close()

    assert record.title == "Clean Power Rate Enhancement Rider"
    assert record.category == "rider"
    assert record.metadata_json is not None


def test_progress_nc_historical_import_service_accepts_family_key_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    pdf_path = tmp_path / "ncuc-rider.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        "duke_rates.historical.manual_import.extract_pdf_text",
        lambda path: "RIDER CPRE\nEffective for service rendered on and after December 1, 2020",
    )

    service = ProgressNCHistoricalImportService(settings, repo)
    try:
        record = service.import_document(
            title="CPRE Rider",
            category="rider",
            source_label="ncuc-edocket",
            source_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=test",
            local_file=pdf_path,
            docket_number="E-2, Sub 1254",
            family_key_override="ncuc-dep-640",
        )
    finally:
        service.close()

    assert record.family_key == "ncuc-dep-640"


def test_progress_nc_historical_import_service_uses_parse_text_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    pdf_path = tmp_path / "ncuc-rider.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def _boom(path):  # pragma: no cover - should never execute
        raise AssertionError("PDF extraction should not run when parse_text_override is provided")

    monkeypatch.setattr("duke_rates.historical.manual_import.extract_pdf_text", _boom)

    service = ProgressNCHistoricalImportService(settings, repo)
    try:
        record = service.import_document(
            title="Demand Side Management Rider",
            category="rider",
            source_label="ncuc-edocket",
            local_file=pdf_path,
            parse_text_override=(
                "Demand Side Management Rider\n"
                "Effective January 1, 2018\n"
                "Residential customers would see a DSM rider decrease of 0.010 cents per kWh."
            ),
        )
    finally:
        service.close()

    assert record.parsed_result_json is not None
    assert record.raw_text_path is not None
    assert record.raw_text_path.read_text(encoding="utf-8").startswith(
        "Demand Side Management Rider"
    )


def test_progress_nc_historical_import_service_separates_ncuc_records_by_family(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/duke_rates.db",
        manifest_path=tmp_path / "data/manifests/discovery.jsonl",
    )
    settings.ensure_directories()
    repo = Repository(settings.database_path)
    pdf_path = tmp_path / "ncuc-rider.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        "duke_rates.historical.manual_import.extract_pdf_text",
        lambda path: "placeholder",
    )

    service = ProgressNCHistoricalImportService(settings, repo)
    try:
        ee_record = service.import_document(
            title="Energy Efficiency Rider",
            category="rider",
            source_label="ncuc-edocket",
            source_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=shared",
            local_file=pdf_path,
            family_key_override="ncuc-dep-610",
            parse_text_override="Energy Efficiency Rider",
        )
        dsm_record = service.import_document(
            title="Demand Side Management Rider",
            category="rider",
            source_label="ncuc-edocket",
            source_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=shared",
            local_file=pdf_path,
            family_key_override="ncuc-dep-611",
            parse_text_override="Demand Side Management Rider",
        )
    finally:
        service.close()

    assert ee_record.id != dsm_record.id
    assert ee_record.archived_url.endswith("#family=ncuc-dep-610")
    assert dsm_record.archived_url.endswith("#family=ncuc-dep-611")
