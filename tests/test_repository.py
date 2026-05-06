from datetime import UTC, date, datetime
import json
from pathlib import Path

import pytest

from duke_rates.cli import _resolve_carolinas_rider_keys
from duke_rates.db.artifact_cache import save_page_artifacts, save_span_artifacts
from duke_rates.db.repository import Repository
from duke_rates.models.bill import BillingSummaryData, BillStatementData
from duke_rates.models.bill_observation import BillComponentObservation
from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.ncuc import (
    NcucAcquisitionMethod,
    NcucDiscoveryRecord,
    NcucFetchStatus,
)
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.pipeline import PageEvidence, TariffSpan
from duke_rates.models.rate_schedule import FixedCharge, RateScheduleData
from duke_rates.models.tariff import (
    TariffChargeRecord,
    RiderApplicabilityRecord,
    TariffFamilyRecord,
    TariffVersionRecord,
)
from duke_rates.utils.dates import utc_now


def test_repository_upsert_and_get(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    record = DiscoveryRecord(
        title="Test Tariff",
        source_page_url="https://www.duke-energy.com/source",
        document_url="https://www.duke-energy.com/test.pdf",
        state="NC",
        company="progress",
        category=DocumentCategory.RATE,
        kind=DocumentKind.PDF,
        retrieval_timestamp=utc_now(),
        local_path=str(tmp_path / "test.pdf"),
        content_hash="abc123",
        status_code=200,
    )
    doc_id = repo.upsert_document(record)
    stored = repo.get_document(doc_id)
    assert stored is not None
    assert stored.title == "Test Tariff"


def test_resolve_carolinas_rider_keys_falls_back_to_current_document_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = Repository(tmp_path / "test.db")
    pdf_path = tmp_path / "cei.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="CEI Rider",
            source_page_url="https://www.duke-energy.com/source",
            document_url="https://www.duke-energy.com/cei.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RIDER,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(pdf_path),
            content_hash="hash",
            status_code=200,
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-rider-CEI",
            state="NC",
            company="carolinas",
            tariff_identifier="rider-CEI",
            schedule_code="CEI",
            family_type="rider",
            title="CEI",
            current_document_id=doc_id,
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-schedule-RS",
            state="NC",
            company="carolinas",
            tariff_identifier="schedule-RS",
            schedule_code="RS",
            family_type="rate_schedule",
            title="RS",
        )
    )
    repo.upsert_rider_applicability(
        RiderApplicabilityRecord(
            rider_family_key="nc-carolinas-leaf-242",
            applies_to_family_key="nc-carolinas-schedule-RS",
            mandatory=True,
            source_type="tariff_text",
            confidence_score=0.9,
        )
    )

    monkeypatch.setattr(
        "duke_rates.parse.pdf_text.extract_pdf_text",
        lambda _path: "Duke Energy Carolinas, LLC NC Original Leaf No. 242\nRIDER CEI",
    )

    _resolve_carolinas_rider_keys(repo, "NC")

    links = repo.list_rider_applicability(applies_to_family_key="nc-carolinas-schedule-RS")
    assert [link.rider_family_key for link in links] == ["nc-carolinas-rider-CEI"]


def test_repository_upsert_and_get_historical_document(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    snapshot_timestamp = datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC)
    retrieved_at = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
    record = HistoricalDocumentRecord(
        current_document_id=7,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
        title="Residential Service Schedule RES",
        state="NC",
        company="progress",
        category=DocumentCategory.RATE.value,
        kind=DocumentKind.PDF.value,
        canonical_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=old",
        archived_url="https://web.archive.org/web/20241118190307/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=old",
        snapshot_timestamp=snapshot_timestamp,
        local_path=tmp_path / "historical/raw/nc/progress/rate/res.pdf",
        raw_text_path=tmp_path / "historical/raw/nc/progress/rate/res.pdf.txt",
        content_hash="deadbeef",
        content_type="application/pdf",
        direct_status_code=403,
        direct_downloadable=False,
        revision_label="NC First Revised Leaf No. 500",
        supersedes_label="NC Original Leaf No. 500",
        leaf_no="500",
        effective_start="October 1, 2024",
        effective_end="September 30, 2025",
        retrieved_at=retrieved_at,
        notes=["source=wayback"],
    )

    historical_id = repo.upsert_historical_document(record)
    stored = repo.get_historical_document(historical_id)

    assert stored is not None
    assert stored.title == "Residential Service Schedule RES"
    assert stored.archived_url == record.archived_url
    assert stored.revision_label == "NC First Revised Leaf No. 500"
    assert stored.direct_downloadable is False


def test_repository_lists_and_promotes_provisional_tariff_families(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
            aliases=["SMART ENERGY NOW PROGRAM (NC)"],
            notes="Provisional historical family created from unmatched NCUC tariff span.",
        )
    )

    provisional = repo.list_provisional_tariff_families(state="NC", company="carolinas")
    assert [family.family_key for family in provisional] == ["nc-carolinas-program-SMARTENERGYNOWPROGRAM"]

    promoted = repo.promote_provisional_tariff_family(
        "nc-carolinas-program-SMARTENERGYNOWPROGRAM",
        aliases=["SMART ENERGY NOW PROGRAM", "SEN PROGRAM"],
    )

    assert promoted is not None
    assert promoted.notes == "Promoted from provisional historical family."
    assert "SMART ENERGY NOW PROGRAM" in promoted.aliases
    assert "SEN PROGRAM" in promoted.aliases
    assert repo.list_provisional_tariff_families(state="NC", company="carolinas") == []


def test_repository_scores_provisional_review_candidates(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "review.db")
    now = datetime(2026, 4, 21, tzinfo=UTC)

    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            state="NC",
            company="progress",
            tariff_identifier="doc-TYPEOFSERVICE-LONG-LONG-LONG",
            schedule_code="TYPEOFSERVICE",
            family_type="doc",
            title="Type of Service",
            notes="Provisional historical family created from unmatched NCUC tariff span.",
        )
    )
    generic_hd_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            title="Type of Service (Span 2-3)",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://example.test/generic.pdf",
            archived_url="https://example.test/generic.pdf",
            snapshot_timestamp=now,
            local_path=tmp_path / "generic.pdf",
            content_hash="hash-generic",
            effective_start="2024-01-01",
            retrieved_at=now,
            start_page=2,
            end_page=3,
        )
    )
    generic_version = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            historical_document_id=generic_hd_id,
            effective_start="2024-01-01",
            source_type="regulator",
            confidence_score=0.4,
        )
    )
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=generic_version,
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            charge_type="fixed",
            charge_label="Charge",
            rate_value=None,
            rate_unit="$",
            confidence_score=0.2,
        )
    )

    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Residential Service",
            notes="Provisional historical family created from unmatched NCUC tariff span.",
        )
    )
    clean_hd_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-500",
            title="Schedule RES - Residential Service",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://example.test/res.pdf",
            archived_url="https://example.test/res.pdf",
            snapshot_timestamp=now,
            local_path=tmp_path / "res.pdf",
            content_hash="hash-res",
            effective_start="2024-01-01",
            retrieved_at=now,
            start_page=1,
            end_page=2,
        )
    )
    clean_version = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="nc-progress-leaf-500",
            historical_document_id=clean_hd_id,
            effective_start="2024-01-01",
            source_type="regulator",
            confidence_score=0.9,
        )
    )
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=clean_version,
            family_key="nc-progress-leaf-500",
            charge_type="fixed",
            charge_label="Basic Customer Charge",
            rate_value=14.0,
            rate_unit="$/month",
            confidence_score=0.95,
        )
    )
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=clean_version,
            family_key="nc-progress-leaf-500",
            charge_type="energy_block",
            charge_label="Energy Charge",
            rate_value=0.11,
            rate_unit="$/kWh",
            confidence_score=0.95,
        )
    )

    rows = repo.score_provisional_tariff_families(state="NC", company="progress", limit=10)

    assert rows[0]["family_key"] == "nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG"
    assert rows[0]["review_band"] == "high"
    assert "generic_family_key" in rows[0]["review_reasons"]
    assert rows[0]["suggested_family_type"] == "rate_schedule"
    assert rows[0]["promotion_command"] is None

    clean_row = next(row for row in rows if row["family_key"] == "nc-progress-leaf-500")
    assert clean_row["review_score"] < rows[0]["review_score"]
    assert clean_row["suggested_schedule_code"] == "RES"
    assert "promote-provisional-family nc-progress-leaf-500" in str(clean_row["promotion_command"])


def test_repository_lists_historical_only_tariff_families(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
            notes="Promoted from provisional historical family.",
        )
    )
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            title="SMART ENERGY NOW PROGRAM (NC) (Span 2-2)",
            state="NC",
            company="carolinas",
            category="program",
            kind="pdf",
            canonical_url="https://example.test/smart-energy-now",
            archived_url="ncuc://E-7/2215#page=2",
            snapshot_timestamp=datetime(2014, 1, 8, tzinfo=UTC),
            local_path=tmp_path / "smart-energy-now.pdf",
            content_hash="hash-smart-energy-now",
            content_type="application/pdf",
            direct_status_code=200,
            direct_downloadable=True,
            leaf_no="172",
            start_page=2,
            end_page=2,
            retrieved_at=datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC),
        )
    )

    rows = repo.list_historical_only_tariff_families(state="NC", company="carolinas")

    assert len(rows) == 1
    assert rows[0]["family_key"] == "nc-carolinas-program-SMARTENERGYNOWPROGRAM"
    assert rows[0]["historical_document_count"] == 1
    assert rows[0]["current_document_id"] is None


def test_repository_suggests_current_documents_for_historical_only_family(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
            aliases=["SMART ENERGY NOW PROGRAM", "SEN PROGRAM"],
            notes="Promoted from provisional historical family.",
        )
    )
    repo.upsert_document(
        DiscoveryRecord(
            title="Residential Smart Energy Now Program",
            source_page_url="https://example.test/source",
            document_url="https://example.test/smart-energy-now.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.PROGRAM,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(tmp_path / "smart-energy-now.pdf"),
            content_hash="hash-smart-energy-now-current",
            status_code=200,
        )
    )
    repo.upsert_document(
        DiscoveryRecord(
            title="BPM True-Up Rider",
            source_page_url="https://example.test/source-bpm",
            document_url="https://example.test/bpm.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RIDER,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(tmp_path / "bpm.pdf"),
            content_hash="hash-bpm",
            status_code=200,
        )
    )

    suggestions = repo.suggest_current_documents_for_family(
        "nc-carolinas-program-SMARTENERGYNOWPROGRAM"
    )

    assert len(suggestions) == 1
    assert suggestions[0]["title"] == "Residential Smart Energy Now Program"
    assert suggestions[0]["score"] >= 4


