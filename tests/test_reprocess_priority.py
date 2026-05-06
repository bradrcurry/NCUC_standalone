from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.db.reprocess import record_historical_processing_run
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.reprocess_priority import build_reprocess_priority_report


def _seed_historical_document(
    conn,
    *,
    doc_id: int,
    family_key: str,
    title: str,
    local_path: str,
    content_hash: str,
    effective_start: str | None = "2024-01-01",
    company: str = "progress",
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


def _seed_parse_attempt(
    conn,
    *,
    source_pdf: str,
    historical_document_id: int,
    family_key: str,
    parser_profile: str | None,
    status: str,
    review_flags: list[str] | None = None,
    selection: dict | None = None,
) -> int:
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_pdf,
            "e-2-sub-1300",
            1,
            2,
            "historical_bulk",
            parser_profile,
            status,
            0.4,
            "DEP",
            "RES",
            "2024-01-01",
            0,
            json.dumps(review_flags or []),
            json.dumps(
                {
                    "historical_document_id": historical_document_id,
                    "family_key": family_key,
                    "selection": selection or {},
                },
                sort_keys=True,
            ),
            now,
        ),
    )
    return int(cur.lastrowid)


def _seed_queue_row(
    conn,
    *,
    queue_id: int,
    historical_document_id: int,
    source_pdf: str,
    family_key: str,
    priority: int,
    queue_reason: str,
    metadata: dict | None = None,
) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            id, historical_document_id, source_pdf, family_key, priority,
            queue_reason, requested_by, status, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            queue_id,
            historical_document_id,
            source_pdf,
            family_key,
            priority,
            queue_reason,
            "test-suite",
            "pending",
            json.dumps(metadata or {}, sort_keys=True),
            now,
        ),
    )


def test_build_reprocess_priority_report_ranks_mixed_queue_reasons(tmp_path: Path) -> None:
    conn = connect(tmp_path / "reprocess-priority.db")

    pdf_empty = str(tmp_path / "empty.pdf")
    pdf_ocr = str(tmp_path / "ocr.pdf")
    pdf_profile = str(tmp_path / "profile.pdf")
    pdf_unlinked = str(tmp_path / "unlinked.pdf")

    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-500",
        title="Residential Service",
        local_path=pdf_empty,
        content_hash="hash-empty",
    )
    _seed_historical_document(
        conn,
        doc_id=2,
        family_key="nc-progress-leaf-501",
        title="General Service",
        local_path=pdf_ocr,
        content_hash="hash-ocr",
    )
    _seed_historical_document(
        conn,
        doc_id=3,
        family_key="nc-progress-leaf-502",
        title="Profile Impact",
        local_path=pdf_profile,
        content_hash="hash-profile",
    )
    _seed_historical_document(
        conn,
        doc_id=4,
        family_key="nc-progress-leaf-503",
        title="Strong But Unlinked",
        local_path=pdf_unlinked,
        content_hash="hash-unlinked",
    )

    record_historical_processing_run(
        conn,
        historical_document_id=1,
        source_pdf=pdf_empty,
        family_key="nc-progress-leaf-500",
        content_hash="hash-empty",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="test-v1",
        processing_mode="historical_bulk",
        status="empty",
        outcome_quality="empty",
        charge_count=0,
        review_flags=["no_charges_extracted"],
    )
    _seed_parse_attempt(
        conn,
        source_pdf=pdf_empty,
        historical_document_id=1,
        family_key="nc-progress-leaf-500",
        parser_profile="generic_residential",
        status="empty",
        review_flags=["no_charges_extracted"],
        selection={"fallback_triggered_by": "empty"},
    )

    record_historical_processing_run(
        conn,
        historical_document_id=2,
        source_pdf=pdf_ocr,
        family_key="nc-progress-leaf-501",
        content_hash="hash-ocr",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="test-v1",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="weak",
        charge_count=1,
        review_flags=["generic_fallback_selected"],
    )
    _seed_parse_attempt(
        conn,
        source_pdf=pdf_ocr,
        historical_document_id=2,
        family_key="nc-progress-leaf-501",
        parser_profile="generic_residential",
        status="parsed",
        review_flags=["generic_fallback_selected"],
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
            pdf_ocr,
            "hash-ocr",
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

    record_historical_processing_run(
        conn,
        historical_document_id=3,
        source_pdf=pdf_profile,
        family_key="nc-progress-leaf-502",
        content_hash="hash-profile",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="test-v1",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="strong",
        charge_count=2,
    )
    _seed_parse_attempt(
        conn,
        source_pdf=pdf_profile,
        historical_document_id=3,
        family_key="nc-progress-leaf-502",
        parser_profile="generic_residential",
        status="parsed",
    )

    record_historical_processing_run(
        conn,
        historical_document_id=4,
        source_pdf=pdf_unlinked,
        family_key="nc-progress-leaf-503",
        content_hash="hash-unlinked",
        parser_stage="historical_bulk",
        parser_profile="special_profile",
        parser_version="test-v1",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="strong",
        charge_count=3,
    )
    _seed_parse_attempt(
        conn,
        source_pdf=pdf_unlinked,
        historical_document_id=4,
        family_key="nc-progress-leaf-503",
        parser_profile="special_profile",
        status="parsed",
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            1,
            "2024-01-01",
            "regulator",
            0.9,
            "linked",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-501",
            2,
            "2024-01-01",
            "regulator",
            0.9,
            "linked",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, notes, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-502",
            3,
            "2024-01-01",
            "regulator",
            0.9,
            "linked",
            datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
        ),
    )

    _seed_queue_row(
        conn,
        queue_id=11,
        historical_document_id=1,
        source_pdf=pdf_empty,
        family_key="nc-progress-leaf-500",
        priority=70,
        queue_reason="needs_review:generic_residential",
    )
    _seed_queue_row(
        conn,
        queue_id=12,
        historical_document_id=2,
        source_pdf=pdf_ocr,
        family_key="nc-progress-leaf-501",
        priority=85,
        queue_reason="stale_stage:ocr_backend_version",
        metadata={"stale_reasons": ["ocr_backend_version"]},
    )
    _seed_queue_row(
        conn,
        queue_id=13,
        historical_document_id=3,
        source_pdf=pdf_profile,
        family_key="nc-progress-leaf-502",
        priority=88,
        queue_reason="profile_dependency:progress_residential_tou:family_key,candidate_profile",
        metadata={"impact_profile": "progress_residential_tou", "impact_reasons": ["family_key", "candidate_profile"]},
    )
    _seed_queue_row(
        conn,
        queue_id=14,
        historical_document_id=4,
        source_pdf=pdf_unlinked,
        family_key="nc-progress-leaf-503",
        priority=75,
        queue_reason="manual:inspect",
    )
    conn.commit()

    report = build_reprocess_priority_report(conn, limit=10)
    summary = report["summary"]
    rows = report["rows"]

    assert summary["queue_row_count"] == 4
    assert summary["category_counts"]["empty_parse"] == 1
    assert summary["category_counts"]["ocr_needed"] == 1
    assert summary["category_counts"]["profile_impact"] == 1
    assert summary["category_counts"]["strong_but_unlinked"] == 1

    assert [row["priority_category"] for row in rows] == [
        "empty_parse",
        "ocr_needed",
        "profile_impact",
        "strong_but_unlinked",
    ]
    assert rows[0]["impact_summary"][0] == "latest_outcome=empty"
    assert rows[1]["ocr_queue_status"] == "pending"
    assert rows[2]["impact_profile"] == "progress_residential_tou"
    assert rows[3]["has_version_link"] is False

    conn.close()
