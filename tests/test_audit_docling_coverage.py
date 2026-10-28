"""Phase 2E M1: read-only exposure audit for unverifiable Docling artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import fitz
import pytest
from typer.testing import CliRunner

from duke_rates.cli_commands.provenance import provenance_app
from duke_rates.db.schema import SCHEMA_SQL, migrate
from duke_rates.document_intelligence.page_integrity_audit import (
    audit_docling_page_integrity,
    open_read_only,
)

_HD_INSERT = (
    "INSERT INTO historical_documents (id, family_key, title, state, company, category, kind,"
    " canonical_url, archived_url, snapshot_timestamp, local_path, content_hash, retrieved_at,"
    " start_page, end_page) VALUES (?, ?, 't', 'NC', 'progress', 'rider', 'pdf', ?, 'a' || ?, 's', ?,"
    " 'h', 'r', ?, ?)"
)
CHUNKED = {"degraded_modes": ["chunked"], "skipped_pages": [], "used_chunking": True}


def _pdf(path: Path, pages: int) -> Path:
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    doc.save(path)
    doc.close()
    return path


def _doc_json(pages):
    return {
        "pages": {str(p): {"page_no": p} for p in pages},
        "texts": [{"text": f"p{p}", "prov": [{"page_no": p}]} for p in pages],
        "tables": [],
    }


@pytest.fixture
def scratch(tmp_path):
    db = tmp_path / "audit.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    bundle = _pdf(tmp_path / "bundle.pdf", 30)
    whole = _pdf(tmp_path / "whole.pdf", 4)
    for pdf, doc_json, meta, count in [
        (bundle, _doc_json(range(1, 6)), CHUNKED, 30),  # last chunk only, numbered 1..5
        (whole, _doc_json(range(1, 5)), {}, 4),
    ]:
        conn.execute(
            """INSERT INTO docling_artifacts (source_pdf, file_hash, backend_version, accelerator,
                pipeline, status, page_count, table_count, plain_text_content, doc_json_content,
                tables_json_content, metadata_json, created_at, updated_at)
               VALUES (?, ?, 'docling_v2', 'cuda', 'standard', 'success', ?, 0, 'x', ?, '[]', ?, 'n', 'n')""",
            (str(pdf), hashlib.sha256(pdf.read_bytes()).hexdigest(), count, json.dumps(doc_json),
             json.dumps(meta)),
        )
    docs = [
        (1, "nc-progress-leaf-600", 1, 1, True),   # overlaps 1..5 -> wrong_pages
        (2, "nc-progress-leaf-528", 2, 9, True),   # overlaps -> wrong_pages
        (3, "nc-progress-leaf-532", 12, 15, True),  # beyond -> not_covered
        (4, "nc-progress-leaf-601", 20, 20, False),  # no version
        (5, "nc-progress-leaf-574", None, None, True),  # unbounded
    ]
    for family in {d[1] for d in docs}:
        conn.execute(
            "INSERT INTO tariff_families (family_key, state, company, family_type, created_at,"
            " updated_at) VALUES (?, 'NC', 'progress', 'rider', 'n', 'n')",
            (family,),
        )
    for hd_id, family, start, end, has_version in docs:
        conn.execute(
            _HD_INSERT,
            (hd_id, family, f"u{hd_id}", hd_id, str(bundle), start, end),
        )
        if has_version:
            conn.execute(
                "INSERT INTO tariff_versions (family_key, historical_document_id, effective_start,"
                " source_type, created_at) VALUES (?, ?, '2023-10-01', 'test', 'n')",
                (family, hd_id),
            )
    conn.execute(_HD_INSERT, (9, "nc-x", "u9", 9, str(whole), 1, 2))
    conn.commit()
    conn.close()
    return db


def test_audit_classifies_documents_on_unsafe_artifacts(scratch):
    report = audit_docling_page_integrity(open_read_only(scratch))
    s = report["summary"]
    assert s["artifacts_total"] == 2 and s["unsafe_artifacts"] == 1
    assert s["artifact_verdicts"]["unverifiable:chunk_relative_numbering"] == 1
    assert s["bound_documents"] == 5  # the whole-document artifact is safe; its doc is not listed
    assert s["span_class"] == {"wrong_pages": 2, "not_covered": 2, "unbounded": 1}
    assert s["possible_false_positive_exposures"] == 3  # two overlapping spans + unbounded
    assert s["missing_structure_exposures"] == 1
    assert s["affected_tariff_versions"] == 4
    by_id = {d["historical_document_id"]: d for d in report["documents"]}
    assert by_id[1]["classification"] == "unsafe_page_mapping"
    assert by_id[3]["classification"] == "incomplete_structured_coverage"
    assert by_id[4]["classification"] == "no_structured_extraction_required"
    assert not any(d["legacy_slicer_would_accept"] for d in report["documents"])


def test_audit_connection_is_read_only(scratch):
    conn = open_read_only(scratch)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM docling_artifacts")
    conn.close()


def test_cli_writes_json_and_does_not_modify_the_db(scratch, tmp_path):
    before = hashlib.sha256(scratch.read_bytes()).hexdigest()
    out = tmp_path / "audit.json"
    result = CliRunner().invoke(
        provenance_app, ["audit-docling-coverage", "--db", str(scratch), "--json", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["unsafe_pdfs"] == 1
    assert len(json.loads(out.read_text(encoding="utf-8"))["documents"]) == 5
    assert hashlib.sha256(scratch.read_bytes()).hexdigest() == before
