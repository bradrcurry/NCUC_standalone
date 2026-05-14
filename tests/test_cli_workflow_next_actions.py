from __future__ import annotations

import threading
from datetime import UTC, datetime

from typer.testing import CliRunner

from duke_rates import cli
from duke_rates.cli_commands import ocr as ocr_module
from duke_rates.db.sqlite import connect


class _FakeTriage:
    def __init__(self, *, ocr_confidence_score: float = 0.9, structure_complexity_score: float = 0.4, gpu_ocr_candidate: bool = False) -> None:
        self.ocr_confidence_score = ocr_confidence_score
        self.structure_complexity_score = structure_complexity_score
        self.gpu_ocr_candidate = gpu_ocr_candidate


def _seed_history_doc(conn, tmp_path, *, doc_id: int = 1, family_key: str = "nc-progress-leaf-715", parser_profile: str = "unknown", outcome_quality: str = "empty") -> None:
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / f"{doc_id}.pdf"
    pdf_path.write_text("pdf placeholder", encoding="utf-8")
    raw_text_path = tmp_path / f"{doc_id}.txt"
    raw_text_path.write_text("", encoding="utf-8")
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
            doc_id,
            family_key,
            f"{family_key} title",
            "NC",
            "progress",
            "rate",
            "pdf",
            f"https://example.test/{doc_id}",
            f"https://archive.test/{doc_id}",
            now,
            str(pdf_path),
            str(raw_text_path),
            f"hash-{doc_id}",
            "2024-01-01",
            now,
            1,
            3,
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
            doc_id,
            str(pdf_path),
            family_key,
            f"hash-{doc_id}",
            "historical_bulk",
            parser_profile,
            "test",
            "targeted",
            "completed",
            outcome_quality,
            0,
            "[]",
            "{}",
            now,
            now,
        ),
    )


def test_build_workflow_next_actions_prefers_pending_queues(tmp_path) -> None:
    db_path = tmp_path / "workflow-next.db"
    conn = connect(db_path)
    _seed_history_doc(conn, tmp_path, doc_id=1)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO ocr_processing_queue (
            discovery_record_id, source_pdf, file_hash, backend, priority, status,
            ocr_confidence, structure_complexity, gpu_candidate, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (None, str(tmp_path / "1.pdf"), None, "pytesseract_cpu", 80, "pending", 0.9, 0.4, 0, "{}", now),
    )
    conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            historical_document_id, source_pdf, family_key, priority, queue_reason, status, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (1, str(tmp_path / "1.pdf"), "nc-progress-leaf-715", 70, "test_reason", "pending", "{}", now),
    )
    conn.commit()

    report = cli._build_workflow_next_actions_nc_report(conn, limit=10)
    assert report["rows"][0]["action_type"] == "process_ocr_queue"
    assert report["rows"][1]["action_type"] == "process_reprocess_queue"
    assert report["rows"][0]["concurrency_policy"] == "workers_allowed"
    assert report["rows"][0]["workers_allowed"] is True
    assert report["rows"][0]["recommended_parallel_command"] is not None
    assert report["rows"][1]["concurrency_policy"] == "workers_allowed"
    conn.close()


