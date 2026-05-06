from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from duke_rates import cli
from duke_rates.db.repository import Repository


def test_repair_legacy_ncuc_data_cli_dry_run_and_execute(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy-ncuc.db"
    repo = Repository(db_path)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()

    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO ncuc_discovery_records (
                docket_number,
                utility,
                filing_title,
                filing_classification,
                referenced_schedule_codes_json,
                referenced_rider_codes_json,
                referenced_leaf_nos_json,
                family_keys_json,
                acquisition_method,
                fetch_status,
                provenance_notes_json,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "E-2, Sub 1219",
                "Duke Energy Progress",
                "Legacy portal harvest row",
                "other",
                "[]",
                "[]",
                "[]",
                '["nc-progress-leaf-602"]',
                "portal_harvest",
                "success",
                "[]",
                now,
            ),
        )
        conn.execute(
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
                "32e921d3-7055-4672-8ef7-949ed489030a",
                "nc-progress-leaf-500",
                "Residential Service",
                "NC",
                "progress",
                "rate",
                "pdf",
                "https://example.test/res.pdf",
                "https://example.test/res.pdf",
                now,
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
                now,
                "{}",
                None,
                1,
                2,
                "{}",
            ),
        )
        conn.commit()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), Repository(db_path)),
    )
    runner = CliRunner()

    dry_run = runner.invoke(cli.app, ["repair-legacy-ncuc-data", "--dry-run"])
    assert dry_run.exit_code == 0
    assert "legacy_portal_harvest=1" in dry_run.stdout
    assert "malformed_historical_current_document_id=1" in dry_run.stdout

    execute = runner.invoke(cli.app, ["repair-legacy-ncuc-data", "--execute"])
    assert execute.exit_code == 0
    assert "portal_harvest->playwright=1" in execute.stdout
    assert "cleared_historical_current_document_id=1" in execute.stdout

    repaired_repo = Repository(db_path)
    report = repaired_repo.audit_legacy_ncuc_data_issues()
    assert report["legacy_portal_harvest_count"] == 0
    assert report["malformed_historical_current_document_id_count"] == 0