def test_repository_suggests_current_documents_from_mined_page_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
            aliases=["SMART ENERGY NOW PROGRAM"],
            notes="Promoted from provisional historical family.",
        )
    )

    strong_pdf = tmp_path / "generic-current.pdf"
    strong_pdf.write_bytes(b"%PDF-1.4")
    weak_pdf = tmp_path / "other-current.pdf"
    weak_pdf.write_bytes(b"%PDF-1.4")

    repo.upsert_document(
        DiscoveryRecord(
            title="Leaf 172",
            source_page_url="https://example.test/source",
            document_url="https://example.test/generic-current.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.PROGRAM,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(strong_pdf),
            content_hash="hash-generic-current",
            status_code=200,
        )
    )
    repo.upsert_document(
        DiscoveryRecord(
            title="Unrelated Filing",
            source_page_url="https://example.test/source-other",
            document_url="https://example.test/other-current.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.PROGRAM,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(weak_pdf),
            content_hash="hash-other-current",
            status_code=200,
        )
    )

    def _fake_mine_document_pages(file_path: str, max_pages: int | None = None) -> list[PageEvidence]:
        if file_path.endswith("generic-current.pdf"):
            return [
                PageEvidence(
                    page_number=1,
                    text_length=80,
                    extracted_leaf_nos=["172"],
                    extracted_schedule_codes=["SMART ENERGY NOW PROGRAM (NC)"],
                    has_schedule_heading=True,
                )
            ]
        return [
            PageEvidence(
                page_number=1,
                text_length=40,
                extracted_schedule_codes=["UNRELATED FILING"],
            )
        ]

    monkeypatch.setattr(
        "duke_rates.db.repository.mine_document_pages",
        _fake_mine_document_pages,
    )

    suggestions = repo.suggest_current_documents_for_family(
        "nc-carolinas-program-SMARTENERGYNOWPROGRAM"
    )

    assert len(suggestions) == 1
    assert suggestions[0]["title"] == "Leaf 172"
    assert "page_heading_phrase" in suggestions[0]["reasons"]
    assert suggestions[0]["candidate_headings"] == ["SMART ENERGY NOW PROGRAM (NC)"]
    assert suggestions[0]["candidate_leaf_nos"] == ["172"]


def test_repository_suppresses_mined_candidates_with_historical_leaf_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
            aliases=["SMART ENERGY NOW PROGRAM"],
            notes="Promoted from provisional historical family.",
        )
    )
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            title="SMART ENERGY NOW PROGRAM (NC) (Span 2-2)",
            state="NC",
            company="carolinas",
            category="program",
            kind="pdf",
            canonical_url="https://example.test/smart-energy-now",
            archived_url="ncuc://E-7/2215#page=2",
            snapshot_timestamp=datetime(2014, 1, 8, tzinfo=UTC),
            local_path=tmp_path / "smart-energy-now-historical.pdf",
            content_hash="hash-smart-energy-now-historical",
            content_type="application/pdf",
            direct_status_code=200,
            direct_downloadable=True,
            leaf_no="172",
            start_page=2,
            end_page=2,
            retrieved_at=datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC),
        )
    )

    candidate_pdf = tmp_path / "candidate.pdf"
    candidate_pdf.write_bytes(b"%PDF-1.4")
    repo.upsert_document(
        DiscoveryRecord(
            title="Residential Smart Energy Program",
            source_page_url="https://example.test/source",
            document_url="https://example.test/candidate.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.PROGRAM,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(candidate_pdf),
            content_hash="hash-candidate-current",
            status_code=200,
        )
    )

    monkeypatch.setattr(
        "duke_rates.db.repository.mine_document_pages",
        lambda _file_path, max_pages=None: [
            PageEvidence(
                page_number=1,
                text_length=80,
                extracted_leaf_nos=["168"],
                extracted_schedule_codes=["SMART ENERGY PROGRAM"],
                has_schedule_heading=True,
            )
        ],
    )

    suggestions = repo.suggest_current_documents_for_family(
        "nc-carolinas-program-SMARTENERGYNOWPROGRAM"
    )

    assert suggestions == []


def test_repository_reviews_historical_only_tariff_families_with_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-UNRESOLVEDPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-UNRESOLVEDPROGRAM",
            schedule_code="UNRESOLVEDPROGRAM",
            family_type="program",
            title="UNRESOLVED PROGRAM",
        )
    )
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            title="SMART ENERGY NOW PROGRAM (NC) (Span 2-2)",
            state="NC",
            company="carolinas",
            category="program",
            kind="pdf",
            canonical_url="https://example.test/smart-energy-now",
            archived_url="ncuc://E-7/2215#page=2",
            snapshot_timestamp=datetime(2014, 1, 8, tzinfo=UTC),
            local_path=tmp_path / "smart-energy-now-historical.pdf",
            content_hash="hash-smart-energy-now-historical",
            content_type="application/pdf",
            direct_status_code=200,
            direct_downloadable=True,
            leaf_no="172",
            start_page=2,
            end_page=2,
            retrieved_at=datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC),
        )
    )
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-program-UNRESOLVEDPROGRAM",
            title="UNRESOLVED PROGRAM (Span 1-1)",
            state="NC",
            company="carolinas",
            category="program",
            kind="pdf",
            canonical_url="https://example.test/unresolved-program",
            archived_url="ncuc://E-7/9999#page=1",
            snapshot_timestamp=datetime(2016, 1, 8, tzinfo=UTC),
            local_path=tmp_path / "unresolved-historical.pdf",
            content_hash="hash-unresolved-historical",
            content_type="application/pdf",
            direct_status_code=200,
            direct_downloadable=True,
            leaf_no="199",
            start_page=1,
            end_page=1,
            retrieved_at=datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC),
        )
    )
    candidate_pdf = tmp_path / "candidate.pdf"
    candidate_pdf.write_bytes(b"%PDF-1.4")
    repo.upsert_document(
        DiscoveryRecord(
            title="Leaf 172",
            source_page_url="https://example.test/source",
            document_url="https://example.test/candidate.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.PROGRAM,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(candidate_pdf),
            content_hash="hash-candidate-current",
            status_code=200,
        )
    )

    monkeypatch.setattr(
        "duke_rates.db.repository.mine_document_pages",
        lambda _file_path, max_pages=None: [
            PageEvidence(
                page_number=1,
                text_length=80,
                extracted_leaf_nos=["172"],
                extracted_schedule_codes=["SMART ENERGY NOW PROGRAM (NC)"],
                has_schedule_heading=True,
            )
        ],
    )

    rows = repo.review_historical_only_tariff_families(state="NC", company="carolinas")
    by_key = {row["family_key"]: row for row in rows}

    assert by_key["nc-carolinas-program-SMARTENERGYNOWPROGRAM"]["review_status"] == "review_candidates"
    assert by_key["nc-carolinas-program-SMARTENERGYNOWPROGRAM"]["candidate_count"] == 1
    assert by_key["nc-carolinas-program-SMARTENERGYNOWPROGRAM"]["top_candidate_score"] is not None
    assert by_key["nc-carolinas-program-UNRESOLVEDPROGRAM"]["review_status"] == "unresolved"
    assert by_key["nc-carolinas-program-UNRESOLVEDPROGRAM"]["candidate_count"] == 0
    assert by_key["nc-carolinas-program-UNRESOLVEDPROGRAM"]["top_candidate_score"] is None


def test_repository_attaches_current_document_to_family(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Smart Energy Now Program",
            source_page_url="https://example.test/source",
            document_url="https://example.test/smart-energy-now.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.PROGRAM,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(tmp_path / "smart-energy-now.pdf"),
            content_hash="hash-smart-energy-now-current",
            status_code=200,
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
            notes="Promoted from provisional historical family.",
        )
    )

    attached = repo.attach_current_document_to_family(
        "nc-carolinas-program-SMARTENERGYNOWPROGRAM",
        document_id=doc_id,
    )

    assert attached is not None
    assert attached.current_document_id == doc_id


