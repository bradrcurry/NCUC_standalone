"""Phase 2C: BulkExtractor stores candidate runs; tariff_charges changes only by promotion.

Scratch databases only. The PDF parse is stubbed so the tests exercise the
persistence contract, not a parser profile.
"""

from __future__ import annotations

import pytest

from duke_rates.db import provenance as pv
from duke_rates.db.provenance_schema import ensure_provenance_schema
from duke_rates.historical.ncuc.pipeline import bulk_extractor as be
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.historical.ncuc.pipeline.rate_extractor import ExtractedCharge

from test_extraction_provenance import _charges, db, migrated  # noqa: F401  (fixtures)


def _charge(ctype, label, value, unit="$/month"):
    return ExtractedCharge(
        charge_type=ctype, charge_label=label, rate_value=value, rate_unit=unit,
        season="all_year", source_snippet=f"{label} {value}", confidence_score=0.9,
    )


NEW = [
    _charge("fixed", "Basic Customer Charge", 15.0),
    _charge("energy_block", "Energy Charge", 0.13, "$/kWh"),
]


def _extractor(path, charges=NEW, status="parsed", mode=None, selection=None):
    """A BulkExtractor whose parse is stubbed; records the mode each parse ran in."""
    ext = BulkExtractor(str(path), structured_mode=mode)
    ext.seen_modes = []

    def in_mode(m):
        ext.seen_modes.append(m)
        clone = BulkExtractor._in_mode(ext, m) if m != ext.structured_mode else ext
        clone.extract_charges_from_document = lambda doc: (
            list(charges), "test_profile", [], status, None,
            {"full_text": "", "text_source": "stub"}, dict(selection or {}),
        )
        return clone

    ext._in_mode = in_mode
    ext._record_bookkeeping = lambda extraction: None
    return ext


def _doc(ids, pdf):
    return {
        "id": ids["hd_id"], "family_key": "nc-progress-leaf-500", "local_path": str(pdf),
        "effective_start": "2024-10-01", "start_page": None, "end_page": None,
        "version_id": ids["version_id"],
    }


