"""Tests for audit-stale-gold-nc."""
from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from duke_rates.cli import app


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "stale_gold.db"
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
        CREATE TABLE document_type_gold (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
            label TEXT NOT NULL, labeler TEXT NOT NULL, source TEXT NOT NULL,
            evidence_json TEXT, superseded_by INTEGER, notes TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    # Five fixture docs covering distinct cases:
    #   hd=1: gold says TESTIMONY, v2 says COVER_LETTER at 0.95 -> stale
    #   hd=2: gold says ORDER_FINAL, v2 says ORDER_FINAL -> agrees, not stale
    #   hd=3: gold says TARIFF_SHEET, v2 says RIDER at 0.6 -> v2 too low conf
    #   hd=4: gold says ORDER_FINAL, v2 says TARIFF_SHEET at 0.92 -> stale
    #   hd=5: superseded gold row -> excluded
    for hd_id in (1, 2, 3, 4, 5):
        conn.execute(
            "INSERT INTO historical_documents (id, family_key, state, title) VALUES (?, ?, 'NC', ?)",
            (hd_id, f"nc-progress-leaf-{500 + hd_id}", f"Doc {hd_id}"),
        )
    gold_rows = [
        # (hd_id, label, superseded_by, notes)
        (1, "TESTIMONY",     None, None),
        (2, "ORDER_FINAL",   None, None),
        (3, "TARIFF_SHEET",  None, None),
        (4, "ORDER_FINAL",   None, "pre-existing note"),
        (5, "TESTIMONY",     999, None),  # superseded — should be ignored
    ]
    for hd_id, label, sup_by, notes in gold_rows:
        conn.execute(
            """INSERT INTO document_type_gold
               (subject_kind, subject_id, label, labeler, source,
                superseded_by, notes, created_at)
               VALUES ('historical_document', ?, ?, 'seed', 'seed', ?, ?, '2026-05-21T00:00:00Z')""",
            (str(hd_id), label, sup_by, notes),
        )
    v2 = [
        (1, "COVER_LETTER",  0.95),
        (2, "ORDER_FINAL",   0.95),
        (3, "RIDER",         0.60),
        (4, "TARIFF_SHEET",  0.92),
        (5, "RIDER",         0.99),  # gold row superseded, doesn't matter
    ]
    for hd_id, label, conf in v2:
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
    return db_path


def test_audit_flags_disagreement_with_high_confidence(seeded_db):
    """hd=1 and hd=4 qualify — v2 disagrees with gold at >=0.9. hd=3's v2
    confidence is too low, hd=5 is superseded, hd=2 agrees."""
    runner = CliRunner()
    result = runner.invoke(app, ["audit-stale-gold-nc", "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["total_stale"] == 2
    assert summary["by_gold_label"] == {"TESTIMONY": 1, "ORDER_FINAL": 1}
    assert summary["by_v2_label"] == {"COVER_LETTER": 1, "TARIFF_SHEET": 1}


def test_min_confidence_threshold_changes_yield(seeded_db):
    """Lower threshold picks up hd=3's low-conf disagreement."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["audit-stale-gold-nc", "--min-v2-confidence", "0.5", "--json"]
    )
    summary = json.loads(result.output)
    assert summary["total_stale"] == 3


def test_mark_for_review_annotates_notes(seeded_db):
    """--mark-for-review appends a v2-disagreement note to each flagged
    row's notes field, preserving any existing notes."""
    runner = CliRunner()
    result = runner.invoke(app, ["audit-stale-gold-nc", "--mark-for-review", "--json"])
    summary = json.loads(result.output)
    assert summary["marked_for_review"] == 2

    conn = sqlite3.connect(seeded_db)
    rows = conn.execute(
        "SELECT subject_id, notes FROM document_type_gold WHERE superseded_by IS NULL "
        "ORDER BY subject_id"
    ).fetchall()
    conn.close()

    # hd=1 (no prior notes): annotation only
    assert "v2 disagrees: COVER_LETTER@0.95" in rows[0][1]
    # hd=4 (had pre-existing note): annotation appended after
    assert "pre-existing note" in rows[3][1]
    assert "v2 disagrees: TARIFF_SHEET@0.92" in rows[3][1]


def test_mark_for_review_is_idempotent(seeded_db):
    """Running --mark-for-review twice shouldn't duplicate the annotation."""
    runner = CliRunner()
    runner.invoke(app, ["audit-stale-gold-nc", "--mark-for-review"])
    runner.invoke(app, ["audit-stale-gold-nc", "--mark-for-review", "--json"])
    # Re-read state — annotation appears exactly once per row
    conn = sqlite3.connect(seeded_db)
    notes = conn.execute(
        "SELECT notes FROM document_type_gold WHERE subject_id = '1'"
    ).fetchone()[0]
    conn.close()
    assert notes.count("v2 disagrees: COVER_LETTER") == 1


def test_jsonl_export_carries_per_doc_detail(seeded_db, tmp_path):
    out_path = tmp_path / "stale.jsonl"
    runner = CliRunner()
    result = runner.invoke(app, ["audit-stale-gold-nc", "--out", str(out_path)])
    assert result.exit_code == 0
    lines = out_path.read_text().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    for k in (
        "hd_id", "family_key", "title", "gold_id", "gold_label",
        "gold_labeler", "gold_source", "v2_label", "v2_confidence",
    ):
        assert k in row


def test_excludes_superseded_gold_rows(seeded_db):
    """hd=5 has superseded_by set, so it must be excluded even though v2
    disagrees with the original label."""
    runner = CliRunner()
    result = runner.invoke(app, ["audit-stale-gold-nc", "--json"])
    summary = json.loads(result.output)
    # If hd=5 leaked, total_stale would be 3 not 2
    assert summary["total_stale"] == 2