def test_repository_lists_weak_unbounded_historical_documents_with_review_actions(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-672",
            state="NC",
            company="progress",
            tariff_identifier="leaf-672",
            schedule_code="RIDER_CEI",
            family_type="rider",
            title="Clean Energy Impact Rider",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-718",
            state="NC",
            company="progress",
            tariff_identifier="leaf-718",
            schedule_code="PROGRAM_CAP",
            family_type="program",
            title="Customer Assistance Program Credit CAP",
        )
    )
    current_hist_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-672",
            title="Clean Energy Impact Rider",
            state="NC",
            company="progress",
            category="rider",
            kind="pdf",
            canonical_url="https://example.test/current-cei.pdf",
            archived_url="archive://current-cei",
            snapshot_timestamp="2025-01-15T00:00:00Z",
            local_path=r"data\raw\nc\progress\rider\leaf-no-672-rider-cei.pdf",
            content_hash="hash-current-cei",
            direct_status_code=200,
            direct_downloadable=True,
            effective_start="2025-01-15",
            retrieved_at=datetime(2026, 3, 27, 0, 0, 0, tzinfo=UTC),
        )
    )
    discovery_hist_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-718",
            title="Customer Assistance Program Credit CAP",
            state="NC",
            company="progress",
            category="program",
            kind="pdf",
            canonical_url="https://example.test/cap.pdf",
            archived_url="archive://cap",
            snapshot_timestamp="2023-12-07T00:00:00Z",
            local_path=r"data\historical\ncuc\e-2-sub-1333\cap.pdf",
            content_hash="hash-cap",
            direct_status_code=200,
            direct_downloadable=True,
            effective_start="2023-12-07",
            retrieved_at=datetime(2026, 3, 27, 0, 0, 0, tzinfo=UTC),
        )
    )
    repo.upsert_ncuc_discovery_record(
        NcucDiscoveryRecord(
            docket_number="E-2, Sub 1333",
            utility="Duke Energy Progress",
            filing_title="Customer Assistance Program Credit CAP",
            local_path=r"data\historical\ncuc\e-2-sub-1333\cap.pdf",
            fetch_status=NcucFetchStatus.SUCCESS,
        )
    )

    with repo._connect() as conn:
        now = datetime(2026, 3, 27, 0, 0, 0, tzinfo=UTC).isoformat()
        for family_key, source_pdf in [
            ("nc-progress-leaf-672", r"data\raw\nc\progress\rider\leaf-no-672-rider-cei.pdf"),
            ("nc-progress-leaf-718", r"data\historical\ncuc\e-2-sub-1333\cap.pdf"),
        ]:
            conn.execute(
                """
                INSERT INTO parse_attempt_logs (
                    source_pdf, page_start, page_end, parser_stage, parser_profile,
                    status, confidence, utility, effective_date, charge_count,
                    review_flags_json, metadata_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source_pdf,
                    1,
                    1,
                    "historical_bulk",
                    "generic_residential",
                    "parsed",
                    0.5,
                    "DEP",
                    None,
                    0,
                    "[]",
                    json.dumps({"family_key": family_key, "outcome_quality": "weak"}),
                    now,
                ),
            )

    rows = repo.list_weak_unbounded_historical_documents(state="NC", company="progress")
    by_family = {row["family_key"]: row for row in rows}

    assert by_family["nc-progress-leaf-672"]["historical_document_id"] == current_hist_id
    assert by_family["nc-progress-leaf-672"]["source_kind"] == "current_pdf"
    assert by_family["nc-progress-leaf-672"]["review_action"] == "add_profile_or_current_parser_bridge"

    assert by_family["nc-progress-leaf-718"]["historical_document_id"] == discovery_hist_id
    assert by_family["nc-progress-leaf-718"]["source_kind"] == "discovery_pdf"
    assert by_family["nc-progress-leaf-718"]["discovery_record_id"] is not None
    assert by_family["nc-progress-leaf-718"]["review_action"] == "remine_from_discovery_record"


def test_repository_infers_discovery_record_for_legacy_raw_attachment(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    regulator_pdf = tmp_path / "historical" / "ncuc" / "e-2-sub-9999" / "example.pdf"
    regulator_pdf.parent.mkdir(parents=True, exist_ok=True)
    regulator_pdf.write_bytes(b"%PDF-1.4\n")

    discovery_id = repo.upsert_ncuc_discovery_record(
        NcucDiscoveryRecord(
            docket_number="E-2, Sub 9999",
            filing_title="Example legacy rider filing",
            discovered_url="https://example.test/e-2-sub-9999",
            attachment_url="https://example.test/e-2-sub-9999.pdf",
            local_path=str(regulator_pdf),
            fetch_status=NcucFetchStatus.SUCCESS,
            content_hash="discovery-hash",
            fetched_at="2026-03-27T12:00:00Z",
        )
    )

    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-718",
            title="Customer Assistance Program Credit CAP",
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/legacy-cap.pdf",
            archived_url="https://example.test/legacy-cap.pdf#family=nc-progress-leaf-718",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=tmp_path / "historical" / "raw" / "nc" / "progress" / "rider" / "legacy-cap.pdf",
            raw_text_path=tmp_path / "historical" / "raw" / "nc" / "progress" / "rider" / "legacy-cap.pdf.txt",
            content_hash="legacy-cap-hash",
            content_type="application/pdf",
            effective_start="2023-12-07",
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            metadata_json=json.dumps(
                {
                    "metadata_json": json.dumps(
                        {
                            "local_file": str(regulator_pdf),
                            "family_key_override": "nc-progress-leaf-718",
                            "parse_text_metadata": {
                                "family_code": "718",
                                "matched_terms": ["718", "CAP"],
                            },
                        }
                    )
                }
            ),
        )
    )

    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, page_start, page_end, parser_stage, parser_profile,
                status, confidence, utility, effective_date, charge_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(tmp_path / "historical" / "raw" / "nc" / "progress" / "rider" / "legacy-cap.pdf"),
                1,
                1,
                "historical_bulk",
                "generic_residential",
                "parsed",
                0.5,
                "DEP",
                None,
                0,
                "[]",
                json.dumps({"family_key": "nc-progress-leaf-718", "outcome_quality": "weak"}),
                datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC).isoformat(),
            ),
        )

    rows = repo.list_weak_unbounded_historical_documents(state="NC", company="progress")
    assert len(rows) == 1
    assert rows[0]["discovery_record_id"] == discovery_id
    assert rows[0]["review_action"] == "remine_from_discovery_record"


def test_repository_marks_legacy_raw_with_bounded_regulator_peer_for_manual_lineage_review(
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    regulator_pdf = tmp_path / "historical" / "ncuc" / "e-2-sub-1333" / "bundle.pdf"
    regulator_pdf.parent.mkdir(parents=True, exist_ok=True)
    regulator_pdf.write_bytes(b"%PDF-1.4\n")
    legacy_pdf = r"data\historical\raw\nc\progress\rider\legacy-jaa.pdf"

    discovery_id = repo.upsert_ncuc_discovery_record(
        NcucDiscoveryRecord(
            docket_number="E-2, Sub 1333",
            filing_title="Joint request for tariff revisions",
            local_path=str(regulator_pdf),
            fetch_status=NcucFetchStatus.SUCCESS,
            content_hash="bundle-hash",
            fetched_at="2026-03-27T12:00:00Z",
        )
    )

    legacy_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-602",
            title="Joint Agency Asset Rider JAA",
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER.value,
            kind=DocumentKind.PDF.value,
                canonical_url="https://example.test/legacy-jaa.pdf",
                archived_url="https://example.test/legacy-jaa.pdf#family=nc-progress-leaf-602",
                snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
                local_path=legacy_pdf,
            content_hash="legacy-jaa-hash",
            content_type="application/pdf",
            effective_start="2023-12-07",
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            metadata_json=json.dumps(
                {
                    "metadata_json": json.dumps(
                        {
                            "local_file": str(regulator_pdf),
                            "family_key_override": "nc-progress-leaf-602",
                            "parse_text_metadata": {
                                "family_code": "602",
                                "matched_terms": ["602", "JAA"],
                            },
                        }
                    )
                }
            ),
        )
    )

    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-533",
            title="Large General Service Time-of-Use LGS-TOU (Span 19-28)",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/bundle.pdf",
            archived_url="ncuc://E-2/1124#page=19",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=regulator_pdf,
            content_hash="bundle-hash",
            content_type="application/pdf",
            start_page=19,
            end_page=28,
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, page_start, page_end, parser_stage, parser_profile,
                status, confidence, utility, effective_date, charge_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    legacy_pdf,
                    1,
                    1,
                    "historical_bulk",
                "generic_residential",
                "parsed",
                0.5,
                "DEP",
                None,
                0,
                "[]",
                json.dumps({"family_key": "nc-progress-leaf-602", "outcome_quality": "weak"}),
                datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC).isoformat(),
            ),
        )

    rows = repo.list_weak_unbounded_historical_documents(state="NC", company="progress")
    assert len(rows) == 1
    assert rows[0]["historical_document_id"] == legacy_id
    assert rows[0]["discovery_record_id"] == discovery_id
    assert rows[0]["review_action"] == "manual_lineage_review"


def test_repository_marks_bundle_reference_legacy_raw_for_retirement(
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    regulator_pdf = tmp_path / "historical" / "ncuc" / "e-2-sub-1334" / "bundle.pdf"
    regulator_pdf.parent.mkdir(parents=True, exist_ok=True)
    regulator_pdf.write_bytes(b"%PDF-1.4\n")
    legacy_pdf = r"data\historical\raw\nc\progress\rider\legacy-jaa.pdf"

    discovery_id = repo.upsert_ncuc_discovery_record(
        NcucDiscoveryRecord(
            docket_number="E-2, Sub 1334",
            filing_title="Joint request for tariff revisions",
            local_path=str(regulator_pdf),
            fetch_status=NcucFetchStatus.SUCCESS,
            content_hash="bundle-reference-hash",
            fetched_at="2026-03-27T12:00:00Z",
        )
    )

    legacy_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-602",
            title="Joint Agency Asset Rider JAA",
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/legacy-jaa.pdf",
            archived_url="https://example.test/legacy-jaa.pdf#family=nc-progress-leaf-602",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=legacy_pdf,
            content_hash="legacy-jaa-hash",
            content_type="application/pdf",
            effective_start="2023-12-07",
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            metadata_json=json.dumps(
                {
                    "metadata_json": json.dumps(
                        {
                            "local_file": str(regulator_pdf),
                            "family_key_override": "nc-progress-leaf-602",
                            "parse_text_metadata": {
                                "family_code": "602",
                                "matched_terms": ["602", "JAA"],
                            },
                        }
                    )
                }
            ),
        )
    )

    host_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-533",
            title="Large General Service Time-of-Use LGS-TOU (Span 19-28)",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/bundle.pdf",
            archived_url="ncuc://E-2/1334#page=19",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=str(regulator_pdf),
            content_hash="bundle-reference-hash",
            content_type="application/pdf",
            start_page=19,
            end_page=28,
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        save_span_artifacts(
            conn,
            discovery_record_id=discovery_id,
            source_pdf=str(regulator_pdf),
            file_hash="bundle-reference-hash",
            spans=[
                TariffSpan(
                    start_page=19,
                    end_page=28,
                    doc_type="tariff",
                    confidence=0.88,
                    extracted_leaf_nos={"601", "602", "604", "605", "609", "610", "612"},
                    extracted_schedule_titles={"Large General Service Time-of-Use LGS-TOU"},
                )
            ],
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, page_start, page_end, parser_stage, parser_profile,
                status, confidence, utility, effective_date, charge_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                legacy_pdf,
                1,
                1,
                "historical_bulk",
                "generic_residential",
                "parsed",
                0.5,
                "DEP",
                None,
                0,
                "[]",
                json.dumps({"family_key": "nc-progress-leaf-602", "outcome_quality": "weak"}),
                datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC).isoformat(),
            ),
        )
        conn.commit()

    rows = repo.list_weak_unbounded_historical_documents(state="NC", company="progress")
    assert len(rows) == 1
    assert rows[0]["historical_document_id"] == legacy_id
    assert rows[0]["review_action"] == "retire_bundle_reference_residue"
    overlap = rows[0]["bundle_reference_overlap"]
    assert overlap is not None
    assert overlap["target_leaf"] == "602"
    assert overlap["host_count"] == 1
    assert overlap["hosts"][0]["host_historical_document_id"] == host_id

    bundle_rows = repo.list_bundle_reference_legacy_raw_historical_documents(
        state="NC",
        company="progress",
    )
    assert len(bundle_rows) == 1
    assert bundle_rows[0]["historical_document_id"] == legacy_id


def test_repository_marks_procedural_legacy_raw_for_retirement(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    regulator_pdf = tmp_path / "historical" / "ncuc" / "e-2-sub-1252" / "procedural.pdf"
    regulator_pdf.parent.mkdir(parents=True, exist_ok=True)
    regulator_pdf.write_bytes(b"%PDF-1.4\n")
    legacy_pdf = r"data\historical\raw\nc\progress\rider\legacy-sts.pdf"

    discovery_id = repo.upsert_ncuc_discovery_record(
        NcucDiscoveryRecord(
            docket_number="E-2, Sub 1252",
            filing_title="DSM/EE rider application",
            local_path=str(regulator_pdf),
            fetch_status=NcucFetchStatus.SUCCESS,
            content_hash="procedural-hash",
            fetched_at="2026-03-27T12:00:00Z",
        )
    )

    with repo._connect() as conn:
        save_page_artifacts(
            conn,
            discovery_record_id=discovery_id,
            source_pdf=str(regulator_pdf),
            file_hash="procedural-hash",
            pages=[
                PageEvidence(
                    page_number=1,
                    text_length=300,
                    extracted_schedule_codes=["Management and Energy Efficiency Cost Recovery Rider"],
                    procedural_vocab_density=0.03,
                    tariff_vocab_density=0.01,
                ),
                PageEvidence(
                    page_number=2,
                    text_length=100,
                    has_schedule_heading=True,
                    extracted_schedule_codes=["CERTIFICATE OF SERVICE"],
                    procedural_vocab_density=0.03,
                ),
            ],
        )
        conn.commit()

    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-613",
            title="Storm Securitization Rider",
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/legacy-sts.pdf",
            archived_url="https://example.test/legacy-sts.pdf#family=nc-progress-leaf-613",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=legacy_pdf,
            content_hash="legacy-sts-hash",
            content_type="application/pdf",
            effective_start="2020-09-14",
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            metadata_json=json.dumps(
                {
                    "metadata_json": json.dumps(
                        {
                            "local_file": str(regulator_pdf),
                            "family_key_override": "nc-progress-leaf-613",
                            "parse_text_metadata": {
                                "family_code": "613",
                                "matched_terms": ["STS"],
                            },
                        }
                    )
                }
            ),
        )
    )

    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, page_start, page_end, parser_stage, parser_profile,
                status, confidence, utility, effective_date, charge_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                legacy_pdf,
                1,
                1,
                "historical_bulk",
                "generic_residential",
                "parsed",
                0.5,
                "DEP",
                None,
                0,
                "[]",
                json.dumps({"family_key": "nc-progress-leaf-613", "outcome_quality": "weak"}),
                datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC).isoformat(),
            ),
        )

    rows = repo.list_weak_unbounded_historical_documents(state="NC", company="progress")
    assert len(rows) == 1
    assert rows[0]["discovery_record_id"] == discovery_id
    assert rows[0]["review_action"] == "retire_legacy_raw_attachment"


def test_repository_lists_redundant_legacy_raw_historical_documents(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    regulator_pdf = r"data\historical\ncuc\e-2-sub-1253\esm.pdf"
    legacy_pdf = r"data\historical\raw\nc\progress\rider\legacy-esm.pdf"

    repo.upsert_ncuc_discovery_record(
        NcucDiscoveryRecord(
            docket_number="E-2, Sub 1253",
            filing_title="Rider ESM",
            local_path=regulator_pdf,
            fetch_status=NcucFetchStatus.SUCCESS,
        )
    )

    legacy_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-609",
            title="Rider ESM",
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/legacy-esm.pdf",
            archived_url="https://example.test/legacy-esm.pdf#family=nc-progress-leaf-609",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=legacy_pdf,
            content_hash="legacy-esm-hash",
            content_type="application/pdf",
            effective_start="2020-09-14",
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            metadata_json=json.dumps(
                {
                    "metadata_json": json.dumps(
                        {
                            "local_file": regulator_pdf,
                            "family_key_override": "nc-progress-leaf-609",
                        }
                    )
                }
            ),
        )
    )
    replacement_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-609",
            title="Rider ESM (Span 1-18)",
            state="NC",
            company="progress",
            category=DocumentCategory.RIDER.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/esm.pdf",
            archived_url="ncuc://E-2, Sub 1253/986#page=1",
            snapshot_timestamp=datetime(2020, 9, 14, 0, 0, 0, tzinfo=UTC),
            local_path=regulator_pdf,
            content_hash="regulator-esm-hash",
            content_type="application/pdf",
            effective_start="2020-09-15",
            start_page=1,
            end_page=18,
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, page_start, page_end, parser_stage, parser_profile,
                status, confidence, utility, effective_date, charge_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                legacy_pdf,
                1,
                1,
                "historical_bulk",
                "generic_residential",
                "parsed",
                0.5,
                "DEP",
                None,
                0,
                "[]",
                json.dumps({"family_key": "nc-progress-leaf-609", "outcome_quality": "weak"}),
                datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC).isoformat(),
            ),
        )

    rows = repo.list_redundant_legacy_raw_historical_documents(state="NC", company="progress")
    assert len(rows) == 1
    assert rows[0]["historical_document_id"] == legacy_id
    assert rows[0]["discovery_record_id"] is not None
    assert rows[0]["replacement_count"] == 1
    assert rows[0]["replacement_ids"] == [replacement_id]


def test_repository_lists_placeholder_heading_residue_historical_documents(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    source_pdf = r"data\raw\historical\ncuc\e-7\duke-s-rate-schedule.pdf"

    residue_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-doc-TYPEOFSERVICE",
            title="TYPE OF SERVICE (Span 59-61)",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/type-of-service.pdf",
            archived_url="ncuc://E-7#page=59",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=source_pdf,
            content_hash="type-service-hash",
            content_type="application/pdf",
            start_page=59,
            end_page=61,
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
        )
    )
    host_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-schedule-MP",
            title="SCHEDULE MP (NC) MULTIPLE PREMISES SERVICE (Span 63-63)",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/mp.pdf",
            archived_url="ncuc://E-7#page=63",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=source_pdf,
            content_hash="mp-hash",
            content_type="application/pdf",
            start_page=63,
            end_page=63,
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
        )
    )

    rows = repo.list_placeholder_heading_residue_historical_documents(
        state="NC",
        company="carolinas",
    )

    assert len(rows) == 1
    assert rows[0]["historical_document_id"] == residue_id
    assert rows[0]["review_action"] == "retire_placeholder_heading_residue"
    assert rows[0]["neighbor_count"] == 1
    assert rows[0]["neighbors"][0]["historical_document_id"] == host_id


def test_repository_retires_historical_document_cascade(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    source_pdf = r"data\raw\historical\ncuc\e-7\duke-s-rate-schedule.pdf"
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-doc-EFFECTIVEFORSERVICE",
            title="Effective for service (Span 42-45)",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/effective.pdf",
            archived_url="ncuc://E-7#page=42",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=source_pdf,
            content_hash="effective-hash",
            content_type="application/pdf",
            start_page=42,
            end_page=45,
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO tariff_versions (
                family_key, document_id, historical_document_id, effective_start,
                effective_end, revision_label, supersedes_label, source_type,
                confidence_score, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "nc-carolinas-doc-EFFECTIVEFORSERVICE",
                None,
                historical_id,
                "2010-01-01",
                None,
                None,
                None,
                "historical_extract",
                0.8,
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        version_id = int(
            conn.execute("SELECT id FROM tariff_versions ORDER BY id DESC LIMIT 1").fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label, rate_value,
                rate_unit, source_snippet, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                "nc-carolinas-doc-EFFECTIVEFORSERVICE",
                "fixed",
                "Placeholder",
                1.0,
                "$",
                "placeholder",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_processing_runs (
                historical_document_id, source_pdf, family_key, content_hash, parser_stage,
                parser_profile, parser_version, processing_mode, status, outcome_quality,
                charge_count, review_flags_json, metadata_json, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                source_pdf,
                "nc-carolinas-doc-EFFECTIVEFORSERVICE",
                "effective-hash",
                "historical_bulk_extraction",
                "generic_residential",
                "historical_bulk_v2",
                "manual",
                "parsed",
                "weak",
                0,
                "[]",
                "{}",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_reprocess_queue (
                historical_document_id, source_pdf, family_key, priority, queue_reason,
                requested_by, status, metadata_json, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                source_pdf,
                "nc-carolinas-doc-EFFECTIVEFORSERVICE",
                95,
                "test",
                "test-suite",
                "pending",
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, page_start, page_end, parser_stage, parser_profile, status,
                confidence, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_pdf,
                42,
                45,
                "historical_bulk_extraction",
                "generic_residential",
                "empty",
                0.1,
                0,
                "[]",
                json.dumps({"family_key": "nc-carolinas-doc-EFFECTIVEFORSERVICE"}),
                datetime.now(UTC).isoformat(),
            ),
        )
        parse_attempt_id = int(
            conn.execute("SELECT id FROM parse_attempt_logs ORDER BY id DESC LIMIT 1").fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, page_start, page_end, parser_stage,
                parser_profile, review_source, outcome, correction_count,
                notes_json, corrections_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parse_attempt_id,
                source_pdf,
                42,
                45,
                "historical_bulk_extraction",
                "generic_residential",
                "rule",
                "needs_review",
                0,
                "{}",
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO document_fingerprints (
                source_pdf, page_start, page_end, text_length, line_count,
                numeric_line_count, review_flags_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_pdf,
                42,
                45,
                100,
                10,
                2,
                "[]",
                json.dumps({"family_key": "nc-carolinas-doc-EFFECTIVEFORSERVICE"}),
                datetime.now(UTC).isoformat(),
            ),
        )

    assert repo.retire_historical_document(historical_id) is True

    with repo._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_documents WHERE id = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tariff_versions WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_processing_runs WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_reprocess_queue WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM parse_attempt_logs
            WHERE source_pdf = ? AND page_start = 42 AND page_end = 45
            """,
            (source_pdf,),
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM parse_review_outcomes
            WHERE source_pdf = ? AND page_start = 42 AND page_end = 45
            """,
            (source_pdf,),
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM document_fingerprints
            WHERE source_pdf = ? AND page_start = 42 AND page_end = 45
            """,
            (source_pdf,),
        ).fetchone()[0] == 0


