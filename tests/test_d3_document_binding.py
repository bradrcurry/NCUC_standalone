"""Phase 2E D3: reviewed binding of archived, unbound sheets (``bind_unbound_document``).

Uses the D1 ``archive`` fixture. ``loose.pdf`` p.2 is an unbound clean
Third Revised Leaf 600 (effective October 1, 2025) that no version uses.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from types import SimpleNamespace

import pytest

from duke_rates.billing.tariff_engine import select_version_with_coverage
from duke_rates.historical.ncuc import document_binding as bind
from duke_rates.historical.ncuc import document_promotion as dp
from test_d1_clue_ledger import _insert, _pdf, _sheet, archive  # noqa: F401  (fixture reuse)
from test_d2_redline_and_dates import _redraw_with_markup

FAMILY = "nc-progress-leaf-600"
REV3 = "NC:leaf-600:rev-3"


def _params(path, start, end, **extra) -> dict:
    return {"source_pdf": str(path), "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "start_page": start, "end_page": end, "family_key": FAMILY, "expected_revision_value": REV3} | extra


def _table_sha(conn, table: str) -> str:
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
        h.update(json.dumps(tuple(row), default=str).encode())
    return h.hexdigest()


def _state(conn) -> dict:
    return {t: _table_sha(conn, t) for t in ("tariff_versions", "historical_documents", "tariff_charges")}


def _approve_apply(conn, pid):
    dp.approve(conn, pid, approved_by="reviewer", note="page 2 read: clean Third Revised Leaf 600")
    return dp.apply(conn, pid, applied_by="operator")


def test_create_version_is_not_billable_keeps_charges_and_rolls_back(archive):  # noqa: F811
    conn, ids = archive
    before = _state(conn)
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="proposer",
                     params=_params(ids.paths.loose, 2, 2))
    assert out["state"] == "proposed", out["blocking_failures"]
    assert out["plan"]["mode"] == "create_version" and out["plan"]["effective_start"] == "2025-10-01"
    with pytest.raises(dp.PromotionError):
        dp.apply(conn, out["proposal_id"], applied_by="operator")        # not approved
    with pytest.raises(dp.PromotionError):
        dp.approve(conn, out["proposal_id"], approved_by="proposer", note="self")
    result = _approve_apply(conn, out["proposal_id"])
    assert result["created_version"] and result["charges_created"] == 0 and result["charges_deleted"] == 0
    assert result["post_apply_validation"]["ok"]
    v = conn.execute("SELECT * FROM tariff_versions WHERE id = ?", (result["tariff_version_id"],)).fetchone()
    assert (v["status"], v["effective_start"], v["revision_label"]) == (
        bind.NEW_VERSION_STATUS, "2025-10-01", "NC Third Revised Leaf No. 600")
    hd = conn.execute("SELECT * FROM historical_documents WHERE id = ?", (result["historical_document_id"],)).fetchone()
    assert (hd["start_page"], hd["end_page"], hd["content_hash"]) == (2, 2, _params(ids.paths.loose, 2, 2)["file_sha256"])
    # Billing never selects a version without reviewed charges.
    versions = [SimpleNamespace(**dict(r)) for r in conn.execute(
        "SELECT * FROM tariff_versions WHERE family_key = ? ORDER BY effective_start", (FAMILY,))]
    chosen, _ = select_version_with_coverage(versions, datetime.date(2025, 11, 1))
    assert chosen.id != result["tariff_version_id"]
    # The same pages cannot be bound twice.
    again = dp.propose(conn, "bind_unbound_document", None, proposed_by="p", params=_params(ids.paths.loose, 2, 2))
    assert "pages_not_already_bound_to_family" in again["blocking_failures"]
    assert dp.rollback(conn, out["proposal_id"], by="operator")["restored"]
    assert _state(conn) == before


@pytest.mark.parametrize("pages,extra,failure", [
    ((1, 1), {}, "first_page_prints_family_leaf"),                                 # the cover letter
    ((2, 2), {"expected_revision_value": "NC:leaf-600:rev-5"}, "printed_revision_is_expected"),
    ((2, 2), {"file_sha256": "0" * 64}, "file_is_the_reviewed_file"),
])
def test_identity_failures_block(archive, pages, extra, failure):  # noqa: F811
    conn, ids = archive
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="p",
                     params=_params(ids.paths.loose, *pages, **extra))
    assert out["state"] == "blocked" and failure in out["blocking_failures"]


def test_other_utility_sheet_blocks(archive):  # noqa: F811
    conn, ids = archive
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="p", params=_params(ids.paths.other, 1, 1))
    assert "utility_matches_family" in out["blocking_failures"]


def test_existing_sourced_version_blocks_instead_of_creating_a_duplicate(archive):  # noqa: F811
    conn, ids = archive
    # rev1.pdf is bound to v_rev1; a second copy of the same sheet elsewhere must not create a version.
    copy = ids.paths.loose.parent / "rev1_copy.pdf"
    _pdf(copy, [_sheet("First Revised", "Original", effective="October 1, 2023")])
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="p",
                     params=_params(copy, 1, 1, expected_revision_value="NC:leaf-600:rev-1"))
    assert out["state"] == "blocked" and out["plan"]["mode"] is None
    assert out["plan"]["target_version_id"] == ids.v_rev1


def test_attach_to_a_placeholder_version_and_roll_back(archive):  # noqa: F811
    conn, ids = archive
    placeholder = _insert(conn, "tariff_versions", family_key=FAMILY, historical_document_id=None,
                          effective_start="2025-10-01", revision_label="NC Third Revised Leaf No. 600",
                          source_type="backfill_missing_version", created_at="t", status="pending_document")
    conn.commit()
    before = _state(conn)
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="p", params=_params(ids.paths.loose, 2, 2))
    assert out["plan"] | {} and out["plan"]["mode"] == "attach" and out["plan"]["target_version_id"] == placeholder
    result = _approve_apply(conn, out["proposal_id"])
    assert not result["created_version"] and result["previous_historical_document_id"] is None
    row = conn.execute("SELECT historical_document_id, status FROM tariff_versions WHERE id = ?", (placeholder,)).fetchone()
    assert row[0] == result["historical_document_id"] and row[1] == "pending_document"  # status is not promoted
    dp.rollback(conn, out["proposal_id"], by="operator")
    assert _state(conn) == before


def test_replace_a_redline_source_with_the_clean_sheet(archive, tmp_path):  # noqa: F811
    conn, ids = archive
    red = tmp_path / "red.pdf"
    sha = _redraw_with_markup(red, [_sheet("Third Revised", "Second Revised", effective="October 1, 2025")])
    hd_red = _insert(conn, "historical_documents", family_key=FAMILY, local_path=str(red), start_page=1, end_page=1,
                     content_hash=sha, title="t", state="NC", company="progress", category="rider", kind="pdf",
                     canonical_url="r", archived_url="r", snapshot_timestamp="t", retrieved_at="t", status="approved")
    v_red = _insert(conn, "tariff_versions", family_key=FAMILY, historical_document_id=hd_red,
                    effective_start="2025-10-01", source_type="t", created_at="t", status="approved")
    for i in range(2):
        _insert(conn, "tariff_charges", version_id=v_red, family_key=FAMILY, charge_type="rider",
                charge_label=f"legacy {i}", rate_value=0.1, rate_unit="$/kWh", created_at="t")
    conn.commit()
    charges = _table_sha(conn, "tariff_charges")
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="p", params=_params(ids.paths.loose, 2, 2))
    assert out["plan"]["mode"] == "replace_redline_source" and out["plan"]["target_version_id"] == v_red
    result = _approve_apply(conn, out["proposal_id"])
    assert result["previous_historical_document_id"] == hd_red and _table_sha(conn, "tariff_charges") == charges
    assert conn.execute("SELECT COUNT(*) FROM historical_documents WHERE id = ?", (hd_red,)).fetchone()[0] == 1
    dp.rollback(conn, out["proposal_id"], by="operator")
    assert conn.execute("SELECT historical_document_id FROM tariff_versions WHERE id = ?", (v_red,)).fetchone()[0] == hd_red


def test_file_changed_after_approval_blocks_apply(archive):  # noqa: F811
    conn, ids = archive
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="p", params=_params(ids.paths.loose, 2, 2))
    dp.approve(conn, out["proposal_id"], approved_by="r", note="ok")
    _pdf(ids.paths.loose, ["Cover letter", _sheet("Third Revised", "Second Revised", effective="November 1, 2025")])
    with pytest.raises(dp.PromotionError, match="apply time"):
        dp.apply(conn, out["proposal_id"], applied_by="o")


def test_gap_resolves_only_after_a_validated_apply(archive):  # noqa: F811
    conn, ids = archive
    gap = _insert(conn, "lineage_gaps_typed", gap_uid="g-1", gap_type="missing_intermediate_revision",
                  evidence_basis="explicit", family_key=FAMILY, status="verified", active=1, created_at="t",
                  updated_at="t-before", builder_version="t")
    conn.commit()
    out = dp.propose(conn, "bind_unbound_document", None, proposed_by="p",
                     params=_params(ids.paths.loose, 2, 2, gap_id=gap))
    assert conn.execute("SELECT status FROM lineage_gaps_typed WHERE id = ?", (gap,)).fetchone()[0] == "verified"
    _approve_apply(conn, out["proposal_id"])
    assert conn.execute("SELECT status FROM lineage_gaps_typed WHERE id = ?", (gap,)).fetchone()[0] == "resolved"
    dp.rollback(conn, out["proposal_id"], by="o")
    assert tuple(conn.execute("SELECT status, updated_at FROM lineage_gaps_typed WHERE id = ?", (gap,)).fetchone()) \
        == ("verified", "t-before")
