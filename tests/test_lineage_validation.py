from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.reprocess import record_historical_processing_run
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.lineage_validation import build_lineage_validation_report


def _seed_historical_document(
    conn,
    *,
    doc_id: int,
    family_key: str,
    company: str,
    title: str,
    local_path: str,
    content_hash: str | None,
    effective_start: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO historical_documents (
            id, current_document_id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path, raw_text_path,
            content_hash, content_type, direct_status_code, direct_downloadable,
            revision_label, supersedes_label, leaf_no, effective_start, effective_end,
            retrieved_at, metadata_json, parsed_result_json, start_page, end_page, evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_id,
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
            None,
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


def test_build_lineage_validation_report_surfaces_assignment_provenance_and_readiness(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, aliases_json, notes, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "NC",
            "progress",
            "leaf-500",
            "RES",
            "rate_schedule",
            "Residential Service",
            "[]",
            "Curated family.",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, aliases_json, notes, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-501",
            "NC",
            "progress",
            "leaf-501",
            "GS",
            "rate_schedule",
            "General Service",
            "[]",
            "Provisional historical family created by importer.",
            now,
            now,
        ),
    )

    healthy_path = str(tmp_path / "e-2-sub-1300" / "healthy.pdf")
    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-500",
        company="progress",
        title="Healthy Document",
        local_path=healthy_path,
        content_hash="hash-healthy",
        effective_start="2024-01-01",
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at, docket_number, order_date, leaf_no, source_pdf, docket_dir
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            1,
            "2024-01-01",
            "regulator",
            0.9,
            now,
            "E-2, Sub 1300",
            "2024-01-01",
            "500",
            healthy_path,
            "e-2-sub-1300",
        ),
    )
    version_id = int(
        conn.execute("SELECT id FROM tariff_versions WHERE historical_document_id = 1").fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO tariff_charges (
            version_id, family_key, charge_type, charge_label, rate_value, rate_unit,
            source_snippet, confidence_score, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            version_id,
            "nc-progress-leaf-500",
            "fixed",
            "Customer Charge",
            14.0,
            "month",
            "Customer Charge",
            0.9,
            now,
        ),
    )
    record_historical_processing_run(
        conn,
        historical_document_id=1,
        source_pdf=healthy_path,
        family_key="nc-progress-leaf-500",
        content_hash="hash-healthy",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="test-v1",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="strong",
        charge_count=1,
    )
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
            101,
            "E-2, Sub 1300",
            None,
            "Duke Energy Progress",
            "Healthy Filing",
            "2024-01-01",
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
            "success",
            healthy_path,
            "hash-healthy",
            "application/pdf",
            1000,
            "[]",
            None,
            None,
            None,
            "{}",
            now,
            now,
        ),
    )

    provisional_path = str(tmp_path / "e-2-sub-1400" / "provisional.pdf")
    _seed_historical_document(
        conn,
        doc_id=2,
        family_key="nc-progress-leaf-501",
        company="progress",
        title="Provisional No Charges",
        local_path=provisional_path,
        content_hash="None",
        effective_start="2024-02-01",
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-501",
            2,
            "2024-02-01",
            "regulator",
            0.9,
            now,
        ),
    )
    record_historical_processing_run(
        conn,
        historical_document_id=2,
        source_pdf=provisional_path,
        family_key="nc-progress-leaf-501",
        content_hash="None",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="test-v1",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="empty",
        charge_count=0,
    )
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
            102,
            "E-2, Sub 1400",
            None,
            "Duke Energy Progress",
            "Provisional Filing",
            "2024-02-01",
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
            "success",
            provisional_path,
            "hash-provisional-other",
            "application/pdf",
            1000,
            "[]",
            None,
            None,
            None,
            "{}",
            now,
            now,
        ),
    )

    missing_family_path = str(tmp_path / "raw" / "missing-family.pdf")
    _seed_historical_document(
        conn,
        doc_id=3,
        family_key="nc-progress-missing-family",
        company="progress",
        title="Missing Family",
        local_path=missing_family_path,
        content_hash="hash-missing-family",
        effective_start=None,
    )

    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_lineage_validation_report(repo, limit=10)
    summary = report["summary"]
    rows = report["rows"]

    assert summary["total_documents_count"] == 3
    assert summary["clean_document_count"] == 1
    assert summary["blocking_issue_document_count"] == 2
    assert summary["warning_only_document_count"] == 0
    assert summary["missing_tariff_family_count"] == 1
    assert summary["provisional_family_count"] == 1
    assert summary["missing_effective_start_count"] == 1
    assert summary["missing_version_link_count"] == 1
    assert summary["linked_without_charges_count"] == 1
    assert summary["version_provenance_gap_count"] == 1
    assert summary["missing_discovery_match_count"] == 1
    assert summary["path_only_discovery_link_count"] == 1
    assert summary["extracted_with_charges_count"] == 1
    assert summary["skipped_reference_count"] == 0

    by_id = {row["historical_document_id"]: row for row in rows}

    assert by_id[2]["blocking_issues"] == [
        "linked_without_charges",
    ]
    assert by_id[2]["warning_issues"] == [
        "provisional_family",
        "version_provenance_gap",
        "path_only_discovery_link",
    ]
    assert by_id[3]["blocking_issues"] == [
        "missing_tariff_family",
        "missing_effective_start",
        "missing_version_link",
    ]
    assert by_id[3]["warning_issues"] == [
        "missing_discovery_match",
    ]
