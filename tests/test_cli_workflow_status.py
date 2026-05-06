from __future__ import annotations

import json
from datetime import UTC, datetime

from duke_rates.cli import _build_workflow_status_nc_report
from duke_rates.db.sqlite import connect


def test_build_workflow_status_nc_report_summarizes_operational_counts(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")

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
            "Provisional historical family created by importer.",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )

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
            "nc-progress-leaf-500",
            "Residential Service",
            "NC",
            "progress",
            "tariff",
            "pdf",
            "https://example.com/res.pdf",
            "https://example.com/res.pdf",
            "2024-01-01T00:00:00+00:00",
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
            "2024-01-02T00:00:00+00:00",
            "{}",
            None,
            1,
            2,
            "{}",
        ),
    )
    historical_document_id = int(cur.lastrowid)

    cur = conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            historical_document_id,
            "2024-01-01",
            "regulator",
            0.9,
            "Seeded for test.",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )
    version_id = int(cur.lastrowid)

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
            "Customer Charge $14.00",
            0.9,
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )

    attempt_id = int(
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage, parser_profile,
                status, confidence, utility, schedule_code, effective_date, charge_count,
                review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(tmp_path / "res.pdf"),
                "e-2-sub-1300",
                1,
                2,
                "historical_bulk",
                "generic_residential",
                "parsed",
                0.4,
                "DEP",
                "RES",
                "2024-01-01",
                1,
                json.dumps(["generic_fallback_selected"]),
                json.dumps(
                    {
                        "historical_document_id": historical_document_id,
                        "family_key": "nc-progress-leaf-500",
                    },
                    sort_keys=True,
                ),
                datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
            ),
        ).lastrowid
    )

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
            str(tmp_path / "res.pdf"),
            "e-2-sub-1300",
            1,
            2,
            "historical_bulk",
            "generic_residential",
            "DEP",
            "rule",
            "needs_review",
            0,
            "{}",
            "{}",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )

    conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            historical_document_id, source_pdf, family_key, priority,
            queue_reason, requested_by, status, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            historical_document_id,
            str(tmp_path / "res.pdf"),
            "nc-progress-leaf-500",
            80,
            "needs_review:generic_residential",
            "test",
            "pending",
            "{}",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )

    conn.execute(
        """
        INSERT INTO ocr_processing_queue (
            discovery_record_id, source_pdf, file_hash, backend, priority, status,
            ocr_confidence, structure_complexity, gpu_candidate, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            None,
            str(tmp_path / "scan.pdf"),
            "hash-scan",
            "pytesseract_cpu",
            90,
            "pending",
            0.9,
            0.8,
            0,
            "{}",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
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
            historical_document_id,
            str(tmp_path / "res.pdf"),
            "nc-progress-leaf-500",
            "hash-res",
            "historical_bulk",
            "generic_residential",
            "test-version",
            "targeted",
            "completed",
            "weak",
            1,
            "[]",
            "{}",
            datetime(2026, 4, 1, 12, 0, tzinfo=UTC).isoformat(),
            datetime(2026, 4, 1, 12, 1, tzinfo=UTC).isoformat(),
        ),
    )

    conn.commit()

    report = _build_workflow_status_nc_report(conn)

    assert report["historical_document_count"] == 1
    assert report["linked_version_count"] == 1
    assert report["versions_with_charges_count"] == 1
    assert report["extraction_coverage_pct"] == 100.0
    assert report["parse_review_needs_review_count"] == 1
    assert report["parse_review_active_needs_review_count"] == 1
    assert report["parse_review_legacy_needs_review_count"] == 0
    assert report["reprocess_pending_count"] == 1
    assert report["ocr_pending_count"] == 1
    assert report["stale_historical_count"] == 1
    assert report["provisional_family_count"] == 1
    assert report["null_effective_start_count"] == 0
    assert report["last_historical_run_at"] == datetime(2026, 4, 1, 12, 1, tzinfo=UTC).isoformat()
    assert report["top_needs_review_profiles"] == ["generic_residential"]

    conn.close()