def test_build_workflow_next_actions_prioritizes_stale_running_recovery(tmp_path) -> None:
    db_path = tmp_path / "workflow-next-stale.db"
    conn = connect(db_path)
    _seed_history_doc(conn, tmp_path, doc_id=1)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            historical_document_id, source_pdf, family_key, priority, queue_reason,
            requested_by, status, metadata_json, requested_at, started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (1, str(tmp_path / "1.pdf"), "nc-progress-leaf-715", 90, "stale_stage:parser_version", "test-suite", "running", "{}", now, "2026-04-01T00:00:00+00:00"),
    )
    conn.execute(
        """
        INSERT INTO historical_reprocess_queue (
            historical_document_id, source_pdf, family_key, priority, queue_reason,
            requested_by, status, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (1, str(tmp_path / "1.pdf"), "nc-progress-leaf-715", 70, "needs_review:generic_residential", "test-suite", "pending", "{}", now),
    )
    conn.commit()

    report = cli._build_workflow_next_actions_nc_report(conn, limit=10)
    assert report["rows"][0]["action_type"] == "recover_stale_reprocess"
    assert report["rows"][1]["action_type"] == "process_reprocess_queue"
    assert report["rows"][0]["concurrency_policy"] == "sequential_only"
    conn.close()


def test_execute_workflow_next_action_enqueues_ocr_remediation(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "workflow-next-exec.db"
    conn = connect(db_path)
    _seed_history_doc(conn, tmp_path, doc_id=1)
    conn.commit()
    conn.close()

    fake_bootstrap = lambda: (type("S", (), {"database_path": str(db_path)})(), None)
    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(cli, "triage_pdf", lambda _path: _FakeTriage())
    monkeypatch.setattr(ocr_module, "triage_pdf", lambda _path: _FakeTriage())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["execute-workflow-next-action-nc", "--limit", "1"])

    assert result.exit_code == 0
    assert "Executing workflow next action" in result.stdout
    assert "enqueue_ocr_remediation" in result.stdout

    conn = connect(db_path)
    queue_count = conn.execute("SELECT COUNT(*) FROM ocr_processing_queue").fetchone()[0]
    receipt = conn.execute(
        "SELECT action_type, status, command_text FROM workflow_action_receipts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert queue_count == 1
    assert receipt[0] == "enqueue_ocr_remediation"
    assert receipt[1] == "completed"
    assert "ocr enqueue-remediation-nc" in receipt[2]


def test_execute_workflow_next_action_uses_workers_for_local_queue(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "workflow-next-workers.db"
    conn = connect(db_path)
    _seed_history_doc(conn, tmp_path, doc_id=1)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO ocr_processing_queue (
            discovery_record_id, source_pdf, file_hash, backend, priority, status,
            ocr_confidence, structure_complexity, gpu_candidate, metadata_json, requested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (None, str(tmp_path / "1.pdf"), None, "pytesseract_cpu", 80, "pending", 0.9, 0.4, 0, "{}", now),
    )
    conn.commit()
    conn.close()

    fake_bootstrap = lambda: (type("S", (), {"database_path": str(db_path)})(), None)
    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "_bootstrap", fake_bootstrap)

    calls: list[tuple[int, int, bool]] = []

    def _fake_process_ocr_queue_nc(*, limit, force, workers):
        calls.append((limit, workers, force))

    monkeypatch.setattr(cli, "process_ocr_queue_nc", _fake_process_ocr_queue_nc)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["execute-workflow-next-action-nc", "--limit", "2", "--workers", "2"],
    )

    assert result.exit_code == 0
    assert "policy=workers_allowed workers=2" in result.stdout
    assert calls == [(2, 2, False)]

    conn = connect(db_path)
    receipt = conn.execute(
        "SELECT action_type, status, command_text FROM workflow_action_receipts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert receipt[0] == "process_ocr_queue"
    assert receipt[1] == "completed"
    assert receipt[2] == "python -m duke_rates ocr process-queue-nc --limit 2 --workers 2"


def test_show_workflow_action_receipts_cli(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "workflow-receipts.db"
    conn = connect(db_path)
    conn.execute(
        """
        INSERT INTO workflow_action_receipts (
            workflow, action_type, status, target_family_key, command_text,
            requested_limit, metadata_json, started_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc_guided",
            "process_ocr_queue",
            "completed",
            "nc-progress-leaf-720",
            "python -m duke_rates ocr process-queue-nc --limit 1",
            1,
            "{}",
            datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
            datetime(2026, 4, 21, tzinfo=UTC).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    fake_bootstrap = lambda: (type("S", (), {"database_path": str(db_path)})(), None)
    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "_bootstrap", fake_bootstrap)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["show-workflow-action-receipts-nc", "--limit", "5"])

    assert result.exit_code == 0
    assert "Workflow Action Receipts (NC)" in result.stdout
    assert "process_ocr_queue" in result.stdout
    assert "nc-progress-leaf-720" in result.stdout


