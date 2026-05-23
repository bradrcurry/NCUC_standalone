from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import UTC, datetime

from typer.testing import CliRunner

from duke_rates import cli
from duke_rates.cli_commands import reprocess as reprocess_module
from duke_rates.cli_commands import lineage as lineage_module
from duke_rates.db.reprocess import (
    claim_next_historical_reprocess,
    complete_historical_reprocess,
    enqueue_profile_impacted_historical_documents,
    enqueue_historical_reprocess,
    enqueue_reprocess_candidates_from_review_queue,
    enqueue_specific_historical_documents,
    enqueue_stale_historical_documents,
    find_profile_impacted_historical_documents,
    find_stale_historical_documents,
    find_stale_running_historical_reprocess_queue,
    latest_processing_run_for_document,
    recover_stale_running_historical_reprocess_queue,
    record_historical_processing_run,
)
from duke_rates.db.artifact_cache import save_page_artifacts, save_span_artifacts
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.historical.ncuc.pipeline.stage_versions import OCR_NORMALIZATION_VERSION
from duke_rates.models.pipeline import PageEvidence, PipelineRoute, TariffSpan


GENERIC_RESIDENTIAL_TEXT = """
Schedule RES
Customer Charge $14.00 per month
Energy Charge 12.34¢ per kWh
"""


def test_enqueue_reprocess_candidates_from_review_queue_is_targeted_and_deduped(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    doc_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Progress Residential",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/progress-500.pdf",
            "https://archive.test/progress-500",
            "2026-03-26T00:00:00Z",
            "data/historical/ncuc/e-2-sub-1300/progress-500.pdf",
            "hash-progress-500",
            "2024-01-01",
            now,
        ),
    ).lastrowid

    attempt_id = conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/e-2-sub-1300/progress-500.pdf",
            "e-2-sub-1300",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.41,
            "DEP",
            "RES",
            "2024-01-01",
            1,
            json.dumps(["generic_fallback_selected"]),
            json.dumps(
                {
                    "historical_document_id": doc_id,
                    "family_key": "nc-progress-leaf-500",
                    "company": "progress",
                },
                sort_keys=True,
            ),
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
            "data/historical/ncuc/e-2-sub-1300/progress-500.pdf",
            "e-2-sub-1300",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "DEP",
            "rule",
            "needs_review",
            0,
            json.dumps({"outcome_quality": "weak"}, sort_keys=True),
            "{}",
            now,
        ),
    )
    conn.commit()

    report = enqueue_reprocess_candidates_from_review_queue(conn, requested_by="test-suite")
    conn.commit()

    assert report["inserted"] == 1
    assert report["skipped"] == 0

    row = conn.execute(
        """
        SELECT historical_document_id, family_key, priority, queue_reason, requested_by, status, metadata_json
        FROM historical_reprocess_queue
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["historical_document_id"] == doc_id
    assert row["family_key"] == "nc-progress-leaf-500"
    assert row["priority"] >= 80
    assert row["queue_reason"] == "needs_review:generic_residential"
    assert row["requested_by"] == "test-suite"
    assert row["status"] == "pending"
    assert json.loads(row["metadata_json"])["parse_attempt_id"] == attempt_id

    report_again = enqueue_reprocess_candidates_from_review_queue(conn, requested_by="test-suite")
    conn.commit()
    assert report_again["inserted"] == 0
    assert report_again["skipped"] >= 1
    conn.close()


def test_enqueue_specific_historical_documents_queues_by_hd_id(tmp_path) -> None:
    conn = connect(tmp_path / "direct-requeue.db")
    now = datetime(2026, 4, 16, tzinfo=UTC).isoformat()

    historical_id = int(
        conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "nc-progress-leaf-704",
                "RSSEE",
                "NC",
                "progress",
                "rate",
                "pdf",
                "https://example.test/704.pdf",
                "https://archive.test/704",
                "2026-04-16T00:00:00Z",
                "data/historical/ncuc/e-2-sub-1300/leaf-704.pdf",
                "hash-704",
                "2023-10-01",
                now,
            ),
        ).lastrowid
    )
    conn.commit()

    report = enqueue_specific_historical_documents(
        conn,
        historical_document_ids=[historical_id, 999999],
        priority=81,
        requested_by="test-suite",
        queue_reason="manual_requeue",
    )
    conn.commit()

    assert report["inserted"] == 1
    assert report["skipped"] == 1
    assert report["missing_ids"] == [999999]
    row = conn.execute(
        """
        SELECT historical_document_id, source_pdf, family_key, priority, queue_reason, requested_by
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert row is not None
    assert row["historical_document_id"] == historical_id
    assert row["source_pdf"] == "data/historical/ncuc/e-2-sub-1300/leaf-704.pdf"
    assert row["family_key"] == "nc-progress-leaf-704"
    assert row["priority"] == 81
    assert row["queue_reason"] == "manual_requeue"
    assert row["requested_by"] == "test-suite"


def test_enqueue_reprocess_cli_hd_id_does_not_pull_needs_review_by_default(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cli-direct-requeue.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 16, tzinfo=UTC).isoformat()

    target_historical_id = int(
        conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "nc-progress-leaf-669",
                "Net Metering Bridge Rider NMB",
                "NC",
                "progress",
                "rider",
                "pdf",
                "https://example.test/669.pdf",
                "https://archive.test/669",
                "2026-04-16T00:00:00Z",
                "data/raw/nc/progress/rider/leaf-no-669.pdf",
                "hash-669",
                "2026-01-01",
                now,
            ),
        ).lastrowid
    )
    review_historical_id = int(
        conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "nc-carolinas-rider-RIDEREDIT3",
                "RIDER EDIT-3",
                "NC",
                "carolinas",
                "rider",
                "pdf",
                "https://example.test/edit3.pdf",
                "https://archive.test/edit3",
                "2026-04-16T00:00:00Z",
                "data/historical/ncuc/e-7-sub-1146/edit3.pdf",
                "hash-edit3",
                "2018-01-01",
                now,
            ),
        ).lastrowid
    )
    attempt_id = int(
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage,
                parser_profile, status, confidence, utility, schedule_code,
                effective_date, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "data/historical/ncuc/e-7-sub-1146/edit3.pdf",
                "e-7-sub-1146",
                1,
                1,
                "historical_bulk",
                "generic_residential",
                "parsed",
                0.2,
                "DEC",
                "RIDEREDIT3",
                "2018-01-01",
                0,
                json.dumps(["no_charges_extracted"]),
                json.dumps(
                    {
                        "historical_document_id": review_historical_id,
                        "family_key": "nc-carolinas-rider-RIDEREDIT3",
                        "company": "carolinas",
                    },
                    sort_keys=True,
                ),
                now,
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
            "data/historical/ncuc/e-7-sub-1146/edit3.pdf",
            "e-7-sub-1146",
            1,
            1,
            "historical_bulk",
            "generic_residential",
            "DEC",
            "rule",
            "needs_review",
            0,
            json.dumps({"outcome_quality": "weak"}, sort_keys=True),
            "{}",
            now,
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), None),
    )
    monkeypatch.setattr(
        reprocess_module,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), None),
    )
    runner = CliRunner()

    result = runner.invoke(
        cli.app,
        ["reprocess", "enqueue-nc", "--hd-id", str(target_historical_id), "--requested-by", "test-suite"],
    )

    assert result.exit_code == 0
    conn = connect(db_path)
    rows = conn.execute(
        """
        SELECT historical_document_id, queue_reason, requested_by
        FROM historical_reprocess_queue
        ORDER BY historical_document_id
        """
    ).fetchall()
    conn.close()

    assert [row["historical_document_id"] for row in rows] == [target_historical_id]
    assert rows[0]["queue_reason"] == "manual_requeue"
    assert rows[0]["requested_by"] == "test-suite"


