from __future__ import annotations

import json
from pathlib import Path

from duke_rates.db.artifact_cache import load_docling_artifact, save_docling_artifact
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.pipeline.docling_backend import (
    DOCLING_BACKEND_VERSION,
    ACCELERATOR_CPU,
    _compute_file_hash,
    _docling_json_sidecar_path,
    _docling_text_sidecar_path,
    _docling_tables_sidecar_path,
    _load_cached_docling_artifact,
    _write_docling_artifacts,
    convert_pdf_with_docling,
    get_docling_unavailable_reason,
)
from duke_rates.historical.ncuc.pipeline.stage_versions import DOCLING_BACKEND_VERSION as SV_DOCLING


def test_stage_version_constant_exists() -> None:
    assert SV_DOCLING
    assert SV_DOCLING.startswith("docling_")


def test_get_docling_unavailable_reason_no_package(monkeypatch) -> None:
    """When docling is not installed the reason string is non-empty."""
    import builtins
    real_import = builtins.__import__

    def _block_docling(name, *args, **kwargs):
        if name == "docling":
            raise ImportError("no module docling")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_docling)
    reason = get_docling_unavailable_reason()
    assert reason is not None
    assert "not installed" in reason.lower() or "docling" in reason.lower()


def test_sidecar_path_helpers(tmp_path) -> None:
    pdf = tmp_path / "leaf500.pdf"
    assert _docling_json_sidecar_path(str(pdf), ACCELERATOR_CPU).name == "leaf500.pdf.docling_cpu.json"
    assert _docling_text_sidecar_path(str(pdf), ACCELERATOR_CPU).name == "leaf500.pdf.docling_cpu.txt"
    assert _docling_tables_sidecar_path(str(pdf), ACCELERATOR_CPU).name == "leaf500.pdf.docling_cpu_tables.json"


def test_load_cached_returns_none_when_missing(tmp_path) -> None:
    pdf = tmp_path / "missing.pdf"
    result = _load_cached_docling_artifact(str(pdf), ACCELERATOR_CPU)
    assert result is None


def test_write_and_load_cached_docling_artifact(tmp_path) -> None:
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    file_hash = _compute_file_hash(str(pdf))

    j, t, tbl = _write_docling_artifacts(
        str(pdf),
        file_hash=file_hash,
        accelerator=ACCELERATOR_CPU,
        pipeline="standard",
        doc_json={"tables": []},
        plain_text="Leaf No. 500\nCustomer Charge: $16.85",
        tables=[],
        page_count=1,
        conversion_status="success",
        conversion_confidence=0.95,
    )

    assert j.exists()
    assert t.exists()
    assert tbl.exists()
    assert t.read_text(encoding="utf-8") == "Leaf No. 500\nCustomer Charge: $16.85"

    cached = _load_cached_docling_artifact(str(pdf), ACCELERATOR_CPU, expected_hash=file_hash)
    assert cached is not None
    assert cached["backend_version"] == DOCLING_BACKEND_VERSION
    assert cached["accelerator"] == ACCELERATOR_CPU
    assert cached["page_count"] == 1
    assert cached["conversion_status"] == "success"


def test_load_cached_rejects_stale_hash(tmp_path) -> None:
    pdf = tmp_path / "stale.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    _write_docling_artifacts(
        str(pdf),
        file_hash="old-hash",
        accelerator=ACCELERATOR_CPU,
        pipeline="standard",
        doc_json={},
        plain_text="old",
        tables=[],
        page_count=1,
        conversion_status="success",
        conversion_confidence=None,
    )

    result = _load_cached_docling_artifact(str(pdf), ACCELERATOR_CPU, expected_hash="new-hash")
    assert result is None


def test_convert_pdf_returns_none_when_docling_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "duke_rates.historical.ncuc.pipeline.docling_backend.get_docling_unavailable_reason",
        lambda: "Docling is not installed.",
    )
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    result = convert_pdf_with_docling(str(pdf))
    assert result is None


