"""Verified traversal of the existing DocketDetails document grid.

This is the inventory counterpart of DocumentParamSearcher's result traversal,
using its outcome and blocked-page rules. It never downloads or promotes PDFs.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from duke_rates.historical.ncuc.document_param_search import (
    DocParamSearchOutcome,
    is_blocked_page,
    items_reported,
)

_POSTBACK = re.compile(
    r"""__doPostBack\(\s*(?P<q>['"])(?P<target>[^'"]+)(?P=q)\s*,\s*"""
    r"""(?P<a>['"])Page\$(?P<page>\d+|Next|Last|Previous|First)(?P=a)\s*\)""", re.I,
)
RETRIEVER_VERSION = "docket_inventory@1.0.0"


class DocketDocumentList(list):
    """Legacy list contract with explicit inventory metadata and restricted URLs withheld."""

    def __init__(self, outcome: DocParamSearchOutcome):
        self.inventory_outcome = outcome
        super().__init__({
            "doc_type": row.doc_type, "description": row.description,
            "date_filed": row.date_filed,
            "document_url": row.document_detail_url if row.availability != "restricted" else None,
            "view_file_urls": list(row.view_file_urls) if row.availability != "restricted" else [],
        } for row in outcome.results)


def inventory_page_state(html: str, *, docket_label: str, docket_id: str | None = None) -> dict:
    """Read only this grid's pager, stable row IDs, and optional docket identity."""
    from duke_rates.historical.ncuc.remote_discovery import docket_inventory_outcome

    soup = BeautifulSoup(html or "", "lxml")
    grid = soup.find(id=re.compile(r"documentsGridView$"))
    identity = soup.find("input", id=re.compile(r"hdnDocketid$", re.I))
    form = soup.find("form")
    identities = [identity.get("value")] if identity and identity.get("value") else []
    if form:
        identities.extend(value for key, values in parse_qs(
            urlsplit(form.get("action", "")).query).items()
                          if key.lower() == "docketid" for value in values)
    wrong_docket = bool(docket_id and (not identities or any(
        value.lower() != docket_id.lower() for value in identities)))
    outcome = docket_inventory_outcome(html, [], docket_label=docket_label)
    rows = outcome.results
    ids = [(r.inventory_entry_id or "").lower() for r in rows]
    footer = grid.find("tr", class_="searchResultsFooter") if grid else None
    current = None
    if footer:
        numbers = [int(s.get_text(strip=True)) for s in footer.find_all("span")
                   if s.get_text(strip=True).isdigit()]
        if len(numbers) == 1:
            current = numbers[0]
    links = {}
    forward_unknown = False
    advertised = [current] if current else []
    for anchor in footer.find_all("a", href=True) if footer else []:
        match = _POSTBACK.search(anchor["href"])
        if not match:
            if "documentsgridview" in anchor["href"].lower() and "page$" in anchor["href"].lower():
                forward_unknown = True
            continue
        if not match.group("target").endswith("documentsGridView"):
            continue
        argument = match.group("page")
        if argument.isdigit():
            number = int(argument)
            advertised.append(number)
            links[number] = anchor["href"]
        elif argument.lower() in ("next", "last"):
            forward_unknown = True
            if argument.lower() == "next" and current:
                links[current + 1] = anchor["href"]
        if anchor.get_text(strip=True) in ("...", "…") and (current is None or
                (argument.isdigit() and int(argument) > current)):
            forward_unknown = True
    if not footer:
        # A changed pager layout must not make a multipage grid look single-page.
        has_unknown_pager = bool(grid and any(
            "documentsgridview" in a.get("href", "").lower()
            and "page$" in a.get("href", "").lower() for a in grid.find_all("a")))
        current = None if has_unknown_pager else 1
    return {
        "valid": bool(grid and rows and not wrong_docket and all(ids) and current),
        "error": "docket identity mismatch" if wrong_docket else
                 "missing grid, rows, stable document ID, or selected page",
        "rows": rows, "current": current, "links": links,
        "advertised": max(advertised, default=1), "forward_unknown": forward_unknown,
        "signature": hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest(),
        "items_reported": items_reported(html),
    }


def _capture_inventory_page(directory: Path, number: int, html: str, metadata: dict) -> None:
    """Persist evidence atomically, removing form state and executable scripts."""
    soup = BeautifulSoup(html, "lxml")
    for field in soup.select('input[type="hidden"]'):
        field["value"] = ""
    for script in soup.find_all("script"):
        script.clear()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"page_{number}.html.tmp"
    temporary.write_text(str(soup), encoding="utf-8")
    temporary.replace(directory / f"page_{number}.html")
    temporary = directory / "inventory.json.tmp"
    temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    temporary.replace(directory / "inventory.json")


def walk_docket_inventory(
    page: Any, *, docket_label: str, docket_id: str | None = None,
    max_pages: int = 100, transition_timeout: float = 10, retries: int = 1,
    checkpoint_dir: Path | None = None, capture: Callable[[str], None] | None = None,
    advance: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DocParamSearchOutcome:
    """Traverse sequentially; failed transitions retain only verified pages.

    Checkpoints preserve accepted pages on interruption. Restart revalidates all
    pages against the current portal, and existing candidate IDs prevent duplicate
    catalog rows. Cached HTML is never substituted for a current portal response.
    ``advance`` is injectable for deterministic captured-page tests.
    """
    if not 1 <= max_pages <= 1000 or not 0 < transition_timeout <= 60 or not 0 <= retries <= 2:
        raise ValueError("inventory limits: pages 1..1000, timeout (0,60], retries 0..2")
    out = DocParamSearchOutcome(results=[], stop_reason="inventory_complete", pages_total=None)
    seen_ids, seen_pages = set(), set()
    metadata = {"docket_number": docket_label, "docket_id": docket_id,
                "method": "playwright_docket_inventory", "retrieved_pages": [],
                "advertised_pages": None, "unique_documents": 0, "complete": False,
                "retriever": RETRIEVER_VERSION}
    out.inventory_metadata = metadata
    html, title = "", ""
    try:
        html, title = page.content(), page.title()
    except Exception as exc:
        out.stop_reason = "unrecognized_page"
        out.error = f"cannot read initial inventory: {type(exc).__name__}: {exc}"[:500]
    expected, advertised = 1, 1
    while True:
        if out.stop_reason != "inventory_complete":
            break
        if is_blocked_page(title, html):
            out.stop_reason = "blocked_initial" if not out.results else "blocked_pagination"
            out.error = "challenge on docket inventory; no retry"
            break
        state = inventory_page_state(html, docket_label=docket_label, docket_id=docket_id)
        if not state["valid"]:
            out.stop_reason, out.error = "unrecognized_page", state["error"]
            break
        if state["current"] != expected or state["signature"] in seen_pages:
            out.stop_reason = "stale_grid"
            out.error = f"expected inventory page {expected} with new rows"
            break
        seen_pages.add(state["signature"])
        out.grid_signatures.append(state["signature"])
        advertised = max(advertised, state["advertised"])
        # A forward ellipsis is a lower bound, not the advertised last page.
        out.pages_total = None if state["forward_unknown"] else advertised
        for row in state["rows"]:
            row.document_id = row.document_id.lower()
            if row.inventory_entry_id not in seen_ids:
                seen_ids.add(row.inventory_entry_id)
                out.results.append(row)
        out.pages_reached = expected
        if state["items_reported"] is not None:
            if out.items_reported is not None and out.items_reported != state["items_reported"]:
                out.stop_reason = "item_count_mismatch"
                out.error = "inventory count changed during traversal"
            out.items_reported = state["items_reported"]
        metadata.update(retrieved_pages=list(range(1, expected + 1)),
                        advertised_pages=out.pages_total, observed_page_bound=advertised,
                        unique_documents=len(out.results),
                        restricted_entries=sum(r.availability == "restricted" for r in out.results))
        if checkpoint_dir:
            _capture_inventory_page(Path(checkpoint_dir), expected, html, metadata)
        if capture:
            capture(html)
        if out.stop_reason != "inventory_complete":
            break
        href = state["links"].get(expected + 1)
        if href is None:
            if expected < advertised or state["forward_unknown"]:
                out.stop_reason = "pagination_error"
                out.error = "advertised next page has no usable sequential link"
            elif out.items_reported is not None and len(out.results) != out.items_reported:
                out.stop_reason = "item_count_mismatch"
                out.error = "unique rows do not match advertised item count"
            break
        if expected >= max_pages:
            out.stop_reason, out.error = "page_cap", f"inventory stopped at {max_pages} pages"
            break
        next_number = expected + 1
        transitioned = False
        for _attempt in range(retries + 1):
            try:
                if advance:
                    advance(href)
                else:
                    page.locator(f"a[href={json.dumps(href)}]").first.click(
                        timeout=int(transition_timeout * 1000))
            except Exception as exc:
                out.error = f"inventory page {next_number}: {type(exc).__name__}: {exc}"[:500]
            # A click can time out after navigation committed. Validate the DOM
            # before deciding it failed; networkidle alone did not prove paging.
            waited = 0.0
            while True:
                try:
                    current_html, current_title = page.content(), page.title()
                except Exception as exc:
                    # Playwright can reject content() while the postback's
                    # navigation replaces its execution context. Keep polling.
                    out.error = f"reading page {next_number}: {type(exc).__name__}: {exc}"[:500]
                else:
                    html, title = current_html, current_title
                    if is_blocked_page(title, html):
                        out.stop_reason = "blocked_pagination"
                        out.error = "challenge on inventory postback; no retry"
                        break
                    new = inventory_page_state(html, docket_label=docket_label,
                                               docket_id=docket_id)
                    if (new["valid"] and new["current"] == next_number
                            and new["signature"] not in seen_pages):
                        transitioned = True
                        break
                    out.error = (f"expected page {next_number}; observed {new['current']}; "
                                 + (new["error"] if not new["valid"] else "unchanged/wrong grid"))
                if waited >= transition_timeout:
                    break
                sleep(0.25)
                waited += 0.25
            if transitioned or out.stop_reason == "blocked_pagination":
                break
        if not transitioned:
            if out.stop_reason != "blocked_pagination":
                out.stop_reason = "stale_grid"
                out.error = out.error or (
                    f"page {next_number} not verified after {retries + 1} postbacks")
            if checkpoint_dir:
                _capture_inventory_page(Path(checkpoint_dir) / "unverified", next_number,
                                        html, metadata | {"error": out.error})
            if capture:
                capture(html)
            break
        out.error = None
        expected = next_number
    metadata.update(complete=out.complete, stop_reason=out.stop_reason, error=out.error)
    if checkpoint_dir:
        # Final metadata must describe partial runs too. Keep page evidence from
        # the last verified page, rather than labelling a blocked response valid.
        directory = Path(checkpoint_dir)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "inventory.json.tmp"
        temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        temporary.replace(directory / "inventory.json")
    return out
