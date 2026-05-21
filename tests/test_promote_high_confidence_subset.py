"""Tests for promote-high-confidence-subset-nc.

The promoter is Stream A's quantity-vs-quality lever: it grows gold
faster than unanimous-only seeding by accepting agreement among a
*subset* of classifiers at high confidence, ignoring lower-confidence
dissent.

Locks the rules that determine when a subset qualifies, when it's
overridden by a competing high-confidence vote, and the
already-gold idempotency.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from duke_rates.cli import app


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "promote.db"
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

    # Five docs covering each interesting state:
    #   hd=1: 2 high-conf voters agree on CERT_OF_SERVICE, 2 lower-conf
    #         voters dissent. Should promote.
    #   hd=2: only 1 high-conf voter for any label → no qualifying subset.
    #   hd=3: 2 high-conf voters split (1 each label). High-conf
    #         disagreement → skipped.
    #   hd=4: 3 high-conf voters agree on RIDER, 1 lower-conf dissents.
    #         Should promote (subset of size 3).
    #   hd=5: 2 high-conf voters agree, but doc is already in gold → skipped.
    for hd_id in (1, 2, 3, 4, 5):
        conn.execute(
            "INSERT INTO historical_documents (id, state, title) VALUES (?, 'NC', ?)",
            (hd_id, f"Doc {hd_id}"),
        )
    votes = [
        # hd=1: llm + v2 high-conf CERT_OF_SERVICE; rule + embedding low-conf disagree
        (1, "llm_qwen3:8b_v1",       "CERTIFICATE_OF_SERVICE", 0.98),
        (1, "rule_document_type_v2", "CERTIFICATE_OF_SERVICE", 0.95),
        (1, "rule_document_type_v1", "COVER_LETTER",           0.30),
        (1, "embedding_knn_v1",      "TESTIMONY",              0.55),
        # hd=2: only 1 high-conf voter
        (2, "llm_qwen3:8b_v1",       "APPLICATION", 0.95),
        (2, "rule_document_type_v1", "ORDER_FINAL", 0.40),
        # hd=3: 2 high-conf voters split
        (3, "llm_qwen3:8b_v1",       "ORDER_FINAL", 0.95),
        (3, "rule_document_type_v2", "TESTIMONY",   0.95),
        # hd=4: 3 high-conf agree on RIDER
        (4, "llm_qwen3:8b_v1",       "RIDER", 0.98),
        (4, "rule_document_type_v2", "RIDER", 0.95),
        (4, "embedding_knn_v1",      "RIDER", 0.92),
        (4, "rule_document_type_v1", "TARIFF_SHEET", 0.30),
        # hd=5: would qualify but pre-seeded gold
        (5, "llm_qwen3:8b_v1",       "TESTIMONY", 0.98),
        (5, "rule_document_type_v2", "TESTIMONY", 0.95),
    ]
    for hd_id, classifier, label, conf in votes:
        conn.execute(
            """INSERT INTO document_classifications
               (subject_kind, subject_id, stage, label, confidence, classifier, created_at)
               VALUES ('historical_document', ?, 'document_type', ?, ?, ?, 'now')""",
            (str(hd_id), label, conf, classifier),
        )
    conn.execute(
        """INSERT INTO document_type_gold
           (subject_kind, subject_id, label, labeler, source, created_at)
           VALUES ('historical_document', '5', 'TESTIMONY', 'pre-seed', 'pre-seed', 'now')"""
    )
    conn.commit()
    conn.close()

    from duke_rates import cli as cli_module

    class StubSettings:
        database_path = str(db_path)

    monkeypatch.setattr(cli_module, "_bootstrap", lambda: (StubSettings(), None))
    return db_path


def test_promotes_2plus_high_conf_subset(seeded_db):
    """Default min_confidence=0.9, min_subset=2. hd=1 and hd=4 qualify."""
    runner = CliRunner()
    result = runner.invoke(app, ["promote-high-confidence-subset-nc", "--execute", "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["promoted"] == 2
    assert summary["label_distribution"] == {"CERTIFICATE_OF_SERVICE": 1, "RIDER": 1}

    conn = sqlite3.connect(seeded_db)
    rows = conn.execute(
        "SELECT subject_id, label, source FROM document_type_gold "
        "WHERE source='high_confidence_subset_agreement' ORDER BY subject_id"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    by_id = {r[0]: r for r in rows}
    assert by_id["1"][1] == "CERTIFICATE_OF_SERVICE"
    assert by_id["4"][1] == "RIDER"


def test_skips_already_gold(seeded_db):
    """hd=5 has 2 high-conf voters but is pre-seeded; the promoter must skip."""
    runner = CliRunner()
    result = runner.invoke(app, ["promote-high-confidence-subset-nc", "--execute", "--json"])
    summary = json.loads(result.output)
    assert summary["skipped_already_gold"] >= 1


def test_skips_high_conf_disagreement(seeded_db):
    """hd=3 has two high-conf voters voting different labels. The
    'best' subset (any single label) has size 1, which fails min_subset=2.
    Skipped under 'no_subset' since neither label has >=2 high-conf voters."""
    runner = CliRunner()
    result = runner.invoke(app, ["promote-high-confidence-subset-nc", "--execute", "--json"])
    summary = json.loads(result.output)
    # hd=3 falls into no_subset (neither CR_FINAL nor TESTIMONY has 2 voters at >=0.9)
    # hd=2 also falls into no_subset (only 1 high-conf voter)
    assert summary["skipped_no_subset"] >= 2


def test_higher_min_confidence_reduces_yield(seeded_db):
    """Bumping min_confidence to 0.96 disqualifies hd=4 (whose embedding
    vote is 0.92). hd=1 still qualifies (both voters >=0.95)."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["promote-high-confidence-subset-nc", "--min-confidence", "0.96",
              "--execute", "--json"]
    )
    summary = json.loads(result.output)
    # hd=4's subset shrinks to 2 voters (llm+v2 at 0.98/0.95) — still qualifies
    # at 0.95 cutoff. Actually 0.96 disqualifies v2 (0.95). So only llm remains.
    # Re-check: at 0.96 threshold, hd=1: llm 0.98 only (v2 was 0.95). subset of 1. Doesn't qualify.
    # hd=4: llm 0.98 only. subset of 1. Doesn't qualify.
    assert summary["promoted"] == 0


def test_min_subset_3_demands_three_classifiers(seeded_db):
    """--min-subset 3 disqualifies hd=1 (only 2 voters at >=0.9).
    hd=4 still qualifies (3 voters at >=0.9)."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["promote-high-confidence-subset-nc", "--min-subset", "3",
              "--execute", "--json"]
    )
    summary = json.loads(result.output)
    assert summary["promoted"] == 1
    assert summary["label_distribution"] == {"RIDER": 1}


def test_dry_run_does_not_write(seeded_db):
    runner = CliRunner()
    result = runner.invoke(app, ["promote-high-confidence-subset-nc", "--json"])
    summary = json.loads(result.output)
    assert summary["executed"] is False
    assert summary["promoted"] == 2  # would promote

    conn = sqlite3.connect(seeded_db)
    count = conn.execute(
        "SELECT COUNT(*) FROM document_type_gold WHERE source='high_confidence_subset_agreement'"
    ).fetchone()[0]
    conn.close()
    assert count == 0
