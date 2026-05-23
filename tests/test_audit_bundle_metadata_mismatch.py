"""Tests for audit-bundle-metadata-mismatch-nc.

Locks the join logic between rule_document_type_v2 output and
historical_documents.family_key. Tests use a 5-doc fixture covering:

  - admin-content doc with tariff family_key  -> FLAGGED
  - admin-content doc with admin family_key   -> not flagged
  - tariff-content doc with tariff family_key -> not flagged
  - low-confidence v2 admin-content doc       -> not flagged (under threshold)
  - non-NC doc                                -> excluded by state filter
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from duke_rates.cli import app
from duke_rates.cli_commands import doc_intel as doc_intel_module


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bundle_audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE historical_documents (
            id INTEGER PRIMARY KEY, family_key TEXT, company TEXT, title TEXT,
            state TEXT, local_path TEXT, content_hash TEXT, effective_start TEXT,
            revision_label TEXT, supersedes_label TEXT, leaf_no TEXT,
            start_page INTEGER, end_page INTEGER
        );
        CREATE TABLE document_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
            stage TEXT NOT NULL, label TEXT NOT NULL, confidence REAL NOT NULL,
            classifier TEXT NOT NULL, classifier_version TEXT NOT NULL DEFAULT '',
            evidence_json TEXT, alternatives_json TEXT, metadata_json TEXT,
            superseded_by INTEGER, created_at TEXT NOT NULL
        );
        """
    )
    docs = [
        # hd=1: COVER_LETTER content tagged as a tariff leaf -> flagged
        (1, "nc-progress-leaf-602", "NC", "JAA Filing Cover Letter"),
        # hd=2: ORDER_FINAL content with admin family_key (e.g. an order family) -> not flagged
        (2, "nc-progress-order-2024", "NC", "Order Approving Rates"),
        # hd=3: TARIFF_SHEET content with tariff family -> not flagged
        (3, "nc-progress-leaf-500", "NC", "Residential Service"),
        # hd=4: COVER_LETTER content tagged tariff but LOW confidence -> not flagged
        (4, "nc-progress-leaf-601", "NC", "Maybe a cover letter"),
        # hd=5: COVER_LETTER content tagged with NC-style family but SC state
        # -> not flagged with --state NC, but IS flagged with --state SC.
        # Uses an NC-prefix family on purpose so the test isolates the
        # state filter (otherwise the prefix list would also exclude it).
        (5, "nc-progress-leaf-602", "SC", "Out-of-state cover letter"),
    ]
    for hd_id, family_key, state, title in docs:
        conn.execute(
            "INSERT INTO historical_documents (id, family_key, state, title) VALUES (?, ?, ?, ?)",
            (hd_id, family_key, state, title),
        )
    # v2 classifications
    v2_rows = [
        (1, "COVER_LETTER",  0.95),
        (2, "ORDER_FINAL",   0.95),
        (3, "TARIFF_SHEET",  0.98),
        (4, "COVER_LETTER",  0.60),  # under default 0.9 threshold
        (5, "COVER_LETTER",  0.95),  # SC, excluded by state filter
    ]
    for hd_id, label, conf in v2_rows:
        conn.execute(
            """INSERT INTO document_classifications
               (subject_kind, subject_id, stage, label, confidence, classifier, created_at)
               VALUES ('historical_document', ?, 'document_type', ?, ?,
                       'rule_document_type_v2', 'now')""",
            (str(hd_id), label, conf),
        )
    conn.commit()
    conn.close()

    from duke_rates import cli as cli_module

    class StubSettings:
        database_path = str(db_path)

    monkeypatch.setattr(cli_module, "_bootstrap", lambda: (StubSettings(), None))
    monkeypatch.setattr(doc_intel_module, "_bootstrap", lambda: (StubSettings(), None))
    return tmp_path


def test_audit_flags_admin_content_with_tariff_family(seeded_db):
    """Only hd=1 satisfies all criteria: admin-type v2 label + tariff family
    + NC state + confidence >= 0.9."""
    runner = CliRunner()
    result = runner.invoke(app, ["doc-intel", "audit-bundle-metadata-mismatch", "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["total_mismatches"] == 1
    assert summary["by_v2_label"] == {"COVER_LETTER": 1}


def test_audit_respects_state_filter(seeded_db):
    """The SC doc (hd=5) has the same content/family pattern but a different
    state. Default state=NC excludes it."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["doc-intel", "audit-bundle-metadata-mismatch", "--state", "SC", "--json"]
    )
    summary = json.loads(result.output)
    assert summary["total_mismatches"] == 1  # only hd=5 in SC
    assert summary["by_v2_label"] == {"COVER_LETTER": 1}


def test_audit_min_confidence_filters_low_conf(seeded_db):
    """Lowering --min-confidence to 0.5 should pick up hd=4 as well."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["doc-intel", "audit-bundle-metadata-mismatch",
              "--min-confidence", "0.5", "--json"]
    )
    summary = json.loads(result.output)
    # hd=1 (0.95) and hd=4 (0.60) both flagged
    assert summary["total_mismatches"] == 2
    assert summary["by_v2_label"] == {"COVER_LETTER": 2}


def test_audit_jsonl_export_carries_per_doc_detail(seeded_db, tmp_path):
    """--out PATH.jsonl must write one row per mismatched doc with the
    fields a triage UI needs."""
    out_path = tmp_path / "mismatches.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        app, ["doc-intel", "audit-bundle-metadata-mismatch",
              "--out", str(out_path)]
    )
    assert result.exit_code == 0, result.output
    lines = out_path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["hd_id"] == 1
    assert row["v2_label"] == "COVER_LETTER"
    assert row["family_key"] == "nc-progress-leaf-602"
    assert row["v2_confidence"] == 0.95


def test_audit_top_pairs_reflects_label_family_combinations(seeded_db):
    """Top pairs should list (COVER_LETTER, nc-progress-leaf-)."""
    runner = CliRunner()
    result = runner.invoke(app, ["doc-intel", "audit-bundle-metadata-mismatch", "--json"])
    summary = json.loads(result.output)
    assert len(summary["top_pairs"]) == 1
    top = summary["top_pairs"][0]
    assert top["v2_label"] == "COVER_LETTER"
    assert top["family_prefix"] == "nc-progress-leaf-"
    assert top["count"] == 1
