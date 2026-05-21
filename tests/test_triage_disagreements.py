"""Tests for triage-disagreements-nc — the labeling queue exporter.

Focus: agreement-vs-disagreement logic, underrepresented-bucket weighting,
gold-skip behavior, and label-filter. The text-sample enrichment is the
slow path and is exercised only at the integration level so the unit
tests stay fast.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from duke_rates.cli import app


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "triage.db"
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

    # 6 docs covering each interesting state:
    #   hd=1: 3-way disagreement (TARIFF_SHEET / RIDER / ORDER_FINAL)
    #         touches under-represented buckets -> high priority
    #   hd=2: 2-way disagreement (COVER_LETTER / CERTIFICATE_OF_SERVICE)
    #         both very under-represented -> highest priority
    #   hd=3: unanimous TARIFF_SHEET (3 classifiers) — should be skipped
    #   hd=4: only 1 classifier — should be skipped (no disagreement possible)
    #   hd=5: 2-way disagreement (TESTIMONY / ORDER_FINAL) — already gold,
    #         should be skipped via document_type_gold pre-filter
    #   hd=6: 2-way disagreement on TARIFF_SHEET / TESTIMONY (both common in
    #         hypothetical gold) — lower priority
    for hd_id in (1, 2, 3, 4, 5, 6):
        conn.execute(
            "INSERT INTO historical_documents (id, state, family_key, title) "
            "VALUES (?, 'NC', ?, ?)",
            (hd_id, f"nc-progress-leaf-{500 + hd_id}", f"Doc {hd_id}"),
        )

    votes = [
        (1, "rule_document_type_v1", "TARIFF_SHEET", 0.4),
        (1, "embedding_knn_v1",      "RIDER",        0.6),
        (1, "llm_qwen3:8b_v1",       "ORDER_FINAL",  0.9),
        (2, "rule_document_type_v1", "COVER_LETTER",         0.5),
        (2, "embedding_knn_v1",      "CERTIFICATE_OF_SERVICE", 0.6),
        (3, "rule_document_type_v1", "TARIFF_SHEET", 0.5),
        (3, "embedding_knn_v1",      "TARIFF_SHEET", 0.7),
        (3, "llm_qwen3:8b_v1",       "TARIFF_SHEET", 0.95),
        (4, "rule_document_type_v1", "RIDER",        0.4),
        (5, "rule_document_type_v1", "TESTIMONY",    0.5),
        (5, "embedding_knn_v1",      "ORDER_FINAL",  0.6),
        (6, "rule_document_type_v1", "TARIFF_SHEET", 0.4),
        (6, "embedding_knn_v1",      "TESTIMONY",    0.6),
    ]
    for hd_id, classifier, label, conf in votes:
        conn.execute(
            """INSERT INTO document_classifications
               (subject_kind, subject_id, stage, label, confidence, classifier, created_at)
               VALUES ('historical_document', ?, 'document_type', ?, ?, ?, '2026-05-21T00:00:00Z')""",
            (str(hd_id), label, conf, classifier),
        )

    # Pre-seed gold: hd=5 is settled. Also seed many TARIFF_SHEET + TESTIMONY
    # so they're well-represented (low weight) for the priority test.
    conn.execute(
        """INSERT INTO document_type_gold
           (subject_kind, subject_id, label, labeler, source, created_at)
           VALUES ('historical_document', '5', 'TESTIMONY', 'agreement:rule+embedding',
                   'unanimous_classifier_agreement', '2026-05-21T00:00:00Z')"""
    )
    for i in range(100):
        conn.execute(
            """INSERT INTO document_type_gold
               (subject_kind, subject_id, label, labeler, source, created_at)
               VALUES ('historical_document', ?, 'TARIFF_SHEET', 'seed', 'seed', 'now')""",
            (f"9{i:02d}",),
        )
    for i in range(50):
        conn.execute(
            """INSERT INTO document_type_gold
               (subject_kind, subject_id, label, labeler, source, created_at)
               VALUES ('historical_document', ?, 'TESTIMONY', 'seed', 'seed', 'now')""",
            (f"8{i:02d}",),
        )
    conn.commit()
    conn.close()

    from duke_rates import cli as cli_module

    class StubSettings:
        database_path = str(db_path)

    def fake_bootstrap():
        return StubSettings(), None

    monkeypatch.setattr(cli_module, "_bootstrap", fake_bootstrap)
    # Stub BulkExtractor methods so the text-sample enrichment doesn't try
    # to read real PDFs.
    from duke_rates.historical.ncuc.pipeline import bulk_extractor as be
    monkeypatch.setattr(
        be.BulkExtractor, "get_document_for_extraction",
        lambda self, hd_id: {"id": hd_id, "local_path": "/dev/null"},
    )
    monkeypatch.setattr(
        be.BulkExtractor, "extract_text_from_pdf",
        lambda self, *args, **kwargs: ("stub text sample for hd", "stub"),
    )
    return tmp_path


def test_triage_includes_disagreements_skips_unanimous_and_singleton(seeded_db):
    """Only docs with >=2 classifiers AND >=2 distinct labels qualify."""
    runner = CliRunner()
    out = seeded_db / "triage.jsonl"
    result = runner.invoke(app, ["triage-disagreements-nc", "--out", str(out)])
    assert result.exit_code == 0, result.output

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    hd_ids = {r["hd_id"] for r in rows}
    # Disagreement docs that are not pre-settled: hd=1, hd=2, hd=6
    assert hd_ids == {1, 2, 6}
    # hd=3 (unanimous) and hd=4 (singleton) excluded
    # hd=5 excluded by gold pre-filter


def test_triage_priority_orders_underrepresented_first(seeded_db):
    """hd=2's labels (COVER_LETTER, CERT_OF_SERVICE) are not in gold → max
    weight. hd=1 has RIDER + ORDER_FINAL + TARIFF_SHEET mix. hd=6 is all
    well-represented (TARIFF_SHEET + TESTIMONY) → lowest priority."""
    runner = CliRunner()
    out = seeded_db / "triage_priority.jsonl"
    result = runner.invoke(app, ["triage-disagreements-nc", "--out", str(out)])
    assert result.exit_code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    ordered_ids = [r["hd_id"] for r in rows]
    # hd=2 should rank first; hd=6 should be last (or absent if limit).
    assert ordered_ids[0] == 2
    assert ordered_ids.index(2) < ordered_ids.index(6)


def test_triage_label_filter_restricts_to_specified_buckets(seeded_db):
    """--label COVER_LETTER should only include docs where at least one
    classifier voted COVER_LETTER. Only hd=2 qualifies."""
    runner = CliRunner()
    out = seeded_db / "triage_cl.jsonl"
    result = runner.invoke(
        app,
        ["triage-disagreements-nc", "--out", str(out), "--label", "COVER_LETTER"],
    )
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["hd_id"] for r in rows] == [2]


def test_triage_no_weight_falls_back_to_id_order(seeded_db):
    """--no-weight removes the priority sort — output order is by hd_id."""
    runner = CliRunner()
    out = seeded_db / "triage_noweight.jsonl"
    result = runner.invoke(
        app,
        ["triage-disagreements-nc", "--out", str(out), "--no-weight"],
    )
    assert result.exit_code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    ids = [r["hd_id"] for r in rows]
    assert ids == sorted(ids)  # priority all tied → id-asc tiebreaker


def test_triage_row_carries_votes_majority_text_sample(seeded_db):
    """Each row must have the structured fields the labeling UI needs."""
    runner = CliRunner()
    out = seeded_db / "triage_schema.jsonl"
    result = runner.invoke(app, ["triage-disagreements-nc", "--out", str(out)])
    assert result.exit_code == 0
    row = json.loads(out.read_text().splitlines()[0])
    for k in (
        "hd_id", "priority", "votes", "labels_voted", "majority_label",
        "family_key", "title", "text_sample", "text_source",
    ):
        assert k in row, f"missing field {k}"
    # votes are per-classifier dicts
    assert all("classifier" in v and "label" in v and "confidence" in v for v in row["votes"])
