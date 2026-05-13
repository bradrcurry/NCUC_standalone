from __future__ import annotations

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


def _seed_remediation_db(db_path, tmp_path) -> None:
    conn = connect(db_path)
    now = datetime(2026, 4, 21, tzinfo=UTC).isoformat()

    pdf_path = tmp_path / "leaf715.pdf"
    pdf_path.write_text("pdf placeholder", encoding="utf-8")
    raw_text_path = tmp_path / "leaf715.txt"
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
            1,
            "nc-progress-leaf-715",
            "Residential Service Load Control LC",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/1",
            "https://archive.test/1",
            now,
            str(pdf_path),
            str(raw_text_path),
            "hash-1",
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
            1,
            str(pdf_path),
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
    conn.commit()
    conn.close()


def test_enqueue_ocr_remediation_nc_dry_run(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "ocr-remediation-dry.db"
    _seed_remediation_db(db_path, tmp_path)

    fake_bootstrap = lambda: (type("S", (), {"database_path": str(db_path)})(), None)
    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "triage_pdf", lambda _path: _FakeTriage())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["ocr", "enqueue-remediation-nc", "--limit", "5"])

    assert result.exit_code == 0
    assert "OCR remediation enqueue (dry_run)" in result.stdout
    assert "considered=1" in result.stdout
    assert "inserted=0" in result.stdout


def test_enqueue_ocr_remediation_nc_execute_inserts_queue(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "ocr-remediation-exec.db"
    _seed_remediation_db(db_path, tmp_path)

    fake_bootstrap = lambda: (type("S", (), {"database_path": str(db_path)})(), None)
    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ocr_module, "triage_pdf", lambda _path: _FakeTriage())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["ocr", "enqueue-remediation-nc", "--limit", "5", "--execute"])

    assert result.exit_code == 0
    assert "OCR remediation enqueue (execute)" in result.stdout
    assert "considered=1" in result.stdout
    assert "inserted=1" in result.stdout

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM ocr_processing_queue").fetchone()[0]
    metadata = conn.execute("SELECT metadata_json FROM ocr_processing_queue LIMIT 1").fetchone()[0]
    conn.close()

    assert count == 1
    assert "ocr_remediation_audit" in metadata
