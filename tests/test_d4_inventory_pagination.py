"""Derived DocketDetails sequences; page 1 structure comes from the real D3 capture.

Pages 2+ here are explicitly synthetic, not evidence of live portal completeness.
"""

import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from duke_rates.db.provenance_schema import ensure_provenance_schema
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.docket_inventory import inventory_page_state, walk_docket_inventory
from duke_rates.historical.ncuc.search_ledger import SearchLedger

CAPTURE = (
    Path(__file__).parent / "fixtures/d3_portal/docket_inventory_e7_1026_page1.html"
).read_text(encoding="utf8")
TARGET = "ctl00$ContentPlaceHolder1$PortalPageControl1$ctl91$documentsGridView"


def derived_page(number, total=6, *, items=None, rows=3, window=None):
    soup = BeautifulSoup(CAPTURE, "lxml")
    grid = soup.find(id=re.compile(r"documentsGridView$"))
    data = grid.find_all("tr", class_=re.compile(r"^searchResults(?:Alternating)?Item$"))
    for row in data[rows:]:
        row.decompose()
    for index, row in enumerate(data[:rows]):
        link = row.find("a", id=re.compile(r"documentDetailsLink$"))
        link["href"] = re.sub(r"DocumentId=[a-f0-9-]{36}",
                              f"DocumentId={number:08d}-0000-0000-0000-{index:012d}", link["href"])
    footer = grid.find("tr", class_="searchResultsFooter")
    lo, hi = window or (1, total)
    pager = "".join(f"<span>{p}</span>" if p == number else
                    f"<a href=\"javascript:__doPostBack('{TARGET}','Page${p}')\">{p}</a>"
                    for p in range(lo, hi + 1))
    footer.clear()
    footer.append(BeautifulSoup(pager, "lxml"))
    html = str(soup)
    if items is not None:
        html += f"<div>Items Count: {items}</div>"
    return html


class InventoryPage:
    def __init__(self, pages):
        self.pages, self.html, self.postbacks = pages, pages[1], []

    def content(self):
        return self.html

    def title(self):
        return "Docket Details"

    def advance(self, href):
        number = int(re.search(r"Page\$(\d+)", href)[1])
        self.postbacks.append(number)
        self.html = self.pages[number]


def walk(page, **kwargs):
    return walk_docket_inventory(page, docket_label="E-7 Sub 1026", advance=page.advance,
                                 sleep=lambda _: None, transition_timeout=0.25, **kwargs)


def test_all_six_pages_and_stable_ids_are_deduplicated(tmp_path):
    page = InventoryPage({n: derived_page(n, items=18) for n in range(1, 7)})
    out = walk(page, checkpoint_dir=tmp_path)
    assert out.complete and out.ledger_outcome == "hits"
    assert (out.pages_reached, out.pages_total, len(out.results)) == (6, 6, 18)
    assert page.postbacks == [2, 3, 4, 5, 6]
    assert len({r.document_id for r in out.results}) == 18
    meta = json.loads((tmp_path / "inventory.json").read_text())
    assert meta["retrieved_pages"] == [1, 2, 3, 4, 5, 6] and meta["complete"]
    assert len(list(tmp_path.glob("page_*.html"))) == 6


def test_real_capture_is_one_of_six_and_not_empty_evidence():
    state = inventory_page_state(CAPTURE, docket_label="E-7 Sub 1026")
    assert state["valid"] and state["current"] == 1 and state["advertised"] == 6
    assert len(state["rows"]) == 100
    assert sum(r.doc_type == "Order" for r in state["rows"]) == 33


def test_stale_postback_is_bounded_and_retains_verified_rows():
    first = derived_page(1)
    page = InventoryPage({1: first, 2: first})
    out = walk(page)
    assert (out.stop_reason, out.pages_reached, len(out.results)) == ("stale_grid", 1, 3)
    assert out.ledger_outcome == "incomplete" and not out.complete
    assert page.postbacks == [2, 2]


def test_repeated_rows_with_a_changed_page_number_are_still_stale():
    first = derived_page(1, total=2)
    second = first.replace("<span>1</span>", "<span>2</span>")
    page = InventoryPage({1: first, 2: second})
    assert walk(page, retries=0).stop_reason == "stale_grid"


def test_blocked_pagination_stops_without_retry():
    page = InventoryPage({1: derived_page(1), 2: "<html>checking your browser</html>"})
    out = walk(page)
    assert out.stop_reason == "blocked_pagination" and out.ledger_outcome == "blocked"
    assert out.pages_reached == 1 and len(out.results) == 3
    assert page.postbacks == [2]


