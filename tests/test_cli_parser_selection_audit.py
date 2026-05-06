from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from duke_rates import cli
from duke_rates.db.sqlite import connect


def test_build_parser_selection_audit_report_summarizes_fallbacks(tmp_path) -> None:
    conn = connect(tmp_path / "parser-selection.db")
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()

    for idx, family_key in enumerate(("nc-progress-leaf-500", "nc-progress-leaf-502"), start=1):
        conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp, local_path,
                content_hash, effective_start, retrieved_at, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                f"Test title {idx}",
                "NC",
                "progress",
                "rate",
                "pdf",
                f"https://example.test/{idx}",
                f"https://archive.test/{idx}",
                now,
                str(tmp_path / f"{idx}.pdf"),
                f"hash-{idx}",
                f"2024-0{idx}-01",
                now,
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
            "nc-progress-leaf-500",
            "hash-1",
            "historical_bulk",
            "generic_residential",
            "test-version",
            "targeted",
            "completed",
            "weak",
            1,
            "[]",
            json.dumps(
                {
                    "selection": {
                        "initial_parser_profile": "progress_residential_tou",
                        "final_parser_profile": "generic_residential",
                        "fallback_applied": True,
                        "fallback_triggered_by": "weak",
                        "fallback_reason": "material_charge_gain",
                        "initial_outcome_quality": "weak",
                        "final_outcome_quality": "weak",
                    }
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
            2,
            str(tmp_path / "2.pdf"),
            "nc-progress-leaf-502",
            "hash-2",
            "historical_bulk",
            "progress_residential_tou",
            "test-version",
            "targeted",
            "completed",
            "strong",
            3,
            "[]",
            json.dumps(
                {
                    "selection": {
                        "initial_parser_profile": "progress_residential_tou",
                        "final_parser_profile": "progress_residential_tou",
                        "fallback_applied": False,
                        "initial_outcome_quality": "strong",
                        "final_outcome_quality": "strong",
                    }
                },
                sort_keys=True,
            ),
            now,
            now,
        ),
    )
    conn.commit()

    report = cli._build_parser_selection_audit_nc_report(conn, limit=10)

    assert report["summary"]["latest_run_count"] == 2
    assert report["summary"]["fallback_applied_count"] == 1
    assert report["summary"]["generic_final_profile_count"] == 1
    assert report["summary"]["weak_count"] == 1
    assert report["summary"]["strong_count"] == 1
    assert report["top_profile_transitions"] == [
        {"transition": "progress_residential_tou -> generic_residential", "count": 1}
    ]
    assert report["fallback_reason_summary"] == [
        {"reason": "material_charge_gain", "count": 1}
    ]
    conn.close()


def test_show_parser_selection_audit_nc_cli(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "parser-selection-cli.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path,
            content_hash, effective_start, retrieved_at, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Residential Service",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/1",
            "https://archive.test/1",
            now,
            str(tmp_path / "1.pdf"),
            "hash-1",
            "2024-01-01",
            now,
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
            "nc-progress-leaf-500",
            "hash-1",
            "historical_bulk",
            "generic_residential",
            "test-version",
            "targeted",
            "completed",
            "weak",
            1,
            "[]",
            json.dumps(
                {
                    "selection": {
                        "initial_parser_profile": "progress_residential_tou",
                        "final_parser_profile": "generic_residential",
                        "fallback_applied": True,
                        "fallback_triggered_by": "weak",
                        "fallback_reason": "material_charge_gain",
                        "initial_outcome_quality": "weak",
                        "final_outcome_quality": "weak",
                    }
                },
                sort_keys=True,
            ),
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
    result = runner.invoke(cli.app, ["show-parser-selection-audit-nc", "--limit", "5"])

    assert result.exit_code == 0
    assert "Parser Selection Audit (NC)" in result.stdout
    assert "fallback_applied=1" in result.stdout
    assert "generic_final=1" in result.stdout
    assert "progress_residential_tou -> generic_residential" in result.stdout
