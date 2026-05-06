from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakeDiscovery:
    id: int
    docket_number: str | None = None
    filing_title: str | None = None
    filing_date: str | None = None
    fetch_status: str = "pending"
    family_keys: list[str] = field(default_factory=list)
    download_url: str | None = None
    viewer_url: str | None = None
    discovered_url: str | None = None
    local_path: str | None = None
    content_hash: str | None = None
    acquisition_method: str = "playwright"
    provenance_notes: list[str] = field(default_factory=list)
    metadata_json: str | None = None
    search_confidence_score: float | None = None
    search_ideality: str | None = None
    attachment_url: str | None = None


@dataclass
class FakeHistoricalLead:
    id: int
    extracted_title: str | None = None
    docket_number: str | None = None
    effective_start: str | None = None
    confidence_score: float = 0.0
    extraction_method: str = ""
    source_class: str = ""
    source_label: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class FakeDocketLead:
    id: int
    docket_number: str
    utility: str
    proceeding_type: str | None = None
    date_start: str | None = None
    contains_tariff_text: bool = False
    confidence_score: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class FakeHistoricalDoc:
    id: int
    family_key: str
    title: str
    effective_start: str | None
    start_page: int
    end_page: int | None
    local_path: Path
    canonical_url: str | None
    archived_url: str | None
    evidence_json: str | None = None


@dataclass
class FakeVersion:
    id: int
    historical_document_id: int | None
    effective_start: str | None
    effective_end: str | None = None
    revision_label: str | None = None
    supersedes_label: str | None = None
    docket_number: str | None = None
    confidence_score: float = 0.0


class FakeRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.discovery = {}
        self.historical = {}
        self.leads = {}
        self.dockets = {}
        self.versions = {}

    def _connect(self):
        return self.conn

    def get_ncuc_discovery_record(self, record_id: int):
        return self.discovery.get(record_id)

    def get_historical_document(self, historical_id: int):
        return self.historical.get(historical_id)

    def list_ncuc_discovery_records(self, *, family_key=None, fetch_status=None):
        rows = list(self.discovery.values())
        if family_key:
            rows = [row for row in rows if family_key in row.family_keys]
        if fetch_status:
            rows = [row for row in rows if row.fetch_status == fetch_status]
        return rows

    def list_historical_documents(self, *, state=None, company=None):
        return list(self.historical.values())

    def list_historical_leads(self, *, family_key=None, target_code=None, disposition=None):
        return list(self.leads.get(family_key, []))

    def list_regulatory_docket_leads(self, *, family_key=None, docket_number=None):
        return list(self.dockets.get(family_key, []))

    def list_tariff_versions(self, family_key: str):
        return list(self.versions.get(family_key, []))


