from __future__ import annotations

import json
from datetime import UTC, datetime

from duke_rates.cli import _build_ocr_benchmark_nc_report
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
)


def test_build_ocr_benchmark_nc_report_groups_backend_and_outcomes(tmp_path) -> None:
    conn = connect(tmp_path / "ocr-benchmark.db")
    now = datetime(2026, 4, 19, tzinfo=UTC).isoformat()

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Progress Residential",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/doc.pdf",
            "https://archive.test/doc",
            now,
            str(tmp_path / "doc.pdf"),
            "hash-ocr-bench",
            "2024-01-01",
            now,
            "{}",
        ),
    ).lastrowid

    conn.execute(
        """
        INSERT INTO ocr_artifacts (
            discovery_record_id, source_pdf, file_hash, backend, status,
            text_sidecar_path, pages_sidecar_path, page_count, ocr_confidence,
            metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            str(tmp_path / "doc.pdf"),
            "hash-ocr-bench",
            "ocrmypdf_tesseract",
            "completed",
            None,
            None,
            2,
            0.91,
            json.dumps(
                {
                    "selected_backend": "ocrmypdf_tesseract",
                    "attempted_backends": ["ocrmypdf_tesseract"],
                    "ocr_normalization_version": "ocr_normalization_v1",
                },
                sort_keys=True,
            ),
            now,
            now,
        ),
    )
    historical_id_2 = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-501",
            "Progress Residential TOU",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/doc2.pdf",
            "https://archive.test/doc2",
            now,
            str(tmp_path / "doc2.pdf"),
            "hash-ocr-bench-2",
            "2024-02-01",
            now,
            "{}",
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO ocr_artifacts (
            discovery_record_id, source_pdf, file_hash, backend, status,
            text_sidecar_path, pages_sidecar_path, page_count, ocr_confidence,
            metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            str(tmp_path / "doc2.pdf"),
            "hash-ocr-bench-2",
            "pytesseract_cpu",
            "completed",
            None,
            None,
            1,
            0.51,
            json.dumps(
                {
                    "selected_backend": "pytesseract_cpu",
                    "attempted_backends": ["ocrmypdf_tesseract", "pytesseract_cpu"],
                    "ocr_normalization_version": "ocr_normalization_v1",
                },
                sort_keys=True,
            ),
            now,
            now,
        ),
    )

    conn.execute(
        """
        INSERT INTO historical_processing_runs (
            historical_document_id, source_pdf, family_key, content_hash,
            parser_stage, parser_profile, parser_version, processing_mode,
            status, outcome_quality, charge_count, review_flags_json,
            metadata_json, started_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            historical_id,
            str(tmp_path / "doc.pdf"),
            "nc-progress-leaf-500",
            "hash-ocr-bench",
            "historical_bulk",
            "generic_residential",
            "historical_bulk_v2",
            "historical_bulk",
            "parsed",
            "strong",
            3,
            "[]",
            "{}",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO historical_processing_runs (
            historical_document_id, source_pdf, family_key, content_hash,
            parser_stage, parser_profile, parser_version, processing_mode,
            status, outcome_quality, charge_count, review_flags_json,
            metadata_json, started_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            historical_id_2,
            str(tmp_path / "doc2.pdf"),
            "nc-progress-leaf-501",
            "hash-ocr-bench-2",
            "historical_bulk",
            "generic_residential",
            "historical_bulk_v2",
            "historical_bulk",
            "parsed",
            "weak",
            1,
            "[]",
            "{}",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO ncuc_page_artifacts (
            discovery_record_id, source_pdf, file_hash, artifact_version,
            page_number, text_length, text_content, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            str(tmp_path / "doc.pdf"),
            "hash-ocr-bench",
            "page_miner_v6",
            1,
            8,
            "OCR text",
            json.dumps(
                {
                    "artifact_source": "ocr",
                    "ocr_backend_version": OCR_BACKEND_VERSION,
                    "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                },
                sort_keys=True,
            ),
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
            str(tmp_path / "doc.pdf"),
            "hash-ocr-bench",
            "segmentation_v8",
            0,
            1,
            1,
            "tariff",
            0.9,
            "[]",
            "[]",
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
        INSERT INTO ncuc_page_artifacts (
            discovery_record_id, source_pdf, file_hash, artifact_version,
            page_number, text_length, text_content, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            str(tmp_path / "doc2.pdf"),
            "hash-ocr-bench-2",
            "page_miner_old",
            1,
            6,
            "OCR 2",
            json.dumps(
                {
                    "artifact_source": "ocr",
                    "ocr_backend_version": "old_backend",
                    "ocr_normalization_version": "old_normalization",
                },
                sort_keys=True,
            ),
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
            str(tmp_path / "doc2.pdf"),
            "hash-ocr-bench-2",
            "segmentation_old",
            0,
            1,
            1,
            "tariff",
            0.8,
            "[]",
            "[]",
            "[]",
            "[]",
            "{}",
            "{}",
            now,
            now,
        ),
    )
    attempt_id = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage, parser_profile,
            status, confidence, utility, schedule_code, effective_date, charge_count,
            review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(tmp_path / "doc.pdf"),
            "e-2-sub-1300",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.7,
            "DEP",
            "RES",
            "2024-01-01",
            3,
            "[]",
            json.dumps({"historical_document_id": historical_id}, sort_keys=True),
            now,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO parse_review_outcomes (
            parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
            parser_stage, parser_profile, utility, review_source, outcome,
            correction_count, notes_json, corrections_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id,
            str(tmp_path / "doc.pdf"),
            "e-2-sub-1300",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "DEP",
            "rule",
            "needs_review",
            0,
            "{}",
            "{}",
            now,
        ),
    )
    attempt_id_2 = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage, parser_profile,
            status, confidence, utility, schedule_code, effective_date, charge_count,
            review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(tmp_path / "doc2.pdf"),
            "e-2-sub-1301",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.5,
            "DEP",
            "RES",
            "2024-02-01",
            1,
            "[]",
            json.dumps({"historical_document_id": historical_id_2}, sort_keys=True),
            now,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO parse_review_outcomes (
            parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
            parser_stage, parser_profile, utility, review_source, outcome,
            correction_count, notes_json, corrections_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id_2,
            str(tmp_path / "doc2.pdf"),
            "e-2-sub-1301",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "DEP",
            "rule",
            "accepted",
            0,
            "{}",
            "{}",
            now,
        ),
    )
    conn.commit()

    report = _build_ocr_benchmark_nc_report(conn, limit=10)

    assert report["row_count"] == 2
    assert report["backend_summary"] == [
        {"backend": "ocrmypdf_tesseract", "count": 1},
        {"backend": "pytesseract_cpu", "count": 1},
    ]
    assert report["normalization_summary"] == [{"ocr_normalization_version": "ocr_normalization_v1", "count": 2}]
    assert report["outcome_summary"] == [
        {"outcome_quality": "strong", "count": 1},
        {"outcome_quality": "weak", "count": 1},
    ]
    assert report["route_reason_summary"] == [
        {"route_reason": "no_usable_text", "count": 2},
    ]
    assert report["recommended_lane_summary"] == [
        {"recommended_lane": "queue_ocr_or_paddle", "count": 2},
    ]
    assert report["page_artifact_version_summary"] == [
        {"page_artifact_version": "page_miner_old", "count": 1},
        {"page_artifact_version": "page_miner_v6", "count": 1},
    ]
    assert report["span_artifact_version_summary"] == [
        {"span_artifact_version": "segmentation_old", "count": 1},
        {"span_artifact_version": "segmentation_v8", "count": 1},
    ]
    assert report["review_outcome_summary"] == [
        {"review_outcome": "accepted", "count": 1},
        {"review_outcome": "needs_review", "count": 1},
    ]
    assert report["backend_outcome_summary"] == [
        {"backend": "ocrmypdf_tesseract", "outcome_quality": "strong", "count": 1}
        ,{"backend": "pytesseract_cpu", "outcome_quality": "weak", "count": 1}
    ]
    assert report["rows"][0]["parser_profile"] == "generic_residential"
    assert "route_reason" in report["rows"][0]
    assert "recommended_lane" in report["rows"][0]
    filtered_backend = _build_ocr_benchmark_nc_report(conn, limit=10, backend_filter="pytesseract_cpu")
    assert filtered_backend["row_count"] == 1
    assert filtered_backend["rows"][0]["backend"] == "pytesseract_cpu"

    filtered_outcome = _build_ocr_benchmark_nc_report(conn, limit=10, outcome_filter="strong")
    assert filtered_outcome["row_count"] == 1
    assert filtered_outcome["rows"][0]["outcome_quality"] == "strong"

    filtered_review = _build_ocr_benchmark_nc_report(conn, limit=10, needs_review_only=True)
    assert filtered_review["row_count"] == 1
    assert filtered_review["rows"][0]["review_outcome"] == "needs_review"

    filtered_stale = _build_ocr_benchmark_nc_report(conn, limit=10, stale_only=True)
    assert filtered_stale["row_count"] == 1
    assert filtered_stale["rows"][0]["backend"] == "pytesseract_cpu"
    assert "page_artifact_version" in filtered_stale["rows"][0]["stale_reasons"]
    assert "span_artifact_version" in filtered_stale["rows"][0]["stale_reasons"]
    assert "ocr_backend_version" in filtered_stale["rows"][0]["stale_reasons"]
    assert "ocr_normalization_version" in filtered_stale["rows"][0]["stale_reasons"]

    weak_first = _build_ocr_benchmark_nc_report(conn, limit=10, sort_by="weak-first")
    assert weak_first["rows"][0]["outcome_quality"] == "weak"

    review_first = _build_ocr_benchmark_nc_report(conn, limit=10, sort_by="review-first")
    assert review_first["rows"][0]["review_outcome"] == "needs_review"

    stale_first = _build_ocr_benchmark_nc_report(conn, limit=10, sort_by="stale-first")
    assert stale_first["rows"][0]["stale_reasons"]
    conn.close()