def test_process_document_stores_candidate_and_leaves_tariff_charges(migrated):
    conn, ids, path, pdf = migrated
    before = _charges(conn, ids["version_id"])
    ext = _extractor(path)
    result = ext.process_document_result(_doc(ids, pdf))
    assert result.persistence == "candidate" and result.inserted == 0 and result.extracted == 2
    assert _charges(conn, ids["version_id"]) == before
    run = conn.execute("SELECT * FROM extraction_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["sealed_at"] and run["document_sha256_status"] == "computed"
    assert '"structured_mode":"hybrid"' in run["config_json"]
    assert '"persistence":"candidate"' in run["config_json"]
    assert pv.run_review_status(conn, result.run_id) == "candidate"
    # Compatibility tuple: (doc_id, family_key, num_inserted, status, profile)
    assert ext.process_document(_doc(ids, pdf)) == (
        ids["hd_id"], "nc-progress-leaf-500", 0, "parsed", "test_profile"
    )


def test_every_reprocess_is_a_new_immutable_run(migrated):
    conn, ids, path, pdf = migrated
    ext = _extractor(path)
    runs = {ext.process_document_result(_doc(ids, pdf)).run_id for _ in range(3)}
    assert len(runs) == 3
    uuids = {r[0] for r in conn.execute("SELECT run_uuid FROM extraction_runs")}
    assert len(uuids) == 3


def test_empty_reprocess_with_force_clear_does_not_delete_without_legacy_replace(migrated):
    conn, ids, path, pdf = migrated
    before = _charges(conn, ids["version_id"])
    _extractor(path, charges=[], status="empty").process_document(_doc(ids, pdf), force_clear=True)
    assert _charges(conn, ids["version_id"]) == before


def test_hybrid_is_candidate_default_and_explicit_mode_wins(migrated):
    conn, ids, path, pdf = migrated
    ext = _extractor(path)
    ext.process_document_result(_doc(ids, pdf))
    assert ext.seen_modes[-1] == "hybrid"
    legacy = _extractor(path, mode="legacy")
    r = legacy.process_document_result(_doc(ids, pdf))
    assert r.structured_mode == "legacy"
    # A legacy replace without a chosen mode reproduces the legacy parse.
    assert BulkExtractor(str(path)).candidate_mode(legacy_replace=True) == "legacy"
    assert BulkExtractor(str(path), structured_mode="native").candidate_mode(legacy_replace=True) == "native"


def test_legacy_replace_promotes_keeps_curated_rows_and_rolls_back(migrated):
    conn, ids, path, pdf = migrated
    before = _charges(conn, ids["version_id"])
    result = _extractor(path).process_document_result(_doc(ids, pdf), legacy_replace=True)
    assert result.persistence == "legacy_replace" and result.inserted == 2
    after = _charges(conn, ids["version_id"])
    kept = {r["id"] for r in after}
    assert ids["manual_id"] in kept and ids["llm_id"] in kept
    assert {r["charge_label"]: r["rate_value"] for r in after}["Basic Customer Charge"] == 15.0
    assert pv.run_review_status(conn, result.run_id) == "active"
    pv.rollback(conn, "tariff_version", ids["version_id"], by="pytest", reason="undo")
    assert _charges(conn, ids["version_id"]) == before


def test_legacy_replace_env_switch(migrated, monkeypatch):
    conn, ids, path, pdf = migrated
    monkeypatch.setenv(be.LEGACY_REPLACE_ENV, "1")
    assert _extractor(path).process_document_result(_doc(ids, pdf)).inserted == 2


def test_legacy_replace_refuses_runs_that_fail_validation(migrated):
    conn, ids, path, pdf = migrated
    before = _charges(conn, ids["version_id"])
    bad = [_charge("energy_block", "Energy Charge", 12.0, "$/kWh")]  # implausible magnitude
    result = _extractor(path, charges=bad).process_document_result(_doc(ids, pdf), legacy_replace=True)
    assert result.inserted == 0 and "refused" in (result.error or "")
    assert _charges(conn, ids["version_id"]) == before


def test_review_findings_block_promotion_until_acknowledged(migrated):
    conn, ids, path, pdf = migrated
    selection = {"parse_warnings": ["RES: table parse rows ['PIM'] not added: printed TOTAL cannot vouch for them; needs review"]}
    result = _extractor(path, selection=selection).process_document_result(_doc(ids, pdf))
    assert result.needs_review
    with pytest.raises(pv.ProvenanceError, match="need review"):
        pv.promote(conn, result.run_id, promoted_by="alice", reason="r")
    pv.promote(conn, result.run_id, promoted_by="alice", reason="reviewed", acknowledge_review=True)
    assert pv.run_review_status(conn, result.run_id) == "active"


def test_degraded_structured_input_is_review(migrated):
    conn, ids, path, pdf = migrated
    selection = {"structured_input": {"mode": "hybrid", "degraded": True, "degraded_reason": "docling failed on pages 3-9"}}
    result = _extractor(path, selection=selection).process_document_result(_doc(ids, pdf))
    assert result.needs_review
    assert any(i["code"] == "degraded_structured_input" for i in result.issues)


def test_bulk_extraction_refuses_database_without_provenance_schema(db):
    conn, ids, path, pdf = db
    with pytest.raises(pv.ProvenanceError, match="provenance migrate"):
        _extractor(path).process_document_result(_doc(ids, pdf))
    with pytest.raises(pv.ProvenanceError):
        BulkExtractor(str(path)).run_extraction(limit=0)


def test_run_extraction_reports_candidate_runs(migrated, monkeypatch):
    conn, ids, path, pdf = migrated
    ext = _extractor(path)
    monkeypatch.setattr(ext, "get_documents_needing_extraction", lambda family_key=None: [_doc(ids, pdf)])
    monkeypatch.setattr(ext, "count_documents_missing_versions", lambda family_key=None: 0)
    before = _charges(conn, ids["version_id"])
    out = ext.run_extraction(max_workers=1)
    assert out["persistence"] == "candidate" and out["total_charges_inserted"] == 0
    assert out["total_charges_extracted"] == 2 and len(out["candidate_runs"]) == 1
    assert _charges(conn, ids["version_id"]) == before


def test_bookkeeping_still_recorded(migrated, monkeypatch):
    conn, ids, path, pdf = migrated
    ext = _extractor(path)
    calls = []
    ext._record_bookkeeping = lambda extraction: calls.append(extraction.status)
    ext.process_document_result(_doc(ids, pdf))
    assert calls == ["parsed"]


def test_review_cli_lists_inspects_and_gates_promotion(migrated):
    import json

    from typer.testing import CliRunner

    from duke_rates.cli_commands.provenance import provenance_app

    conn, ids, path, pdf = migrated
    selection = {"parse_warnings": ["RES: rows not added: printed TOTAL cannot vouch for them; needs review"]}
    run_id = _extractor(path, selection=selection).process_document_result(_doc(ids, pdf)).run_id
    runner = CliRunner()

    listed = runner.invoke(provenance_app, ["candidates", "--db", str(path), "--needs-review"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert rows[0]["run_id"] == run_id and rows[0]["review_state"] == "candidate"
    assert rows[0]["structured_mode"] == "hybrid" and rows[0]["review_findings"] == 1

    issues = json.loads(runner.invoke(provenance_app, ["issues", "--db", str(path), "--run-id", str(run_id)]).output)
    assert issues["needs_review"][0]["code"] == "parser_conflict"

    evidence = json.loads(runner.invoke(provenance_app, ["evidence", "--db", str(path), "--run-id", str(run_id), "--label", "Energy"]).output)
    assert evidence[0]["source_pdf"]["status"] == "available" and evidence[0]["page"]["status"] == "unavailable"

    refused = runner.invoke(provenance_app, ["promote", "--db", str(path), "--run-id", str(run_id), "--by", "a", "--reason", "r"])
    assert refused.exit_code != 0
    ok = runner.invoke(provenance_app, ["promote", "--db", str(path), "--run-id", str(run_id), "--by", "a", "--reason", "reviewed", "--acknowledge-review"])
    assert ok.exit_code == 0, ok.output
