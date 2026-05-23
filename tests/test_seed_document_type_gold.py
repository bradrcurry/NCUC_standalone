"""Tests for the document_type_gold seeder CLI helper logic.

Lives separately from the CLI command wrapper to keep DB-state fixtures
small. The seeder is the foundation of Stream A: it converts classifier
agreement into ground truth that Stream D fine-tuning depends on, so
its rules need explicit locking.
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
    """Build a minimal DB with a few classified docs + the gold table."""
    db_path = tmp_path / "gold.db"
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
    # Seed historical_documents: 5 docs, all NC
    for hd_id in (1, 2, 3, 4, 5):
        conn.execute(
            "INSERT INTO historical_documents (id, state, family_key, title) VALUES (?, 'NC', ?, ?)",
            (hd_id, f"nc-progress-leaf-{500+hd_id}", f"Doc {hd_id}"),
        )
    # Classification matrix:
    #   hd=1: rule + embedding + llm all say TARIFF_SHEET  -> unanimous (3 classifiers)
    #   hd=2: rule + embedding say ORDER_FINAL             -> unanimous (2 classifiers)
    #   hd=3: rule says TARIFF_SHEET, embedding says RIDER -> disagreement
    #   hd=4: only rule classifier ran                     -> too few classifiers
    #   hd=5: rule + embedding + llm all say TESTIMONY     -> unanimous (3)
    rows = [
        (1, "rule_document_type_v1", "TARIFF_SHEET", 0.5),
        (1, "embedding_knn_v1",      "TARIFF_SHEET", 0.8),
        (1, "llm_qwen3:8b_v1",       "TARIFF_SHEET", 0.95),
        (2, "rule_document_type_v1", "ORDER_FINAL",  0.6),
        (2, "embedding_knn_v1",      "ORDER_FINAL",  0.7),
        (3, "rule_document_type_v1", "TARIFF_SHEET", 0.4),
        (3, "embedding_knn_v1",      "RIDER",        0.6),
        (4, "rule_document_type_v1", "COVER_LETTER", 0.3),
        (5, "rule_document_type_v1", "TESTIMONY",    0.5),
        (5, "embedding_knn_v1",      "TESTIMONY",    0.75),
        (5, "llm_qwen3:8b_v1",       "TESTIMONY",    0.95),
    ]
    for hd_id, classifier, label, conf in rows:
        conn.execute(
            """INSERT INTO document_classifications
               (subject_kind, subject_id, stage, label, confidence, classifier, created_at)
               VALUES ('historical_document', ?, 'document_type', ?, ?, ?, '2026-05-21T00:00:00Z')""",
            (str(hd_id), label, conf, classifier),
        )
    conn.commit()
    conn.close()

    # Patch _bootstrap so the CLI sees this db
    from duke_rates import cli as cli_module
    import duke_rates.config as cfg_module
    original_settings = cfg_module.get_settings()

    class StubSettings:
        database_path = str(db_path)

    def fake_bootstrap():
        return StubSettings(), None

    monkeypatch.setattr(cli_module, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(doc_intel_module, "_bootstrap", fake_bootstrap)
    return db_path


def test_seed_default_min_classifiers_seeds_unanimous_2plus(seeded_db):
    """At default min_classifiers=2, all three unanimous docs (hd=1, 2, 5)
    seed gold. hd=3 is disagreement, hd=4 has too few classifiers."""
    runner = CliRunner()
    result = runner.invoke(app, ["doc-intel", "seed-document-type-gold", "--execute", "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["seeded"] == 3
    assert summary["skipped_disagreement"] == 1  # hd=3
    assert summary["skipped_too_few_classifiers"] == 1  # hd=4
    assert summary["label_distribution"] == {
        "TARIFF_SHEET": 1, "ORDER_FINAL": 1, "TESTIMONY": 1,
    }

    # Verify rows landed in document_type_gold
    conn = sqlite3.connect(seeded_db)
    rows = conn.execute(
        "SELECT subject_id, label, labeler, source FROM document_type_gold ORDER BY subject_id"
    ).fetchall()
    conn.close()
    assert len(rows) == 3
    by_id = {r[0]: r for r in rows}
    assert by_id["1"][1] == "TARIFF_SHEET"
    assert "rule_document_type_v1" in by_id["1"][2]
    assert "embedding_knn_v1" in by_id["1"][2]
    assert "llm_qwen3:8b_v1" in by_id["1"][2]
    assert by_id["1"][3] == "unanimous_classifier_agreement"
    assert by_id["5"][1] == "TESTIMONY"


def test_seed_strict_min_classifiers_3_filters_to_three_way_agreement(seeded_db):
    """At min_classifiers=3, only hd=1 and hd=5 (which have all three
    classifiers agreeing) qualify. hd=2 (only 2 classifiers) drops out."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["doc-intel", "seed-document-type-gold", "--min-classifiers", "3", "--execute", "--json"]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["seeded"] == 2
    assert summary["skipped_too_few_classifiers"] >= 2  # hd=2 (only 2) + hd=4


def test_seed_is_idempotent_on_rerun(seeded_db):
    """Re-running the seeder should skip docs that already have an active
    gold row — no duplicate inserts."""
    runner = CliRunner()
    result1 = runner.invoke(app, ["doc-intel", "seed-document-type-gold", "--execute", "--json"])
    assert result1.exit_code == 0
    summary1 = json.loads(result1.output)
    assert summary1["seeded"] == 3

    result2 = runner.invoke(app, ["doc-intel", "seed-document-type-gold", "--execute", "--json"])
    assert result2.exit_code == 0
    summary2 = json.loads(result2.output)
    assert summary2["seeded"] == 0
    assert summary2["skipped_already_gold"] == 3


def test_seed_exclude_classifier_recomputes_agreement(seeded_db):
    """With --exclude-classifier rule_document_type_v1, hd=3's disagreement
    becomes a single-classifier vote (embedding=RIDER) — drops to
    skipped_too_few_classifiers, not skipped_disagreement."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "doc-intel", "seed-document-type-gold",
            "--exclude-classifier", "rule_document_type_v1",
            "--execute", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    # hd=1 still unanimous on embedding + llm
    # hd=2 drops to 1 classifier (embedding) -> too few
    # hd=3 drops to 1 classifier (embedding) -> too few (not disagreement)
    # hd=4 had 0 non-rule classifiers -> not even considered
    # hd=5 still unanimous on embedding + llm
    assert summary["seeded"] == 2
    assert summary["skipped_too_few_classifiers"] >= 2


def test_dry_run_does_not_write(seeded_db):
    """Without --execute, the seeder must not insert any rows."""
    runner = CliRunner()
    result = runner.invoke(app, ["doc-intel", "seed-document-type-gold", "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["executed"] is False
    assert summary["seeded"] == 3  # would seed, but didn't

    conn = sqlite3.connect(seeded_db)
    count = conn.execute("SELECT COUNT(*) FROM document_type_gold").fetchone()[0]
    conn.close()
    assert count == 0
