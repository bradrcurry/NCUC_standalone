"""Phase 2E D3: what the supervised live pilot showed, as regression tests on the captured pages.

``tests/fixtures/d3_portal/`` holds two pages captured during the pilot
(hidden ASP.NET state and scripts blanked):

* ``param_search_docket_empty_e7_1026.html``: DocumentParamSearch for
  E-7 Sub 1026 with the ORDER filter. The portal prints "** NO RESULTS FOUND **".
* ``docket_inventory_e7_1026_page1.html``: the same docket's DocketDetails
  document inventory, page 1 of 6: 100 rows, 33 of them orders.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import fitz

from duke_rates.db.provenance_schema import ensure_provenance_schema
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc import local_gap_resolution as lgr
from duke_rates.historical.ncuc import remote_discovery as rd
from duke_rates.historical.ncuc.document_param_search import explicit_empty_result, items_reported
from duke_rates.historical.ncuc.search_ledger import SearchLedger
from test_d2_portal_search import FakePage, _searcher

FIX = Path(__file__).parent / "fixtures" / "d3_portal"
EMPTY = (FIX / "param_search_docket_empty_e7_1026.html").read_text(encoding="utf-8")
INVENTORY = (FIX / "docket_inventory_e7_1026_page1.html").read_text(encoding="utf-8")


def test_real_docket_scoped_empty_page_is_not_negative_evidence():
    assert explicit_empty_result(EMPTY) and items_reported(EMPTY) is None
    out = _searcher().search_with_outcome(FakePage({}, after_submit=EMPTY), docket_number="E-7 Sub 1026",
                                          company_name="Duke Energy Carolinas", filing_types=["ORDER"])
    assert out.stop_reason == "docket_scoped_empty" and not out.complete
    assert out.ledger_outcome == "incomplete"          # retryable; never a completed no_results
    # Without a docket the portal's empty statement still is negative evidence.
    out = _searcher().search_with_outcome(FakePage({}, after_submit=EMPTY), filing_types=["ORDER"])
    assert out.stop_reason == "empty_result" and out.ledger_outcome == "no_results"


def test_inventory_grid_is_parsed_from_its_labelled_cells():
    rows = rd.parse_docket_inventory(INVENTORY)
    assert len(rows) == 100 and all(r["description"] and r["date_filed"] for r in rows)
    assert sum(r["doc_type"] == "Order" for r in rows) == 33
    first = rows[0]
    assert first["doc_type"] == "Filing" and first["description"].startswith("DEC Compliance Tariffs for BPM")
    assert first["view_file_urls"] and "PSCDocumentDetails" in first["document_url"]


def test_inventory_first_page_of_six_is_incomplete():
    out = rd.docket_inventory_outcome(INVENTORY, [], docket_label="E-7 Sub 1026")
    assert (out.stop_reason, out.pages_total, len(out.results)) == ("inventory_first_page_only", 6, 100)
    assert out.ledger_outcome == "incomplete"
    orders = [r for r in out.results if rd.is_order_row(r)]
    assert len(orders) == 33 and all(r.document_id for r in orders)
    assert {r.description for r in orders} >= {"Order Approving EDPR Rider", "Errata Order"}


def test_order_rows_exclude_filings_about_orders():
    row = SimpleNamespace(doc_type="Filing", description="DEP's Compliance Tariffs Pursuant to September 15, 2014 Order")
    assert not rd.is_order_row(row)
    assert rd.is_order_row(SimpleNamespace(doc_type="", description="Errata Order"))
    assert rd.is_order_row(SimpleNamespace(doc_type="Order", description="Order Approving Riders"))


def test_docket_inventory_search_resolves_exact_docket_only():
    page = SimpleNamespace(content=lambda: INVENTORY, title=lambda: "Docket")
    calls = []

    def resolve(_page, docket):
        return [{"docket_number": "E-7 Sub 1026", "docket_id": "a" * 36, "match_type": "exact"},
                {"docket_number": "E-7 Sub 10261", "docket_id": "b" * 36, "match_type": "partial"}]

    out = rd.docket_inventory_search(page, {"method": "docket_inventory", "docket_number": "E-7 Sub 1026"},
                                     resolve=resolve, list_documents=lambda p, i: calls.append(i) or [])
    assert calls == ["a" * 36] and len(out.results) == 100
    none = rd.docket_inventory_search(page, {"docket_number": "E-7 Sub 9999"}, resolve=lambda p, d: [],
                                      list_documents=lambda p, i: [])
    assert none.stop_reason == "docket_not_resolved" and none.ledger_outcome == "error"


def test_inventory_query_is_one_per_docket_and_ledgered(tmp_path):
    conn = connect(tmp_path / "s.db")
    ensure_provenance_schema(conn)
    target = {"gap_id": None, "tariff_version_id": 1, "docket": "E-7 Sub 1026",
              "order_window": ["06/01/2017", "06/30/2017"]}
    plan = {"queries": [{"channel": "portal_orders", "params": {"method": "docket_inventory",
                                                                 "docket_number": "E-7 Sub 1026"},
                         "params_hash": "h", "targets": [target]}]}
    outcome = rd.docket_inventory_outcome(INVENTORY, [], docket_label="E-7 Sub 1026")
    stats = rd.execute_plan(conn, plan, created_by="t", docket_inventory=lambda p: outcome)
    assert stats["inventory_rows"] == 100 and stats["order_rows"] == 33 and stats["candidates_listed"] == 33
    assert stats["outcome_incomplete"] == 1
    row = conn.execute("SELECT outcome, stop_reason, pages_total FROM search_attempts").fetchone()
    assert tuple(row) == ("incomplete", "inventory_first_page_only", 6)
    # The order window ranks rows; it does not filter them.
    in_window = conn.execute("SELECT COUNT(*) FROM remote_document_candidates "
                             "WHERE json_extract(score_json, '$.date_in_window') = 1").fetchone()[0]
    assert in_window == 2   # the two 6/20/2017 orders
    # Incomplete: the planner will offer it again.
    assert SearchLedger(conn, created_by="t").prior_completed("portal_orders", plan["queries"][0]["params"]) is None


def test_local_order_search_reads_the_dockets_printed_in_the_caption(tmp_path):
    conn = connect(tmp_path / "s.db")
    ensure_provenance_schema(conn)
    pdf = tmp_path / "order.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "STATE OF NORTH CAROLINA\nUTILITIES COMMISSION\nDOCKET NO. E-7, SUB 487\n"
                                         "DOCKET NO. E-7, SUB 1026\nDOCKET NO. E-7, SUB 1214\nORDER APPROVING EDPR RIDER")
    doc.save(pdf)
    doc.close()
    conn.execute("INSERT INTO ncuc_discovery_records (docket_number, sub_number, filing_classification, local_path, "
                 "fetch_status, created_at) VALUES ('E-7, Sub 1214', '1214', 'order', ?, 'fetched', 't')", (str(pdf),))
    conn.commit()
    index = lgr.order_caption_index(conn)
    assert set(index) == {"E-7 Sub 487", "E-7 Sub 1026", "E-7 Sub 1214"}
    assert index["E-7 Sub 1026"] == [1]
