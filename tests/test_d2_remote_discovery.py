"""Phase 2E D2: docket-scoped order discovery and network gap resolution (no network).

Builds on the D1 ``archive`` fixture. The "loose" PDF that holds the missing
Third Revised sheet is removed from the archive first, so the local search
completes with nothing found and the gap becomes eligible for a network
search. Portal and public searches are fakes that return
``DocParamSearchOutcome`` objects.
"""

from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

from duke_rates.historical.ncuc import clue_ledger, clue_lineage, local_gap_resolution
from duke_rates.historical.ncuc import remote_discovery as rd
from duke_rates.historical.ncuc.document_param_search import DocParamSearchOutcome, DocParamSearchResult
from test_d1_clue_ledger import _artifacts, _insert, _pdf, archive  # noqa: F401  (fixture reuse)


def _row(doc_id: str, desc: str, docket="E-2 Sub 1300", filed="08/18/2023", doc_type="ORDER") -> DocParamSearchResult:
    return DocParamSearchResult(
        description=desc, doc_type=doc_type, date_filed=filed, docket_number=docket, docket_id="d",
        company_name="Duke Energy Progress",
        document_detail_url=f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}",
        document_id=doc_id, view_file_urls=[f"https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id={doc_id}"],
    )


def _outcome(rows, stop="last_page") -> DocParamSearchOutcome:
    return DocParamSearchOutcome(results=rows, stop_reason=stop, pages_reached=1 if rows else 0,
                                 items_reported=len(rows))


@pytest.fixture()
def unresolved(archive):  # noqa: F811
    conn, ids = archive
    conn.execute("DELETE FROM ncuc_page_artifacts WHERE source_pdf IN (?, ?)",
                 (str(ids.paths.loose), str(ids.paths.other)))
    conn.commit()
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    return conn, ids


def _rev3_gap(conn, ids):
    return conn.execute("SELECT * FROM lineage_gaps_typed WHERE gap_type = 'missing_predecessor_revision' "
                        "AND anchor_version_id = ?", (ids.v_rev4,)).fetchone()


def test_local_first_gaps_without_completed_local_search_are_not_planned(unresolved):
    conn, ids = unresolved
    gap = _rev3_gap(conn, ids)
    # Pretend the gap was marked not found without any local search.
    conn.execute("UPDATE lineage_gaps_typed SET status = 'searched_not_found' WHERE id = ?", (gap["id"],))
    conn.commit()
    plan = rd.plan_network_searches(conn, include_blank_order_clues=False)
    assert plan["skipped"].get("local_search_not_complete", 0) >= 1
    assert all(gap["id"] not in [t.get("gap_id") for t in q["targets"]] for q in plan["queries"])


def test_plan_after_local_search_is_docket_scoped_and_deduplicated(unresolved):
    conn, ids = unresolved
    local_gap_resolution.resolve_gaps_locally(conn, created_by="t")
    gap = _rev3_gap(conn, ids)
    assert gap["status"] == "searched_not_found"
    plan = rd.plan_network_searches(conn)
    by_channel = {}
    for q in plan["queries"]:
        by_channel.setdefault(q["channel"], []).append(q)
    params = [q for q in by_channel["portal_params"] if any(t.get("gap_id") == gap["id"] for t in q["targets"])]
    assert params and params[0]["params"]["docket_number"] == "E-2 Sub 1300"
    assert params[0]["params"]["company_name"] == "Duke Energy Progress"
    zoom = [q for q in by_channel["zoom"] if any(t.get("gap_id") == gap["id"] for t in q["targets"])]
    assert zoom and "NC Third Revised Leaf No. 600" in zoom[0]["params"]["query_text"]
    # Every planned query is distinct by hash; explicit gaps are ordered first.
    assert len({q["params_hash"] for q in plan["queries"]}) == len(plan["queries"])
    ranks = [tuple(q["priority"])[0] for q in plan["queries"]]
    assert ranks == sorted(ranks)