def _seed_status_tables(conn: sqlite3.Connection, source_pdf: str):
    conn.execute(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY,
            family_key TEXT,
            local_path TEXT,
            canonical_url TEXT,
            start_page INTEGER,
            end_page INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE parse_attempt_logs (
            id INTEGER PRIMARY KEY,
            source_pdf TEXT,
            parser_stage TEXT,
            parser_profile TEXT,
            status TEXT,
            confidence REAL,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE parse_review_outcomes (
            id INTEGER PRIMARY KEY,
            parse_attempt_id INTEGER,
            source_pdf TEXT,
            page_start INTEGER,
            page_end INTEGER,
            outcome TEXT,
            notes_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE historical_processing_runs (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER,
            source_pdf TEXT,
            parser_profile TEXT,
            parser_stage TEXT,
            parser_version TEXT,
            status TEXT,
            outcome_quality TEXT,
            charge_count INTEGER,
            review_flags_json TEXT,
            metadata_json TEXT,
            started_at TEXT,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE historical_reprocess_queue (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER,
            status TEXT,
            priority INTEGER,
            queue_reason TEXT,
            metadata_json TEXT,
            requested_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE ocr_artifacts (
            id INTEGER PRIMARY KEY,
            source_pdf TEXT,
            backend TEXT,
            status TEXT,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            historical_document_id INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO historical_documents (id, family_key, local_path, canonical_url, start_page, end_page) VALUES (1, 'fk1', ?, 'https://example/doc', 2, 3)",
        (source_pdf,),
    )
    conn.execute(
        "INSERT INTO parse_attempt_logs (id, source_pdf, parser_stage, parser_profile, status, confidence, metadata_json) VALUES (10, ?, 'historical_bulk', 'progress_residential', 'parsed', 0.91, ?)",
        (source_pdf, json.dumps({"historical_document_id": 1})),
    )
    conn.execute(
        "INSERT INTO parse_review_outcomes (id, parse_attempt_id, source_pdf, page_start, page_end, outcome, notes_json) VALUES (20, 10, ?, 2, 3, 'needs_review', '{}')",
        (source_pdf,),
    )
    conn.execute(
        "INSERT INTO historical_processing_runs (id, historical_document_id, source_pdf, parser_profile, parser_stage, parser_version, status, outcome_quality, charge_count, review_flags_json, metadata_json, started_at, completed_at) VALUES (30, 1, ?, 'progress_residential', 'historical_bulk', 'v1', 'completed', 'weak', 2, '[]', '{}', '2026-04-20T00:00:00Z', '2026-04-20T00:01:00Z')",
        (source_pdf,),
    )
    conn.execute(
        "INSERT INTO historical_reprocess_queue (id, historical_document_id, status, priority, queue_reason, metadata_json, requested_at) VALUES (40, 1, 'pending', 90, 'missing_doc_workflow', ?, '2026-04-20T00:02:00Z')",
        (json.dumps({"promotion_basis": "historical_import", "family_match_score": 48.0, "historical_document_id": 1}),),
    )
    conn.execute(
        "INSERT INTO ocr_artifacts (id, source_pdf, backend, status, metadata_json) VALUES (50, ?, 'ocrmypdf_tesseract', 'completed', '{}')",
        (source_pdf,),
    )
    conn.execute(
        "INSERT INTO tariff_versions (id, historical_document_id) VALUES (60, 1)"
    )
    conn.commit()


def test_build_nc_missing_doc_status_report_family_key(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_status import build_nc_missing_doc_status_report

    db_path = tmp_path / "status.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    source_pdf = str(tmp_path / "doc.pdf")
    _seed_status_tables(conn, source_pdf)

    repo = FakeRepository(conn)
    repo.discovery[7] = FakeDiscovery(
        id=7,
        docket_number="E-2 Sub 976",
        filing_title="Revised Rate Tariffs",
        filing_date="2010-11-15",
        fetch_status="success",
        family_keys=["fk1"],
        download_url="https://example/doc.pdf",
        viewer_url="https://example/doc.pdf",
        discovered_url="https://example/doc",
        local_path=source_pdf,
        content_hash="abc",
        metadata_json=json.dumps({"missing_clean_doc_search": True}),
        search_confidence_score=76.0,
        search_ideality="ideal",
        attachment_url="https://example/doc.pdf",
    )
    repo.historical[1] = FakeHistoricalDoc(
        id=1,
        family_key="fk1",
        title="Schedule RES (Span 2-3)",
        effective_start="2010-12-01",
        start_page=2,
        end_page=3,
        local_path=Path(source_pdf),
        canonical_url="https://example/doc",
        archived_url="ncuc://E-2 Sub 976/7#page=2",
        evidence_json=json.dumps({"explicit_leaf_hit": 40.0, "tariff_vocab_density": 8.0}),
    )
    repo.leads["fk1"] = [
        FakeHistoricalLead(
            id=11,
            extracted_title="Revised Rate Tariffs",
            docket_number="E-2 Sub 976",
            effective_start="2010-12-01",
            confidence_score=87.0,
            extraction_method="structured_portal_missing_clean_doc_search",
            source_class="ncuc_missing_doc_search",
            notes=["missing_kind=missing_superseded_revision"],
        )
    ]
    repo.dockets["fk1"] = [
        FakeDocketLead(
            id=21,
            docket_number="E-2 Sub 976",
            utility="Duke Energy Progress",
            proceeding_type="compliance_filing",
            contains_tariff_text=True,
            confidence_score=88.0,
        )
    ]
    repo.versions["fk1"] = [
        FakeVersion(
            id=60,
            historical_document_id=1,
            effective_start="2010-12-01",
            revision_label="RES-13",
            supersedes_label="RES-12",
            docket_number="E-2 Sub 976",
            confidence_score=0.5,
        )
    ]

    report = build_nc_missing_doc_status_report(repo, family_key="fk1")

    assert report["summary"]["family_key"] == "fk1"
    assert report["summary"]["discovery_record_count"] == 1
    assert report["summary"]["historical_document_count"] == 1
    assert report["summary"]["needs_review_count"] == 1
    assert report["summary"]["queued_reprocess_count"] == 1
    assert report["historical_documents"][0]["current_stage"] == "queued_for_reprocess:pending"
    assert report["historical_documents"][0]["latest_review"]["outcome"] == "needs_review"
    assert report["historical_documents"][0]["next_action"] == "wait_for_reprocess_completion"
    assert report["historical_documents"][0]["blocked_reason"] == "needs_review"
    assert report["discovery_records"][0]["search_promotion_assessment"]["promotable"] is True
    assert report["discovery_records"][0]["next_action"] == "monitor_linked_document"
    assert report["historical_documents"][0]["family_match_score"] == 48.0
    assert report["historical_documents"][0]["import_promotion_assessment"]["promotable"] is True
    assert report["historical_documents"][0]["latest_reprocess_queue"]["metadata"]["promotion_basis"] == "historical_import"


def test_build_nc_missing_doc_status_report_surfaces_actionable_blockers(tmp_path: Path):
    from duke_rates.historical.ncuc.missing_doc_status import build_nc_missing_doc_status_report

    db_path = tmp_path / "status-blocked.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    source_pdf = str(tmp_path / "blocked.pdf")
    _seed_status_tables(conn, source_pdf)
    conn.execute("DELETE FROM historical_reprocess_queue")
    conn.execute("DELETE FROM parse_review_outcomes")
    conn.execute(
        """
        UPDATE historical_processing_runs
        SET outcome_quality = 'empty'
        WHERE historical_document_id = 1
        """
    )
    conn.commit()

    repo = FakeRepository(conn)
    repo.discovery[8] = FakeDiscovery(
        id=8,
        docket_number="E-2 Sub 999",
        filing_title="Weak search result",
        filing_date="2011-01-01",
        fetch_status="pending",
        family_keys=["fk1"],
        metadata_json=json.dumps({}),
        search_confidence_score=10.0,
        search_ideality="possible",
    )
    repo.historical[1] = FakeHistoricalDoc(
        id=1,
        family_key="fk1",
        title="Schedule RES (Span 2-3)",
        effective_start="2010-12-01",
        start_page=2,
        end_page=3,
        local_path=Path(source_pdf),
        canonical_url="https://example/doc",
        archived_url="ncuc://E-2 Sub 999/8#page=2",
        evidence_json=json.dumps({"explicit_leaf_hit": 40.0, "tariff_vocab_density": 8.0}),
    )
    repo.versions["fk1"] = [
        FakeVersion(
            id=60,
            historical_document_id=1,
            effective_start="2010-12-01",
            revision_label="RES-13",
        )
    ]

    report = build_nc_missing_doc_status_report(repo, family_key="fk1")

    assert report["discovery_records"][0]["next_action"] == "fetch_document"
    assert report["discovery_records"][0]["blocked_reason"] == "ideality_below_threshold:possible"
    assert report["historical_documents"][0]["next_action"] == "retry_with_better_parser_context"
    assert report["historical_documents"][0]["blocked_reason"] == "processed_empty"