def test_repository_repairs_stale_current_document_snapshot(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    stale_pdf = Path(r"data\raw\nc\carolinas\other\cip-other.pdf")
    anchor_pdf = Path(r"data\raw\nc\carolinas\rate\schedule-pp.pdf")

    stale_doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Customer Information Program Overview",
            source_page_url="https://example.test/source/cip",
            document_url="https://example.test/cip.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.OTHER,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(stale_pdf),
            content_hash="hash-cip-stale",
            status_code=200,
        )
    )
    anchor_doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Parallel Power Service Schedule PP",
            source_page_url="https://example.test/source/pp",
            document_url="https://example.test/schedule-pp.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            effective_date="2021-10-11",
            retrieval_timestamp=utc_now(),
            local_path=str(anchor_pdf),
            content_hash="hash-pp-anchor",
            status_code=200,
        )
    )
    with repo._connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET tariff_identifier = ?, schedule_code = ?
            WHERE id = ?
            """,
            ("schedule-PP", "PP", anchor_doc_id),
        )

    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-schedule-PP",
            state="NC",
            company="carolinas",
            tariff_identifier="schedule-PP",
            schedule_code="PP",
            family_type="rate_schedule",
            title="Parallel Power Service Schedule PP",
            current_document_id=anchor_doc_id,
        )
    )

    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=stale_doc_id,
            family_key="nc-carolinas-schedule-PP",
            title="Customer Information Program Overview",
            state="NC",
            company="carolinas",
            category=DocumentCategory.OTHER.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/cip.pdf",
            archived_url=f"documents/{stale_doc_id}",
            snapshot_timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            local_path=stale_pdf,
            content_hash="hash-cip-stale",
            content_type="application/pdf",
            effective_start="2021-01-01",
            retrieved_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        version_cursor = conn.execute(
            """
            INSERT INTO tariff_versions (
                family_key, historical_document_id, effective_start,
                source_type, confidence_score, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "nc-carolinas-schedule-PP",
                historical_id,
                "2021-01-01",
                "ncuc_mined",
                0.7,
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        version_id = int(version_cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label,
                rate_value, rate_unit, confidence_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                "nc-carolinas-schedule-PP",
                "fixed",
                "Customer Charge",
                10.0,
                "$/month",
                0.6,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_processing_runs (
                historical_document_id, source_pdf, family_key, content_hash,
                parser_stage, parser_profile, parser_version, processing_mode,
                status, outcome_quality, charge_count, review_flags_json, metadata_json,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                str(stale_pdf),
                "nc-carolinas-schedule-PP",
                "hash-cip-stale",
                "historical_bulk",
                "generic_residential",
                "v1",
                "manual",
                "parsed",
                "weak",
                1,
                "[]",
                json.dumps({"family_key": "nc-carolinas-schedule-PP"}),
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_reprocess_queue (
                historical_document_id, source_pdf, family_key, priority,
                queue_reason, requested_by, status, metadata_json, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                str(stale_pdf),
                "nc-carolinas-schedule-PP",
                50,
                "test",
                "system",
                "pending",
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, parser_stage, parser_profile, status, confidence,
                charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(stale_pdf),
                "historical_bulk",
                "generic_residential",
                "parsed",
                0.4,
                1,
                "[]",
                json.dumps({"family_key": "nc-carolinas-schedule-PP", "outcome_quality": "weak"}),
                datetime.now(UTC).isoformat(),
            ),
        )
        parse_attempt_id = int(
            conn.execute("SELECT id FROM parse_attempt_logs ORDER BY id DESC LIMIT 1").fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, review_source, outcome, correction_count,
                notes_json, corrections_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parse_attempt_id,
                str(stale_pdf),
                "rule",
                "needs_review",
                0,
                json.dumps({"family_key": "nc-carolinas-schedule-PP"}),
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO document_fingerprints (
                source_pdf, text_length, line_count, numeric_line_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(stale_pdf),
                80,
                8,
                1,
                "[]",
                json.dumps({"family_key": "nc-carolinas-schedule-PP"}),
                datetime.now(UTC).isoformat(),
            ),
        )

    rows = repo.list_weak_unbounded_historical_documents(state="NC", company="carolinas")
    assert len(rows) == 1
    assert rows[0]["historical_document_id"] == historical_id
    assert rows[0]["review_action"] == "repair_current_document_snapshot"
    assert rows[0]["stale_current_snapshot"] is not None
    assert rows[0]["stale_current_snapshot"]["anchor_document_id"] == anchor_doc_id

    repaired = repo.repair_historical_current_document_snapshot(
        historical_id,
        requested_by="test-suite",
        queue_priority=97,
    )

    assert repaired is not None
    assert repaired.current_document_id == anchor_doc_id
    assert repaired.title == "Parallel Power Service Schedule PP"
    assert repaired.category == DocumentCategory.RATE.value
    assert repaired.archived_url == f"documents/{anchor_doc_id}"
    assert repaired.local_path == anchor_pdf
    assert repaired.content_hash == "hash-pp-anchor"
    assert repaired.effective_start == "2021-10-11"

    with repo._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tariff_versions WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM tariff_charges
            WHERE version_id = ?
            """,
            (version_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_processing_runs WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM parse_attempt_logs WHERE source_pdf = ?",
            (str(stale_pdf),),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM parse_review_outcomes WHERE source_pdf = ?",
            (str(stale_pdf),),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM document_fingerprints WHERE source_pdf = ?",
            (str(stale_pdf),),
        ).fetchone()[0] == 0
        queue_rows = conn.execute(
            """
            SELECT source_pdf, queue_reason, requested_by, priority, metadata_json
            FROM historical_reprocess_queue
            WHERE historical_document_id = ?
            """,
            (historical_id,),
        ).fetchall()
        assert len(queue_rows) == 1
        queued = queue_rows[0]
        assert queued["source_pdf"] == str(anchor_pdf)
        assert queued["queue_reason"] == "repair_current_document_snapshot"
        assert queued["requested_by"] == "test-suite"
        assert queued["priority"] == 97
        metadata = json.loads(queued["metadata_json"])
        assert metadata["stale_document_id"] == stale_doc_id
        assert metadata["anchor_document_id"] == anchor_doc_id
        assert "stale_document_category_other" in metadata["repair_reasons"]


def test_repository_rebinds_historical_page_range_and_requeues(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-601",
            title="Annual Billing Adjustments",
            state="NC",
            company="progress",
            category="rider",
            kind="pdf",
            canonical_url="https://example.test/ba.pdf",
            archived_url="https://archive.test/ba",
            snapshot_timestamp=datetime(2026, 4, 16, tzinfo=UTC),
            local_path=pdf_path,
            content_hash="hash-ba",
            content_type="application/pdf",
            start_page=2,
            end_page=18,
            effective_start="2020-09-01",
            retrieved_at=datetime(2026, 4, 16, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        version_id = int(
            conn.execute(
                """
                INSERT INTO tariff_versions (
                    family_key, historical_document_id, effective_start,
                    source_type, confidence_score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "nc-progress-leaf-601",
                    historical_id,
                    "2020-09-01",
                    "regulator",
                    0.8,
                    datetime.now(UTC).isoformat(),
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label,
                rate_value, rate_unit, confidence_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                "nc-progress-leaf-601",
                "fixed",
                "Charge",
                1.0,
                "$/month",
                0.9,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_processing_runs (
                historical_document_id, source_pdf, family_key, content_hash,
                parser_stage, parser_profile, parser_version, processing_mode,
                status, outcome_quality, charge_count, review_flags_json, metadata_json,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                str(pdf_path),
                "nc-progress-leaf-601",
                "hash-ba",
                "historical_bulk",
                "progress_billing_adjustments",
                "v1",
                "manual",
                "parsed",
                "strong",
                1,
                "[]",
                "{}",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        attempt_id = int(
            conn.execute(
                """
                INSERT INTO parse_attempt_logs (
                    source_pdf, page_start, page_end, parser_stage, parser_profile,
                    status, confidence, charge_count, review_flags_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(pdf_path),
                    2,
                    18,
                    "historical_bulk",
                    "progress_billing_adjustments",
                    "parsed",
                    0.9,
                    1,
                    "[]",
                    json.dumps({"historical_document_id": historical_id}, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, page_start, page_end, parser_stage,
                parser_profile, review_source, outcome, correction_count,
                notes_json, corrections_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                str(pdf_path),
                2,
                18,
                "historical_bulk",
                "progress_billing_adjustments",
                "rule",
                "needs_review",
                0,
                "{}",
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_reprocess_queue (
                historical_document_id, source_pdf, family_key, priority,
                queue_reason, requested_by, status, metadata_json, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                str(pdf_path),
                "nc-progress-leaf-601",
                50,
                "test",
                "system",
                "pending",
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )

    rebound = repo.rebind_historical_page_range(
        historical_id,
        start_page=92,
        end_page=93,
        requeue=True,
        requested_by="test-suite",
        queue_priority=88,
    )

    assert rebound is not None
    assert rebound.start_page == 92
    assert rebound.end_page == 93

    with repo._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tariff_versions WHERE id = ?",
            (version_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_processing_runs WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM parse_attempt_logs WHERE json_extract(metadata_json, '$.historical_document_id') = ?",
            (historical_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM parse_review_outcomes WHERE parse_attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0] == 0
        queue_row = conn.execute(
            """
            SELECT queue_reason, requested_by, priority, metadata_json
            FROM historical_reprocess_queue
            WHERE historical_document_id = ?
            """,
            (historical_id,),
        ).fetchone()
        assert queue_row is not None
        assert queue_row["queue_reason"] == "page_range_rebind"
        assert queue_row["requested_by"] == "test-suite"
        assert queue_row["priority"] == 88
        metadata = json.loads(queue_row["metadata_json"])
        assert metadata["old_start_page"] == 2
        assert metadata["old_end_page"] == 18
        assert metadata["new_start_page"] == 92
        assert metadata["new_end_page"] == 93


def test_repository_clears_redline_fingerprint_for_slice(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-carolinas-rider-STS",
            title="Storm Securitization Rider",
            state="NC",
            company="carolinas",
            category="rider",
            kind="pdf",
            canonical_url="https://example.test/sts.pdf",
            archived_url="https://archive.test/sts",
            snapshot_timestamp=datetime(2026, 4, 16, tzinfo=UTC),
            local_path=pdf_path,
            content_hash="hash-sts",
            content_type="application/pdf",
            start_page=2,
            end_page=4,
            effective_start="2025-01-01",
            retrieved_at=datetime(2026, 4, 16, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO document_fingerprints (
                source_pdf, page_start, page_end, is_redline_candidate, redline_confidence,
                redline_signals_json, red_text_samples_json, strikethrough_samples_json,
                red_is_index_only, review_flags_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(pdf_path),
                2,
                4,
                1,
                0.85,
                json.dumps(["red_text_in_body=4"]),
                json.dumps(["Tenth Eleventh"]),
                json.dumps([]),
                0,
                json.dumps(["redline_candidate"]),
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )

    result = repo.clear_redline_fingerprint_for_historical_document(historical_id)

    assert result is not None
    assert result["updated_count"] == 1
    with repo._connect() as conn:
        row = conn.execute(
            """
            SELECT is_redline_candidate, redline_confidence, redline_signals_json,
                   red_text_samples_json, review_flags_json
            FROM document_fingerprints
            WHERE source_pdf = ? AND page_start = 2 AND page_end = 4
            """,
            (str(pdf_path),),
        ).fetchone()
        assert row["is_redline_candidate"] == 0
        assert row["redline_confidence"] == 0.0
        assert json.loads(row["redline_signals_json"]) == []
        assert json.loads(row["red_text_samples_json"]) == []
        assert "manual_redline_clear" in json.loads(row["review_flags_json"])


def test_repository_retires_tariff_version_only(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-704",
            title="RSSEE",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://example.test/704.pdf",
            archived_url="https://archive.test/704",
            snapshot_timestamp=datetime(2026, 4, 16, tzinfo=UTC),
            local_path=tmp_path / "704.pdf",
            content_hash="hash-704",
            content_type="application/pdf",
            effective_start="2023-10-01",
            retrieved_at=datetime(2026, 4, 16, tzinfo=UTC),
        )
    )

    with repo._connect() as conn:
        version_id = int(
            conn.execute(
                """
                INSERT INTO tariff_versions (
                    family_key, historical_document_id, effective_start,
                    source_type, confidence_score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "nc-progress-leaf-704",
                    historical_id,
                    "2023-10-01",
                    "regulator",
                    0.8,
                    datetime.now(UTC).isoformat(),
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label,
                rate_value, rate_unit, confidence_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                "nc-progress-leaf-704",
                "fixed",
                "Charge",
                1.0,
                "$/month",
                0.9,
                datetime.now(UTC).isoformat(),
            ),
        )

    result = repo.retire_tariff_version(version_id)

    assert result is not None
    assert result["deleted_charge_count"] == 1
    with repo._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tariff_versions WHERE id = ?",
            (version_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_documents WHERE id = ?",
            (historical_id,),
        ).fetchone()[0] == 1


def test_repository_rejects_mismatched_current_document_attachment(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Progress Program",
            source_page_url="https://example.test/source",
            document_url="https://example.test/progress-program.pdf",
            state="NC",
            company="progress",
            category=DocumentCategory.PROGRAM,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(tmp_path / "progress-program.pdf"),
            content_hash="hash-progress-program",
            status_code=200,
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            state="NC",
            company="carolinas",
            tariff_identifier="program-SMARTENERGYNOWPROGRAM",
            schedule_code="SMARTENERGYNOWPROGRAM",
            family_type="program",
            title="SMART ENERGY NOW PROGRAM (NC)",
            notes="Promoted from provisional historical family.",
        )
    )

    with pytest.raises(ValueError, match="company mismatch"):
        repo.attach_current_document_to_family(
            "nc-carolinas-program-SMARTENERGYNOWPROGRAM",
            document_id=doc_id,
        )


def test_repository_lists_current_anchor_mismatches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "dep-r-toud.pdf"
    current_pdf.write_bytes(b"%PDF-1.4")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)",
            source_page_url="https://example.test/source",
            document_url="https://example.test/r-toud.pdf",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="hash-r-toud-current",
            status_code=200,
        )
    )
    with repo._connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET tariff_identifier = ?, schedule_code = ?
            WHERE id = ?
            """,
            ("leaf-501", "R_TOUD", doc_id),
        )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-501",
            state="NC",
            company="progress",
            tariff_identifier="leaf-501",
            schedule_code="FUEL",
            family_type="rate_schedule",
            title="Fuel Charge Adjustment",
            current_document_id=doc_id,
        )
    )

    monkeypatch.setattr(
        "duke_rates.db.repository.mine_document_pages",
        lambda _file_path, max_pages=None: [
            PageEvidence(
                page_number=1,
                text_length=80,
                extracted_leaf_nos=["501"],
                extracted_schedule_codes=["SCHEDULE R-TOUD"],
                has_schedule_heading=True,
            )
        ],
    )

    rows = repo.list_current_anchor_mismatches(state="NC", company="progress")

    assert len(rows) == 1
    assert rows[0]["family_key"] == "nc-progress-leaf-501"
    assert rows[0]["review_action"] == "sync_family_metadata_from_current_document"
    assert "mined_schedule_code_mismatch" in rows[0]["reasons"]
    assert rows[0]["candidate_leaf_nos"] == ["501"]


def test_repository_ignores_consistent_current_anchor_family(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "dep-res.pdf"
    current_pdf.write_bytes(b"%PDF-1.4")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Schedule RES",
            source_page_url="https://example.test/source",
            document_url="https://example.test/res.pdf",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="hash-res-current",
            status_code=200,
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-500",
            state="NC",
            company="progress",
            tariff_identifier="leaf-500",
            schedule_code="RES",
            family_type="rate_schedule",
            title="Residential Service Schedule RES",
            current_document_id=doc_id,
        )
    )

    monkeypatch.setattr(
        "duke_rates.db.repository.mine_document_pages",
        lambda _file_path, max_pages=None: [
            PageEvidence(
                page_number=1,
                text_length=80,
                extracted_leaf_nos=["500"],
                extracted_schedule_codes=["SCHEDULE RES"],
                has_schedule_heading=True,
            )
        ],
    )

    assert repo.list_current_anchor_mismatches(state="NC", company="progress") == []


def test_repository_syncs_family_metadata_from_current_document(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "dep-r-toud.pdf"
    current_pdf.write_bytes(b"%PDF-1.4")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)",
            source_page_url="https://example.test/source",
            document_url="https://example.test/r-toud.pdf",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="hash-r-toud-current",
            status_code=200,
        )
    )
    with repo._connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET tariff_identifier = ?, schedule_code = ?
            WHERE id = ?
            """,
            ("leaf-501", "R_TOUD", doc_id),
        )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-501",
            state="NC",
            company="progress",
            tariff_identifier="leaf-501",
            schedule_code="FUEL",
            family_type="rate_schedule",
            title="Fuel Charge Adjustment",
            aliases=["Fuel Charge Adjustment"],
            current_document_id=doc_id,
        )
    )

    synced = repo.sync_family_metadata_from_current_document("nc-progress-leaf-501")

    assert synced is not None
    assert synced.schedule_code == "R_TOUD"
    assert synced.tariff_identifier == "leaf-501"
    assert synced.title == "Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)"
    assert "Fuel Charge Adjustment" in synced.aliases


