"""Phase 2E D2: reviewed document promotion (propose -> approve -> apply -> rollback).

Uses the D1 ``archive`` fixture: ``hd_index`` is bound to the bundle's index
page (p.1), as hd 2275 / hd 7265 are in production, and the sheet it should
point at is p.2 (Second Revised Leaf 600). The scratch DB is the fixture's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc import document_promotion as dp
from test_d1_clue_ledger import _insert, _sheet, archive  # noqa: F401  (fixture reuse)
from test_d2_redline_and_dates import _redraw_with_markup


def _charges(conn, version_id: int, n: int = 3) -> None:
    for i in range(n):
        _insert(conn, "tariff_charges", version_id=version_id, family_key="nc-progress-leaf-600",
                charge_type="rider", charge_label=f"legacy {i}", rate_value=0.1 * i, rate_unit="$/kWh",
                created_at="t")
    conn.commit()


def _charge_rows(conn, version_id):
    return [tuple(r) for r in conn.execute("SELECT * FROM tariff_charges WHERE version_id = ? ORDER BY id",
                                           (version_id,))]


def test_staged_rebind_keeps_charges_snapshots_and_rolls_back(archive):  # noqa: F811
    conn, ids = archive
    _charges(conn, ids.v_index)
    before_charges = _charge_rows(conn, ids.v_index)
    before_hd = tuple(conn.execute("SELECT * FROM historical_documents WHERE id = ?", (ids.hd_index,)).fetchone())

    out = dp.propose(conn, "rebind_page_range", ids.hd_index, params={"start_page": 2, "end_page": 2},
                     proposed_by="proposer", evidence={"binding_check": "index page"})
    assert out["state"] == "proposed", out
    checks = json.loads(conn.execute("SELECT checks_json FROM promotion_proposals WHERE id = ?",
                                     (out["proposal_id"],)).fetchone()[0])
    assert checks["first_page_prints_family_leaf"]["ok"] and checks["pages_not_redline"]["ok"]
    assert checks["existing_charges_preserved"]["detail"]["legacy_rebind_would_delete"] == 3
    assert checks["no_other_document_bound_to_pages"]["ok"] is False  # hd_rev2 also covers p.2: reported
    pid = out["proposal_id"]

    with pytest.raises(dp.PromotionError):
        dp.apply(conn, pid, applied_by="x")                     # not approved
    with pytest.raises(dp.PromotionError):
        dp.approve(conn, pid, approved_by="proposer", note="self")  # proposer cannot approve
    dp.approve(conn, pid, approved_by="reviewer", note="pages 2 read; Second Revised Leaf 600")
    result = dp.apply(conn, pid, applied_by="operator")
    assert result["charges_deleted"] == 0 and result["charges_kept"] == 3
    hd = conn.execute("SELECT start_page, end_page, raw_text_path FROM historical_documents WHERE id = ?",
                      (ids.hd_index,)).fetchone()
    assert tuple(hd) == (2, 2, None)
    assert _charge_rows(conn, ids.v_index) == before_charges        # nothing destroyed
    snaps = dict(conn.execute("SELECT phase, COUNT(*) FROM promotion_snapshots WHERE proposal_id = ? GROUP BY 1",
                              (pid,)).fetchall())
    assert snaps == {"before": 1 + 1 + 3, "after": 1 + 1 + 3}       # document, version, charges

    restored = dp.rollback(conn, pid, by="operator")
    assert restored == {"restored": True, "charges_unchanged": True}
    assert tuple(conn.execute("SELECT * FROM historical_documents WHERE id = ?", (ids.hd_index,)).fetchone()) == before_hd
    with pytest.raises(dp.PromotionError):
        dp.rollback(conn, pid, by="operator")                    # only once


def test_rollback_refuses_after_a_later_charge_change(archive):  # noqa: F811
    conn, ids = archive
    _charges(conn, ids.v_index)
    pid = dp.propose(conn, "rebind_page_range", ids.hd_index, params={"start_page": 2, "end_page": 2},
                     proposed_by="p")["proposal_id"]
    dp.approve(conn, pid, approved_by="r", note="ok")
    dp.apply(conn, pid, applied_by="o")
    conn.execute("DELETE FROM tariff_charges WHERE version_id = ? AND charge_label = 'legacy 0'", (ids.v_index,))
    conn.commit()
    with pytest.raises(dp.PromotionError, match="provenance rollback"):
        dp.rollback(conn, pid, by="o")


def test_rebind_to_wrong_leaf_or_redline_is_blocked(archive, tmp_path: Path):  # noqa: F811
    conn, ids = archive
    wrong = dp.propose(conn, "rebind_page_range", ids.hd_index, params={"start_page": 3, "end_page": 3},
                       proposed_by="p")
    assert wrong["state"] == "blocked" and "first_page_prints_family_leaf" in wrong["blocking_failures"]
    with pytest.raises(dp.PromotionError):
        dp.approve(conn, wrong["proposal_id"], approved_by="r", note="n")

    red = tmp_path / "redline.pdf"
    sha = _redraw_with_markup(red, [_sheet("Second Revised", "First Revised", effective="October 1, 2024")])
    hd_red = _insert(conn, "historical_documents", family_key="nc-progress-leaf-600", local_path=str(red),
                     start_page=1, end_page=1, content_hash=sha, title="t", state="NC", company="progress",
                     category="rider", kind="pdf", canonical_url="r", archived_url="r", snapshot_timestamp="t",
                     retrieved_at="t", status="approved")
    v_red = _insert(conn, "tariff_versions", family_key="nc-progress-leaf-600", historical_document_id=hd_red,
                    effective_start="2024-10-01", source_type="t", created_at="t", status="approved")
    conn.commit()
    out = dp.propose(conn, "rebind_page_range", hd_red, params={"start_page": 1, "end_page": 1}, proposed_by="p")
    assert out["state"] == "blocked" and "pages_not_redline" in out["blocking_failures"]
    # A charge promotion for the redline version must be acknowledged.
    assert dp.redline_promotion_block(conn, v_red)["reason"] == "redline_markup_on_bound_pages"
    assert dp.redline_promotion_block(conn, ids.v_rev1) is None

    # The redline is recorded as the duplicate of the clean sheet instead.
    dup = dp.propose(conn, "mark_redline_duplicate", hd_red, proposed_by="p",
                     params={"start_page": 1, "end_page": 1, "duplicate_of_historical_document_id": ids.hd_rev2})
    assert dup["state"] == "proposed", dup
    dp.approve(conn, dup["proposal_id"], approved_by="r", note="same Second Revised sheet, redline copy")
    res = dp.apply(conn, dup["proposal_id"], applied_by="o")
    assert res["legacy_rows_changed"] == 0
    assert dp.redline_promotion_block(conn, v_red)["reason"] == "redline_duplicate_assertion"
    again = dp.propose(conn, "rebind_page_range", hd_red, params={"start_page": 1, "end_page": 1}, proposed_by="p")
    assert "no_redline_duplicate_assertion" in again["blocking_failures"]
    dp.rollback(conn, dup["proposal_id"], by="o")
    assert conn.execute("SELECT retracted_at IS NOT NULL FROM document_assertions").fetchone()[0] == 1


def test_apply_rechecks_and_blocks_when_data_changed(archive):  # noqa: F811
    conn, ids = archive
    pid = dp.propose(conn, "rebind_page_range", ids.hd_index, params={"start_page": 2, "end_page": 2},
                     proposed_by="p")["proposal_id"]
    dp.approve(conn, pid, approved_by="r", note="ok")
    conn.execute("UPDATE historical_documents SET family_key = 'nc-progress-leaf-601' WHERE id = ?", (ids.hd_index,))
    conn.commit()
    with pytest.raises(dp.PromotionError, match="apply time"):
        dp.apply(conn, pid, applied_by="o")
    assert conn.execute("SELECT state FROM promotion_proposals WHERE id = ?", (pid,)).fetchone()[0] == "blocked"


def test_legacy_rebind_command_refuses_to_delete_charges_silently(archive, monkeypatch):  # noqa: F811
    from duke_rates.cli import app
    from duke_rates.cli_commands import lineage

    conn, ids = archive
    _charges(conn, ids.v_index)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    repo = Repository(db_path)
    calls = []
    # The repository method itself is unchanged (it deletes); record that it is reached only with the flag.
    monkeypatch.setattr(repo, "rebind_historical_page_range", lambda *a, **k: calls.append((a, k)) or None)
    monkeypatch.setattr(lineage, "_bootstrap", lambda: (None, repo))
    runner = CliRunner()
    refused = runner.invoke(app, ["lineage", "rebind-historical-page-range", str(ids.hd_index), "--start-page", "2"])
    assert refused.exit_code == 1 and "would delete 3 tariff_charges" in refused.output
    assert calls == [] and len(_charge_rows(conn, ids.v_index)) == 3
    done = runner.invoke(app, ["lineage", "rebind-historical-page-range", str(ids.hd_index), "--start-page", "2",
                               "--allow-charge-deletion"])
    assert "Deleting 3 tariff_charges" in done.output and len(calls) == 1
