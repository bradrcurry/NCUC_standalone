from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from duke_rates import cli
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.document_classification_audit import (
    build_document_classification_audit_report,
    build_unknown_routing_audit_report,
)


def _seed_historical_document(
    conn,
    *,
    doc_id: int,
    family_key: str,
    title: str,
    company: str = "progress",
    local_path: str,
    effective_start: str = "2024-01-01",
    raw_text: str | None = "sample tariff text",
) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC).isoformat()
    raw_text_path = None
    if raw_text is not None:
        raw_path = Path(local_path).with_suffix(".txt")
        raw_path.write_text(raw_text, encoding="utf-8")
        raw_text_path = str(raw_path)
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
            f"https://example.test/{doc_id}",
            f"https://archive.test/{doc_id}",
            now,
            local_path,
            raw_text_path,
            f"hash-{doc_id}",
            "application/pdf",
            200,
            1,
            None,
            None,
            None,
            effective_start,
            None,
            now,
            "{}",
            None,
            1,
            4,
            "{}",
        ),
    )


def _seed_processing_run(
    conn,
    *,
    doc_id: int,
    parser_profile: str,
    outcome_quality: str,
    status: str = "completed",
    charge_count: int = 0,
    metadata_json: str = "{}",
) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO historical_processing_runs (
            historical_document_id, source_pdf, family_key, content_hash,
            parser_stage, parser_profile, parser_version, processing_mode,
            status, outcome_quality, charge_count, review_flags_json,
            metadata_json, started_at, completed_at
        )
        SELECT
            id, local_path, family_key, content_hash,
            'historical_bulk', ?, 'test', 'targeted',
            ?, ?, ?, '[]', ?, ?, ?
        FROM historical_documents
        WHERE id = ?
        """,
        (
            parser_profile,
            status,
            outcome_quality,
            charge_count,
            metadata_json,
            now,
            now,
            doc_id,
        ),
    )


def test_build_document_classification_audit_report_buckets(tmp_path: Path) -> None:
    db_path = tmp_path / "classification.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 22, tzinfo=UTC).isoformat()

    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-500",
        title="Residential Service",
        local_path=str(tmp_path / "1.pdf"),
    )
    conn.execute(
        """
        INSERT INTO tariff_versions (
            id, family_key, historical_document_id, effective_start,
            revision_label, source_type, confidence_score, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (101, "nc-progress-leaf-500", 1, "2024-01-01", "v1", "historical", 1.0, now),
    )
    conn.execute(
        """
        INSERT INTO tariff_charges (
            version_id, family_key, charge_type, charge_label, rate_value, rate_unit, confidence_score, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (101, "nc-progress-leaf-500", "customer", "Basic Customer Charge", 14.0, "$/month", 1.0, now),
    )
    _seed_processing_run(conn, doc_id=1, parser_profile="progress_residential_flat", outcome_quality="strong", charge_count=1)

    _seed_historical_document(
        conn,
        doc_id=2,
        family_key="nc-progress-leaf-672",
        title="Rider CEI",
        local_path=str(tmp_path / "2.pdf"),
    )
    _seed_processing_run(
        conn,
        doc_id=2,
        parser_profile="skipped_formula",
        outcome_quality="missing",
        status="skipped",
        metadata_json='{"skip_reason":"formula_only_family"}',
    )

    _seed_historical_document(
        conn,
        doc_id=3,
        family_key="nc-progress-leaf-600",
        title="Summary of Rider Adjustments",
        local_path=str(tmp_path / "3.pdf"),
    )
    _seed_processing_run(conn, doc_id=3, parser_profile="generic_residential", outcome_quality="weak")
    conn.execute(
        """
        INSERT INTO document_fingerprints (
            source_pdf, docket_dir, page_start, page_end, leaf_no, schedule_code, title,
            text_length, line_count, numeric_line_count, has_table_rows, has_rider_summary,
            review_flags_json, metadata_json, created_at, doc_quality_tier,
            is_redline_candidate, redline_confidence
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(tmp_path / "3.pdf"),
            "e-2-sub-1000",
            1,
            4,
            "600",
            "RIDER",
            "Summary of Rider Adjustments",
            500,
            20,
            4,
            1,
            1,
            "[]",
            "{}",
            now,
            "T2",
            1,
            0.85,
        ),
    )

    _seed_historical_document(
        conn,
        doc_id=4,
        family_key="nc-progress-doc-application-1",
        title="Order Approving Application",
        local_path=str(tmp_path / "4.pdf"),
    )
    _seed_processing_run(conn, doc_id=4, parser_profile="unknown", outcome_quality="missing", status="skipped")
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records (
            id, docket_number, utility, filing_title, filing_date, filing_classification,
            acquisition_method, fetch_status, local_path, content_hash, content_type,
            provenance_notes_json, metadata_json, created_at, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            401,
            "E-2, Sub 999",
            "Duke Energy Progress",
            "Order Approving Application",
            "2024-01-01",
            "order",
            "playwright",
            "success",
            str(tmp_path / "4.pdf"),
            "hash-4",
            "application/pdf",
            "[]",
            "{}",
            now,
            now,
        ),
    )

    _seed_historical_document(
        conn,
        doc_id=5,
        family_key="nc-carolinas-leaf-999",
        title="Mystery Tariff Leaf",
        company="carolinas",
        local_path=str(tmp_path / "5.pdf"),
        raw_text=None,
    )
    _seed_processing_run(conn, doc_id=5, parser_profile="unknown", outcome_quality="empty")

    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_document_classification_audit_report(repo, limit=10)
    buckets = {row["document_bucket"]: row["count"] for row in report["summary"]["bucket_counts"]}

    assert report["summary"]["historical_document_count"] == 5
    assert buckets["extractable_charge"] == 1
    assert buckets["formula_only"] == 1
    assert buckets["redline_candidate"] == 1
    assert buckets["unrelated_but_keep"] == 1
    assert buckets["needs_normalization"] == 1


