from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from duke_rates import cli
from duke_rates.db.sqlite import connect


def test_build_ocr_remediation_candidates_report_prioritizes_unknown_no_text(tmp_path) -> None:
    db_path = tmp_path / "ocr-remediation.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()

    empty_text = tmp_path / "empty.txt"
    empty_text.write_text("", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO historical_documents (
            id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path,
            raw_text_path, content_hash, effective_start, retrieved_at,
            start_page, end_page, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            "nc-progress-leaf-715",
            "Residential Service Load Control LC (Span 3-5)",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/1",
            "https://archive.test/1",
            now,
            str(tmp_path / "1.pdf"),
            str(empty_text),
            "hash-1",
            "2024-01-01",
            now,
            3,
            5,
            "{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO historical_documents (
            id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path,
            raw_text_path, content_hash, effective_start, retrieved_at,
            start_page, end_page, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            2,
            "nc-progress-leaf-720",
            "Prepaid Advantage Program PPA Compliance Book (Span 1-8)",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/2",
            "https://archive.test/2",
            now,
            str(tmp_path / "2.pdf"),
            str(empty_text),
            "hash-2",
            "2024-02-01",
            now,
            1,
            8,
            "{}",
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
            1,
            str(tmp_path / "1.pdf"),
            "nc-progress-leaf-715",
            "hash-1",
            "historical_bulk",
            "unknown",
            "test",
            "targeted",
            "completed",
            "empty",
            0,
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
            2,
            str(tmp_path / "2.pdf"),
            "nc-progress-leaf-720",
            "hash-2",
            "historical_bulk",
            "unknown",
            "test",
            "targeted",
            "completed",
            "empty",
            0,
            "[]",
            "{}",
            now,
            now,
        ),
    )
    conn.commit()

    report = cli._build_ocr_remediation_candidates_nc_report(conn, limit=10)

    assert report["candidate_count"] == 2
    assert report["route_reason_summary"] == [
        {"route_reason": "no_usable_text_unknown_profile", "count": 2}
    ]
    assert sorted(report["recommended_lane_summary"], key=lambda item: item["recommended_lane"]) == [
        {"recommended_lane": "queue_ocr_or_paddle", "count": 1},
        {"recommended_lane": "run_docling_or_paddle_structure", "count": 1},
    ]
    assert report["rows"][0]["family_key"] == "nc-progress-leaf-715"
    assert report["rows"][0]["recommended_lane"] == "queue_ocr_or_paddle"
    assert report["rows"][1]["family_key"] == "nc-progress-leaf-720"
    assert report["rows"][1]["recommended_lane"] == "run_docling_or_paddle_structure"
    conn.close()


def test_show_ocr_remediation_candidates_nc_cli(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "ocr-remediation-cli.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()

    empty_text = tmp_path / "empty.txt"
    empty_text.write_text("", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO historical_documents (
            id, family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path,
            raw_text_path, content_hash, effective_start, retrieved_at,
            start_page, end_page, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            "nc-progress-leaf-725",
            "Residential Income-Qualified Load Control Program RIQLC (Span 2-8)",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/1",
            "https://archive.test/1",
            now,
            str(tmp_path / "1.pdf"),
            str(empty_text),
            "hash-1",
            "2024-01-01",
            now,
            2,
            8,
            "{}",
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
            1,
            str(tmp_path / "1.pdf"),
            "nc-progress-leaf-725",
            "hash-1",
            "historical_bulk",
            "unknown",
            "test",
            "targeted",
            "completed",
            "empty",
            0,
            "[]",
            "{}",
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (type("S", (), {"database_path": str(db_path)})(), None),
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["show-ocr-remediation-candidates-nc", "--limit", "5"])

    assert result.exit_code == 0
    assert "OCR Remediation Candidates (NC)" in result.stdout
    assert "no_usable_text_unknown_profile" in result.stdout
    assert "run_docling_or_paddle_structure" in result.stdout