def test_repository_marks_current_anchor_mismatch_as_historical_migration_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = Repository(tmp_path / "test.db")
    current_pdf = tmp_path / "dep-r-toud.pdf"
    current_pdf.write_bytes(b"%PDF-1.4")
    doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)",
            source_page_url="https://example.test/source",
            document_url="https://example.test/r-toud.pdf",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(current_pdf),
            content_hash="hash-r-toud-current-2",
            status_code=200,
        )
    )
    with repo._connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET tariff_identifier = ?, schedule_code = ?
            WHERE id = ?
            """,
            ("leaf-501", "R_TOUD", doc_id),
        )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-501",
            state="NC",
            company="progress",
            tariff_identifier="leaf-501",
            schedule_code="FUEL",
            family_type="rate_schedule",
            title="Fuel Charge Adjustment",
            current_document_id=doc_id,
        )
    )
    repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-501",
            title="Fuel Charge Adjustment",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://example.test/fuel.pdf",
            archived_url="https://web.archive.org/web/20180101000000/https://example.test/fuel.pdf",
            snapshot_timestamp=datetime(2018, 1, 1, tzinfo=UTC),
            local_path=tmp_path / "fuel-historical.pdf",
            content_hash="hash-fuel-historical",
            content_type="application/pdf",
            direct_status_code=200,
            direct_downloadable=True,
            leaf_no="501",
            retrieved_at=datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC),
        )
    )

    monkeypatch.setattr(
        "duke_rates.db.repository.mine_document_pages",
        lambda _file_path, max_pages=None: [
            PageEvidence(
                page_number=1,
                text_length=80,
                extracted_leaf_nos=["501"],
                extracted_schedule_codes=["SCHEDULE R-TOUD"],
                has_schedule_heading=True,
            )
        ],
    )

    rows = repo.list_current_anchor_mismatches(state="NC", company="progress")

    assert len(rows) == 1
    assert rows[0]["review_action"] == "review_historical_family_migration"
    assert "historical_title_conflict" in rows[0]["reasons"]


def test_repository_migrates_historical_family_lineage(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    current_doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)",
            source_page_url="https://example.test/source",
            document_url="https://example.test/r-toud.pdf",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(tmp_path / "r-toud.pdf"),
            content_hash="hash-r-toud",
            status_code=200,
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-leaf-501",
            state="NC",
            company="progress",
            tariff_identifier="leaf-501",
            schedule_code="FUEL",
            family_type="rate_schedule",
            title="Fuel Charge Adjustment",
            current_document_id=current_doc_id,
        )
    )
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-leaf-501",
            title="Fuel Charge Adjustment",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://example.test/fuel.pdf",
            archived_url="https://web.archive.org/web/20180101000000/https://example.test/fuel.pdf",
            snapshot_timestamp=datetime(2018, 1, 1, tzinfo=UTC),
            local_path=tmp_path / "fuel-historical.pdf",
            content_hash="hash-fuel-historical",
            content_type="application/pdf",
            direct_status_code=200,
            direct_downloadable=True,
            leaf_no="501",
            retrieved_at=datetime(2026, 3, 26, 12, 0, 0, tzinfo=UTC),
            metadata_json='{"family_key":"nc-progress-leaf-501"}',
        )
    )
    with repo._connect() as conn:
        version_cursor = conn.execute(
            """
            INSERT INTO tariff_versions (
                family_key, historical_document_id, effective_start, source_type,
                confidence_score, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "nc-progress-leaf-501",
                historical_id,
                "2018-01-01",
                "ncuc_mined",
                0.9,
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        version_id = int(version_cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label,
                rate_value, rate_unit, confidence_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                "nc-progress-leaf-501",
                "energy_block",
                "Fuel",
                0.02,
                "$/kWh",
                0.8,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_processing_runs (
                historical_document_id, source_pdf, family_key, content_hash,
                parser_stage, parser_profile, parser_version, processing_mode,
                status, outcome_quality, charge_count, review_flags_json, metadata_json,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                str(tmp_path / "fuel-historical.pdf"),
                "nc-progress-leaf-501",
                "hash-fuel-historical",
                "extract",
                "generic_residential",
                "v1",
                "manual",
                "parsed",
                "weak",
                1,
                "[]",
                '{"family_key":"nc-progress-leaf-501"}',
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO historical_reprocess_queue (
                historical_document_id, source_pdf, family_key, priority,
                queue_reason, requested_by, status, metadata_json, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historical_id,
                str(tmp_path / "fuel-historical.pdf"),
                "nc-progress-leaf-501",
                50,
                "test",
                "system",
                "pending",
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, parser_stage, parser_profile, status, confidence,
                charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(tmp_path / "fuel-historical.pdf"),
                "extract",
                "generic_residential",
                "parsed",
                0.4,
                1,
                "[]",
                '{"family_key":"nc-progress-leaf-501"}',
                datetime.now(UTC).isoformat(),
            ),
        )
        parse_attempt_id = int(
            conn.execute("SELECT id FROM parse_attempt_logs ORDER BY id DESC LIMIT 1").fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO parse_review_outcomes (
                parse_attempt_id, source_pdf, review_source, outcome, correction_count,
                notes_json, corrections_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parse_attempt_id,
                str(tmp_path / "fuel-historical.pdf"),
                "rule",
                "needs_review",
                0,
                '{"family_key":"nc-progress-leaf-501"}',
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO document_fingerprints (
                source_pdf, text_length, line_count, numeric_line_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(tmp_path / "fuel-historical.pdf"),
                100,
                10,
                2,
                "[]",
                '{"family_key":"nc-progress-leaf-501"}',
                datetime.now(UTC).isoformat(),
            ),
        )

    migrated = repo.migrate_historical_family_lineage(
        "nc-progress-leaf-501",
        "nc-progress-doc-FUELCHARGEADJUSTMENT",
        historical_document_ids=[historical_id],
        title="Fuel Charge Adjustment",
        schedule_code="FUEL",
        family_type="rate_schedule",
        aliases=["Fuel Charge Adjustment"],
        notes="Migrated historical fuel lineage from leaf-501.",
    )

    assert migrated is not None
    assert migrated.current_document_id is None
    assert "Fuel Charge Adjustment" in migrated.aliases

    moved_doc = repo.get_historical_document(historical_id)
    assert moved_doc is not None
    assert moved_doc.family_key == "nc-progress-doc-FUELCHARGEADJUSTMENT"
    assert moved_doc.current_document_id is None

    with repo._connect() as conn:
        version_row = conn.execute(
            "SELECT family_key FROM tariff_versions WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()
        assert version_row["family_key"] == "nc-progress-doc-FUELCHARGEADJUSTMENT"
        charge_row = conn.execute(
            "SELECT family_key FROM tariff_charges WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        assert charge_row["family_key"] == "nc-progress-doc-FUELCHARGEADJUSTMENT"
        run_row = conn.execute(
            "SELECT family_key FROM historical_processing_runs WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()
        assert run_row["family_key"] == "nc-progress-doc-FUELCHARGEADJUSTMENT"
        queue_row = conn.execute(
            "SELECT family_key FROM historical_reprocess_queue WHERE historical_document_id = ?",
            (historical_id,),
        ).fetchone()
        assert queue_row["family_key"] == "nc-progress-doc-FUELCHARGEADJUSTMENT"
        log_row = conn.execute(
            "SELECT json_extract(metadata_json, '$.family_key') AS family_key FROM parse_attempt_logs"
        ).fetchone()
        assert log_row["family_key"] == "nc-progress-doc-FUELCHARGEADJUSTMENT"
        fp_row = conn.execute(
            "SELECT json_extract(metadata_json, '$.family_key') AS family_key FROM document_fingerprints"
        ).fetchone()
        assert fp_row["family_key"] == "nc-progress-doc-FUELCHARGEADJUSTMENT"
        review_row = conn.execute(
            "SELECT json_extract(notes_json, '$.family_key') AS family_key FROM parse_review_outcomes"
        ).fetchone()
        assert review_row["family_key"] == "nc-progress-doc-FUELCHARGEADJUSTMENT"


def test_repository_save_historical_parse_result(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    record = HistoricalDocumentRecord(
        current_document_id=7,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
        title="Residential Service Schedule RES",
        state="NC",
        company="progress",
        category=DocumentCategory.RATE.value,
        kind=DocumentKind.PDF.value,
        canonical_url="https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=old",
        archived_url="https://web.archive.org/web/20241118190307/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=old",
        snapshot_timestamp=datetime(2024, 11, 18, 19, 3, 7, tzinfo=UTC),
        local_path=tmp_path / "historical/raw/nc/progress/rate/res.pdf",
        raw_text_path=tmp_path / "historical/raw/nc/progress/rate/res.pdf.txt",
        content_hash="deadbeef",
        content_type="application/pdf",
        retrieved_at=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
    )
    historical_id = repo.upsert_historical_document(record)

    result = DocumentParseResult(
        document_id=historical_id,
        parser_name="schedule_parser",
        status=ParseStatus.PARSED,
        schedule=RateScheduleData(
            tariff_id="nc_progress_res_2024-10-01",
            utility="Duke Energy",
            state="NC",
            company="progress",
            schedule_code="RES",
            schedule_title="Residential Service",
            customer_class="residential",
            fixed_charges=[FixedCharge(label="Basic customer charge", amount=14.0, unit="month")],
        ),
    )

    repo.save_historical_parse_result(historical_id, result)
    stored = repo.get_historical_document(historical_id)

    assert stored is not None
    assert stored.parsed_result_json is not None
    parsed = DocumentParseResult.model_validate_json(stored.parsed_result_json)
    assert parsed.schedule is not None
    assert parsed.schedule.schedule_code == "RES"


def test_repository_canonicalize_historical_family_key_updates_ancillary_tables(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE",
            state="NC",
            company="carolinas",
            family_type="rate_schedule",
            schedule_code="SG",
            title="Schedule SG - Small General Service",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-schedule-SGS",
            state="NC",
            company="carolinas",
            family_type="rate_schedule",
            schedule_code="SGS",
            title="SGS",
            current_document_id=26,
        )
    )
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key="nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE",
            title="Schedule SG - Small General Service",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/sgs.pdf",
            archived_url="https://archive.test/sgs.pdf",
            snapshot_timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            local_path=tmp_path / "sgs.pdf",
            content_hash="sgs-hash",
            retrieved_at=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        )
    )
    version_id = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE",
            historical_document_id=historical_id,
            effective_start="2017-11-01",
            source_type="historical_document",
        )
    )
    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label, rate_value, rate_unit, confidence_score, created_at
            ) VALUES (?, ?, 'fixed', 'Customer Charge', 10.0, '$/month', 0.9, ?)
            """,
            (version_id, "nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE", datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO historical_leads (
                family_key, target_title, family_type, category, source_class, provenance_class,
                extraction_method, confidence_score, disposition, score_notes_json, notes_json, created_at
            ) VALUES (?, 'Schedule SG - Small General Service', 'rate_schedule', 'schedule', 'regulator', 'historical',
                      'manual', 0.9, 'new', '{}', '{}', ?)
            """,
            ("nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE", datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO candidate_url_variants (
                family_key, variant_url, hostname, path_family, heuristic, notes_json, created_at
            ) VALUES (?, 'https://example.test/sgs.pdf', 'example.test', '/sgs', 'manual', '{}', ?)
            """,
            ("nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE", datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO historical_search_packs (
                family_key, target_title, family_type, payload_json, notes_json, created_at, updated_at
            ) VALUES (?, 'Schedule SG - Small General Service', 'rate_schedule', '{}', '{}', ?, ?)
            """,
            (
                "nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO regulatory_docket_leads (
                family_key, docket_number, utility, referenced_codes_json, evidence_source,
                evidence_source_type, notes_json, created_at
            ) VALUES (?, 'E-7, Sub 1032', 'DEC', '[]', 'manual', 'historical', '{}', ?)
            """,
            ("nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE", datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO evidence_anchors (
                family_key, anchor_type, anchor_value, source_type, notes_json, created_at
            ) VALUES (?, 'schedule_code', 'SG', 'manual', '{}', ?)
            """,
            ("nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE", datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO ncuc_discovery_records (
                utility, filing_classification, referenced_schedule_codes_json, referenced_rider_codes_json,
                referenced_leaf_nos_json, family_keys_json, acquisition_method, fetch_status, created_at
            ) VALUES (
                'Duke Energy Carolinas', 'tariff_sheets', '[]', '[]', '[]', ?, 'manual_seed', 'success', ?
            )
            """,
            (
                json.dumps(["nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE"]),
                datetime.now(UTC).isoformat(),
            ),
        )

    result = repo.canonicalize_historical_family_key(
        "nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE",
        "nc-carolinas-schedule-SGS",
        historical_document_ids=[historical_id],
    )

    assert result is not None
    assert result["source_family_pruned"] is True

    moved_doc = repo.get_historical_document(historical_id)
    assert moved_doc is not None
    assert moved_doc.family_key == "nc-carolinas-schedule-SGS"
    assert repo.get_tariff_family("nc-carolinas-doc-SCHEDULESGSMALLGENERALSERVICE") is None

    with repo._connect() as conn:
        assert conn.execute(
            "SELECT family_key FROM tariff_versions WHERE id = ?",
            (version_id,),
        ).fetchone()["family_key"] == "nc-carolinas-schedule-SGS"
        assert conn.execute(
            "SELECT family_key FROM tariff_charges WHERE version_id = ?",
            (version_id,),
        ).fetchone()["family_key"] == "nc-carolinas-schedule-SGS"
        assert conn.execute(
            "SELECT family_key FROM historical_leads"
        ).fetchone()["family_key"] == "nc-carolinas-schedule-SGS"
        assert conn.execute(
            "SELECT family_key FROM candidate_url_variants"
        ).fetchone()["family_key"] == "nc-carolinas-schedule-SGS"
        assert conn.execute(
            "SELECT family_key FROM historical_search_packs"
        ).fetchone()["family_key"] == "nc-carolinas-schedule-SGS"
        assert conn.execute(
            "SELECT family_key FROM regulatory_docket_leads"
        ).fetchone()["family_key"] == "nc-carolinas-schedule-SGS"
        assert conn.execute(
            "SELECT family_key FROM evidence_anchors"
        ).fetchone()["family_key"] == "nc-carolinas-schedule-SGS"
        discovery = conn.execute(
            "SELECT family_keys_json FROM ncuc_discovery_records"
        ).fetchone()["family_keys_json"]
        assert json.loads(discovery) == ["nc-carolinas-schedule-SGS"]


def test_repository_canonicalize_historical_family_key_moves_current_anchor_and_current_version(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    current_doc_id = repo.upsert_document(
        DiscoveryRecord(
            title="FCAR",
            source_page_url="https://example.test/rates",
            document_url="https://example.test/fcar.pdf",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RATE,
            kind=DocumentKind.PDF,
            retrieval_timestamp=utc_now(),
            local_path=str(tmp_path / "fcar.pdf"),
            content_hash="fcar-current",
            status_code=200,
            tariff_identifier="doc-FUELCOSTADJRDR",
            schedule_code="FUELCOSTADJRDR",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-carolinas-doc-FUELCOSTADJRDR",
            state="NC",
            company="carolinas",
            family_type="rate_schedule",
            schedule_code="FUELCOSTADJRDR",
            title="FCAR",
            current_document_id=current_doc_id,
        )
    )
    current_version_id = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="nc-carolinas-doc-FUELCOSTADJRDR",
            document_id=current_doc_id,
            effective_start="2025-09-01",
            source_type="utility_current",
        )
    )
    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label, rate_value, rate_unit, confidence_score, created_at
            ) VALUES (?, ?, 'adjustment', 'Fuel', 0.1234, '$/kWh', 0.9, ?)
            """,
            (current_version_id, "nc-carolinas-doc-FUELCOSTADJRDR", datetime.now(UTC).isoformat()),
        )

    result = repo.canonicalize_historical_family_key(
        "nc-carolinas-doc-FUELCOSTADJRDR",
        "nc-carolinas-rider-FCAR",
        title="FCAR",
        schedule_code="FCAR",
        family_type="rider",
        tariff_identifier="rider-FCAR",
    )

    assert result is not None
    family = repo.get_tariff_family("nc-carolinas-rider-FCAR")
    assert family is not None
    assert family.current_document_id == current_doc_id
    assert repo.get_tariff_family("nc-carolinas-doc-FUELCOSTADJRDR") is None

    with repo._connect() as conn:
        version_row = conn.execute(
            "SELECT family_key FROM tariff_versions WHERE id = ?",
            (current_version_id,),
        ).fetchone()
        assert version_row["family_key"] == "nc-carolinas-rider-FCAR"
        charge_row = conn.execute(
            "SELECT family_key FROM tariff_charges WHERE version_id = ?",
            (current_version_id,),
        ).fetchone()
        assert charge_row["family_key"] == "nc-carolinas-rider-FCAR"


def test_repository_deduplicate_tariff_charges_for_version(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="test-family",
            state="NC",
            company="carolinas",
            family_type="rate_schedule",
            schedule_code="TEST",
            title="Test",
        )
    )
    version_id = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="test-family",
            effective_start="2026-01-01",
            source_type="historical_document",
        )
    )
    with repo._connect() as conn:
        now = datetime.now(UTC).isoformat()
        for _ in range(3):
            conn.execute(
                """
                INSERT INTO tariff_charges (
                    version_id, family_key, charge_type, charge_label, rate_value, rate_unit, season, confidence_score, created_at
                ) VALUES (?, ?, 'fixed', 'Base', 10.0, '$/month', 'all_year', 0.9, ?)
                """,
                (version_id, "test-family", now),
            )
        conn.execute(
            """
            INSERT INTO tariff_charges (
                version_id, family_key, charge_type, charge_label, rate_value, rate_unit, season, confidence_score, created_at
            ) VALUES (?, ?, 'fixed', 'Other', 12.0, '$/month', 'all_year', 0.9, ?)
            """,
            (version_id, "test-family", now),
        )

    result = repo.deduplicate_tariff_charges_for_version(version_id)

    assert result["before_count"] == 4
    assert result["after_count"] == 2
    assert result["duplicates_removed"] == 2