def test_page_cap_cannot_be_completed_negative_evidence():
    page = InventoryPage({n: derived_page(n) for n in range(1, 7)})
    out = walk(page, max_pages=2)
    assert (out.stop_reason, out.pages_reached, out.pages_total) == ("page_cap", 2, 6)
    assert out.ledger_outcome == "incomplete"


def test_item_count_mismatch_and_cross_page_duplicate():
    pages = {n: derived_page(n, total=2, items=6) for n in (1, 2)}
    pages[2] = pages[2].replace("00000002-0000-0000-0000-000000000000",
                               "00000001-0000-0000-0000-000000000000")
    out = walk(InventoryPage(pages))
    assert len(out.results) == 5 and out.stop_reason == "item_count_mismatch"
    assert out.pages_reached == 2 and not out.complete


def test_changed_portal_item_count_is_partial_even_if_rows_fit():
    out = walk(InventoryPage({1: derived_page(1, total=2, items=5),
                             2: derived_page(2, total=2, items=6)}))
    assert out.stop_reason == "item_count_mismatch"
    assert "changed" in out.error


def test_missing_sequential_link_is_partial():
    html = derived_page(1, total=3)
    soup = BeautifulSoup(html, "lxml")
    soup.find("a", href=re.compile(r"Page\$2")).decompose()
    out = walk(InventoryPage({1: str(soup)}))
    assert out.stop_reason == "pagination_error" and out.pages_total == 3


@pytest.mark.parametrize("html", ["<html>unexpected page</html>", "<html>No documents found</html>",
                                 derived_page(1).replace("DocumentId=", "Unknown=")])
def test_unknown_or_unidentified_inventory_is_never_verified_empty(html):
    out = walk(InventoryPage({1: html}))
    assert out.stop_reason == "unrecognized_page" and out.ledger_outcome == "error"
    assert not out.complete


def test_wrong_docket_identity_is_rejected():
    html = derived_page(1)
    soup = BeautifulSoup(html, "lxml")
    soup.find("input", id=re.compile(r"hdnDocketid$", re.I))["value"] = "other-docket"
    page = InventoryPage({1: str(soup)})
    out = walk_docket_inventory(page, docket_label="E-7 Sub 1026", docket_id="expected")
    assert out.stop_reason == "unrecognized_page" and "docket identity" in out.error


def test_interruption_keeps_checkpoint_and_restart_revalidates(tmp_path):
    page = InventoryPage({n: derived_page(n, total=2) for n in (1, 2)})

    def interrupt(href):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        walk_docket_inventory(page, docket_label="E-7 Sub 1026", advance=interrupt,
                              checkpoint_dir=tmp_path)
    meta = json.loads((tmp_path / "inventory.json").read_text())
    assert meta["retrieved_pages"] == [1] and not meta["complete"]
    # Recovery reads current pages rather than trusting previously saved HTML.
    (tmp_path / "page_1.html").write_text("stale checkpoint")
    assert walk(page, checkpoint_dir=tmp_path).complete
    assert page.postbacks == [2]
    assert "stale checkpoint" not in (tmp_path / "page_1.html").read_text()


def test_complete_inventory_is_ledgered_and_partial_stays_retryable(tmp_path):
    conn = connect(tmp_path / "ledger.db")
    ensure_provenance_schema(conn)
    ledger = SearchLedger(conn, created_by="d4-test")
    params = {"method": "docket_inventory", "docket_number": "E-7 Sub 1026"}
    partial = walk(InventoryPage({1: derived_page(1), 2: derived_page(1)}))
    with ledger.attempt("portal_orders", params, caller="test") as attempt:
        attempt.finish_outcome(partial)
    assert ledger.prior_completed("portal_orders", params) is None
    complete = walk(InventoryPage({1: derived_page(1, total=1)}))
    with ledger.attempt("portal_orders", params, caller="test") as attempt:
        attempt.finish_outcome(complete)
    row = ledger.prior_completed("portal_orders", params)
    assert row["pages_reached"] == 1 and row["result_count"] == 3
    assert len(json.loads(row["result_refs_json"])) == 3


def test_unrecognized_pager_layout_does_not_silently_complete():
    page = InventoryPage({1: derived_page(1).replace('class="searchResultsFooter"',
                                                   'class="changedPager"')})
    assert walk(page).stop_reason == "unrecognized_page"