def test_show_document_classification_audit_nc_cli(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "classification-cli.db"
    conn = connect(db_path)
    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-carolinas-leaf-999",
        title="Mystery Tariff Leaf",
        company="carolinas",
        local_path=str(tmp_path / "1.pdf"),
        raw_text=None,
    )
    _seed_processing_run(conn, doc_id=1, parser_profile="unknown", outcome_quality="empty")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (type("S", (), {"database_path": str(db_path)})(), Repository(db_path)),
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["show-document-classification-audit-nc", "--limit", "5"])

    assert result.exit_code == 0
    assert "Document Classification Audit (NC)" in result.stdout
    assert "bucket_counts:" in result.stdout
    assert "bucket=needs_normalization" in result.stdout
    assert "lane=queue_ocr_or_paddle" in result.stdout


def test_build_unknown_routing_audit_report_groups_families(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown-routing.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 22, tzinfo=UTC).isoformat()

    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-carolinas-rider-PROSPECTIVERIDER",
        title="Prospective Rider (Span 4-4)",
        company="carolinas",
        local_path=str(tmp_path / "1.pdf"),
    )
    _seed_processing_run(conn, doc_id=1, parser_profile="unknown", outcome_quality="empty")

    _seed_historical_document(
        conn,
        doc_id=2,
        family_key="nc-carolinas-rider-PROSPECTIVERIDER",
        title="Prospective Rider (Span 25-25)",
        company="carolinas",
        local_path=str(tmp_path / "2.pdf"),
    )
    _seed_processing_run(conn, doc_id=2, parser_profile="unknown", outcome_quality="empty")

    _seed_historical_document(
        conn,
        doc_id=3,
        family_key="nc-progress-doc-application-1",
        title="Order Approving Application",
        company="progress",
        local_path=str(tmp_path / "3.pdf"),
    )
    _seed_processing_run(conn, doc_id=3, parser_profile="unknown", outcome_quality="missing", status="skipped")
    conn.execute(
        """
        INSERT INTO ncuc_discovery_records (
            id, docket_number, utility, filing_title, filing_date, filing_classification,
            acquisition_method, fetch_status, local_path, content_hash, content_type,
            provenance_notes_json, metadata_json, created_at, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            301,
            "E-2, Sub 1",
            "Duke Energy Progress",
            "Order Approving Application",
            "2024-01-01",
            "order",
            "playwright",
            "success",
            str(tmp_path / "3.pdf"),
            "hash-3",
            "application/pdf",
            "[]",
            "{}",
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_unknown_routing_audit_report(repo, limit=10)

    assert report["summary"]["problem_document_count"] == 2
    assert report["summary"]["problem_family_count"] == 1
    assert report["rows"][0]["family_key"] == "nc-carolinas-rider-PROSPECTIVERIDER"
    assert report["rows"][0]["recommended_action"] == "evaluate_formula_or_program_lane"


def test_unknown_routing_audit_prioritizes_no_text_as_ocr_remediation(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown-routing-no-text.db"
    conn = connect(db_path)

    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-725",
        title="Residential Income-Qualified Load Control Program RIQLC (Span 2-8)",
        company="progress",
        local_path=str(tmp_path / "1.pdf"),
        raw_text=None,
    )
    _seed_processing_run(conn, doc_id=1, parser_profile="unknown", outcome_quality="empty")
    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_unknown_routing_audit_report(repo, limit=10)

    assert report["summary"]["problem_document_count"] == 1
    assert report["rows"][0]["family_key"] == "nc-progress-leaf-725"
    assert report["rows"][0]["recommended_action"] == "enqueue_ocr_remediation"
    assert report["rows"][0]["top_normalization_lane"] == "queue_ocr_or_paddle"


def test_unknown_routing_audit_prioritizes_usable_text_without_run_as_reprocess(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown-routing-needs-processing.db"
    conn = connect(db_path)

    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-progress-leaf-602",
        title="Joint Agency Asset Rider JAA (Span 1-1)",
        company="progress",
        local_path=str(tmp_path / "1.pdf"),
        raw_text="JOINT AGENCY ASSET RIDER JAA\nMONTHLY RATE\nResidential 0.00464",
    )
    conn.commit()
    conn.close()

    repo = Repository(db_path)
    report = build_unknown_routing_audit_report(repo, limit=10)

    assert report["summary"]["problem_document_count"] == 1
    assert report["rows"][0]["family_key"] == "nc-progress-leaf-602"
    assert report["rows"][0]["recommended_action"] == "enqueue_reprocess"
    assert report["rows"][0]["historical_document_ids"] == [1]
    assert report["rows"][0]["action_historical_document_ids"] == [1]


def test_show_parser_improvement_candidates_nc_cli(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "parser-improvement.db"
    conn = connect(db_path)
    _seed_historical_document(
        conn,
        doc_id=1,
        family_key="nc-carolinas-rider-PROSPECTIVERIDER",
        title="Prospective Rider (Span 4-4)",
        company="carolinas",
        local_path=str(tmp_path / "1.pdf"),
        raw_text=None,
    )
    _seed_processing_run(conn, doc_id=1, parser_profile="unknown", outcome_quality="empty")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (type("S", (), {"database_path": str(db_path)})(), Repository(db_path)),
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["show-parser-improvement-candidates-nc", "--limit", "5"])

    assert result.exit_code == 0
    assert "Parser Improvement Candidates (NC)" in result.stdout
    assert "action=enqueue_ocr_remediation" in result.stdout
    assert "next=python -m duke_rates ocr enqueue-remediation-nc --limit 10 --family-key nc-carolinas-rider-PROSPECTIVERIDER" in result.stdout