def test_repository_deduplicates_historical_by_family_and_hash(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    base_kwargs = dict(
        current_document_id=7,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-608-rider-rdm-ry1.pdf",
        title="Residential Decoupling Mechanism Rider RDM",
        state="NC",
        company="progress",
        category=DocumentCategory.RIDER.value,
        kind=DocumentKind.PDF.value,
        content_hash="same-pdf-bytes",
        content_type="application/pdf",
        retrieved_at=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
    )
    first_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            canonical_url="https://www.duke-energy.com/rdm.pdf",
            archived_url="https://web.archive.org/web/20240519170239/https://www.duke-energy.com/rdm.pdf?rev=a",
            snapshot_timestamp=datetime(2024, 5, 19, 17, 2, 39, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/rdm-a.pdf",
            raw_text_path=tmp_path / "historical/raw/rdm-a.pdf.txt",
            revision_label="NC Original Leaf No. RDM 608",
            leaf_no="RDM 608",
            effective_start="October 1, 2023",
            **base_kwargs,
        )
    )
    second_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            canonical_url="https://www.duke-energy.com/rdm.pdf",
            archived_url="https://web.archive.org/web/20240531151048/https://www.duke-energy.com/rdm.pdf",
            snapshot_timestamp=datetime(2024, 5, 31, 15, 10, 48, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/rdm-b.pdf",
            raw_text_path=tmp_path / "historical/raw/rdm-b.pdf.txt",
            revision_label="NC Original Leaf No. RDM 608",
            leaf_no="RDM 608",
            effective_start="October 1, 2023",
            **base_kwargs,
        )
    )

    rows = repo.list_historical_documents(state="NC", company="progress")

    assert first_id == second_id
    assert len(rows) == 1