def test_forward_ellipsis_is_only_a_lower_bound():
    soup = BeautifulSoup(derived_page(1, total=3), "lxml")
    soup.find("a", href=re.compile(r"Page\$3")).string = "..."
    out = walk(InventoryPage({1: str(soup)}), max_pages=1)
    assert out.stop_reason == "page_cap" and out.pages_total is None
    assert out.inventory_metadata["observed_page_bound"] == 3


def test_ledger_preserves_partial_retrieval_error(tmp_path):
    conn = connect(tmp_path / "errors.db")
    ensure_provenance_schema(conn)
    out = walk(InventoryPage({1: derived_page(1), 2: derived_page(1)}), retries=0)
    with SearchLedger(conn, created_by="d4-test").attempt(
        "portal_orders", {"method": "docket_inventory", "docket_number": "E-7 Sub 1026"},
        caller="test",
    ) as attempt:
        attempt.finish_outcome(out)
    row = conn.execute(
        "SELECT outcome, stop_reason, error_detail, result_count FROM search_attempts"
    ).fetchone()
    assert tuple(row) == ("incomplete", "stale_grid", out.error, 3)


def test_search_path_uses_complete_traversal_with_the_exact_docket(monkeypatch, tmp_path):
    from duke_rates.historical.ncuc import docket_inventory as di
    from duke_rates.historical.ncuc import remote_discovery as rd

    page = InventoryPage({n: derived_page(n, total=2) for n in (1, 2)})
    page.locator = lambda _: None  # mark as a browser-compatible page
    original = di.walk_docket_inventory
    docket_id = "8903f042-4167-4b27-971e-be19e44b0240"
    monkeypatch.setattr(di, "walk_docket_inventory", lambda page, **kwargs: original(
        page, advance=page.advance, transition_timeout=0.25, sleep=lambda _: None, **kwargs))
    out = rd.docket_inventory_search(
        page, {"docket_number": "E-7 Sub 1026"},
        resolve=lambda p, d: [{"docket_id": docket_id, "docket_number": d,
                               "match_type": "exact"}],
        list_documents=lambda p, d: [], checkpoint_dir=tmp_path,
    )
    assert out.complete and out.pages_reached == 2
    assert out.inventory_metadata["docket_id"] == docket_id


class NavigatingInventoryPage(InventoryPage):
    def __init__(self, failures):
        super().__init__({n: derived_page(n, total=2) for n in (1, 2)})
        self.failures, self.pending = failures, 0

    def advance(self, href):
        super().advance(href)
        self.pending = self.failures

    def content(self):
        if self.pending:
            self.pending -= 1
            raise RuntimeError("Unable to retrieve content because the page is navigating")
        return super().content()


def test_transient_navigation_read_error_does_not_discard_page_two():
    page = NavigatingInventoryPage(1)
    out = walk_docket_inventory(page, docket_label="E-7 Sub 1026", advance=page.advance,
                                 transition_timeout=1, sleep=lambda _: None, retries=0)
    assert out.complete and out.pages_reached == 2 and out.error is None


def test_persistent_navigation_error_preserves_partial_inventory(tmp_path):
    page = NavigatingInventoryPage(50)
    out = walk(page, retries=0, checkpoint_dir=tmp_path)
    assert out.stop_reason == "stale_grid" and out.pages_reached == 1
    assert len(out.results) == 3 and "navigating" in out.error
    assert (tmp_path / "unverified/page_2.html").exists()
    metadata = json.loads((tmp_path / "inventory.json").read_text())
    assert not metadata["complete"] and metadata["retrieved_pages"] == [1]


def test_real_confidential_entries_have_distinct_listing_identity_only():
    path = Path(__file__).parent / "fixtures/d4_portal/docket_inventory_e7_1026_page2.html"
    state = inventory_page_state(path.read_text(encoding="utf8"), docket_label="E-7 Sub 1026")
    assert state["valid"] and state["current"] == 2 and len(state["rows"]) == 100
    restricted = [r for r in state["rows"] if r.availability == "restricted"]
    assert len(restricted) == 11
    assert all(not r.document_id and not r.document_detail_url for r in restricted)
    assert all(r.identity_basis == "restricted_file_references" for r in restricted)
    assert len({r.inventory_entry_id for r in restricted}) == 11
    assert all(r.restriction_reason for r in restricted)


