"""Phase 2A: immutable extraction runs, promotion/rollback, metadata audit.

Every test builds its own scratch database; nothing here touches production.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from duke_rates.db import provenance as pv
from duke_rates.db.provenance_schema import (
    PROVENANCE_TABLES,
    ProvenanceSchemaInUseError,
    drop_provenance_schema,
    ensure_provenance_schema,
    provenance_schema_installed,
)
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.pipeline.candidate_runs import (
    _quote_contains_value,
    to_candidate_charge,
)
from duke_rates.historical.ncuc.pipeline.rate_extractor import ExtractedCharge
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus

NOW = "2026-10-27T00:00:00+00:00"


def _seed(conn: sqlite3.Connection, pdf: Path) -> dict[str, int]:
    conn.execute(
        "INSERT INTO tariff_families (family_key, state, company, family_type, title, created_at, updated_at) "
        "VALUES ('nc-progress-leaf-500', 'NC', 'progress', 'rate_schedule', 'RES', ?, ?)",
        (NOW, NOW),
    )
    hd = conn.execute(
        "INSERT INTO historical_documents (family_key, title, category, kind, canonical_url, archived_url, "
        "snapshot_timestamp, local_path, content_hash, effective_start, retrieved_at, parsed_result_json) "
        "VALUES ('nc-progress-leaf-500', 'RES', 'rate', 'pdf', 'u', 'a1', 's', ?, 'abc', '2024-10-01', ?, ?)",
        (str(pdf), NOW, json.dumps({"schedule": {"bcc": 14.0}})),
    ).lastrowid
    vid = conn.execute(
        "INSERT INTO tariff_versions (family_key, historical_document_id, effective_start, source_type, created_at) "
        "VALUES ('nc-progress-leaf-500', ?, '2024-10-01', 'historical', ?)",
        (hd, NOW),
    ).lastrowid
    rows = [
        ("fixed", "Basic Customer Charge", 14.0, "$/month", None, "residential", None),
        ("energy_block", "Energy Charge", 0.12119, "$/kWh", None, "residential", None),
        # A manual correction: notes mark it curated.
        ("fixed", "Three-Phase Charge", 9.0, "$/month", None, "residential", "correction C3"),
        # An LLM-promoted row (linked from llm_promoted_charge_audit below).
        ("minimum", "Minimum Bill", 14.0, "$/month", None, "residential", None),
    ]
    ids = []
    for ctype, label, value, unit, tou, cls, notes in rows:
        ids.append(
            conn.execute(
                "INSERT INTO tariff_charges (version_id, family_key, charge_type, charge_label, rate_value, rate_unit, "
                "tou_period, season, customer_class, source_snippet, confidence_score, notes, created_at) "
                "VALUES (?, 'nc-progress-leaf-500', ?, ?, ?, ?, ?, 'all_year', ?, ?, 0.9, ?, ?)",
                (vid, ctype, label, value, unit, tou, cls, label, notes, NOW),
            ).lastrowid
        )
    conn.execute(
        "INSERT INTO llm_promoted_charge_audit (proposal_id, tariff_charge_id, validation_id, extraction_id, row_index) "
        "VALUES (1, ?, 1, 1, 0)",
        (ids[3],),
    )
    bill = conn.execute(
        "INSERT INTO bill_statements (source_path, content_hash, statement_json, created_at) VALUES ('b.pdf', 'h', ?, ?)",
        (json.dumps({"total": 121.15}), NOW),
    ).lastrowid
    conn.commit()
    return {
        "version_id": vid,
        "hd_id": hd,
        "bcc_id": ids[0],
        "energy_id": ids[1],
        "manual_id": ids[2],
        "llm_id": ids[3],
        "bill_id": bill,
    }


@pytest.fixture
def db(tmp_path: Path):
    pdf = tmp_path / "leaf-500.pdf"
    pdf.write_bytes(b"%PDF-1.4 test leaf 500")
    path = tmp_path / "scratch.db"
    conn = connect(path)
    ids = _seed(conn, pdf)
    yield conn, ids, path, pdf
    conn.close()


@pytest.fixture
def migrated(db):
    conn, ids, path, pdf = db
    ensure_provenance_schema(conn)
    return conn, ids, path, pdf


def _charges(conn, vid):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM tariff_charges WHERE version_id = ? ORDER BY id", (vid,)
        )
    ]


def _candidate(conn, vid, pdf, charges, *, effective_start=None, parser="profile:test"):
    run_id = pv.start_run(
        conn,
        target_kind="tariff_version",
        target_id=vid,
        parser_name=parser,
        parser_version="test-sha",
        extraction_method="deterministic_profile",
        created_by="pytest",
        family_key="nc-progress-leaf-500",
        version_id=vid,
        source_path=str(pdf),
        config={"ocr": False},
        interpreted_effective_start=effective_start,
    )
    pv.record_charges(conn, run_id, charges)
    return run_id


def _cc(ctype, label, value, unit="$/month", **kw):
    kw.setdefault("customer_class", "residential")
    kw.setdefault("season", "all_year")
    return pv.CandidateCharge(
        charge_type=ctype, charge_label=label, rate_value=value, rate_unit=unit, **kw
    )


# ---------------------------------------------------------------------------
# Migration and rollback of the schema itself
# ---------------------------------------------------------------------------


def _schema_objects(conn):
    return sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))


def test_connect_does_not_install_provenance_schema(db):
    conn, *_ = db
    assert not provenance_schema_installed(conn)


def test_migration_is_additive_idempotent_and_reversible(db):
    conn, ids, *_ = db
    before_schema = _schema_objects(conn)
    before_rows = _charges(conn, ids["version_id"])

    ensure_provenance_schema(conn)
    ensure_provenance_schema(conn)  # idempotent
    assert provenance_schema_installed(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(PROVENANCE_TABLES) <= names
    assert _charges(conn, ids["version_id"]) == before_rows

    drop_provenance_schema(conn)
    assert _schema_objects(conn) == before_schema
    assert _charges(conn, ids["version_id"]) == before_rows


def test_schema_drop_refuses_to_discard_history(migrated):
    conn, ids, *_ = migrated
    conn.execute(
        "UPDATE tariff_versions SET effective_start = '2024-10-02' WHERE id = ?",
        (ids["version_id"],),
    )
    conn.commit()
    with pytest.raises(ProvenanceSchemaInUseError):
        drop_provenance_schema(conn)
    drop_provenance_schema(conn, force=True)
    assert not provenance_schema_installed(conn)


def test_api_refuses_unmigrated_database(db):
    conn, ids, _, pdf = db
    with pytest.raises(pv.ProvenanceError, match="not installed"):
        _candidate(conn, ids["version_id"], pdf, [])


# ---------------------------------------------------------------------------
# Legacy backfill reproduces the read model byte for byte
# ---------------------------------------------------------------------------


def test_backfill_records_live_data_without_changing_it(migrated):
    conn, ids, *_ = migrated
    before = _charges(conn, ids["version_id"])
    payload_before = conn.execute("SELECT parsed_result_json FROM historical_documents").fetchone()[
        0
    ]

    stats = pv.backfill_legacy(conn, created_by="pytest")
    assert stats["versions_backfilled"] == 1
    assert stats["charges_backfilled"] == 4
    assert stats["historical_document_backfilled"] == 1
    assert stats["bill_statement_backfilled"] == 1

    assert _charges(conn, ids["version_id"]) == before
    assert (
        conn.execute("SELECT parsed_result_json FROM historical_documents").fetchone()[0]
        == payload_before
    )
    report = pv.verify_materialization(conn)
    assert report["ok"], report
    # Resumable: a second pass skips everything.
    again = pv.backfill_legacy(conn, created_by="pytest")
    assert again.get("versions_backfilled", 0) == 0

    legacy = conn.execute(
        "SELECT * FROM extracted_charges WHERE legacy_tariff_charge_id = ?", (ids["bcc_id"],)
    ).fetchone()
    assert legacy["page_status"] == "unavailable"
    assert legacy["page_number"] is None
    assert legacy["quote_status"] == "legacy_unverified"
    assert legacy["source_pdf_status"] == "legacy_unverified"


def test_backfill_tracks_charges_whose_version_row_is_gone(migrated):
    conn, ids, *_ = migrated
    conn.execute(
        "INSERT INTO tariff_charges (version_id, family_key, charge_type, rate_value, created_at) "
        "VALUES (424242, 'nc-carolinas-schedule-PL', 'fixed', 5.0, ?)",
        (NOW,),
    )
    conn.commit()
    stats = pv.backfill_legacy(conn, created_by="pytest")
    assert stats["orphan_versions_backfilled"] == 1
    report = pv.verify_materialization(conn)
    assert report["ok"] and report["versions_with_charges_but_no_active_run"] == 0


def test_verify_materialization_detects_out_of_band_writes(migrated):
    conn, ids, *_ = migrated
    pv.backfill_legacy(conn, created_by="pytest")
    conn.execute("UPDATE tariff_charges SET rate_value = 15.0 WHERE id = ?", (ids["bcc_id"],))
    conn.commit()
    report = pv.verify_materialization(conn)
    assert not report["ok"]
    assert report["mismatches"][0]["target_id"] == ids["version_id"]


# ---------------------------------------------------------------------------
# Immutable runs and explicit provenance
# ---------------------------------------------------------------------------


def test_multiple_runs_are_retained_and_do_not_touch_tariff_charges(migrated):
    conn, ids, _, pdf = migrated
    before = _charges(conn, ids["version_id"])
    r1 = _candidate(conn, ids["version_id"], pdf, [_cc("fixed", "Basic Customer Charge", 14.0)])
    pv.seal_run(conn, r1)
    r2 = _candidate(conn, ids["version_id"], pdf, [_cc("fixed", "Basic Customer Charge", 14.5)])
    pv.seal_run(conn, r2)
    assert _charges(conn, ids["version_id"]) == before
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE target_id = ?", (ids["version_id"],)
        ).fetchone()[0]
        == 2
    )
    assert pv.run_review_status(conn, r1) == "candidate"
    run = conn.execute("SELECT * FROM extraction_runs WHERE id = ?", (r1,)).fetchone()
    assert run["document_sha256_status"] == "computed"
    assert len(run["document_sha256"]) == 64
    assert run["config_fingerprint"] == pv.config_fingerprint({"ocr": False})


def test_sealed_runs_and_outputs_are_immutable(migrated):
    conn, ids, _, pdf = migrated
    run_id = _candidate(conn, ids["version_id"], pdf, [_cc("fixed", "BCC", 14.0)])
    pv.seal_run(conn, run_id)
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        conn.execute("UPDATE extraction_runs SET status = 'failed' WHERE id = ?", (run_id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE extracted_charges SET rate_value = 1 WHERE run_id = ?", (run_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM extracted_charges WHERE run_id = ?", (run_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM extraction_runs WHERE id = ?", (run_id,))
    with pytest.raises(pv.ProvenanceError):
        pv.seal_run(conn, run_id)
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        pv.record_charges(conn, run_id, [_cc("fixed", "late", 1.0)])


def test_unknown_provenance_is_stored_as_unavailable_never_fabricated(migrated):
    conn, ids, _, pdf = migrated
    run_id = _candidate(
        conn,
        ids["version_id"],
        pdf,
        [
            _cc(
                "energy_block",
                "Energy Charge",
                12.119,
                "cents/kWh",
                source_pdf_path=str(pdf),
                page_number=2,
                source_quote="Energy Charge 12.119 ¢ per kWh",
                bbox={"x0": 1, "y0": 2, "x1": 3, "y1": 4},
                printed_value_text="12.119",
                printed_unit="¢ per kWh",
            ),
            _cc("fixed", "Basic Customer Charge", 14.0),
        ],
    )
    rows = conn.execute(
        "SELECT * FROM extracted_charges WHERE run_id = ? ORDER BY ordinal", (run_id,)
    ).fetchall()
    full, bare = rows
    assert (
        full["page_status"],
        full["bbox_status"],
        full["quote_status"],
        full["source_pdf_status"],
    ) == ("available", "available", "available", "available")
    assert full["canonical_unit"] == "$/kWh"
    assert full["canonical_value"] == pytest.approx(0.12119)
    # Phase 2B: exact canonical value computed from the printed text and unit.
    assert full["unit_normalization"] == "cents_to_dollars:from_printed"
    assert (
        bare["page_status"],
        bare["bbox_status"],
        bare["quote_status"],
        bare["source_pdf_status"],
    ) == ("unavailable", "unavailable", "unavailable", "unavailable")
    assert bare["page_number"] is None and bare["bbox_json"] is None

    # The schema itself refuses a page without status, or a status without a page.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO extracted_charges (run_id, ordinal, version_id, family_key, charge_type, unit_normalization, "
            "source_pdf_status, page_status, bbox_status, quote_status, extraction_method, natural_key) "
            "VALUES (?, 99, 1, 'f', 'fixed', 'identity', 'unavailable', 'available', 'unavailable', "
            "'unavailable', 'x', 'k')",
            (run_id,),
        )

    completeness = pv.provenance_completeness(conn, [run_id])["profile:test"]
    assert completeness["n"] == 2
    assert completeness["page_available"] == 1
    assert completeness["quote_available"] == 1


def test_validation_flags_bad_runs_and_blocks_their_promotion(migrated):
    conn, ids, _, pdf = migrated
    run_id = _candidate(
        conn,
        ids["version_id"],
        pdf,
        [
            _cc("energy_block", "Energy", 12.119, "$/kWh"),  # implausible: $12/kWh
            _cc("Fixed Monthly Charge", "BCC", 14.0),  # unsupported type
        ],
    )
    summary = pv.seal_run(conn, run_id)
    assert summary["validation_status"] == "failed"
    detail = json.loads(
        conn.execute(
            "SELECT validation_json FROM extraction_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
    )
    codes = {i["code"] for issues in detail["charges"].values() for i in issues}
    assert {"implausible_magnitude", "unsupported_charge_type", "provenance_incomplete"} <= codes
    with pytest.raises(pv.ProvenanceError, match="failed validation"):
        pv.promote(conn, run_id, promoted_by="alice", reason="test")


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_compare_reports_every_change_category(migrated):
    conn, ids, _, pdf = migrated
    pv.backfill_legacy(conn, created_by="pytest")
    run_id = _candidate(
        conn,
        ids["version_id"],
        pdf,
        [
            _cc(
                "fixed",
                "Basic Customer Charge",
                14.0,
                page_number=1,
                source_pdf_path=str(pdf),
                source_quote="Basic Customer Charge $14.00",
            ),  # provenance only
            _cc("energy_block", "Energy Charge", 12.5, "cents/kWh"),  # value + unit
            _cc(
                "fixed", "Three-Phase Charge", 9.0, customer_class="commercial_small"
            ),  # rate class
            _cc(
                "minimum",
                "Minimum Bill",
                14.0,
                effective_start="2024-11-01",
                applicability={"schedules": ["RES"]},
            ),  # date + applicability
            _cc("demand", "Demand Charge", 3.32, "$/kW"),  # added
        ],
        effective_start="2024-10-15",
    )
    pv.seal_run(conn, run_id)
    diff = pv.compare_run(conn, run_id)
    s = diff["summary"]
    assert s["added"] == 1 and diff["added"][0]["charge_type"] == "demand"
    assert s["removed"] == 0
    assert s["value_changes"] == 1 and s["unit_changes"] == 1
    assert s["rate_class_changes"] == 1
    assert diff["rate_class_changes"][0]["change"] == ["residential", "commercial_small"]
    assert s["applicability_changes"] == 1
    assert diff["target_effective_date_changes"] == {
        "effective_start": ["2024-10-01", "2024-10-15"]
    }
    assert any(c["change"] == ["2024-10-01", "2024-11-01"] for c in diff["effective_date_changes"])
    assert s["provenance_differences"] >= 1
    bcc = next(
        d
        for d in diff["provenance_differences"]
        if d["active"]["charge_label"] == "Basic Customer Charge"
    )
    assert bcc["change"]["page_status"] == ["unavailable", "available"]


def test_compare_marks_removed_protected_rows(migrated):
    conn, ids, _, pdf = migrated
    run_id = _candidate(conn, ids["version_id"], pdf, [_cc("fixed", "Basic Customer Charge", 14.0)])
    pv.seal_run(conn, run_id)
    diff = pv.compare_run(conn, run_id)
    assert diff["summary"]["removed"] == 3
    assert diff["summary"]["removed_but_protected"] == 2


# ---------------------------------------------------------------------------
# Promotion and rollback
# ---------------------------------------------------------------------------


def test_promotion_requires_explicit_sealed_reviewed_run(migrated):
    conn, ids, _, pdf = migrated
    run_id = _candidate(conn, ids["version_id"], pdf, [_cc("fixed", "Basic Customer Charge", 14.0)])
    with pytest.raises(pv.ProvenanceError, match="not sealed"):
        pv.promote(conn, run_id, promoted_by="alice", reason="r")
    pv.seal_run(conn, run_id)
    with pytest.raises(pv.ProvenanceError, match="reason"):
        pv.promote(conn, run_id, promoted_by="alice", reason=" ")
    with pytest.raises(pv.PromotionConflictError):
        pv.promote(conn, run_id, promoted_by="alice", reason="r", expected_active_run_id=12345)
    empty = _candidate(conn, ids["version_id"], pdf, [])
    pv.seal_run(conn, empty)
    with pytest.raises(pv.ProvenanceError, match="empty"):
        pv.promote(conn, empty, promoted_by="alice", reason="r")
    preview = pv.promote(conn, run_id, promoted_by="alice", reason="r", dry_run=True)
    assert preview["dry_run"] and pv.active_run(conn, "tariff_version", ids["version_id"]) is None


def test_promote_preserves_curated_rows_and_rollback_restores_exactly(migrated):
    conn, ids, _, pdf = migrated
    pv.backfill_legacy(conn, created_by="pytest")
    vid = ids["version_id"]
    before = _charges(conn, vid)
    legacy_run = pv.active_run(conn, "tariff_version", vid)["run_id"]

    run_id = _candidate(
        conn,
        vid,
        pdf,
        [
            _cc("fixed", "Basic Customer Charge", 14.0),
            _cc("energy_block", "Energy Charge", 0.12500, "$/kWh"),
            _cc("fixed", "Three-Phase Charge", 9.0),  # same key as the manual correction
        ],
    )
    pv.seal_run(conn, run_id)
    result = pv.promote(
        conn, run_id, promoted_by="alice", reason="reviewed", expected_active_run_id=legacy_run
    )

    after = _charges(conn, vid)
    assert sorted(result["preserved_protected"]) == sorted([ids["manual_id"], ids["llm_id"]])
    assert result["deleted"] == 2 and result["inserted"] == 2
    assert len(result["skipped_candidates_for_protected"]) == 1  # no double three-phase charge
    labels = sorted((r["charge_label"], r["rate_value"]) for r in after)
    assert labels == sorted(
        [
            ("Basic Customer Charge", 14.0),
            ("Energy Charge", 0.125),
            ("Three-Phase Charge", 9.0),
            ("Minimum Bill", 14.0),
        ]
    )
    assert pv.run_review_status(conn, run_id) == "active"
    assert pv.run_review_status(conn, legacy_run) == "previously_active"
    assert pv.verify_materialization(conn)["ok"]

    # Receipts: the deletes and inserts were attributed to the promotion.
    actors = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT changed_by FROM tariff_charge_revisions WHERE version_id = ?", (vid,)
        )
    }
    assert actors == {"provenance.promote:alice"}

    back = pv.rollback(conn, "tariff_version", vid, by="alice", reason="regressed")
    assert back["active_run_id"] == legacy_run
    assert _charges(conn, vid) == before  # same ids, same values, same created_at
    assert pv.verify_materialization(conn)["ok"]
    assert pv.run_review_status(conn, run_id) == "previously_active"

    # Promote again after the rollback, then roll back again.
    pv.promote(conn, run_id, promoted_by="alice", reason="second look")
    pv.rollback(conn, "tariff_version", vid, by="alice", reason="again")
    assert _charges(conn, vid) == before
    with pytest.raises(pv.ProvenanceError, match="no promotion"):
        pv.rollback(conn, "tariff_version", vid, by="alice", reason="nothing left")


def test_reviewed_replacement_can_supersede_a_protected_row(migrated):
    conn, ids, _, pdf = migrated
    vid = ids["version_id"]
    run_id = _candidate(conn, vid, pdf, [_cc("fixed", "Three-Phase Charge", 9.5)])
    pv.seal_run(conn, run_id)
    with pytest.raises(pv.ProvenanceError, match="not protected"):
        pv.promote(
            conn, run_id, promoted_by="alice", reason="r", supersede_protected_ids=[ids["bcc_id"]]
        )
    result = pv.promote(
        conn,
        run_id,
        promoted_by="alice",
        reason="C3 superseded by re-extraction",
        supersede_protected_ids=[ids["manual_id"]],
    )
    rows = {r["charge_label"]: r for r in _charges(conn, vid)}
    assert rows["Three-Phase Charge"]["rate_value"] == 9.5
    assert result["preserved_protected"] == [ids["llm_id"]]


def test_rollback_refuses_after_out_of_band_change(migrated):
    conn, ids, _, pdf = migrated
    vid = ids["version_id"]
    run_id = _candidate(conn, vid, pdf, [_cc("fixed", "Basic Customer Charge", 14.0)])
    pv.seal_run(conn, run_id)
    pv.promote(conn, run_id, promoted_by="alice", reason="r")
    conn.execute(
        "UPDATE tariff_charges SET rate_value = 99 WHERE version_id = ? AND notes IS NULL", (vid,)
    )
    conn.commit()
    snapshot = _charges(conn, vid)
    with pytest.raises(pv.PromotionConflictError):
        pv.rollback(conn, "tariff_version", vid, by="alice", reason="r")
    assert _charges(conn, vid) == snapshot  # atomic: nothing changed


def test_promotion_failure_is_atomic(migrated, monkeypatch):
    conn, ids, _, pdf = migrated
    vid = ids["version_id"]
    before = _charges(conn, vid)
    run_id = _candidate(conn, vid, pdf, [_cc("fixed", "Basic Customer Charge", 15.0)])
    pv.seal_run(conn, run_id)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pv, "_set_active", boom)
    with pytest.raises(RuntimeError):
        pv.promote(conn, run_id, promoted_by="alice", reason="r")
    assert _charges(conn, vid) == before
    assert conn.execute("SELECT COUNT(*) FROM promotion_log").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM provenance_write_context").fetchone()[0] == 0


def test_rejected_runs_are_kept_and_cannot_be_promoted(migrated):
    conn, ids, _, pdf = migrated
    run_id = _candidate(conn, ids["version_id"], pdf, [_cc("fixed", "Basic Customer Charge", 14.0)])
    pv.seal_run(conn, run_id)
    pv.reject_run(conn, run_id, by="alice", reason="wrong leaf")
    assert pv.run_review_status(conn, run_id) == "rejected"
    with pytest.raises(pv.ProvenanceError, match="rejected"):
        pv.promote(conn, run_id, promoted_by="alice", reason="r")


def test_overrides_apply_on_promotion(migrated):
    conn, ids, _, pdf = migrated
    vid = ids["version_id"]
    energy = {
        "charge_type": "energy_block",
        "charge_label": "Energy Charge",
        "customer_class": "residential",
        "season": "all_year",
        "tier_min": None,
        "tier_max": None,
        "tou_period": None,
        "rate_unit": "$/kWh",  # Phase 2B: the unit is part of a charge's identity
    }
    pv.add_override(
        conn,
        version_id=vid,
        action="replace",
        natural_key_row=energy,
        payload={**energy, "rate_value": 0.12119, "rate_unit": "$/kWh"},
        source_kind="correction",
        source_ref="manifest:C1",
        approved_by="alice",
        evidence_page=2,
        evidence_quote="12.119 cents per kWh",
    )
    noise = {
        "charge_type": "fixed",
        "charge_label": "Footer",
        "customer_class": "residential",
        "season": "all_year",
        "rate_unit": "$/month",
    }
    pv.add_override(
        conn,
        version_id=vid,
        action="suppress",
        natural_key_row=noise,
        source_kind="manual",
        source_ref="review:1",
        approved_by="alice",
    )
    added = {
        "charge_type": "demand",
        "charge_label": "Demand",
        "customer_class": "residential",
        "season": "all_year",
        "rate_value": 3.0,
        "rate_unit": "$/kW",
    }
    add_id = pv.add_override(
        conn,
        version_id=vid,
        action="add",
        payload=added,
        source_kind="llm",
        source_ref="llm:7",
        approved_by="alice",
    )
    run_id = _candidate(
        conn,
        vid,
        pdf,
        [
            _cc("energy_block", "Energy Charge", 0.99, "$/kWh"),
            _cc("fixed", "Footer", 1.0),
        ],
    )
    pv.seal_run(conn, run_id)
    result = pv.promote(conn, run_id, promoted_by="alice", reason="r")
    rows = {r["charge_label"]: r for r in _charges(conn, vid)}
    assert rows["Energy Charge"]["rate_value"] == 0.12119
    assert rows["Energy Charge"]["notes"] == "override:manifest:C1"
    assert "Footer" not in rows
    assert rows["Demand"]["notes"] == "override:llm:7"
    assert len(result["applied_overrides"]) == 3
    pv.retire_override(conn, add_id, by="alice", reason="superseded")
    with pytest.raises(pv.ProvenanceError):
        pv.retire_override(conn, add_id, by="alice", reason="twice")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM charge_overrides WHERE id = ?", (add_id,))


# ---------------------------------------------------------------------------
# Versioned parsed_result_json / statement_json
# ---------------------------------------------------------------------------


def test_payload_runs_promote_and_roll_back(migrated):
    conn, ids, _, pdf = migrated
    pv.backfill_legacy(conn, created_by="pytest")
    hd = ids["hd_id"]
    original = conn.execute(
        "SELECT parsed_result_json FROM historical_documents WHERE id = ?", (hd,)
    ).fetchone()[0]
    run_id = pv.start_run(
        conn,
        target_kind="historical_document",
        target_id=hd,
        parser_name="parse_document",
        parser_version="test",
        extraction_method="rate_schedule_parser",
        created_by="pytest",
        source_path=str(pdf),
    )
    pv.record_document(conn, run_id, json.dumps({"schedule": {"bcc": 14.0, "energy": 12.119}}))
    pv.seal_run(conn, run_id)
    diff = pv.compare_run(conn, run_id)
    assert diff["changed_paths"] == ["$.schedule.energy"]
    pv.promote(conn, run_id, promoted_by="alice", reason="r")
    assert (
        json.loads(
            conn.execute(
                "SELECT parsed_result_json FROM historical_documents WHERE id = ?", (hd,)
            ).fetchone()[0]
        )["schedule"]["energy"]
        == 12.119
    )
    history = pv.payload_history(conn, "historical_document", hd)
    assert history[-1]["active_run_id"] == run_id and history[-1]["matches_active_run"] is True
    assert any(h["kind"] == "overwritten_value" for h in history)
    pv.rollback(conn, "historical_document", hd, by="alice", reason="r")
    assert (
        conn.execute(
            "SELECT parsed_result_json FROM historical_documents WHERE id = ?", (hd,)
        ).fetchone()[0]
        == original
    )


def test_reparse_outside_the_api_keeps_the_previous_representation(migrated):
    conn, ids, path, _ = migrated
    pv.backfill_legacy(conn, created_by="pytest")
    hd = ids["hd_id"]
    original = conn.execute(
        "SELECT parsed_result_json FROM historical_documents WHERE id = ?", (hd,)
    ).fetchone()[0]
    conn.commit()
    # Repository.save_historical_parse_result overwrites in place (legacy writer).
    Repository(path).save_historical_parse_result(
        hd, DocumentParseResult(document_id=hd, parser_name="test", status=ParseStatus.PARSED)
    )
    rev = conn.execute(
        "SELECT * FROM document_payload_revisions WHERE row_id = ?", (hd,)
    ).fetchone()
    assert rev["old_payload"] == original
    assert rev["changed_by"] == "unattributed"
    history = pv.payload_history(conn, "historical_document", hd)
    assert history[-1]["matches_active_run"] is False  # drift from the active run is visible
    assert not pv.verify_materialization(conn)["ok"]


# ---------------------------------------------------------------------------
# Metadata auditing
# ---------------------------------------------------------------------------


def test_revise_metadata_is_attributed_and_revertible(migrated):
    conn, ids, *_ = migrated
    vid = ids["version_id"]
    rev_id = pv.revise_metadata(
        conn,
        "tariff_versions",
        vid,
        "effective_start",
        "2024-10-02",
        changed_by="cli:normalize-nc-effective-dates",
        evidence="leaf p1",
        expected_old_value="2024-10-01",
    )
    rev = conn.execute("SELECT * FROM metadata_revisions WHERE id = ?", (rev_id,)).fetchone()
    assert (rev["old_value"], rev["new_value"], rev["changed_by"], rev["evidence"]) == (
        "2024-10-01",
        "2024-10-02",
        "cli:normalize-nc-effective-dates",
        "leaf p1",
    )
    assert (
        pv.revise_metadata(
            conn, "tariff_versions", vid, "effective_start", "2024-10-02", changed_by="x"
        )
        is None
    )
    with pytest.raises(pv.ProvenanceError, match="parse"):
        pv.revise_metadata(
            conn, "tariff_versions", vid, "effective_start", "someday", changed_by="x"
        )
    with pytest.raises(pv.ProvenanceError, match="audited"):
        pv.revise_metadata(conn, "tariff_versions", vid, "notes", "n", changed_by="x")
    with pytest.raises(pv.PromotionConflictError):
        pv.revise_metadata(
            conn,
            "tariff_versions",
            vid,
            "effective_start",
            "2024-10-03",
            changed_by="x",
            expected_old_value="2024-10-01",
        )

    revert_id = pv.revert_metadata(conn, rev_id, by="alice")
    assert (
        conn.execute("SELECT effective_start FROM tariff_versions WHERE id = ?", (vid,)).fetchone()[
            0
        ]
        == "2024-10-01"
    )
    assert (
        conn.execute(
            "SELECT reverted_by FROM metadata_revisions WHERE id = ?", (rev_id,)
        ).fetchone()[0]
        == revert_id
    )
    assert (
        conn.execute(
            "SELECT operation FROM metadata_revisions WHERE id = ?", (revert_id,)
        ).fetchone()[0]
        == "revert"
    )
    with pytest.raises(pv.ProvenanceError, match="already reverted"):
        pv.revert_metadata(conn, rev_id, by="alice")


def test_binding_rebind_keeps_integer_type_on_revert(migrated):
    conn, ids, *_ = migrated
    vid = ids["version_id"]
    rev_id = pv.revise_metadata(
        conn, "tariff_versions", vid, "historical_document_id", 999, changed_by="lineage:rebind"
    )
    pv.revert_metadata(conn, rev_id, by="alice")
    value, kind = conn.execute(
        "SELECT historical_document_id, typeof(historical_document_id) FROM tariff_versions WHERE id = ?",
        (vid,),
    ).fetchone()
    assert (value, kind) == (ids["hd_id"], "integer")


def test_writes_that_bypass_the_api_are_still_audited(migrated):
    conn, ids, *_ = migrated
    vid, hd = ids["version_id"], ids["hd_id"]
    # Simulates an ad-hoc script under scripts/ writing directly.
    conn.execute(
        "UPDATE historical_documents SET effective_start = '2024-10-01T00:00:00' WHERE id = ?",
        (hd,),
    )
    conn.execute("UPDATE tariff_versions SET status = 'superseded' WHERE id = ?", (vid,))
    conn.execute("DELETE FROM tariff_charges WHERE id = ?", (ids["energy_id"],))
    conn.commit()
    hist = pv.metadata_history(conn, "historical_documents", hd)
    assert [(h["column_name"], h["old_value"], h["changed_by"]) for h in hist] == [
        ("effective_start", "2024-10-01", "unattributed")
    ]
    assert pv.metadata_history(conn, "tariff_versions", vid)[0]["new_value"] == "superseded"
    deleted = conn.execute(
        "SELECT * FROM tariff_charge_revisions WHERE operation = 'delete'"
    ).fetchone()
    assert json.loads(deleted["old_row_json"])["rate_value"] == 0.12119
    # Unchanged values leave no receipt.
    conn.execute("UPDATE tariff_versions SET status = 'superseded' WHERE id = ?", (vid,))
    assert len(pv.metadata_history(conn, "tariff_versions", vid)) == 1


def test_deleting_a_version_keeps_its_audited_metadata(migrated):
    conn, ids, *_ = migrated
    conn.execute("DELETE FROM tariff_versions WHERE id = ?", (ids["version_id"],))
    conn.commit()
    rev = pv.metadata_history(conn, "tariff_versions", ids["version_id"])[0]
    assert rev["operation"] == "delete"
    assert json.loads(rev["old_value"])["effective_start"] == "2024-10-01"


def test_audit_receipts_are_append_only(migrated):
    conn, ids, *_ = migrated
    conn.execute(
        "UPDATE tariff_versions SET effective_start = '2024-10-02' WHERE id = ?",
        (ids["version_id"],),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM metadata_revisions")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE metadata_revisions SET new_value = 'x'")


# ---------------------------------------------------------------------------
# Candidate extraction mapping (BulkExtractor output -> candidate charges)
# ---------------------------------------------------------------------------


def test_bulk_extractor_snippets_count_as_quotes_only_when_they_print_the_value():
    assert _quote_contains_value("Basic Customer Charge $14.00 per month", 14.0)
    assert _quote_contains_value("12.119 ¢ per kWh", 0.12119)
    assert not _quote_contains_value("Basic Customer Charge", 14.0)
    label_only = ExtractedCharge(
        "fixed", "Basic Customer Charge", 14.0, "$/month", source_snippet="Basic Customer Charge"
    )
    cc = to_candidate_charge(
        label_only, source_pdf_path="x.pdf", source_pdf_sha256="s", profile="p"
    )
    assert cc.source_quote is None and cc.extractor_snippet == "Basic Customer Charge"
    assert cc.page_number is None and cc.bbox is None
    assert cc.extraction_method == "parser_profile:p"
