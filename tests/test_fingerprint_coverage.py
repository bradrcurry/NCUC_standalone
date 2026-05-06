from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.fingerprint_coverage import (
    build_fingerprint_coverage_report,
)


def _seed_historical_document(
    conn,
    *,
    doc_id: int,
    family_key: str,
    company: str,
    title: str,
    local_path: str,
    content_hash: str | None,
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
            None,
            None,
            "2024-01-02T00:00:00+00:00",
            "{}",
            None,
            1,
            4,
            "{}",
        ),
    )


def test_build_fingerprint_coverage_report_counts_hash_and_fingerprint_gaps(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    source_with_fp = str(tmp_path / "historical" / "ncuc" / "e-2-sub-1300" / "with-fp.pdf")
    source_path_only = str(tmp_path / "historical" / "ncuc" / "e-2-sub-1400" / "path-only.pdf")
    source_hash_no_fp = str(tmp_path / "historical" / "raw" / "nc" / "progress" / "hash-no-fp.pdf")

    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-500",
        company="progress",
        title="Residential Service",
        local_path=source_with_fp,
        content_hash="hash-with-fp",
    )
    _seed_historical_document(
        conn,
        doc_id=2,
        family_key="nc-progress-leaf-600",
        company="progress",
        title="General Service",
        local_path=source_path_only,
        content_hash="None",
    )
    _seed_historical_document(
        conn,
        doc_id=3,
        family_key="nc-carolinas-leaf-700",
        company="carolinas",
        title="Large General Service",
        local_path=source_hash_no_fp,
        content_hash="hash-no-fp",
    )

    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO document_fingerprints (
            source_pdf, docket_dir, page_start, page_end, leaf_no, schedule_code, title,
            text_length, line_count, numeric_line_count, has_table_rows, has_rider_summary,
            review_flags_json, metadata_json, created_at, doc_quality_tier
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_with_fp,
            "e-2-sub-1300",
            1,
            4,
            "500",
            "RES",
            "Residential Service",
            1200,
            60,
            20,
            1,
            0,
            "[]",
            '{"family_key":"nc-progress-leaf-500","parser_profile":"generic_residential","outcome_quality":"strong"}',
            now,
            "T2",
        ),
    )
    conn.execute(
        """
        INSERT INTO ncuc_page_artifacts (
            discovery_record_id, source_pdf, file_hash, artifact_version, page_number,
            text_length, text_content, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            source_with_fp,
            "hash-with-fp",
            "test-v1",
            1,
            400,
            "sample",
            "{}",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO ncuc_span_artifacts (
            discovery_record_id, source_pdf, file_hash, artifact_version, span_index,
            start_page, end_page, doc_type, confidence, extracted_leaf_nos_json,
            extracted_schedule_titles_json, header_footer_snippets_json, dates_json,
            evidence_score_breakdown_json, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            source_with_fp,
            "hash-with-fp",
            "test-v1",
            0,
            1,
            4,
            "tariff",
            0.9,
            '["500"]',
            '["Schedule RES"]',
            "[]",
            "[]",
            "{}",
            "{}",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO docling_artifacts (
            discovery_record_id, source_pdf, file_hash, backend_version, accelerator,
            status, json_sidecar_path, text_sidecar_path, tables_sidecar_path, page_count,
            conversion_confidence, table_count, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            source_with_fp,
            "hash-with-fp",
            "docling-v1",
            "cpu",
            "completed",
            None,
            None,
            None,
            4,
            0.9,
            1,
            "{}",
            now,
            now,
        ),
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
            "Residential Service Filing",
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
            source_with_fp,
            "hash-with-fp",
            "application/pdf",
            1024,
            "[]",
            None,
            None,
            None,
            "{}",
            now,
            now,
        ),
    )

    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_fingerprint_coverage_report(repo, limit=10)
    summary = report["summary"]

    assert summary["historical_nc_total_count"] == 3
    assert summary["historical_nc_hash_backed_count"] == 2
    assert summary["historical_nc_path_only_count"] == 1
    assert summary["historical_nc_with_fingerprint_count"] == 1
    assert summary["historical_nc_without_fingerprint_count"] == 2
    assert summary["historical_nc_hash_backed_with_fingerprint_count"] == 1
    assert summary["historical_nc_with_page_artifacts_count"] == 1
    assert summary["historical_nc_with_span_artifacts_count"] == 1
    assert summary["historical_nc_with_docling_count"] == 1
    assert summary["historical_nc_with_ocr_count"] == 0
    assert summary["acquired_discovery_total_count"] == 1
    assert summary["acquired_discovery_with_hash_count"] == 1
    assert summary["acquired_discovery_with_page_artifacts_count"] == 1
    assert summary["acquired_discovery_with_span_artifacts_count"] == 1
    assert summary["acquired_discovery_with_docling_count"] == 1
    assert summary["acquired_discovery_with_ocr_count"] == 0
    assert summary["document_fingerprint_row_count"] == 1
    assert summary["fingerprint_rows_with_family_key_count"] == 1
    assert summary["fingerprint_rows_with_parser_profile_count"] == 1
    assert summary["fingerprint_rows_with_outcome_quality_count"] == 1

    assert report["historical_by_company"][0]["company"] == "progress"
    assert report["fingerprint_quality_breakdown"][0]["outcome_quality"] == "strong"
    assert len(report["historical_documents_without_fingerprint"]) == 2
    assert len(report["hash_backed_historical_documents_without_fingerprint"]) == 1
    assert (
        report["hash_backed_historical_documents_without_fingerprint"][0]["family_key"]
        == "nc-carolinas-leaf-700"
    )