def test_real_second_page_orders_are_candidates_and_restricted_files_are_not(tmp_path):
    from duke_rates.historical.ncuc import remote_discovery as rd
    from duke_rates.historical.ncuc.search_ledger import canonical_params

    path = Path(__file__).parent / "fixtures/d4_portal/docket_inventory_e7_1026_page2.html"
    page = InventoryPage({1: CAPTURE, 2: path.read_text(encoding="utf8")})
    out = walk(page, max_pages=2)
    assert out.pages_reached == 2 and len(out.results) == 200 and not out.complete
    assert any(r.document_id == "dfcd0894-add8-4504-9d42-b8e9d0cb26ee" for r in out.results)
    conn = connect(tmp_path / "real_inventory.db")
    ensure_provenance_schema(conn)
    params = {"method": "docket_inventory", "docket_number": "E-7 Sub 1026"}
    plan = {"queries": [{"channel": "portal_orders", "params": params,
                         "params_hash": canonical_params("portal_orders", params)[1],
                         "targets": [{"gap_id": None, "tariff_version_id": 1,
                                      "docket": "E-7 Sub 1026"}]}]}
    stats = rd.execute_plan(conn, plan, created_by="d4-test", docket_inventory=lambda p: out)
    assert stats["inventory_rows"] == 200 and stats["restricted_inventory_rows"] == 11
    assert stats["order_rows"] == 52 and stats["candidates_listed"] == 52
    assert conn.execute(
        "SELECT COUNT(*) FROM remote_document_candidates WHERE portal_document_id = ?",
        ("dfcd0894-add8-4504-9d42-b8e9d0cb26ee",),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM remote_document_candidates WHERE portal_document_id IS NULL"
    ).fetchone()[0] == 0
    row = conn.execute("SELECT * FROM search_attempts").fetchone()
    assert (row["outcome"], row["pages_reached"], row["result_count"]) == ("incomplete", 2, 200)
    refs = json.loads(row["result_refs_json"])
    assert sum(ref.startswith("restricted-files:") for ref in refs) == 11


def test_complete_real_six_page_regression_finds_the_2013_rate_order(tmp_path):
    directory = Path(__file__).parent / "fixtures/d4_portal"
    page = InventoryPage({n: (directory / f"docket_inventory_e7_1026_page{n}.html").read_text(
        encoding="utf8") for n in range(1, 7)})
    out = walk(page, checkpoint_dir=tmp_path)
    assert out.complete and out.pages_reached == out.pages_total == 6
    assert len(out.results) == 568
    assert sum(r.doc_type == "Order" for r in out.results) == 85
    assert sum(r.availability == "restricted" for r in out.results) == 11
    rate_order = next(r for r in out.results
                      if r.document_id == "413b681f-f9ba-4bfe-9b73-a410602aaffb")
    assert rate_order.date_filed == "9/24/2013"
    assert rate_order.description == "Order Granting General Rate Increase"
    assert rate_order.availability == "public"


def test_form_action_verifies_docket_when_sanitized_hidden_value_is_empty():
    directory = Path(__file__).parent / "fixtures/d4_portal"
    html = (directory / "docket_inventory_e7_1026_page1.html").read_text(encoding="utf8")
    state = inventory_page_state(html, docket_label="E-7 Sub 1026", docket_id="other-docket")
    assert not state["valid"] and state["error"] == "docket identity mismatch"


def test_unknown_postback_syntax_does_not_make_inventory_complete():
    html = derived_page(1, total=2).replace("Page$2", "Page$unexpected")
    out = walk(InventoryPage({1: html}))
    assert out.stop_reason == "pagination_error" and not out.complete


def test_pager_accepts_postback_whitespace_and_both_quote_styles():
    soup = BeautifulSoup(derived_page(1, total=2), "lxml")
    link = soup.find("a", href=re.compile(r"Page\$2"))
    link["href"] = f'javascript:__doPostBack( "{TARGET}", "Page$2" )'
    out = walk(InventoryPage({1: str(soup), 2: derived_page(2, total=2)}))
    assert out.complete and out.pages_reached == 2


def test_pager_windows_do_not_follow_the_backward_ellipsis():
    pages = {}
    for number in range(1, 13):
        lo, hi = (1, 10) if number <= 10 else (11, 12)
        soup = BeautifulSoup(derived_page(number, total=12, window=(lo, hi)), "lxml")
        footer = soup.find("tr", class_="searchResultsFooter")
        target = 11 if number <= 10 else 10
        extra = soup.new_tag("a", href=f"javascript:__doPostBack('{TARGET}','Page${target}')")
        extra.string = "..."
        footer.append(extra)
        pages[number] = str(soup)
    page = InventoryPage(pages)
    out = walk(page)
    assert out.complete and out.pages_reached == out.pages_total == 12
    assert page.postbacks == list(range(2, 13))
