from __future__ import annotations

import json
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.artifact_cache import (
    load_page_artifacts,
    load_span_artifacts,
    save_page_artifacts,
    save_span_artifacts,
)
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc import importer as importer_module
from duke_rates.historical.ncuc.importer import NcucPipelineImporter
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus
from duke_rates.models.pipeline import DateCandidate, PageEvidence, PipelineRoute, TariffSpan
from duke_rates.models.tariff import TariffFamilyRecord


def test_page_and_span_artifacts_round_trip(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    source_pdf = str(tmp_path / "sample.pdf")
    pages = [
        PageEvidence(
            page_number=1,
            text_length=40,
            text_content="Leaf No. 500\nResidential Service",
            has_leaf_header=True,
            extracted_leaf_nos=["500"],
        )
    ]
    spans = [
        TariffSpan(
            parent_discovery_id=5,
            start_page=1,
            end_page=1,
            doc_type="tariff",
            extracted_leaf_nos={"500"},
            extracted_schedule_titles={"RES"},
            header_footer_snippets=["Leaf No. 500"],
            dates=[
                DateCandidate(
                    date_value="2024-01-01",
                    date_type="effective",
                    evidence_text="Effective January 1, 2024",
                    page_number=1,
                    confidence=0.9,
                )
            ],
        )
    ]

    save_page_artifacts(
        conn,
        discovery_record_id=5,
        source_pdf=source_pdf,
        file_hash="hash-1",
        pages=pages,
        metadata={"artifact_source": "native_text"},
    )
    save_span_artifacts(
        conn,
        discovery_record_id=5,
        source_pdf=source_pdf,
        file_hash="hash-1",
        spans=spans,
        metadata={"route_recommendation": "text_parse"},
    )
    conn.commit()

    loaded_pages = load_page_artifacts(conn, source_pdf=source_pdf, file_hash="hash-1")
    loaded_spans = load_span_artifacts(conn, source_pdf=source_pdf, file_hash="hash-1")

    assert len(loaded_pages) == 1
    assert loaded_pages[0].has_leaf_header is True
    assert loaded_pages[0].extracted_leaf_nos == ["500"]

    assert len(loaded_spans) == 1
    assert loaded_spans[0].start_page == 1
    assert loaded_spans[0].extracted_leaf_nos == {"500"}
    assert loaded_spans[0].dates[0].date_value == "2024-01-01"
    conn.close()


def test_importer_reuses_cached_page_and_span_artifacts(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    repo = Repository(db_path)
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Progress Residential Service",
        )
    )

    settings = Settings(database_path=db_path, data_dir=tmp_path / "data")
    importer = NcucPipelineImporter(settings, repo)

    pdf_path = tmp_path / "cached-artifacts.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    conn = connect(db_path)
    save_page_artifacts(
        conn,
        discovery_record_id=9,
        source_pdf=str(pdf_path),
        file_hash="hash-cached",
        pages=[
            PageEvidence(
                page_number=1,
                text_length=120,
                text_content="Leaf No. 500\nResidential Service\nEffective January 1, 2024",
                has_leaf_header=True,
                has_schedule_heading=True,
                extracted_leaf_nos=["500"],
                extracted_schedule_codes=["SCHEDULE RES"],
            )
        ],
        metadata={"artifact_source": "cached"},
    )
    save_span_artifacts(
        conn,
        discovery_record_id=9,
        source_pdf=str(pdf_path),
        file_hash="hash-cached",
        spans=[
            TariffSpan(
                parent_discovery_id=9,
                start_page=1,
                end_page=1,
                doc_type="tariff",
                extracted_leaf_nos={"500"},
                extracted_schedule_titles={"SCHEDULE RES"},
            )
        ],
        metadata={"artifact_source": "cached"},
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        importer_module,
        "triage_pdf",
        lambda _: type(
            "T",
            (),
            {
                "route_recommendation": PipelineRoute.TEXT_PARSE,
                "file_hash": "hash-cached",
            },
        )(),
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("page mining or segmentation should not run when cached artifacts exist")

    monkeypatch.setattr(importer_module, "mine_document_pages", _unexpected)
    monkeypatch.setattr(importer_module, "extract_ocr_document_pages", _unexpected)
    monkeypatch.setattr(importer_module, "segment_document", _unexpected)
    monkeypatch.setattr(importer_module, "extract_dates_from_span", lambda span, _pages: span.dates.append(
        DateCandidate(
            date_value="2024-01-01",
            date_type="effective",
            evidence_text="Effective January 1, 2024",
            page_number=1,
            confidence=0.9,
        )
    ))

    record = NcucDiscoveryRecord(
        id=9,
        utility="Duke Energy Progress",
        filing_title="Progress Energy Carolinas compliance filing",
        filing_date="2024-01-15",
        docket_number="E-2",
        local_path=str(pdf_path),
        fetch_status=NcucFetchStatus.SUCCESS,
        discovered_url="https://example.test/e-2-progress.pdf",
        content_hash="hash-cached",
    )

    created_ids = importer.mine_discovery_record_spans(record)

    assert len(created_ids) == 1
    stored = repo.get_historical_document(created_ids[0])
    assert stored is not None
    assert stored.company == "progress"
    assert stored.family_key == "nc-progress-leaf-500"