def test_repository_deduplicates_historical_by_revision_metadata(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    base_kwargs = dict(
        current_document_id=7,
        family_key="/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-608-rider-rdm-ry1.pdf",
        title="Residential Decoupling Mechanism Rider RDM",
        state="NC",
        company="progress",
        category=DocumentCategory.RIDER.value,
        kind=DocumentKind.PDF.value,
        content_type="application/pdf",
        retrieved_at=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
        revision_label="NC Original Leaf No. RDM 608",
        leaf_no="RDM 608",
        effective_start="October 1, 2023",
    )
    first_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            canonical_url="https://www.duke-energy.com/rdm.pdf",
            archived_url="https://web.archive.org/web/20240519170239/https://www.duke-energy.com/rdm.pdf?rev=a",
            snapshot_timestamp=datetime(2024, 5, 19, 17, 2, 39, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/rdm-a.pdf",
            raw_text_path=tmp_path / "historical/raw/rdm-a.pdf.txt",
            content_hash="hash-a",
            **base_kwargs,
        )
    )
    second_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            canonical_url="https://www.duke-energy.com/rdm.pdf",
            archived_url="https://web.archive.org/web/20240531151048/https://www.duke-energy.com/rdm.pdf",
            snapshot_timestamp=datetime(2024, 5, 31, 15, 10, 48, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/rdm-b.pdf",
            raw_text_path=tmp_path / "historical/raw/rdm-b.pdf.txt",
            content_hash="hash-b",
            **base_kwargs,
        )
    )

    rows = repo.list_historical_documents(state="NC", company="progress")

    assert first_id == second_id
    assert len(rows) == 1