def test_show_workflow_capabilities_cli() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["show-workflow-capabilities-nc"])

    assert result.exit_code == 0
    assert "Workflow Capabilities (NC)" in result.stdout
    assert "type=process_ocr_queue policy=workers_allowed" in result.stdout
    assert "type=portal_search policy=sequential_only" in result.stdout


def test_process_ocr_queue_nc_workers(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "ocr-workers.db"
    connect(db_path).close()

    fake_bootstrap = lambda: (type("S", (), {"database_path": str(db_path)})(), None)
    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "_bootstrap", fake_bootstrap)

    state = {"calls": 0}
    lock = threading.Lock()

    def _fake_process(_database_path, force=False):
        del force
        with lock:
            state["calls"] += 1
            call_no = state["calls"]
        if call_no <= 2:
            return {"processed": True, "completed": 1, "failed": 0}
        return {"processed": False, "completed": 0, "failed": 0}

    monkeypatch.setattr(ocr_module, "_process_single_ocr_queue_item", _fake_process)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["ocr", "process-queue-nc", "--limit", "3", "--workers", "2"])

    assert result.exit_code == 0
    assert "OCR queue processed=2 completed=2 failed=0 workers=2" in result.stdout


def test_process_reprocess_queue_nc_workers(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "reprocess-workers.db"
    connect(db_path).close()

    fake_bootstrap = lambda: (type("S", (), {"database_path": str(db_path)})(), None)
    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "_bootstrap", fake_bootstrap)

    state = {"calls": 0}
    lock = threading.Lock()

    def _fake_process(_database_path):
        with lock:
            state["calls"] += 1
            call_no = state["calls"]
        if call_no <= 2:
            return {"processed": True, "completed": 1, "failed": 0}
        return {"processed": False, "completed": 0, "failed": 0}

    monkeypatch.setattr(cli, "_process_single_reprocess_queue_item", _fake_process)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["process-reprocess-queue-nc", "--limit", "3", "--workers", "2"],
    )

    assert result.exit_code == 0
    assert (
        "Historical reprocess queue processed=2 completed=2 failed=0 workers=2"
        in result.stdout
    )


def test_reconcile_workflow_action_receipts_marks_running_and_completed(tmp_path) -> None:
    db_path = tmp_path / "workflow-reconcile.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()

    conn.execute(
        """
        INSERT INTO workflow_action_receipts (
            workflow, action_type, status, command_text, requested_limit, metadata_json, started_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            "nc_guided",
            "process_ocr_queue",
            "started",
            "python -m duke_rates ocr process-queue-nc --limit 1",
            1,
            "{}",
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO ocr_processing_queue (
            discovery_record_id, source_pdf, file_hash, backend, priority, status,
            ocr_confidence, structure_complexity, gpu_candidate, metadata_json, requested_at, started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (None, str(tmp_path / "1.pdf"), None, "pytesseract_cpu", 80, "running", 0.9, 0.4, 0, "{}", now, now),
    )
    conn.commit()

    report = cli._reconcile_workflow_action_receipts(conn, workflow="nc_guided", limit=10)
    row = conn.execute("SELECT status FROM workflow_action_receipts ORDER BY id DESC LIMIT 1").fetchone()
    assert report["running"] == 1
    assert row[0] == "running"

    conn.execute(
        """
        UPDATE workflow_action_receipts SET status = 'started', completed_at = NULL, error_message = NULL
        """
    )
    conn.execute(
        """
        UPDATE ocr_processing_queue SET status = 'completed', completed_at = ?
        """,
        (now,),
    )
    conn.commit()

    report = cli._reconcile_workflow_action_receipts(conn, workflow="nc_guided", limit=10)
    row = conn.execute(
        "SELECT status, completed_at FROM workflow_action_receipts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert report["completed"] == 1
    assert row[0] == "completed"
    assert row[1] is not None
