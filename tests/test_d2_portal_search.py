"""Phase 2E D2: DocumentsParameterSearch reliability on captured portal HTML.

Fixtures (``tests/fixtures/d2_portal``):

* ``form_captured.html``: the search form as captured on 2026-03-18. The
  filing-type list is ``disabled`` until the checkbox auto-postback runs.
* ``results_captured.html``: page 1 of a real result set ("Items Count: 211",
  10 rows, pager ``<span>1</span>`` + links 2..10 + ``...``).

Every other page is derived here from ``results_captured.html`` by the helpers
below (item count, pager window, document ids), so each derivation is visible.
A fake Playwright page serves them; no network request is made.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from duke_rates.db.provenance_schema import ensure_provenance_schema
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.document_param_search import (
    FILING_TYPE_GUIDS,
    DocumentParamSearcher,
    PortalSearchError,
    _find_next_page_target,
    _parse_results_page,
    explicit_empty_result,
    filing_type_list_disabled,
    grid_signature,
    is_blocked_page,
    items_reported,
    pager_current_page,
    results_grid_present,
)
from duke_rates.historical.ncuc.search_ledger import SearchLedger

FIX = Path(__file__).resolve().parent / "fixtures" / "d2_portal"
FORM = (FIX / "form_captured.html").read_text(encoding="utf-8")
RESULTS = (FIX / "results_captured.html").read_text(encoding="utf-8")
BLOCKED = "<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing</body></html>"
_PAGER_TD = re.compile(r'(<tr class="SearchResultsFooter-text-right">\s*<td>)(.*?)(</td>)', re.S)
_GRID_PREFIX = "ctl00$ContentPlaceHolder1$PortalPageControl1$ctl86$PSCDocumentSearchControl1$resultsGrid$ctl14$"


# ---------------------------------------------------------------------------
# Derived pages
# ---------------------------------------------------------------------------


def _pager(current: int, total_pages: int) -> str:
    """ASP.NET NumericPages pager, 10 buttons per window, as on the captured page."""
    start = (current - 1) // 10 * 10 + 1
    end = min(start + 9, total_pages)
    link = lambda label, page: f"<a href=\"javascript:__doPostBack('{_GRID_PREFIX}pg{page}','')\">{label}</a>"
    parts = [link("...", start - 1)] if start > 1 else []
    parts += [f"<span>{p}</span>" if p == current else link(str(p), p) for p in range(start, end + 1)]
    if end < total_pages:
        parts.append(link("...", end + 1))
    return "&nbsp;".join(parts)


def results_page(page: int, total_items: int, *, rows: int | None = None, pager: bool = True) -> str:
    """The captured page renumbered as page ``page`` of ``total_items`` with unique document ids."""
    total_pages = -(-total_items // 10)
    html = RESULTS.replace("Items Count: 211", f"Items Count: {total_items}")
    counter = iter(range(10))
    html = re.sub(
        r"DocumentId=([a-f0-9-]{36})",
        lambda m: f"DocumentId={page:08d}-0000-0000-0000-{next(counter, 9):012d}", html,
    )
    if rows is not None:  # drop trailing result rows (the last page is short)
        html = _keep_rows(html, rows)
    html = _PAGER_TD.sub(lambda m: m.group(1) + (_pager(page, total_pages) if pager and total_pages > 1 else "") + m.group(3),
                         html)
    return html


def _keep_rows(html: str, rows: int) -> str:
    """Remove result-item tables after the first ``rows`` (each is one grid row)."""
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("table", id=re.compile(r"resultsGridItem$"))
    for table in items[rows:]:
        (table.find_parent("tr") or table).decompose()
    return str(soup)


def empty_results_page() -> str:
    return _keep_rows(RESULTS.replace("Items Count: 211", "Items Count: 0"), 0)


# ---------------------------------------------------------------------------
# Fake Playwright page
# ---------------------------------------------------------------------------


class FakePage:
    """Serves the search form, then result pages keyed by the postback target."""

    def __init__(self, pages: dict[int, str], *, form: str = FORM, title: str = "Documents",
                 checkbox_postback_works: bool = True, after_submit: str | None = None,
                 fail_on_page: int | None = None, stale_on_page: int | None = None,
                 blocked_on_page: int | None = None):
        self.pages = pages
        self._form = form
        self._title = title
        self.checkbox_postback_works = checkbox_postback_works
        self.after_submit = after_submit
        self.fail_on_page, self.stale_on_page, self.blocked_on_page = fail_on_page, stale_on_page, blocked_on_page
        self.html = ""
        self.checked = False
        self.selected: list[str] = []
        self.values: dict[str, str] = {}
        self.submitted = 0
        self.postbacks: list[int] = []

    # navigation
    def goto(self, url, **kwargs):
        self.html = self._form

    def title(self):
        return "Just a moment..." if self.html == BLOCKED else self._title

    def content(self):
        return self.html

    def wait_for_load_state(self, *args, **kwargs):
        return None

    # form
    def is_checked(self, selector):
        return self.checked

    def check(self, selector):
        self.checked = True
        if self.checkbox_postback_works:  # the auto-postback re-renders the list enabled
            self.html = self.html.replace('multiple="multiple" id', 'multiple="multiple" data-enabled id').replace(
                'disabled="disabled"', "")

    def wait_for_selector(self, selector, timeout=None):
        if ":not([disabled])" in selector and filing_type_list_disabled(self.html):
            raise TimeoutError(f"{selector} stayed disabled")

    def select_option(self, selector, value):
        if filing_type_list_disabled(self.html):
            return  # a disabled list silently ignores the selection
        self.selected = list(value)

    def eval_on_selector(self, selector, script):
        return list(self.selected)

    def fill(self, selector, value):
        self.values[selector] = value

    def input_value(self, selector):
        return self.values.get(selector, "")

    def click(self, selector):
        self.submitted += 1
        self.html = self.after_submit if self.after_submit is not None else self.pages[1]

    def evaluate(self, script):
        page = int(re.search(r"pg(\d+)'", script).group(1))
        self.postbacks.append(page)
        if page == self.fail_on_page:
            raise TimeoutError("postback timed out")
        if page == self.blocked_on_page:
            self.html = BLOCKED
        elif page != self.stale_on_page:
            self.html = self.pages[page]


def _searcher() -> DocumentParamSearcher:
    s = DocumentParamSearcher(settings=None)
    s._sleep = lambda seconds: None
    return s


def _set(total: int) -> dict[int, str]:
    n = -(-total // 10)
    return {p: results_page(p, total, rows=(total - 10 * (n - 1)) if p == n else None) for p in range(1, n + 1)}


# ---------------------------------------------------------------------------
# Page-state functions on the captured pages
# ---------------------------------------------------------------------------


def test_captured_pages_state():
    assert filing_type_list_disabled(FORM) is True
    assert items_reported(RESULTS) == 211
    assert pager_current_page(RESULTS) == 1
    assert results_grid_present(RESULTS) and not results_grid_present(FORM)
    assert len(_parse_results_page(RESULTS, "")) == 10
    assert _find_next_page_target(RESULTS, current_page=1).endswith("resultsGrid$ctl14$ctl01")
    assert not explicit_empty_result(RESULTS) and explicit_empty_result(empty_results_page())
    assert is_blocked_page("Just a moment...", BLOCKED) and not is_blocked_page("Documents", RESULTS)
    assert grid_signature(RESULTS) != grid_signature(results_page(2, 211))


def test_next_target_ignores_leading_ellipsis_on_last_page():
    last = results_page(22, 211, rows=1)
    assert pager_current_page(last) == 22
    assert _find_next_page_target(last, current_page=22) is None
    assert _find_next_page_target(results_page(10, 211), current_page=10).endswith("pg11")
    assert _find_next_page_target(results_page(11, 211), current_page=11).endswith("pg12")


# ---------------------------------------------------------------------------
# Search outcomes
# ---------------------------------------------------------------------------


def test_multi_page_success_walks_every_window():
    page = FakePage(_set(211))
    out = _searcher().search_with_outcome(page, filing_types=["ORDER"], docket_number="E-2 Sub 1300")
    assert out.stop_reason == "last_page" and out.complete
    assert (out.pages_reached, out.pages_total, out.items_reported) == (22, 22, 211)
    assert len(out.results) == 211 == len({r.document_id for r in out.results})
    assert out.filter_verified is True and page.selected == [FILING_TYPE_GUIDS["ORDER"]]
    assert out.ledger_outcome == "hits"
    assert page.postbacks == list(range(2, 23))


def test_single_page_result():
    page = FakePage({1: results_page(1, 7, rows=7)})
    out = _searcher().search_with_outcome(page)
    assert (out.stop_reason, out.pages_reached, len(out.results)) == ("single_page", 1, 7)
    assert out.ledger_outcome == "hits"


def test_empty_result_is_negative_evidence():
    page = FakePage({}, after_submit=empty_results_page())
    out = _searcher().search_with_outcome(page)
    assert out.stop_reason == "empty_result" and out.complete
    assert out.ledger_outcome == "no_results" and out.items_reported == 0


def test_unrecognized_page_is_an_error_not_an_empty_search():
    page = FakePage({}, after_submit="<html><title>Documents</title><body>Server Error</body></html>")
    out = _searcher().search_with_outcome(page)
    assert out.stop_reason == "unrecognized_page" and out.ledger_outcome == "error"
    with pytest.raises(PortalSearchError):
        _searcher().search(FakePage({}, after_submit="<html><body>Server Error</body></html>"))


def test_blocked_on_load_raises_like_before():
    page = FakePage({}, form=BLOCKED)
    out = _searcher().search_with_outcome(page)
    assert out.stop_reason == "blocked_initial" and out.ledger_outcome == "blocked"
    with pytest.raises(RuntimeError, match="Cloudflare blocked"):
        _searcher().search(FakePage({}, form=BLOCKED))


def test_blocked_mid_pagination_keeps_rows_but_is_not_complete():
    page = FakePage(_set(25), blocked_on_page=2)
    out = _searcher().search_with_outcome(page)
    assert out.stop_reason == "blocked_pagination" and out.ledger_outcome == "blocked"
    assert out.pages_reached == 1 and len(out.results) == 10
    # search() keeps its old contract: rows collected before the failure are returned.
    assert len(_searcher().search(FakePage(_set(25), blocked_on_page=2))) == 10


def test_pagination_error_is_incomplete():
    out = _searcher().search_with_outcome(FakePage(_set(25), fail_on_page=3))
    assert (out.stop_reason, out.pages_reached, len(out.results)) == ("pagination_error", 2, 20)
    assert out.ledger_outcome == "incomplete" and not out.complete


def test_stale_grid_is_detected_and_rows_not_duplicated():
    out = _searcher().search_with_outcome(FakePage(_set(25), stale_on_page=2), grid_wait_seconds=1)
    assert out.stop_reason == "stale_grid" and out.ledger_outcome == "incomplete"
    assert len(out.results) == 10


def test_filter_not_applied_is_never_submitted_unfiltered():
    page = FakePage(_set(25), checkbox_postback_works=False)
    out = _searcher().search_with_outcome(page, filing_types=["TARIFF", "RATESCED"])
    assert out.stop_reason == "filter_not_applied" and out.filter_verified is False
    assert page.submitted == 0 and out.ledger_outcome == "error"
    with pytest.raises(PortalSearchError, match="filter_not_applied"):
        _searcher().search(FakePage(_set(25), checkbox_postback_works=False))


def test_unknown_filing_type_is_rejected():
    out = _searcher().search_with_outcome(FakePage(_set(5)), filing_types=["NOPE"])
    assert out.stop_reason == "filter_not_applied"


def test_result_cap_is_incomplete():
    out = _searcher().search_with_outcome(FakePage(_set(25)), max_results=15)
    assert (out.stop_reason, len(out.results), out.ledger_outcome) == ("max_results_cap", 15, "incomplete")


def test_item_count_mismatch_is_incomplete():
    pages = _set(25)
    pages = {p: html.replace("Items Count: 25", "Items Count: 30") for p, html in pages.items()}
    out = _searcher().search_with_outcome(FakePage(pages))
    assert out.stop_reason == "item_count_mismatch" and out.ledger_outcome == "incomplete"


def test_search_returns_the_same_rows_as_outcome():
    rows = _searcher().search(FakePage(_set(25)))
    assert [r.document_id for r in rows] == [r.document_id for r in _searcher().search_with_outcome(
        FakePage(_set(25))).results]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@pytest.fixture()
def ledger(tmp_path: Path) -> SearchLedger:
    conn = connect(tmp_path / "l.db")
    ensure_provenance_schema(conn)
    return SearchLedger(conn, created_by="t")


def test_ledger_records_stop_reason_and_retry_policy(ledger: SearchLedger):
    params = {"docket_number": "E-2 Sub 1300", "filing_types": ["ORDER"]}
    with ledger.attempt("portal_orders", params, caller="t") as att:
        att.finish_outcome(_searcher().search_with_outcome(FakePage(_set(25), fail_on_page=2)))
    assert ledger.prior_completed("portal_orders", params) is None  # incomplete: retryable
    with pytest.raises(PortalSearchError):
        with ledger.attempt("portal_orders", params, caller="t"):
            _searcher().search(FakePage({}, form=BLOCKED))
    assert ledger.prior_completed("portal_orders", params) is None  # blocked: retryable
    with ledger.attempt("portal_orders", params, caller="t") as att:
        att.finish_outcome(_searcher().search_with_outcome(FakePage(_set(25))))
    assert ledger.prior_completed("portal_orders", params)["outcome"] == "hits"
    rows = [dict(r) for r in ledger.conn.execute(
        "SELECT outcome, stop_reason, pages_reached, pages_total, items_reported, filter_verified, result_count "
        "FROM search_attempts ORDER BY id")]
    assert rows == [
        {"outcome": "incomplete", "stop_reason": "pagination_error", "pages_reached": 1, "pages_total": 3,
         "items_reported": 25, "filter_verified": 1, "result_count": 10},
        {"outcome": "blocked", "stop_reason": "blocked_initial", "pages_reached": 0, "pages_total": None,
         "items_reported": None, "filter_verified": None, "result_count": 0},
        {"outcome": "hits", "stop_reason": "last_page", "pages_reached": 3, "pages_total": 3,
         "items_reported": 25, "filter_verified": 1, "result_count": 25},
    ]
    assert len(ledger.completed_hashes("portal_orders")) == 1