def test_execute_records_provenance_stops_on_block_and_never_resolves(unresolved, tmp_path: Path):
    conn, ids = unresolved
    local_gap_resolution.resolve_gaps_locally(conn, created_by="t")
    gap = _rev3_gap(conn, ids)
    plan = rd.plan_network_searches(conn, include_blank_order_clues=False)
    calls = []

    def blocked(params):
        calls.append(params)
        return DocParamSearchOutcome(results=[], stop_reason="blocked_initial", error="title='Just a moment...'")

    stats = rd.execute_plan(conn, plan, created_by="t", portal_search=blocked, public_search=lambda p: [],
                            channels=("portal_params",), max_queries=5)
    assert stats["stopped_on_block"] == 1 and len(calls) == 1      # no retry loop against a challenge
    assert tuple(conn.execute("SELECT outcome, stop_reason FROM search_attempts WHERE channel = 'portal_params'"
                        ).fetchone()) == ("blocked", "blocked_initial")

    rows = [_row("doc-1", "DEP Compliance Tariffs - NC Third Revised Leaf No. 600", filed="09/15/2025",
                 doc_type="Filing")]
    stats = rd.execute_plan(conn, plan, created_by="t", portal_search=lambda p: _outcome(rows),
                            public_search=lambda p: [], channels=("portal_params",), max_queries=5)
    assert stats["queries_run"] >= 1 and stats.get("repeated_searches_avoided", 0) == 0  # blocked is retried
    cand = conn.execute("SELECT * FROM remote_document_candidates WHERE gap_id = ?", (gap["id"],)).fetchone()
    assert cand["status"] == "listed" and cand["kind"] == "tariff_document"
    assert cand["search_attempt_id"] and len(cand["params_hash"]) == 64 and cand["portal_document_id"] == "doc-1"
    assert json.loads(cand["view_file_urls_json"]) and cand["channel"] == "portal_params"
    score = json.loads(cand["score_json"])
    assert score["docket_match"] is True and score["mentions_leaf"] is True
    gap = _rev3_gap(conn, ids)
    assert gap["status"] == "candidate_found" and "portal_params" in json.loads(gap["searched_channels_json"])
    assert conn.execute("SELECT COUNT(*) FROM lineage_gaps_typed WHERE status IN ('verified','resolved')").fetchone()[0] == 0
    again = rd.execute_plan(conn, plan, created_by="t", portal_search=lambda p: pytest.fail("repeated"),
                            public_search=lambda p: [], channels=("portal_params",), max_queries=5)
    assert again["repeated_searches_avoided"] >= 1


def test_incomplete_search_is_retryable(unresolved):
    conn, ids = unresolved
    local_gap_resolution.resolve_gaps_locally(conn, created_by="t")
    plan = rd.plan_network_searches(conn, include_blank_order_clues=False)
    rd.execute_plan(conn, plan, created_by="t", portal_search=lambda p: _outcome([_row("x", "y")], "stale_grid"),
                    channels=("portal_params",), max_queries=1)
    assert conn.execute("SELECT outcome FROM search_attempts WHERE channel='portal_params'").fetchone()[0] == "incomplete"
    stats = rd.execute_plan(conn, plan, created_by="t", portal_search=lambda p: _outcome([]),
                            channels=("portal_params",), max_queries=1)
    assert stats["queries_run"] == 1


def _add_blank_order_sheet(conn, tmp_path: Path) -> int:
    """A Fifth Revised sheet whose footer leaves the order date blank ("Order dated ____")."""
    text = ("Duke Energy Progress, LLC NC Fifth Revised Leaf No. 600\n"
            "(North Carolina Only) Superseding NC Fourth Revised Leaf No. 600\nSUMMARY OF RIDER ADJUSTMENTS\n"
            "Effective for service rendered on and after October 1, 2027\n"
            "NCUC Docket No. E-2, Sub 1350, Order dated ____________")
    path = tmp_path / "rev5.pdf"
    sha, _ = _pdf(path, [text])
    _artifacts(conn, path, [text], sha)
    hd = _insert(conn, "historical_documents", family_key="nc-progress-leaf-600", local_path=str(path), start_page=1,
                 end_page=1, content_hash=sha, title="t", state="NC", company="progress", category="rider",
                 kind="pdf", canonical_url=str(path), archived_url=str(path), snapshot_timestamp="t",
                 retrieved_at="t", status="approved")
    vid = _insert(conn, "tariff_versions", family_key="nc-progress-leaf-600", historical_document_id=hd,
                  effective_start="2027-10-01", source_type="t", created_at="t", status="approved")
    conn.commit()
    return vid


