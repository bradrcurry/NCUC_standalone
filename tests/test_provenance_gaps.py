from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.provenance_gaps import build_provenance_gap_report


def _seed_family(
    conn,
    *,
    family_key: str,
    company: str = "progress",
    schedule_code: str = "RES",
    title: str = "Residential Service",
) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, aliases_json, notes, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            family_key,
            "NC",
            company,
            family_key,
            schedule_code,
            "rate_schedule",
            title,
            "[]",
            "Curated family.",
            now,
            now,
        ),
    )


def _seed_historical_document(
    conn,
    *,
    family_key: str,
    title: str,
    local_path: str,
    content_hash: str,
    effective_start: str | None,
    leaf_no: str | None,
    company: str = "progress",
) -> int:
    cur = conn.execute(
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
            None,
            family_key,
            title,
            "NC",
            company,
            "tariff",
            "pdf",
            f"https://example.com/{Path(local_path).name}",
            f"https://archive.example.com/{Path(local_path).name}",
            "2024-01-01T00:00:00+00:00",
            local_path,
            None,
            content_hash,
            "application/pdf",
            200,
            1,
            None,
            None,
            leaf_no,
            effective_start,
            None,
            "2024-01-02T00:00:00+00:00",
            "{}",
            None,
            1,
            3,
            "{}",
        ),
    )
    return int(cur.lastrowid)


def _seed_tariff_version(
    conn,
    *,
    family_key: str,
    historical_document_id: int,
    effective_start: str | None,
    source_type: str = "regulator",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            family_key,
            historical_document_id,
            effective_start,
            source_type,
            0.9,
            "Seeded version.",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )
    return int(cur.lastrowid)


def _seed_discovery_record(
    conn,
    *,
    record_id: int,
    local_path: str,
    content_hash: str,
    docket_number: str | None,
    filing_date: str | None,
    fetch_status: str = "success",
    filing_title: str = "Residential Service Filing",
) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records (
            id, docket_number, sub_number, utility, filing_title, filing_date,
            proceeding_type, filing_classification, exhibit_label,
            referenced_schedule_codes_json, referenced_rider_codes_json,
            referenced_leaf_nos_json, family_keys_json, discovered_url, viewer_url,
            attachment_url, download_url, acquisition_method, fetch_status, local_path,
            content_hash, content_type, file_size_bytes, provenance_notes_json,
            search_query, page_title, error_detail, metadata_json, created_at, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record_id,
            docket_number,
            None,
            "Duke Energy Progress",
            filing_title,
            filing_date,
            None,
            "tariff_sheets",
            None,
            "[]",
            "[]",
            "[]",
            "[]",
            None,
            None,
            None,
            None,
            "manual_seed",
            fetch_status,
            local_path,
            content_hash,
            "application/pdf",
            4096,
            '{"source":"test-suite","label":"seed"}',
            None,
            None,
            None,
            "{}",
            now,
            now,
        ),
    )


def test_build_provenance_gap_report_surfaces_version_and_discovery_gaps(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    _seed_family(conn, family_key="nc-progress-leaf-500")
    _seed_family(
        conn,
        family_key="nc-progress-leaf-600",
        schedule_code="GS",
        title="General Service",
    )
    _seed_family(
        conn,
        family_key="nc-progress-leaf-700",
        schedule_code="LGS",
        title="Large General Service",
    )

    fully_matched_doc_path = str(
        tmp_path / "data" / "historical" / "ncuc" / "e-2-sub-1300" / "leaf500.pdf"
    )
    fully_matched_doc_id = _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-500",
        title="Residential Service",
        local_path=fully_matched_doc_path,
        content_hash="hash-leaf500",
        effective_start="2024-01-01",
        leaf_no="500",
    )
    _seed_tariff_version(
        conn,
        family_key="nc-progress-leaf-500",
        historical_document_id=fully_matched_doc_id,
        effective_start="2024-01-01",
    )
    _seed_discovery_record(
        conn,
        record_id=1,
        local_path=fully_matched_doc_path,
        content_hash="hash-leaf500",
        docket_number="E-2, Sub 1300",
        filing_date="2024-01-01",
    )

    path_only_doc_path = str(
        tmp_path / "data" / "historical" / "ncuc" / "e-2-sub-1400" / "leaf600.pdf"
    )
    _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-600",
        title="General Service",
        local_path=path_only_doc_path,
        content_hash="None",
        effective_start="2024-02-01",
        leaf_no="600",
    )
    _seed_discovery_record(
        conn,
        record_id=2,
        local_path=path_only_doc_path,
        content_hash="hash-leaf600",
        docket_number="E-2, Sub 1400",
        filing_date="2024-02-01",
    )

    _seed_historical_document(
        conn,
        family_key="nc-progress-leaf-700",
        title="Large General Service",
        local_path=str(tmp_path / "data" / "historical" / "raw" / "nc" / "progress" / "leaf700.pdf"),
        content_hash="hash-leaf700",
        effective_start="2024-03-01",
        leaf_no="700",
    )

    _seed_discovery_record(
        conn,
        record_id=3,
        local_path=str(tmp_path / "downloads" / "mystery.pdf"),
        content_hash="hash-mystery",
        docket_number=None,
        filing_date="2024-04-01",
        filing_title="Mystery Filing",
    )

    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_provenance_gap_report(repo, limit=10)
    summary = report["summary"]

    assert summary["historical_versions_count"] == 1
    assert summary["versions_missing_any_provenance_count"] == 1
    assert summary["versions_missing_docket_number_count"] == 1
    assert summary["versions_missing_order_date_count"] == 1
    assert summary["versions_missing_leaf_no_count"] == 1
    assert summary["versions_missing_source_pdf_count"] == 1
    assert summary["versions_missing_docket_dir_count"] == 1
    assert summary["historical_documents_missing_discovery_match_count"] == 1
    assert summary["historical_documents_path_only_discovery_link_count"] == 1
    assert summary["historical_documents_hash_only_discovery_link_count"] == 0
    assert summary["acquired_discovery_records_missing_docket_number_count"] == 1

    version_gap = report["versions_missing_provenance"][0]
    assert version_gap["family_key"] == "nc-progress-leaf-500"
    assert version_gap["discovery_linkage"] == "path+hash"
    assert version_gap["missing_fields"] == [
        "docket_number",
        "order_date",
        "leaf_no",
        "source_pdf",
        "docket_dir",
    ]
    assert version_gap["candidate_fill_fields"] == [
        "leaf_no",
        "source_pdf",
        "docket_dir",
        "docket_number",
        "order_date",
    ]

    missing_doc = report["historical_documents_missing_discovery_match"][0]
    assert missing_doc["family_key"] == "nc-progress-leaf-700"

    weak_doc = report["historical_documents_path_only_discovery_link"][0]
    assert weak_doc["family_key"] == "nc-progress-leaf-600"
    assert weak_doc["matched_discovery_record_id"] == 2

    discovery_gap = report["acquired_discovery_records_missing_docket_number"][0]
    assert discovery_gap["id"] == 3
    assert discovery_gap["provenance_notes"] == ["source=test-suite", "label=seed"]
