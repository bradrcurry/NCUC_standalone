"""Phase 2C: versioned rider-summary extraction on the Phase 2A run model.

Scratch databases only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from duke_rates.db import provenance as pv
from duke_rates.db import rider_summary_provenance as rsp
from duke_rates.db.ncuc_loader import persist_rider_summary_records
from duke_rates.db.provenance_schema import (
    PROVENANCE_DDL,
    PROVENANCE_SCHEMA_VERSION,
    _immutability_triggers,
    ensure_provenance_schema,
)
from duke_rates.db.sqlite import connect

PDF = "data/ncuc_dockets/E-2_Sub_1300/leaf600.pdf"


def _record(bcc_ba=0.500, storm=0.250, total=0.750, eff="2025-12-01", extra_class=False):
    rate_classes = [
        {
            "rate_class": "Residential Service Schedules",
            "applicable_schedules": ["RES", "R-TOUD"],
            "total_cents_per_kwh": total,
            "checksum_ok": True,
            "evidence": {"page": 1},
            "line_items": [
                {"label": "Fuel BA", "rider_code": "BA", "cents_per_kwh": bcc_ba, "effective_date": "12/1/25"},
                {"label": "Storm Recovery", "rider_code": "STS", "cents_per_kwh": storm, "effective_date": "12/1/25"},
                {"label": "TOTAL cents/kWh", "cents_per_kwh": total, "is_total": True},
            ],
        }
    ]
    if extra_class:
        rate_classes.append(
            {
                "rate_class": "Small General Service Schedules",
                "total_cents_per_kwh": 0.4,
                "line_items": [{"label": "Fuel BA", "rider_code": "BA", "cents_per_kwh": 0.4}],
            }
        )
    rate_classes.append({"rate_class": "?", "ambiguous_rate_class": True, "line_items": []})
    return {"source_pdf": PDF, "leaf_no": "600", "effective_date": eff, "rate_classes": rate_classes}


@pytest.fixture(autouse=True)
def source_pdf(tmp_path: Path, monkeypatch):
    """The source sheet exists: the D3 source validation reads its evidence pages before a promotion."""
    from test_extraction_provenance import write_clean_sheet

    monkeypatch.chdir(tmp_path)
    (tmp_path / PDF).parent.mkdir(parents=True)
    write_clean_sheet(tmp_path / PDF, "NC Leaf No. 600 Summary of Rider Adjustments")


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "rider.db")
    persist_rider_summary_records(c, [_record()], utility="DEP")  # the live, accepted reading
    ensure_provenance_schema(c)
    yield c
    c.close()


def _live(c):
    blocks = [dict(r) for r in c.execute("SELECT * FROM rider_summary_blocks ORDER BY id")]
    items = [dict(r) for r in c.execute("SELECT * FROM rider_line_items ORDER BY id")]
    return blocks, items


def _timeline(c):
    """The shape residential timeline queries read: class, date, rider, value."""
    return [
        tuple(r)
        for r in c.execute(
            "SELECT b.rate_class, b.effective_date, li.rider_code, li.cents_per_kwh "
            "FROM rider_summary_blocks b JOIN rider_line_items li ON li.block_id = b.id "
            "WHERE b.utility = 'DEP' AND li.is_total = 0 ORDER BY b.rate_class, li.rider_code"
        )
    ]


def test_runs_are_recorded_without_touching_the_read_model(conn):
    before = _live(conn)
    run_ids = rsp.record_rider_summary_runs(conn, [_record(storm=0.300, total=0.800)], created_by="pytest")
    assert len(run_ids) == 1 and _live(conn) == before
    run = conn.execute("SELECT * FROM extraction_runs WHERE id = ?", (run_ids[0],)).fetchone()
    assert run["target_kind"] == "rider_summary_source" and run["sealed_at"]
    assert json.loads(run["metrics_json"]) == {"blocks": 1, "ambiguous_blocks_skipped": 1}
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM extracted_rider_line_items WHERE run_id = ?", (run_ids[0],))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE extracted_rider_blocks SET rate_class = 'x' WHERE run_id = ?", (run_ids[0],))


def test_compare_reports_value_and_block_changes(conn):
    run_id = rsp.record_rider_summary_runs(
        conn, [_record(storm=0.300, total=0.800, extra_class=True)], created_by="pytest"
    )[0]
    diff = pv.compare_run(conn, run_id)
    assert diff["target_kind"] == "rider_summary_source"
    assert diff["summary"]["value_changes"] == 2  # STS row and the TOTAL row
    assert diff["summary"]["total_changes"] == 1 and diff["summary"]["added_blocks"] == 1
    assert {c["label"] for c in diff["value_changes"]} == {"Storm Recovery", "TOTAL cents/kWh"}


def test_promote_then_rollback_restores_blocks_items_and_ids(conn):
    before = _live(conn)
    timeline_before = _timeline(conn)
    run_id = rsp.record_rider_summary_runs(conn, [_record(storm=0.300, total=0.800)], created_by="pytest")[0]
    result = pv.promote(conn, run_id, promoted_by="alice", reason="reviewed diff")
    assert result["deleted_blocks"] == 1 and result["inserted_items"] == 3
    assert ("Residential Service Schedules", "2025-12-01", "STS", 0.3) in _timeline(conn)
    assert pv.verify_materialization(conn)["mismatches"] == []
    sid = conn.execute("SELECT target_id FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()[0]
    pv.rollback(conn, "rider_summary_source", sid, by="alice", reason="undo")
    assert _live(conn) == before and _timeline(conn) == timeline_before
    # Both readings stay stored.
    assert conn.execute("SELECT COUNT(*) FROM extracted_rider_blocks").fetchone()[0] == 1
    assert pv.run_review_status(conn, run_id) == "previously_active"


def test_rollback_refuses_after_out_of_band_change(conn):
    run_id = rsp.record_rider_summary_runs(conn, [_record(storm=0.300, total=0.800)], created_by="pytest")[0]
    pv.promote(conn, run_id, promoted_by="alice", reason="r")
    conn.execute("UPDATE rider_line_items SET cents_per_kwh = 9 WHERE rider_code = 'STS'")
    sid = conn.execute("SELECT target_id FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()[0]
    with pytest.raises(pv.PromotionConflictError):
        pv.rollback(conn, "rider_summary_source", sid, by="alice", reason="undo")
    assert pv.verify_materialization(conn)["mismatches"]


def test_loader_replace_records_candidates_and_keeps_history(conn, monkeypatch):
    monkeypatch.delenv("DUKE_RATES_LEGACY_REPLACE", raising=False)
    before = _live(conn)
    blocks, skipped = persist_rider_summary_records(conn, [_record(storm=0.300, total=0.800)], replace=True)
    assert (blocks, skipped) == (1, 1)
    assert _live(conn) == before  # the accepted reading is untouched
    assert conn.execute("SELECT COUNT(*) FROM extraction_runs WHERE target_kind = 'rider_summary_source'").fetchone()[0] == 1


def test_loader_replace_with_legacy_override_promotes_reversibly(conn, monkeypatch):
    monkeypatch.setenv("DUKE_RATES_LEGACY_REPLACE", "1")
    before = _live(conn)
    persist_rider_summary_records(conn, [_record(storm=0.300, total=0.800)], replace=True)
    assert ("Residential Service Schedules", "2025-12-01", "STS", 0.3) in _timeline(conn)
    sid = rsp.source_id(conn, "E-2_Sub_1300", PDF, create=False) or conn.execute(
        "SELECT id FROM rider_summary_sources"
    ).fetchone()[0]
    pv.rollback(conn, "rider_summary_source", sid, by="alice", reason="undo legacy replace")
    assert _live(conn) == before


def test_loader_replace_refuses_without_provenance_schema(tmp_path):
    c = connect(tmp_path / "plain.db")
    persist_rider_summary_records(c, [_record()])
    with pytest.raises(pv.ProvenanceError, match="provenance migrate"):
        persist_rider_summary_records(c, [_record(storm=0.3, total=0.8)], replace=True)
    assert c.execute("SELECT cents_per_kwh FROM rider_line_items WHERE rider_code = 'STS'").fetchone()[0] == 0.25


def test_loader_insert_only_path_is_unchanged(conn):
    before = _live(conn)
    assert persist_rider_summary_records(conn, [_record(storm=0.3, total=0.8)]) == (0, 2)
    assert _live(conn) == before


def test_out_of_band_deletes_are_kept_in_revisions(conn):
    conn.execute("DELETE FROM rider_line_items WHERE rider_code = 'BA'")
    rev = conn.execute("SELECT * FROM rider_summary_revisions WHERE operation = 'delete'").fetchone()
    assert rev["table_name"] == "rider_line_items"
    assert json.loads(rev["old_row_json"])["cents_per_kwh"] == 0.5


def test_duplicate_blocks_need_review(conn):
    rec = _record()
    rec["rate_classes"].append(dict(rec["rate_classes"][0]))
    run_id = rsp.record_rider_summary_runs(conn, [rec], created_by="pytest")[0]
    assert any(f["code"] == "rider_duplicate_block" for f in pv.review_findings(conn, run_id))
    with pytest.raises(pv.ProvenanceError, match="need review"):
        pv.promote(conn, run_id, promoted_by="alice", reason="r")


def test_v1_schema_upgrade_keeps_runs_and_triggers(tmp_path):
    c = connect(tmp_path / "v1.db")
    v1 = PROVENANCE_DDL.replace(", 'rider_summary_source'", "")
    c.executescript(v1)
    for stmt in _immutability_triggers():
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass  # triggers on tables v1 did not have
    c.execute("INSERT INTO provenance_schema_meta VALUES ('schema_version', '1')")
    run = pv.start_run(
        c, target_kind="tariff_version", target_id=1, parser_name="p", parser_version="v",
        extraction_method="m", created_by="pytest",
    )
    pv.seal_run(c, run)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO extraction_runs (run_uuid, target_kind, target_id, document_sha256_status, "
            "parser_name, parser_version, extraction_method, started_at, created_by) "
            "VALUES ('u', 'rider_summary_source', 1, 'unavailable', 'p', 'v', 'm', 'now', 'x')"
        )
    ensure_provenance_schema(c)
    assert c.execute("SELECT value FROM provenance_schema_meta WHERE key='schema_version'").fetchone()[0] == str(PROVENANCE_SCHEMA_VERSION)
    assert [tuple(r) for r in c.execute("SELECT id, sealed_at IS NOT NULL FROM extraction_runs")] == [(run, 1)]
    with pytest.raises(sqlite3.DatabaseError, match="sealed"):
        c.execute("UPDATE extraction_runs SET notes = 'x' WHERE id = ?", (run,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM extraction_runs WHERE id = ?", (run,))
    persist_rider_summary_records(c, [_record()])
    assert rsp.record_rider_summary_runs(c, [_record()], created_by="pytest")
    ensure_provenance_schema(c)  # idempotent