def test_order_candidate_lifecycle_requires_retrieval_and_named_review(unresolved, tmp_path: Path):
    conn, ids = unresolved
    _add_blank_order_sheet(conn, tmp_path)
    clue_ledger.build_clue_ledger(conn)
    clue_lineage.rebuild_lineage(conn)
    local_gap_resolution.resolve_gaps_locally(conn, created_by="t")
    order_gap = conn.execute("SELECT * FROM lineage_gaps_typed WHERE gap_type = 'missing_regulatory_order'").fetchone()
    assert order_gap["status"] == "searched_not_found" and order_gap["evidence_basis"] == "explicit"
    plan = rd.plan_network_searches(conn)
    # D3: orders come from the docket's inventory (DocumentParamSearch by docket was empty live).
    orders = [q for q in plan["queries"] if q["channel"] == "portal_orders"]
    assert orders and all(q["params"].get("method") == rd.INVENTORY_METHOD for q in orders)
    assert orders[0]["params"] == {"method": "docket_inventory", "docket_number": "E-2 Sub 1350"}
    # No printed order date: docket-only, never a window guessed from the effective date.
    assert orders[0]["targets"][0]["order_window"] is None and orders[0]["targets"][0]["window_basis"] == "docket_only"
    rows = [_row("ord-1", "Order Approving Compliance Tariffs", docket="E-2 Sub 1350"),
            _row("fil-1", "Compliance Tariffs Pursuant to Order", docket="E-2 Sub 1350", doc_type="TARIFF")]
    rd.execute_plan(conn, plan, created_by="t", docket_inventory=lambda p: _outcome(rows),
                    channels=("portal_orders",), max_queries=50)
    cands = conn.execute("SELECT * FROM remote_document_candidates WHERE kind = 'order'").fetchall()
    assert cands and all(c["status"] == "listed" for c in cands)  # a docket match is only a listing
    cid = cands[0]["id"]
    with pytest.raises(ValueError):
        rd.review_candidate(conn, cid, decision="linked", reviewer="r", version_id=ids.v_rev2)  # not read yet

    def download(url, path):
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "STATE OF NORTH CAROLINA UTILITIES COMMISSION\nDOCKET NO. E-2, SUB 1350\n"
                                             "ORDER APPROVING COMPLIANCE TARIFFS", fontsize=10)
        doc.save(path)
        doc.close()
        return path.stat().st_size

    stats = rd.retrieve_and_check(conn, [c["id"] for c in cands], download=download, dest_dir=tmp_path / "dl")
    assert stats["downloaded"] == 1 and stats.get("download_avoided_same_url", 0) == len(cands) - 1
    row = conn.execute("SELECT * FROM remote_document_candidates WHERE id = ?", (cid,)).fetchone()
    assert row["status"] == "needs_review" and len(row["file_sha256"]) == 64 and row["retrieved_at"]
    identity = json.loads(row["identity_json"])
    assert identity["checks"]["docket_printed"] is True and identity["checks"]["order_wording_printed"] is True
    with pytest.raises(ValueError):
        rd.review_candidate(conn, cid, decision="linked", reviewer=" ", version_id=ids.v_rev2)
    with pytest.raises(ValueError):
        rd.review_candidate(conn, cid, decision="linked", reviewer="r")  # no version named
    rd.review_candidate(conn, cid, decision="linked", reviewer="reviewer", version_id=ids.v_rev2, note="read p.1")
    row = conn.execute("SELECT status, linked_version_id, reviewed_by FROM remote_document_candidates WHERE id = ?",
                       (cid,)).fetchone()
    assert tuple(row) == ("linked", ids.v_rev2, "reviewer")


@pytest.mark.parametrize("docket,utility,company", [
    ("E-2 Sub 1300", "DEC", "Duke Energy Progress"),
    ("E-7 Sub 1026", None, "Duke Energy Carolinas"),
    ("M-100 Sub 138", "DEP", ""),      # commission-wide docket: no company filter
])
def test_company_for(docket, utility, company):
    assert rd.company_for(docket, utility) == company
