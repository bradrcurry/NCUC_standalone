"""Shared session API contracts for complete, bounded and restricted inventories."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_d4_inventory_pagination import InventoryPage, derived_page

from duke_rates.historical.ncuc import remote_discovery as rd
from duke_rates.historical.ncuc import session

DOCKET_ID = "8903f042-4167-4b27-971e-be19e44b0240"


class SessionPage(InventoryPage):
    def goto(self, url, **kwargs):
        self.html = self.pages[1]
        return SimpleNamespace(status=200)

    def locator(self, selector):
        return SimpleNamespace(first=SimpleNamespace(click=lambda **kwargs: self.advance(selector)))


def test_shared_reader_returns_all_pages_and_the_legacy_list_fields(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    page = SessionPage({n: derived_page(n, total=2) for n in (1, 2)})
    documents = session.get_docket_documents(page, DOCKET_ID)
    assert isinstance(documents, list) and len(documents) == 6
    assert set(documents[0]) == {
        "doc_type", "description", "date_filed", "document_url", "view_file_urls"}
    assert documents.inventory_outcome.complete
    assert documents.inventory_outcome.pages_reached == 2
    assert page.postbacks == [2]


def test_shared_reader_refuses_a_silently_partial_list(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    page = SessionPage({n: derived_page(n, total=2) for n in (1, 2)})
    with pytest.raises(session.NcucSessionError, match="Incomplete docket inventory") as caught:
        session.get_docket_documents(page, DOCKET_ID, max_pages=1)
    assert caught.value.outcome.stop_reason == "page_cap"
    assert caught.value.outcome.pages_reached == 1
    assert len(caught.value.outcome.results) == 3
    assert page.postbacks == []


def test_explicit_first_page_loader_leaves_traversal_to_the_caller(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    page = SessionPage({n: derived_page(n, total=2) for n in (1, 2)})
    documents = session.get_docket_documents_first_page(page, DOCKET_ID)
    assert len(documents) == 3 and page.postbacks == []
    assert not hasattr(documents, "inventory_outcome")


def test_shared_reader_withholds_all_confidential_download_urls(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    directory = Path(__file__).parent / "fixtures/d4_portal"
    page = SessionPage({n: (directory / f"docket_inventory_e7_1026_page{n}.html").read_text(
        encoding="utf8") for n in range(1, 7)})
    documents = session.get_docket_documents(page, DOCKET_ID)
    assert len(documents) == 568
    restricted = [(flat, row) for flat, row in zip(
        documents, documents.inventory_outcome.results, strict=True)
                  if row.availability == "restricted"]
    assert len(restricted) == 11
    assert all(not flat["view_file_urls"] and flat["document_url"] is None
               for flat, row in restricted)
    # Original visible attachment references stay as evidence, outside the
    # compatibility list consumed by legacy download loops.
    assert all(row.view_file_urls and row.restriction_reason for flat, row in restricted)


def test_remote_adapter_does_not_traverse_an_already_complete_provider_twice(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    page = SessionPage({n: derived_page(n, total=2) for n in (1, 2)})
    out = rd.docket_inventory_search(
        page, {"docket_number": "E-7 Sub 1026"},
        resolve=lambda p, d: [{"docket_id": DOCKET_ID, "docket_number": d, "match_type": "exact"}],
        list_documents=session.get_docket_documents,
    )
    assert out.complete and len(out.results) == 6
    assert page.postbacks == [2]


def test_remote_adapter_retains_the_shared_readers_partial_outcome(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    page = SessionPage({n: derived_page(n, total=2) for n in (1, 2)})
    out = rd.docket_inventory_search(
        page, {"docket_number": "E-7 Sub 1026"},
        resolve=lambda p, d: [{"docket_id": DOCKET_ID, "docket_number": d, "match_type": "exact"}],
        list_documents=lambda p, d: session.get_docket_documents(p, d, max_pages=1),
    )
    assert out.stop_reason == "page_cap" and out.pages_reached == 1
    assert len(out.results) == 3 and out.ledger_outcome == "incomplete"


def test_invalid_page_cap_fails_before_any_portal_request():
    with pytest.raises(ValueError, match="page limit"):
        session.get_docket_documents(object(), DOCKET_ID, max_pages=0)