def test_convert_pdf_returns_cached_artifact(tmp_path, monkeypatch) -> None:
    """When a valid cache exists, convert_pdf_with_docling returns it without calling docling."""
    pdf = tmp_path / "cached.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    file_hash = _compute_file_hash(str(pdf))

    _write_docling_artifacts(
        str(pdf),
        file_hash=file_hash,
        accelerator=ACCELERATOR_CPU,
        pipeline="standard",
        doc_json={"tables": []},
        plain_text="cached content",
        tables=[],
        page_count=2,
        conversion_status="success",
        conversion_confidence=None,
    )

    # Docling itself must not be called
    monkeypatch.setattr(
        "duke_rates.historical.ncuc.pipeline.docling_backend.get_docling_unavailable_reason",
        lambda: None,  # claim available so the cache path runs
    )

    result = convert_pdf_with_docling(str(pdf), accelerator=ACCELERATOR_CPU, force=False)
    assert result is not None
    assert result["page_count"] == 2
    assert result["conversion_status"] == "success"
    assert result["accelerator"] == ACCELERATOR_CPU


def test_save_and_load_docling_artifact_db(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    artifact_id = save_docling_artifact(
        conn,
        discovery_record_id=None,
        source_pdf="/data/leaf500.pdf",
        file_hash="abc123",
        backend_version=DOCLING_BACKEND_VERSION,
        accelerator=ACCELERATOR_CPU,
        status="success",
        json_sidecar_path="/data/leaf500.pdf.docling_cpu.json",
        text_sidecar_path="/data/leaf500.pdf.docling_cpu.txt",
        tables_sidecar_path="/data/leaf500.pdf.docling_cpu_tables.json",
        page_count=3,
        conversion_confidence=0.92,
        table_count=2,
        metadata={"requested_by": "test"},
    )
    conn.commit()

    assert isinstance(artifact_id, int)
    assert artifact_id > 0

    row = load_docling_artifact(
        conn,
        source_pdf="/data/leaf500.pdf",
        file_hash="abc123",
        backend_version=DOCLING_BACKEND_VERSION,
        accelerator=ACCELERATOR_CPU,
    )
    assert row is not None
    assert row["status"] == "success"
    assert row["page_count"] == 3
    assert row["table_count"] == 2
    assert row["conversion_confidence"] == 0.92
    assert row["json_sidecar_path"] == "/data/leaf500.pdf.docling_cpu.json"

    conn.close()


def test_save_docling_artifact_updates_on_re_run(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    id1 = save_docling_artifact(
        conn,
        discovery_record_id=None,
        source_pdf="/data/leaf500.pdf",
        file_hash="abc123",
        backend_version=DOCLING_BACKEND_VERSION,
        accelerator=ACCELERATOR_CPU,
        status="success",
        json_sidecar_path="/old/path.json",
        text_sidecar_path=None,
        tables_sidecar_path=None,
        page_count=1,
        conversion_confidence=None,
        table_count=0,
    )
    conn.commit()

    id2 = save_docling_artifact(
        conn,
        discovery_record_id=None,
        source_pdf="/data/leaf500.pdf",
        file_hash="abc123",
        backend_version=DOCLING_BACKEND_VERSION,
        accelerator=ACCELERATOR_CPU,
        status="success",
        json_sidecar_path="/new/path.json",
        text_sidecar_path=None,
        tables_sidecar_path=None,
        page_count=1,
        conversion_confidence=None,
        table_count=0,
    )
    conn.commit()

    assert id1 == id2  # same row updated

    row = load_docling_artifact(
        conn,
        source_pdf="/data/leaf500.pdf",
        file_hash="abc123",
        backend_version=DOCLING_BACKEND_VERSION,
        accelerator=ACCELERATOR_CPU,
    )
    assert row is not None
    assert row["json_sidecar_path"] == "/new/path.json"
    conn.close()