def test_repository_deduplicates_historical_by_archived_url_after_insert_conflict(
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    archived_url = "https://web.archive.org/web/20150912055224/http://www.duke-energy.com/pdfs/R3-NC-Schedule-R-TOU-dep.pdf"
    first_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=9,
            family_key="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-504-schedule-r-tou.pdf",
            title="Residential Time-of-Use Energy",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="http://www.duke-energy.com/pdfs/R3-NC-Schedule-R-TOU-dep.pdf",
            archived_url=archived_url,
            snapshot_timestamp=datetime(2015, 9, 12, 5, 52, 24, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/r-toue-a.pdf",
            raw_text_path=tmp_path / "historical/raw/r-toue-a.pdf.txt",
            content_hash="hash-a",
            content_type="application/pdf",
            retrieved_at=datetime(2026, 3, 16, 16, 31, 14, tzinfo=UTC),
        )
    )
    second_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=9,
            family_key="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-504-schedule-r-tou.pdf",
            title="Residential Time-of-Use Energy",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://www.duke-energy.com/pdfs/R3-NC-Schedule-R-TOU-dep.pdf",
            archived_url=archived_url,
            snapshot_timestamp=datetime(2015, 9, 12, 5, 52, 24, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/r-toue-b.pdf",
            raw_text_path=tmp_path / "historical/raw/r-toue-b.pdf.txt",
            content_hash="hash-b",
            content_type="application/pdf",
            retrieved_at=datetime(2026, 3, 16, 16, 31, 24, tzinfo=UTC),
        )
    )

    assert first_id == second_id


def test_repository_prefers_existing_archived_url_before_update_conflict(
    tmp_path: Path,
) -> None:
    repo = Repository(tmp_path / "test.db")
    archived_url = "https://web.archive.org/web/20150912055224/http://example.test/shared.pdf"

    first_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=9,
            family_key="nc-progress-leaf-501",
            title="Old Mapping",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/shared.pdf",
            archived_url=archived_url,
            snapshot_timestamp=datetime(2015, 9, 12, 5, 52, 24, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/shared-a.pdf",
            raw_text_path=tmp_path / "historical/raw/shared-a.pdf.txt",
            content_hash="hash-a",
            content_type="application/pdf",
            retrieved_at=datetime(2026, 3, 16, 16, 31, 14, tzinfo=UTC),
        )
    )

    second_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=9,
            family_key="nc-carolinas-doc-FUELCOSTADJRDR",
            title="Corrected Mapping",
            state="NC",
            company="carolinas",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://example.test/shared.pdf",
            archived_url=archived_url,
            snapshot_timestamp=datetime(2015, 9, 12, 5, 52, 24, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/shared-b.pdf",
            raw_text_path=tmp_path / "historical/raw/shared-b.pdf.txt",
            content_hash="hash-b",
            content_type="application/pdf",
            retrieved_at=datetime(2026, 3, 16, 16, 31, 24, tzinfo=UTC),
        )
    )

    assert first_id == second_id

    stored = repo.get_historical_document(first_id)
    assert stored is not None
    assert stored.family_key == "nc-carolinas-doc-FUELCOSTADJRDR"
    assert stored.company == "carolinas"


def test_repository_update_historical_document_family(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            current_document_id=None,
            family_key="/pdfs/pe-ncscheduler-toue.pdf",
            title="ALL-Energy TIME-OF-USE",
            state="NC",
            company="progress",
            category=DocumentCategory.RATE.value,
            kind=DocumentKind.PDF.value,
            canonical_url="https://www.duke-energy.com/pdfs/pe-NCScheduleR-TOUE.pdf",
            archived_url="https://web.archive.org/web/20120906213849/http://www.duke-energy.com:80/pdfs/pe-NCScheduleR-TOUE.pdf",
            snapshot_timestamp=datetime(2012, 9, 6, 21, 38, 49, tzinfo=UTC),
            local_path=tmp_path / "historical/raw/r-toue.pdf",
            raw_text_path=tmp_path / "historical/raw/r-toue.pdf.txt",
            content_hash="hash-r-toue",
            content_type="application/pdf",
            retrieved_at=datetime(2026, 3, 16, 16, 40, 0, tzinfo=UTC),
        )
    )

    repo.update_historical_document_family(
        historical_id,
        family_key="/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-504-schedule-r-tou.pdf",
        current_document_id=9,
        title="Residential Time-of-Use Energy",
    )
    stored = repo.get_historical_document(historical_id)

    assert stored is not None
    assert stored.family_key.endswith("leaf-no-504-schedule-r-tou.pdf")
    assert stored.current_document_id == 9
    assert stored.title == "Residential Time-of-Use Energy"


def test_repository_upsert_bill_statement(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    statement = BillStatementData(
        source_path=str(tmp_path / "Actual Duke Bills/2025-12-18.pdf"),
        account_number="9101 8064 6213",
        customer_name="BRAD CURRY",
        billing_summary=BillingSummaryData(total_amount_due=211.94),
    )

    bill_id = repo.upsert_bill_statement(
        statement,
        content_hash="bill-hash",
        raw_text_path=str(tmp_path / "processed/bills/2025-12-18.txt"),
    )
    stored = repo.get_bill_statement(bill_id)

    assert stored is not None
    assert stored.account_number == "9101 8064 6213"
    assert stored.total_amount_due == 211.94
    assert stored.raw_text_path is not None


def test_repository_replace_bill_component_observations(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    statement = BillStatementData(source_path="bill.pdf")
    bill_id = repo.upsert_bill_statement(
        statement,
        content_hash="bill-hash",
        raw_text_path="bill.txt",
    )

    repo.replace_bill_component_observations(
        bill_id=bill_id,
        observations=[
            BillComponentObservation(
                bill_id=bill_id,
                source_path="bill.pdf",
                section_name="Electric",
                rate_code="RES",
                component_key="summary_rider_adjustments",
                component_label="Summary of Rider Adjustments",
                amount=15.99,
                service_end=date(2026, 1, 17),
                inferred_unit="cents_per_kwh",
                inferred_value=1.23,
                confidence=0.8,
            )
        ],
    )

    rows = repo.list_bill_component_observations(
        component_key="summary_rider_adjustments"
    )
    assert len(rows) == 1
    assert rows[0].bill_id == bill_id
    assert rows[0].inferred_value == 1.23


def test_repository_ncuc_records_deduplicate_by_attachment_url(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    first = NcucDiscoveryRecord(
        docket_number="E-2, Sub 1107",
        filing_title="Fuel charge filing [attachment 1/2]",
        discovered_url="https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId=abc",
        viewer_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=file-1",
        attachment_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=file-1",
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
        fetch_status=NcucFetchStatus.SUCCESS,
        local_path=str(tmp_path / "file-1.pdf"),
    )
    second = NcucDiscoveryRecord(
        docket_number="E-2, Sub 1107",
        filing_title="Fuel charge filing [attachment 2/2]",
        discovered_url="https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId=abc",
        viewer_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=file-2",
        attachment_url="https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=file-2",
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
        fetch_status=NcucFetchStatus.SUCCESS,
        local_path=str(tmp_path / "file-2.pdf"),
    )

    first_id = repo.upsert_ncuc_discovery_record(first)
    second_id = repo.upsert_ncuc_discovery_record(second)
    rows = repo.list_ncuc_discovery_records(docket_number="E-2, Sub 1107")

    assert first_id != second_id
    assert len(rows) == 2


def test_repository_list_ncuc_discovery_records_tolerates_legacy_portal_harvest(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO ncuc_discovery_records (
                docket_number,
                utility,
                filing_title,
                filing_classification,
                referenced_schedule_codes_json,
                referenced_rider_codes_json,
                referenced_leaf_nos_json,
                family_keys_json,
                acquisition_method,
                fetch_status,
                provenance_notes_json,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "E-2, Sub 1219",
                "Duke Energy Progress",
                "Legacy portal harvest row",
                "other",
                "[]",
                "[]",
                "[]",
                '["nc-progress-leaf-602"]',
                "portal_harvest",
                "success",
                "[]",
                datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
            ),
        )
        conn.commit()

    rows = repo.list_ncuc_discovery_records(docket_number="E-2, Sub 1219")

    assert len(rows) == 1
    assert rows[0].acquisition_method == NcucAcquisitionMethod.PLAYWRIGHT


def test_repository_get_historical_document_coerces_invalid_current_document_id_to_none(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO historical_documents (
                current_document_id, family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp, local_path, raw_text_path,
                content_hash, content_type, direct_status_code, direct_downloadable,
                revision_label, supersedes_label, leaf_no, effective_start, effective_end,
                retrieved_at, metadata_json, parsed_result_json, start_page, end_page, evidence_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "32e921d3-7055-4672-8ef7-949ed489030a",
                "nc-progress-leaf-500",
                "Residential Service",
                "NC",
                "progress",
                "rate",
                "pdf",
                "https://example.test/res.pdf",
                "https://example.test/res.pdf",
                datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
                str(tmp_path / "res.pdf"),
                None,
                "hash-res",
                "application/pdf",
                200,
                1,
                None,
                None,
                "500",
                "2024-01-01",
                None,
                datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
                "{}",
                None,
                1,
                2,
                "{}",
            ),
        )
        historical_id = int(conn.execute("SELECT id FROM historical_documents").fetchone()[0])
        conn.commit()

    stored = repo.get_historical_document(historical_id)

    assert stored is not None
    assert stored.current_document_id is None


def test_delete_tariff_data_for_family_removes_rider_links_on_both_sides(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "test.db")
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="test-base",
            state="NC",
            company="progress",
            family_type="rate_schedule",
        )
    )
    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="test-rider",
            state="NC",
            company="progress",
            family_type="rider",
        )
    )
    repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="test-rider",
            effective_start="2026-01-01",
            source_type="utility_current",
        )
    )
    repo.upsert_rider_applicability(
        RiderApplicabilityRecord(
            rider_family_key="test-rider",
            applies_to_family_key="test-base",
            source_type="tariff_text",
        )
    )

    repo.delete_tariff_data_for_family("test-rider")

    rows = repo.list_rider_applicability()
    assert rows == []