def test_show_reprocess_queue_cli_tolerates_non_console_source_pdf(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cli-show-requeue.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 16, tzinfo=UTC).isoformat()
    historical_id = int(
        conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "nc-progress-leaf-500",
                "Residential Service RS",
                "NC",
                "progress",
                "rate",
                "pdf",
                "https://example.test/500.pdf",
                "https://archive.test/500",
                "2026-04-16T00:00:00Z",
                "data/historical/ncuc/e-2-sub-1142-compliance/\u008b05af8-bad.pdf",
                "hash-500",
                "2026-01-01",
                now,
            ),
        ).lastrowid
    )
    enqueue_historical_reprocess(
        conn,
        historical_document_id=historical_id,
        source_pdf="data/historical/ncuc/e-2-sub-1142-compliance/\u008b05af8-bad.pdf",
        family_key="nc-progress-leaf-500",
        queue_reason="manual_recheck",
        requested_by="test-suite",
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), None),
    )
    monkeypatch.setattr(
        reprocess_module,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), None),
    )
    monkeypatch.setattr(
        cli,
        "sys",
        SimpleNamespace(stdout=SimpleNamespace(encoding="cp1252")),
    )
    runner = CliRunner()

    result = runner.invoke(cli.app, ["reprocess", "show-queue-nc", "--status", "all", "--limit", "10"])

    assert result.exit_code == 0
    assert "manual_recheck" in result.stdout
    conn.close()


def test_recover_stale_running_historical_reprocess_queue_resets_old_running_rows(tmp_path) -> None:
    conn = connect(tmp_path / "stale-running.db")
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    doc_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Progress Residential",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/stale-running.pdf",
            "https://archive.test/stale-running",
            now,
            str(tmp_path / "stale-running.pdf"),
            "hash-stale-running",
            "2024-01-01",
            now,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            historical_document_id, source_pdf, family_key, priority, queue_reason,
            requested_by, status, metadata_json, requested_at, started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_id,
            str(tmp_path / "stale-running.pdf"),
            "nc-progress-leaf-500",
            80,
            "manual_recheck",
            "test-suite",
            "running",
            json.dumps({"seed": True}, sort_keys=True),
            "2026-04-01T00:00:00+00:00",
            "2026-04-01T00:00:00+00:00",
        ),
    )
    conn.commit()

    stale_before = find_stale_running_historical_reprocess_queue(
        conn,
        older_than_minutes=240,
        limit=10,
    )
    assert len(stale_before) == 1
    assert stale_before[0]["queue_id"] == 1

    report = recover_stale_running_historical_reprocess_queue(
        conn,
        older_than_minutes=240,
        limit=10,
        requested_by="test-suite",
    )
    conn.commit()

    assert report["recovered"] == 1
    row = conn.execute(
        """
        SELECT status, latest_run_id, error_message, started_at, completed_at, metadata_json
        FROM historical_reprocess_queue
        WHERE id = 1
        """
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "pending"
    assert row["latest_run_id"] is None
    assert row["error_message"] is None
    assert row["started_at"] is None
    assert row["completed_at"] is None
    metadata = json.loads(row["metadata_json"] or "{}")
    assert metadata["recovered_from_stale_running"] is True
    assert metadata["recovered_by"] == "test-suite"


def test_recover_stale_reprocess_cli_requeues_stale_running_rows(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cli-stale-running.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 1, tzinfo=UTC).isoformat()
    doc_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Progress Residential",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/stale-cli.pdf",
            "https://archive.test/stale-cli",
            now,
            str(tmp_path / "stale-cli.pdf"),
            "hash-stale-cli",
            "2024-01-01",
            now,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            historical_document_id, source_pdf, family_key, priority, queue_reason,
            requested_by, status, metadata_json, requested_at, started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_id,
            str(tmp_path / "stale-cli.pdf"),
            "nc-progress-leaf-500",
            80,
            "manual_recheck",
            "test-suite",
            "running",
            "{}",
            "2026-04-01T00:00:00+00:00",
            "2026-04-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), None),
    )
    monkeypatch.setattr(
        reprocess_module,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), None),
    )
    runner = CliRunner()

    dry_run = runner.invoke(cli.app, ["reprocess", "recover-stale-nc", "--limit", "10"])
    assert dry_run.exit_code == 0
    assert "dry_run" in dry_run.stdout

    execute = runner.invoke(cli.app, ["reprocess", "recover-stale-nc", "--limit", "10", "--execute"])
    assert execute.exit_code == 0
    assert "execute" in execute.stdout

    conn = connect(db_path)
    row = conn.execute(
        "SELECT status, started_at, completed_at FROM historical_reprocess_queue WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["status"] == "pending"
    assert row["started_at"] is None
    assert row["completed_at"] is None


def test_add_historical_document_nc_cli_registers_bounded_slice(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cli-add-historical.db"
    pdf_path = tmp_path / "sub1023_2013_corrections.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test pdf bytes")

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), cli.Repository(db_path)),
    )
    monkeypatch.setattr(
        reprocess_module,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), cli.Repository(db_path)),
    )
    monkeypatch.setattr(
        lineage_module,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), cli.Repository(db_path)),
    )
    runner = CliRunner()

    result = runner.invoke(
        cli.app,
        [
            "lineage", "add-historical-document-nc",
            "--family-key",
            "nc-progress-leaf-501",
            "--company",
            "progress",
            "--local-path",
            str(pdf_path),
            "--archived-url",
            "https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=d373a560-e247-4a9a-b054-4fa5727dddf1",
            "--title",
            "DEP Corrections to Revised Tariffs Filed June 3, 2013",
            "--start-page",
            "4",
            "--end-page",
            "6",
            "--effective-start",
            "2013-06-01",
            "--revision-label",
            "Schedule R-TOUD-24A (Leaf No. 501)",
            "--supersedes-label",
            "Schedule R-TOUD-24",
            "--leaf-no",
            "501",
        ],
    )

    assert result.exit_code == 0
    conn = connect(db_path)
    row = conn.execute(
        """
        SELECT family_key, company, start_page, end_page, effective_start, revision_label, supersedes_label, leaf_no, local_path
        FROM historical_documents
        """
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["family_key"] == "nc-progress-leaf-501"
    assert row["company"] == "progress"
    assert row["start_page"] == 4
    assert row["end_page"] == 6
    assert row["effective_start"] == "2013-06-01"
    assert row["revision_label"] == "Schedule R-TOUD-24A (Leaf No. 501)"
    assert row["supersedes_label"] == "Schedule R-TOUD-24"
    assert row["leaf_no"] == "501"
    assert str(pdf_path) == row["local_path"]


def test_enqueue_reprocess_candidates_from_review_queue_applies_family_filter_before_limit(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    doc_ids: list[int] = []
    for index, family_key in enumerate(
        [
            "nc-progress-leaf-500",
            "nc-progress-leaf-501",
            "nc-progress-leaf-502",
            "nc-progress-leaf-672",
        ],
        start=1,
    ):
        doc_id = conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                family_key,
                "NC",
                "progress",
                "rate",
                "pdf",
                f"https://example.test/{family_key}.pdf",
                f"https://archive.test/{family_key}",
                "2026-03-26T00:00:00Z",
                f"data/historical/{family_key}.pdf",
                f"hash-{family_key}",
                "2024-01-01",
                now,
            ),
        ).lastrowid
        attempt_id = conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage,
                parser_profile, status, confidence, utility, schedule_code,
                effective_date, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"data/historical/{family_key}.pdf",
                "test-docket",
                index,
                index,
                "historical_bulk",
                "generic_residential",
                "empty",
                0.1 * index,
                "DEP",
                None,
                "2024-01-01",
                0,
                json.dumps(["generic_fallback_selected"]),
                json.dumps(
                    {
                        "historical_document_id": doc_id,
                        "family_key": family_key,
                        "company": "progress",
                    },
                    sort_keys=True,
                ),
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
                f"data/historical/{family_key}.pdf",
                "test-docket",
                index,
                index,
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
        doc_ids.append(doc_id)

    conn.commit()

    report = enqueue_reprocess_candidates_from_review_queue(
        conn,
        limit=1,
        requested_by="test-suite",
        family_key="nc-progress-leaf-672",
    )
    conn.commit()

    assert report["inserted"] == 1
    row = conn.execute(
        """
        SELECT historical_document_id, family_key, requested_by
        FROM historical_reprocess_queue
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["historical_document_id"] == doc_ids[-1]
    assert row["family_key"] == "nc-progress-leaf-672"
    assert row["requested_by"] == "test-suite"
    conn.close()


def test_processing_run_is_recorded_and_queue_item_can_complete(tmp_path) -> None:
    db_path = tmp_path / "historical.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "progress-500.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "NC",
            "progress",
            "leaf-500",
            "RES",
            "rate_schedule",
            "Progress Residential",
            now,
            now,
        ),
    )
    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Progress Residential",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/progress-500.pdf",
            "https://archive.test/progress-500",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-progress-500",
            "2024-01-01",
            now,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            historical_id,
            "2024-01-01",
            "historical_ncuc",
            0.9,
            now,
        ),
    )
    queue_id, inserted = enqueue_historical_reprocess(
        conn,
        historical_document_id=historical_id,
        source_pdf=str(pdf_path),
        family_key="nc-progress-leaf-500",
        queue_reason="manual:test",
        requested_by="test-suite",
    )
    conn.commit()
    assert inserted is True
    assert queue_id is not None

    claimed = claim_next_historical_reprocess(conn)
    conn.commit()
    assert claimed is not None
    assert claimed["id"] == queue_id
    assert claimed["status"] == "running"
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (GENERIC_RESIDENTIAL_TEXT, "test")
    doc = extractor.get_document_for_extraction(historical_id)
    assert doc is not None
    doc_id, family_key, inserted_count, _, _ = extractor.process_document(doc)
    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-500"
    assert inserted_count >= 1

    conn = connect(db_path)
    latest_run = latest_processing_run_for_document(conn, historical_document_id=historical_id)
    assert latest_run is not None
    assert latest_run["family_key"] == "nc-progress-leaf-500"
    assert latest_run["parser_stage"] == "historical_bulk"
    assert latest_run["processing_mode"] == "historical_bulk"
    assert latest_run["status"] == "parsed"
    assert latest_run["content_hash"] == "hash-progress-500"

    complete_historical_reprocess(
        conn,
        queue_id=queue_id,
        status="completed",
        latest_run_id=latest_run["id"],
        metadata={"charges_inserted": inserted_count},
    )
    conn.commit()

    completed = conn.execute(
        """
        SELECT status, latest_run_id, metadata_json
        FROM historical_reprocess_queue
        WHERE id = ?
        """,
        (queue_id,),
    ).fetchone()
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["latest_run_id"] == latest_run["id"]
    assert json.loads(completed["metadata_json"])["charges_inserted"] == inserted_count
    conn.close()


def test_process_reprocess_queue_bootstraps_missing_tariff_version(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bootstrap-reprocess.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "bootstrap-progress-500.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "NC",
            "progress",
            "leaf-500",
            "RES",
            "rate_schedule",
            "Progress Residential",
            now,
            now,
        ),
    )
    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Progress Residential",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/bootstrap-progress-500.pdf",
            "https://archive.test/bootstrap-progress-500",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-bootstrap-progress-500",
            "2024-01-01",
            now,
        ),
    ).lastrowid
    queue_id, inserted = enqueue_historical_reprocess(
        conn,
        historical_document_id=historical_id,
        source_pdf=str(pdf_path),
        family_key="nc-progress-leaf-500",
        queue_reason="stale_stage:parser_version",
        requested_by="test-suite",
    )
    conn.commit()
    conn.close()

    assert inserted is True
    assert queue_id is not None

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=db_path), None),
    )
    monkeypatch.setattr(
        reprocess_module,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=db_path), None),
    )

    def _fake_process_document(self, doc: dict) -> tuple[int, str, int, str, str | None]:
        return int(doc["id"]), str(doc["family_key"]), 0, "parsed", None

    monkeypatch.setattr(BulkExtractor, "process_document", _fake_process_document)

    cli.process_reprocess_queue_nc(limit=1)

    conn = connect(db_path)
    version_row = conn.execute(
        """
        SELECT id, family_key, historical_document_id, effective_start, source_type, confidence_score, notes
        FROM tariff_versions
        WHERE historical_document_id = ?
        """,
        (historical_id,),
    ).fetchone()
    assert version_row is not None
    assert version_row["family_key"] == "nc-progress-leaf-500"
    assert version_row["effective_start"] == "2024-01-01"
    assert version_row["source_type"] == "regulator"
    assert version_row["confidence_score"] == 0.5
    assert "Bootstrapped for historical reprocess queue." in (version_row["notes"] or "")

    completed = conn.execute(
        """
        SELECT status, error_message, metadata_json
        FROM historical_reprocess_queue
        WHERE id = ?
        """,
        (queue_id,),
    ).fetchone()
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["error_message"] is None
    metadata = json.loads(completed["metadata_json"] or "{}")
    assert metadata["version_bootstrapped"] is True
    assert metadata["version_id"] == version_row["id"]
    conn.close()


def test_refresh_historical_artifacts_for_reprocess_rebuilds_page_and_span_cache(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "artifact-refresh.db"
    conn = connect(db_path)
    conn.close()

    pdf_path = tmp_path / "artifact-refresh.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "triage_pdf",
        lambda _path: SimpleNamespace(
            route_recommendation=PipelineRoute.TEXT_PARSE,
            ocr_confidence_score=0.0,
            gpu_ocr_candidate=False,
        ),
    )
    monkeypatch.setattr(
        reprocess_module,
        "triage_pdf",
        lambda _path: SimpleNamespace(
            route_recommendation=PipelineRoute.TEXT_PARSE,
            ocr_confidence_score=0.0,
            gpu_ocr_candidate=False,
        ),
    )
    monkeypatch.setattr(
        cli,
        "mine_document_pages",
        lambda _path: [
            PageEvidence(
                page_number=1,
                text_length=42,
                text_content="Schedule RES Customer Charge $14.00 per month",
                has_schedule_heading=True,
            )
        ],
    )
    monkeypatch.setattr(
        reprocess_module,
        "mine_document_pages",
        lambda _path: [
            PageEvidence(
                page_number=1,
                text_length=42,
                text_content="Schedule RES Customer Charge $14.00 per month",
                has_schedule_heading=True,
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "segment_document",
        lambda pages, parent_discovery_id=None: [
            TariffSpan(
                start_page=1,
                end_page=1,
                doc_type="tariff",
                confidence=0.9,
            )
        ],
    )
    monkeypatch.setattr(
        reprocess_module,
        "segment_document",
        lambda pages, parent_discovery_id=None: [
            TariffSpan(
                start_page=1,
                end_page=1,
                doc_type="tariff",
                confidence=0.9,
            )
        ],
    )

    report = cli._refresh_historical_artifacts_for_reprocess(
        db_path,
        source_pdf=str(pdf_path),
        file_hash="hash-artifact-refresh",
        stale_reasons=["page_artifact_missing", "span_artifact_missing"],
    )
    assert report == {"page_refreshed": True, "span_refreshed": True}

    conn = connect(db_path)
    page_row = conn.execute(
        """
        SELECT page_number, text_content, metadata_json
        FROM ncuc_page_artifacts
        WHERE source_pdf = ? AND file_hash = ?
        """,
        (str(pdf_path), "hash-artifact-refresh"),
    ).fetchone()
    assert page_row is not None
    assert page_row["page_number"] == 1
    assert "Schedule RES" in (page_row["text_content"] or "")

    span_row = conn.execute(
        """
        SELECT start_page, end_page, doc_type, metadata_json
        FROM ncuc_span_artifacts
        WHERE source_pdf = ? AND file_hash = ?
        """,
        (str(pdf_path), "hash-artifact-refresh"),
    ).fetchone()
    assert span_row is not None
    assert span_row["start_page"] == 1
    assert span_row["end_page"] == 1
    assert span_row["doc_type"] == "tariff"
    assert "historical_reprocess_queue" in (span_row["metadata_json"] or "")
    conn.close()


def test_find_stale_historical_documents_detects_stage_version_mismatches(tmp_path) -> None:
    conn = connect(tmp_path / "stale.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "stale-progress.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-500",
            "Progress Residential",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/progress-500.pdf",
            "https://archive.test/progress-500",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-stale",
            "2024-01-01",
            now,
        ),
    ).lastrowid

    save_page_artifacts(
        conn,
        discovery_record_id=None,
        source_pdf=str(pdf_path),
        file_hash="hash-stale",
        pages=[PageEvidence(page_number=1, text_length=10, text_content="OCR text")],
        artifact_version="page_miner_old",
        metadata={
            "artifact_source": "ocr",
            "ocr_backend_version": "old_backend",
            "ocr_normalization_version": "old_normalization",
        },
    )
    save_span_artifacts(
        conn,
        discovery_record_id=None,
        source_pdf=str(pdf_path),
        file_hash="hash-stale",
        spans=[TariffSpan(start_page=1, end_page=1, doc_type="tariff")],
        artifact_version="segmentation_old",
    )
    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf=str(pdf_path),
        family_key="nc-progress-leaf-500",
        content_hash="hash-stale",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="historical_bulk_v1",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="weak",
        charge_count=1,
    )
    conn.commit()

    stale = find_stale_historical_documents(conn, limit=10)
    assert len(stale) == 1
    row = stale[0]
    assert row["historical_document_id"] == historical_id
    assert "page_artifact_version" in row["reasons"]
    assert "span_artifact_version" in row["reasons"]
    assert "parser_version" in row["reasons"]
    assert "ocr_backend_version" in row["reasons"]
    assert "ocr_normalization_version" in row["reasons"]


def test_refresh_historical_artifacts_for_reprocess_reacts_to_ocr_normalization_version(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "refresh-ocr-normalization.db"
    pdf_path = tmp_path / "ocr-refresh.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        cli,
        "triage_pdf",
        lambda _path: SimpleNamespace(
            route_recommendation=PipelineRoute.OCR_REQUIRED,
            confidence_score=0.9,
            ocr_confidence_score=0.95,
            native_text_quality_score=0.1,
            reading_order_risk_score=0.0,
            gpu_ocr_candidate=False,
            table_mode_candidate="scanned_text",
            document_archetype_candidate="scanned_bundle",
            native_text_backend="pymupdf",
        ),
    )
    monkeypatch.setattr(
        reprocess_module,
        "triage_pdf",
        lambda _path: SimpleNamespace(
            route_recommendation=PipelineRoute.OCR_REQUIRED,
            confidence_score=0.9,
            ocr_confidence_score=0.95,
            native_text_quality_score=0.1,
            reading_order_risk_score=0.0,
            gpu_ocr_candidate=False,
            table_mode_candidate="scanned_text",
            document_archetype_candidate="scanned_bundle",
            native_text_backend="pymupdf",
        ),
    )
    monkeypatch.setattr(
        cli,
        "extract_ocr_document_pages",
        lambda _path: [PageEvidence(page_number=1, text_length=8, text_content="OCR text")],
    )
    monkeypatch.setattr(
        reprocess_module,
        "extract_ocr_document_pages",
        lambda _path: [PageEvidence(page_number=1, text_length=8, text_content="OCR text")],
    )
    monkeypatch.setattr(
        cli,
        "load_ocr_sidecar_payload",
        lambda _path: {
            "backend": "ocrmypdf_tesseract",
            "backend_version": "backend-v",
            "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
            "page_count": 1,
            "metadata": {"attempted_backends": ["ocrmypdf_tesseract"]},
            "pages": [{"page_number": 1, "text_length": 8, "text_content": "OCR text"}],
        },
    )
    monkeypatch.setattr(
        reprocess_module,
        "load_ocr_sidecar_payload",
        lambda _path: {
            "backend": "ocrmypdf_tesseract",
            "backend_version": "backend-v",
            "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
            "page_count": 1,
            "metadata": {"attempted_backends": ["ocrmypdf_tesseract"]},
            "pages": [{"page_number": 1, "text_length": 8, "text_content": "OCR text"}],
        },
    )
    monkeypatch.setattr(
        cli,
        "segment_document",
        lambda pages, parent_discovery_id=None: [TariffSpan(start_page=1, end_page=1, doc_type="tariff")],
    )
    monkeypatch.setattr(
        reprocess_module,
        "segment_document",
        lambda pages, parent_discovery_id=None: [TariffSpan(start_page=1, end_page=1, doc_type="tariff")],
    )

    report = cli._refresh_historical_artifacts_for_reprocess(
        db_path,
        source_pdf=str(pdf_path),
        file_hash="hash-ocr-normalization",
        stale_reasons=["ocr_normalization_version"],
    )

    assert report == {"page_refreshed": True, "span_refreshed": True}
    conn = connect(db_path)
    row = conn.execute(
        "SELECT metadata_json FROM ncuc_page_artifacts WHERE source_pdf = ? AND file_hash = ?",
        (str(pdf_path), "hash-ocr-normalization"),
    ).fetchone()
    assert row is not None
    metadata = json.loads(row["metadata_json"] or "{}")
    assert metadata["ocr_normalization_version"] == OCR_NORMALIZATION_VERSION
    assert metadata["selected_backend"] == "ocrmypdf_tesseract"
    conn.close()


def test_find_stale_historical_documents_skips_parser_run_missing_rows_without_effective_start(tmp_path) -> None:
    conn = connect(tmp_path / "stale-null-effective.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "null-effective.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-carolinas-doc-EFFECTIVEFORSERVICE",
            "Effective for service",
            "NC",
            "carolinas",
            "rate",
            "pdf",
            "https://example.test/null-effective.pdf",
            "https://archive.test/null-effective",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-null-effective",
            None,
            now,
        ),
    ).lastrowid
    conn.commit()

    stale = find_stale_historical_documents(conn, limit=10)
    impacted_ids = {row["historical_document_id"] for row in stale}
    assert historical_id not in impacted_ids
    conn.close()


def test_find_profile_impacted_historical_documents_targets_only_affected_families(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    def insert_doc(family_key: str, company: str, suffix: str, parser_profile: str) -> int:
        pdf_path = tmp_path / f"{suffix}.pdf"
        pdf_path.write_text("placeholder", encoding="utf-8")
        historical_id = conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                f"{suffix} title",
                "NC",
                company,
                "rate",
                "pdf",
                f"https://example.test/{suffix}.pdf",
                f"https://archive.test/{suffix}",
                "2026-03-26T00:00:00Z",
                str(pdf_path),
                f"hash-{suffix}",
                "2024-01-01",
                now,
            ),
        ).lastrowid
        record_historical_processing_run(
            conn,
            historical_document_id=historical_id,
            source_pdf=str(pdf_path),
            family_key=family_key,
            content_hash=f"hash-{suffix}",
            parser_stage="historical_bulk",
            parser_profile=parser_profile,
            parser_version="historical_bulk_v2",
            processing_mode="historical_bulk",
            status="parsed",
            outcome_quality="strong",
            charge_count=2,
        )
        return int(historical_id)

    doc_leaf_502 = insert_doc("nc-progress-leaf-502", "progress", "progress-502", "generic_residential")
    doc_leaf_503 = insert_doc("nc-progress-leaf-503", "progress", "progress-503", "progress_residential_tou")
    _doc_leaf_500 = insert_doc("nc-progress-leaf-500", "progress", "progress-500", "generic_residential")
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_residential_tou",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {doc_leaf_502, doc_leaf_503}

    by_id = {row["historical_document_id"]: row for row in impacted}
    assert "family_key" in by_id[doc_leaf_502]["reasons"]
    assert "latest_parser_profile" in by_id[doc_leaf_503]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_residential_tou",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 2

    rows = conn.execute(
        """
        SELECT historical_document_id, queue_reason, requested_by, metadata_json
        FROM historical_reprocess_queue
        ORDER BY historical_document_id
        """
    ).fetchall()
    assert len(rows) == 2
    assert all(row["requested_by"] == "test-suite" for row in rows)
    assert all(row["queue_reason"].startswith("profile_dependency:progress_residential_tou:") for row in rows)
    assert json.loads(rows[0]["metadata_json"])["impact_rule"]["parser_profile"] == "progress_residential_tou"

    report_again = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_residential_tou",
        requested_by="test-suite",
    )
    conn.commit()
    assert report_again["inserted"] == 0
    assert report_again["skipped"] == 2
    conn.close()


def test_find_profile_impacted_historical_documents_supports_progress_flat_profile(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-flat.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    def insert_doc(
        family_key: str,
        company: str,
        suffix: str,
        parser_profile: str,
        *,
        candidate_profiles: list[dict[str, object]] | None = None,
        signals: dict[str, object] | None = None,
    ) -> int:
        pdf_path = tmp_path / f"{suffix}.pdf"
        pdf_path.write_text("placeholder", encoding="utf-8")
        historical_id = conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                f"{suffix} title",
                "NC",
                company,
                "rate",
                "pdf",
                f"https://example.test/{suffix}.pdf",
                f"https://archive.test/{suffix}",
                "2026-03-26T00:00:00Z",
                str(pdf_path),
                f"hash-{suffix}",
                "2024-01-01",
                now,
            ),
        ).lastrowid
        record_historical_processing_run(
            conn,
            historical_document_id=historical_id,
            source_pdf=str(pdf_path),
            family_key=family_key,
            content_hash=f"hash-{suffix}",
            parser_stage="historical_bulk",
            parser_profile=parser_profile,
            parser_version="historical_bulk_v2",
            processing_mode="historical_bulk",
            status="parsed",
            outcome_quality="strong",
            charge_count=2,
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage,
                parser_profile, status, confidence, utility, schedule_code,
                effective_date, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(pdf_path),
                None,
                None,
                None,
                "historical_bulk",
                parser_profile,
                "parsed",
                0.88,
                "DEP",
                None,
                "2024-01-01",
                2,
                "[]",
                    json.dumps(
                        {
                            "historical_document_id": historical_id,
                            "family_key": family_key,
                            "candidate_profiles": candidate_profiles or [],
                            "signals": signals or {},
                        }
                    ),
                now,
            ),
        )
        return int(historical_id)

    doc_leaf_500 = insert_doc(
        "nc-progress-leaf-500",
        "progress",
        "progress-500",
        "progress_residential_flat",
        candidate_profiles=[
            {
                "name": "progress_residential_flat",
                "score": 0.95,
                "supported": True,
                "reasons": ["progress_family", "flat_rate_markers", "leaf500"],
            }
        ],
        signals={"has_progress_company_text": True},
    )
    _doc_leaf_590 = insert_doc(
        "nc-progress-leaf-590",
        "progress",
        "progress-590",
        "generic_residential",
        candidate_profiles=[
            {
                "name": "generic_residential",
                "score": 0.1,
                "supported": True,
                "reasons": ["generic_family_fallback"],
            }
        ],
        signals={"has_progress_company_text": True},
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_residential_flat",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {doc_leaf_500}
    assert "latest_parser_profile" in impacted[0]["reasons"]
    assert "family_key" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_residential_flat",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == doc_leaf_500
    assert queued["queue_reason"].startswith("profile_dependency:progress_residential_flat:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "progress_residential_flat"
    conn.close()


def test_progress_flat_profile_impact_does_not_target_progress_docs_without_flat_rate_signals(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-flat-false-positive.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "progress-rider.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-717",
            "Progress Rider",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/progress-rider.pdf",
            "https://archive.test/progress-rider",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-progress-rider",
            "2024-01-01",
            now,
        ),
    ).lastrowid

    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf=str(pdf_path),
        family_key="nc-progress-leaf-717",
        content_hash="hash-progress-rider",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="historical_bulk_v2",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="weak",
        charge_count=1,
    )
    conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(pdf_path),
            None,
            None,
            None,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.35,
            "DEP",
            None,
            "2024-01-01",
            1,
            "[]",
            json.dumps(
                {
                    "candidate_profiles": [],
                    "signals": {
                        "has_progress_company_text": True,
                        "has_flat_rate_markers": False,
                    },
                }
            ),
            now,
        ),
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_residential_flat",
        limit=10,
    )
    assert impacted == []
    conn.close()


def test_progress_current_leaf_bridge_profile_impact_targets_supported_current_leafs(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-current-bridge.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    def insert_doc(
        family_key: str,
        suffix: str,
        parser_profile: str,
        *,
        local_path: str,
        candidate_profiles: list[dict[str, object]] | None = None,
        signals: dict[str, object] | None = None,
    ) -> int:
        historical_id = conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                f"{suffix} title",
                "NC",
                "progress",
                "rate",
                "pdf",
                f"https://example.test/{suffix}.pdf",
                f"https://archive.test/{suffix}",
                "2026-03-26T00:00:00Z",
                local_path,
                f"hash-{suffix}",
                "2024-01-01",
                now,
            ),
        ).lastrowid
        record_historical_processing_run(
            conn,
            historical_document_id=historical_id,
            source_pdf=local_path,
            family_key=family_key,
            content_hash=f"hash-{suffix}",
            parser_stage="historical_bulk",
            parser_profile=parser_profile,
            parser_version="historical_bulk_v2",
            processing_mode="historical_bulk",
            status="parsed",
            outcome_quality="strong",
            charge_count=5,
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage,
                parser_profile, status, confidence, utility, schedule_code,
                effective_date, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                local_path,
                None,
                None,
                None,
                "historical_bulk",
                parser_profile,
                "parsed",
                0.91,
                "DEP",
                None,
                "2024-01-01",
                5,
                "[]",
                    json.dumps(
                        {
                            "historical_document_id": historical_id,
                            "family_key": family_key,
                            "candidate_profiles": candidate_profiles or [],
                            "signals": signals or {},
                        }
                    ),
                now,
            ),
        )
        return int(historical_id)

    doc_leaf_501 = insert_doc(
        "nc-progress-leaf-501",
        "progress-501",
        "progress_current_leaf_bridge",
        local_path=r"data\raw\nc\progress\rate\leaf-no-501.pdf",
        candidate_profiles=[
            {
                "name": "progress_current_leaf_bridge",
                "score": 0.93,
                "supported": True,
                "reasons": ["current_progress_pdf", "leaf501_r_toud", "tou_terms"],
            }
        ],
        signals={"is_current_progress_pdf": True, "has_tou_terms": True},
    )
    doc_leaf_520 = insert_doc(
        "nc-progress-leaf-520",
        "progress-520",
        "progress_current_leaf_bridge",
        local_path=r"data\raw\nc\progress\rate\leaf-no-520.pdf",
        candidate_profiles=[
            {
                "name": "progress_current_leaf_bridge",
                "score": 0.92,
                "supported": True,
                "reasons": ["current_progress_pdf", "leaf520_sgs", "schedule_sgs"],
            }
        ],
        signals={"is_current_progress_pdf": True, "has_progress_company_text": True},
    )
    doc_leaf_532 = insert_doc(
        "nc-progress-leaf-532",
        "progress-532",
        "progress_current_leaf_bridge",
        local_path=r"data\raw\nc\progress\rate\leaf-no-532.pdf",
        candidate_profiles=[
            {
                "name": "progress_current_leaf_bridge",
                "score": 0.92,
                "supported": True,
                "reasons": ["current_progress_pdf", "leaf532_lgs", "schedule_lgs"],
            }
        ],
        signals={"is_current_progress_pdf": True, "has_progress_company_text": True},
    )
    _doc_leaf_609 = insert_doc(
        "nc-progress-leaf-609",
        "progress-609",
        "progress_single_value_rider",
        local_path=r"data\historical\raw\nc\progress\rider\rider-esm.pdf",
        candidate_profiles=[
            {
                "name": "progress_single_value_rider",
                "score": 0.9,
                "supported": True,
                "reasons": ["single_value_rider_family", "monthly_rate"],
            }
        ],
        signals={"is_current_progress_pdf": False},
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_current_leaf_bridge",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {doc_leaf_501, doc_leaf_520, doc_leaf_532}
    assert "latest_parser_profile" in impacted[0]["reasons"]
    assert "family_key" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_current_leaf_bridge",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 3

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        ORDER BY historical_document_id
        """
    ).fetchall()
    assert [row["historical_document_id"] for row in queued] == [doc_leaf_501, doc_leaf_520, doc_leaf_532]
    assert all(row["queue_reason"].startswith("profile_dependency:progress_current_leaf_bridge:") for row in queued)
    assert all(
        json.loads(row["metadata_json"])["impact_rule"]["parser_profile"] == "progress_current_leaf_bridge"
        for row in queued
    )
    conn.close()


def test_progress_single_value_rider_profile_impact_targets_agency_asset_variant(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-single-value-agency-asset.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-rider-AGENCYASSETRIDERTORECOVERCOSTSRELATEDTOFACILITIE",
            "Agency Asset Rider to Recover Costs Related to Facilities Purchased from the North",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/agency-asset-rider.pdf",
            "https://archive.test/agency-asset-rider",
            "2026-03-26T00:00:00Z",
            "data/historical/ncuc/e-2-sub-1207/agency-asset-rider.pdf",
            "hash-agency-asset-rider",
            "2024-01-01",
            now,
        ),
    ).lastrowid
    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf="data/historical/ncuc/e-2-sub-1207/agency-asset-rider.pdf",
        family_key="nc-progress-rider-AGENCYASSETRIDERTORECOVERCOSTSRELATEDTOFACILITIE",
        content_hash="hash-agency-asset-rider",
        parser_stage="historical_bulk",
        parser_profile="unknown",
        parser_version="historical_bulk_v2",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="weak",
        charge_count=0,
    )
    conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/e-2-sub-1207/agency-asset-rider.pdf",
            "e-2-sub-1207",
            1,
            1,
            "historical_bulk",
            "unknown",
            "empty",
            0.1,
            "DEP",
            None,
            None,
            0,
            "[]",
            json.dumps(
                {
                    "historical_document_id": historical_id,
                    "family_key": "nc-progress-rider-AGENCYASSETRIDERTORECOVERCOSTSRELATEDTOFACILITIE",
                    "candidate_profiles": [
                        {
                            "name": "progress_single_value_rider",
                            "score": 0.91,
                            "supported": True,
                            "reasons": ["single_value_rider_family", "monthly_rate", "agency_asset_rider"],
                        }
                    ],
                    "signals": {"has_progress_company_text": True},
                }
            ),
            now,
        ),
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_single_value_rider",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {historical_id}
    assert "candidate_profile" in impacted[0]["reasons"]
    assert "candidate_reason" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_single_value_rider",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == historical_id
    assert queued["queue_reason"].startswith("profile_dependency:progress_single_value_rider:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "progress_single_value_rider"
    conn.close()


def test_progress_recovery_rider_profile_impact_targets_legacy_unknown_family(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-recovery-rider.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-rider-RECOVERYRIDER",
            "Recovery Rider",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/recovery-rider.pdf",
            "https://archive.test/recovery-rider",
            "2026-03-26T00:00:00Z",
            r"data\historical\raw\nc\progress\rider\recovery-rider.pdf",
            "hash-recovery-rider",
            "2024-01-01",
            now,
        ),
    ).lastrowid

    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf=r"data\historical\raw\nc\progress\rider\recovery-rider.pdf",
        family_key="nc-progress-rider-RECOVERYRIDER",
        content_hash="hash-recovery-rider",
        parser_stage="historical_bulk",
        parser_profile="unknown",
        parser_version="historical_bulk_v2",
        processing_mode="historical_bulk",
        status="empty",
        outcome_quality="empty",
        charge_count=0,
    )
    conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            r"data\historical\raw\nc\progress\rider\recovery-rider.pdf",
            None,
            None,
            None,
            "historical_bulk",
            "unknown",
            "empty",
            0.1,
            "DEP",
            None,
            "2024-01-01",
            0,
            "[]",
            json.dumps(
                {
                    "historical_document_id": historical_id,
                    "family_key": "nc-progress-rider-RECOVERYRIDER",
                    "candidate_profiles": [
                        {
                            "name": "progress_recovery_rider",
                            "score": 0.94,
                            "supported": True,
                            "reasons": ["recovery_rider", "monthly_rate"],
                        }
                    ],
                    "signals": {"is_current_progress_pdf": False},
                }
            ),
            now,
        ),
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_recovery_rider",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {historical_id}
    assert "candidate_profile" in impacted[0]["reasons"]
    assert "candidate_reason" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_recovery_rider",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == historical_id
    assert queued["queue_reason"].startswith("profile_dependency:progress_recovery_rider:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "progress_recovery_rider"
    conn.close()


def test_progress_management_cost_recovery_rider_profile_impact_targets_legacy_unknown_family(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-management-rider.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-rider-MANAGEMENTANDENERGYEFFICIENCYCOSTRECOVERYRIDER",
            "Management and Energy Efficiency Cost Recovery Rider",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/management-rider.pdf",
            "https://archive.test/management-rider",
            "2026-03-26T00:00:00Z",
            r"data\historical\raw\nc\progress\rider\management-rider.pdf",
            "hash-management-rider",
            "2024-01-01",
            now,
        ),
    ).lastrowid

    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf=r"data\historical\raw\nc\progress\rider\management-rider.pdf",
        family_key="nc-progress-rider-MANAGEMENTANDENERGYEFFICIENCYCOSTRECOVERYRIDER",
        content_hash="hash-management-rider",
        parser_stage="historical_bulk",
        parser_profile="unknown",
        parser_version="historical_bulk_v2",
        processing_mode="historical_bulk",
        status="empty",
        outcome_quality="empty",
        charge_count=0,
    )
    conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            r"data\historical\raw\nc\progress\rider\management-rider.pdf",
            None,
            None,
            None,
            "historical_bulk",
            "unknown",
            "empty",
            0.1,
            "DEP",
            None,
            "2024-01-01",
            0,
            "[]",
            json.dumps(
                {
                    "historical_document_id": historical_id,
                    "family_key": "nc-progress-rider-MANAGEMENTANDENERGYEFFICIENCYCOSTRECOVERYRIDER",
                    "candidate_profiles": [
                        {
                            "name": "progress_management_energy_efficiency_cost_recovery_rider",
                            "score": 0.94,
                            "supported": True,
                            "reasons": ["management_energy_efficiency_cost_recovery_rider", "monthly_rate"],
                        }
                    ],
                    "signals": {"is_current_progress_pdf": False},
                }
            ),
            now,
        ),
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_management_energy_efficiency_cost_recovery_rider",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {historical_id}
    assert "candidate_profile" in impacted[0]["reasons"]
    assert "candidate_reason" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_management_energy_efficiency_cost_recovery_rider",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == historical_id
    assert queued["queue_reason"].startswith("profile_dependency:progress_management_energy_efficiency_cost_recovery_rider:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "progress_management_energy_efficiency_cost_recovery_rider"
    conn.close()


def test_progress_compliance_report_cost_recovery_rider_profile_impact_targets_legacy_unknown_family(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-compliance-rider.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-rider-COMPLIANCEREPORTANDCOSTRECOVERYRIDER",
            "Compliance Report and Cost Recovery Rider",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/compliance-rider.pdf",
            "https://archive.test/compliance-rider",
            "2026-03-26T00:00:00Z",
            r"data\historical\raw\nc\progress\rider\compliance-rider.pdf",
            "hash-compliance-rider",
            "2024-01-01",
            now,
        ),
    ).lastrowid

    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf=r"data\historical\raw\nc\progress\rider\compliance-rider.pdf",
        family_key="nc-progress-rider-COMPLIANCEREPORTANDCOSTRECOVERYRIDER",
        content_hash="hash-compliance-rider",
        parser_stage="historical_bulk",
        parser_profile="unknown",
        parser_version="historical_bulk_v2",
        processing_mode="historical_bulk",
        status="empty",
        outcome_quality="empty",
        charge_count=0,
    )
    conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            r"data\historical\raw\nc\progress\rider\compliance-rider.pdf",
            None,
            None,
            None,
            "historical_bulk",
            "unknown",
            "empty",
            0.1,
            "DEP",
            None,
            "2024-01-01",
            0,
            "[]",
            json.dumps(
                {
                    "historical_document_id": historical_id,
                    "family_key": "nc-progress-rider-COMPLIANCEREPORTANDCOSTRECOVERYRIDER",
                    "candidate_profiles": [
                        {
                            "name": "progress_compliance_report_and_cost_recovery_rider",
                            "score": 0.94,
                            "supported": True,
                            "reasons": ["compliance_report_and_cost_recovery_rider", "monthly_rate"],
                        }
                    ],
                    "signals": {"is_current_progress_pdf": False},
                }
            ),
            now,
        ),
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_compliance_report_and_cost_recovery_rider",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {historical_id}
    assert "candidate_profile" in impacted[0]["reasons"]
    assert "candidate_reason" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_compliance_report_and_cost_recovery_rider",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == historical_id
    assert queued["queue_reason"].startswith("profile_dependency:progress_compliance_report_and_cost_recovery_rider:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "progress_compliance_report_and_cost_recovery_rider"
    conn.close()


def test_zero_charge_program_profile_impact_targets_program_only_families(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-zero-charge-program.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-program-LIGHTINGPROGRAM",
            "Lighting Program",
            "NC",
            "progress",
            "program",
            "pdf",
            "https://example.test/lighting-program.pdf",
            "https://archive.test/lighting-program",
            "2026-03-26T00:00:00Z",
            "data/historical/ncuc/e-2-sub-1300/lighting-program.pdf",
            "hash-lighting-program",
            None,
            now,
        ),
    ).lastrowid
    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf="data/historical/ncuc/e-2-sub-1300/lighting-program.pdf",
        family_key="nc-progress-program-LIGHTINGPROGRAM",
        content_hash="hash-lighting-program",
        parser_stage="historical_bulk",
        parser_profile="unknown",
        parser_version="historical_bulk_v2",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="weak",
        charge_count=0,
    )
    conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "data/historical/ncuc/e-2-sub-1300/lighting-program.pdf",
            "e-2-sub-1300",
            1,
            1,
            "historical_bulk",
            "unknown",
            "empty",
            0.1,
            "DEP",
            None,
            None,
            0,
            "[]",
            json.dumps(
                {
                    "historical_document_id": historical_id,
                    "family_key": "nc-progress-program-LIGHTINGPROGRAM",
                    "candidate_profiles": [
                        {
                            "name": "zero_charge_program",
                            "score": 0.99,
                            "supported": True,
                            "reasons": ["zero_charge_program_explicit_match"],
                        }
                    ],
                    "signals": {"has_progress_company_text": True},
                }
            ),
            now,
        ),
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="zero_charge_program",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {historical_id}
    assert "candidate_profile" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="zero_charge_program",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == historical_id
    assert queued["queue_reason"].startswith("profile_dependency:zero_charge_program:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "zero_charge_program"
    conn.close()


def test_progress_specialty_rider_profile_impact_targets_supported_current_riders(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-specialty-rider.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    def insert_doc(
        family_key: str,
        suffix: str,
        *,
        parser_profile: str,
        local_path: str,
        candidate_profiles: list[dict[str, object]] | None = None,
        signals: dict[str, object] | None = None,
    ) -> int:
        historical_id = conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                f"{suffix} title",
                "NC",
                "progress",
                "rider",
                "pdf",
                f"https://example.test/{suffix}.pdf",
                f"https://archive.test/{suffix}",
                "2026-03-26T00:00:00Z",
                local_path,
                f"hash-{suffix}",
                "2024-01-01",
                now,
            ),
        ).lastrowid
        record_historical_processing_run(
            conn,
            historical_document_id=historical_id,
            source_pdf=local_path,
            family_key=family_key,
            content_hash=f"hash-{suffix}",
            parser_stage="historical_bulk",
            parser_profile=parser_profile,
            parser_version="historical_bulk_v2",
            processing_mode="historical_bulk",
            status="parsed",
            outcome_quality="strong",
            charge_count=2,
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage,
                parser_profile, status, confidence, utility, schedule_code,
                effective_date, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                local_path,
                None,
                None,
                None,
                "historical_bulk",
                parser_profile,
                "parsed",
                0.91,
                "DEP",
                None,
                "2024-01-01",
                2,
                "[]",
                json.dumps(
                    {
                        "historical_document_id": historical_id,
                        "family_key": family_key,
                        "candidate_profiles": candidate_profiles or [],
                        "signals": signals or {},
                    }
                ),
                now,
            ),
        )
        return int(historical_id)

    doc_leaf_668 = insert_doc(
        "nc-progress-leaf-668",
        "progress-668",
        parser_profile="progress_specialty_rider",
        local_path=r"data\raw\nc\progress\rider\leaf-no-668.pdf",
        candidate_profiles=[
            {
                "name": "progress_specialty_rider",
                "score": 0.94,
                "supported": True,
                "reasons": ["current_progress_pdf", "leaf668_rider_nsc", "credit_terms"],
            }
        ],
        signals={"is_current_progress_pdf": True},
    )
    _doc_leaf_609 = insert_doc(
        "nc-progress-leaf-609",
        "progress-609",
        parser_profile="progress_single_value_rider",
        local_path=r"data\historical\raw\nc\progress\rider\rider-esm.pdf",
        candidate_profiles=[
            {
                "name": "progress_single_value_rider",
                "score": 0.9,
                "supported": True,
                "reasons": ["single_value_rider_family", "monthly_rate"],
            }
        ],
        signals={"is_current_progress_pdf": False},
    )
    _doc_leaf_501 = insert_doc(
        "nc-progress-leaf-501",
        "progress-501",
        parser_profile="generic_residential",
        local_path=r"data\raw\nc\progress\rate\leaf-no-501.pdf",
        candidate_profiles=[
            {
                "name": "generic_residential",
                "score": 0.1,
                "supported": True,
                "reasons": ["generic_family_fallback"],
            }
        ],
        signals={"is_current_progress_pdf": True},
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_specialty_rider",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {doc_leaf_668}
    assert "latest_parser_profile" in impacted[0]["reasons"]
    assert "family_key" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_specialty_rider",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == doc_leaf_668
    assert queued["queue_reason"].startswith("profile_dependency:progress_specialty_rider:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "progress_specialty_rider"
    conn.close()


def test_progress_billing_adjustments_profile_impact_targets_leaf_601_documents(tmp_path) -> None:
    conn = connect(tmp_path / "profile-impact-ba.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    def insert_doc(
        family_key: str,
        suffix: str,
        parser_profile: str,
        *,
        candidate_profiles: list[dict[str, object]] | None = None,
    ) -> int:
        pdf_path = tmp_path / f"{suffix}.pdf"
        pdf_path.write_text("placeholder", encoding="utf-8")
        historical_id = conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                f"{suffix} title",
                "NC",
                "progress",
                "rider",
                "pdf",
                f"https://example.test/{suffix}.pdf",
                f"https://archive.test/{suffix}",
                "2026-03-26T00:00:00Z",
                str(pdf_path),
                f"hash-{suffix}",
                "2024-01-01",
                now,
            ),
        ).lastrowid
        record_historical_processing_run(
            conn,
            historical_document_id=historical_id,
            source_pdf=str(pdf_path),
            family_key=family_key,
            content_hash=f"hash-{suffix}",
            parser_stage="historical_bulk",
            parser_profile=parser_profile,
            parser_version="historical_bulk_v2",
            processing_mode="historical_bulk",
            status="parsed",
            outcome_quality="strong",
            charge_count=5,
        )
        conn.execute(
            """
            INSERT INTO parse_attempt_logs (
                source_pdf, docket_dir, page_start, page_end, parser_stage,
                parser_profile, status, confidence, utility, schedule_code,
                effective_date, charge_count, review_flags_json, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(pdf_path),
                None,
                None,
                None,
                "historical_bulk",
                parser_profile,
                "parsed",
                0.92,
                "DEP",
                "RIDER_BA_RY1",
                "2024-01-01",
                5,
                "[]",
                json.dumps(
                    {
                        "candidate_profiles": candidate_profiles or [],
                        "signals": {"has_progress_company_text": True},
                    }
                ),
                now,
            ),
        )
        return int(historical_id)

    doc_leaf_601 = insert_doc(
        "nc-progress-leaf-601",
        "progress-601",
        "progress_billing_adjustments",
        candidate_profiles=[
            {
                "name": "progress_billing_adjustments",
                "score": 0.96,
                "supported": True,
                "reasons": ["family=leaf601", "billing_adjustment_factors"],
            }
        ],
    )
    _doc_leaf_609 = insert_doc(
        "nc-progress-leaf-609",
        "progress-609",
        "generic_residential",
        candidate_profiles=[
            {
                "name": "generic_residential",
                "score": 0.1,
                "supported": True,
                "reasons": ["generic_family_fallback"],
            }
        ],
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_billing_adjustments",
        limit=10,
    )
    impacted_ids = {row["historical_document_id"] for row in impacted}
    assert impacted_ids == {doc_leaf_601}
    assert "latest_parser_profile" in impacted[0]["reasons"]
    assert "family_key" in impacted[0]["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="progress_billing_adjustments",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT historical_document_id, queue_reason, metadata_json
        FROM historical_reprocess_queue
        """
    ).fetchone()
    assert queued is not None
    assert queued["historical_document_id"] == doc_leaf_601
    assert queued["queue_reason"].startswith("profile_dependency:progress_billing_adjustments:")
    assert json.loads(queued["metadata_json"])["impact_rule"]["parser_profile"] == "progress_billing_adjustments"
    conn.close()


def test_profile_impact_uses_candidate_reasons_and_signals_from_latest_parse_attempt(tmp_path) -> None:
    conn = connect(tmp_path / "profile-signal-impact.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "carolinas-summary.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-carolinas-leaf-777",
            "Carolinas Summary Page",
            "NC",
            "carolinas",
            "rate",
            "pdf",
            "https://example.test/carolinas-summary.pdf",
            "https://archive.test/carolinas-summary",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-carolinas-summary",
            "2024-01-01",
            now,
        ),
    ).lastrowid

    record_historical_processing_run(
        conn,
        historical_document_id=historical_id,
        source_pdf=str(pdf_path),
        family_key="nc-carolinas-leaf-777",
        content_hash="hash-carolinas-summary",
        parser_stage="historical_bulk",
        parser_profile="generic_residential",
        parser_version="historical_bulk_v2",
        processing_mode="historical_bulk",
        status="parsed",
        outcome_quality="weak",
        charge_count=1,
        metadata={
            "signals": {
                "has_summary_text": True,
                "has_carolinas_company_text": True,
            },
            "candidate_profiles": [
                {
                    "name": "carolinas_rider_adjustment_matrix",
                    "score": 0.93,
                    "supported": True,
                    "reasons": ["summary_text", "carolinas_company"],
                }
            ],
        },
    )
    conn.execute(
        """
        INSERT INTO parse_attempt_logs (
            source_pdf, docket_dir, page_start, page_end, parser_stage,
            parser_profile, status, confidence, utility, schedule_code,
            effective_date, charge_count, review_flags_json, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(pdf_path),
            None,
            None,
            None,
            "historical_bulk",
            "generic_residential",
            "parsed",
            0.41,
            "DEC",
            None,
            "2024-01-01",
            1,
            json.dumps(["generic_fallback_selected"]),
            json.dumps(
                {
                    "historical_document_id": int(historical_id),
                    "family_key": "nc-carolinas-leaf-777",
                    "company": "carolinas",
                    "signals": {
                        "has_summary_text": True,
                        "has_carolinas_company_text": True,
                    },
                    "candidate_profiles": [
                        {
                            "name": "carolinas_rider_adjustment_matrix",
                            "score": 0.93,
                            "supported": True,
                            "reasons": ["summary_text", "carolinas_company"],
                        }
                    ],
                },
                sort_keys=True,
            ),
            now,
        ),
    )
    conn.commit()

    impacted = find_profile_impacted_historical_documents(
        conn,
        parser_profile="carolinas_rider_adjustment_matrix",
        limit=10,
    )
    assert len(impacted) == 1
    row = impacted[0]
    assert row["historical_document_id"] == historical_id
    assert "candidate_profile" in row["reasons"]
    assert "candidate_reason" in row["reasons"]
    assert "signal_match" in row["reasons"]

    report = enqueue_profile_impacted_historical_documents(
        conn,
        parser_profile="carolinas_rider_adjustment_matrix",
        requested_by="test-suite",
    )
    conn.commit()
    assert report["inserted"] == 1

    queued = conn.execute(
        """
        SELECT queue_reason, metadata_json
        FROM historical_reprocess_queue
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert queued is not None
    assert "candidate_reason" in queued["queue_reason"]
    metadata = json.loads(queued["metadata_json"])
    assert metadata["latest_parse_attempt_id"] is not None
    assert metadata["latest_candidate_profiles"][0]["name"] == "carolinas_rider_adjustment_matrix"
    assert metadata["latest_signals"]["has_summary_text"] is True
    conn.close()
