"""
DocumentsParameterSearch scraper for the NCUC starw1 portal.

Uses the structured search form at:
  https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx

Unlike the public Zoom search (www.ncuc.gov/search/search.php), this form has:
  - Company name filter (free text — partial match, e.g. "Duke Energy Progress")
  - Docket number filter
  - Date range filters (Filed On Or After / Before)
  - Filing type multi-select with specific GUIDs

The portal is Cloudflare-protected and requires an authenticated Playwright session
(see session.py: create_authenticated_context).

Filing type GUIDs (confirmed from live portal HTML 2026-03-18):
  TARIFF (E - Electric - Tariffs):          e0418685-8231-4312-af73-e6fd4b08bb98
  RATESCED (E - Electric - Rate Sched/Riders): 7ea9010b-5c1a-480c-a65a-ed402515cbde
  ORDER (E - Electric - Order Documents):   5e40c296-9c8e-4b15-8195-7cef6cf8144e
  INFOFILE (E - Electric - Informational):  54c59099-8c4a-46a6-8dfe-9cb2569984ea

Result HTML structure (confirmed 2026-03-18):
  Each result is its own nested <table> within the overall page.
  First data row of each result table:
    cell[1] = document description (plain text)
    cell[2] = filing type code (e.g. "Filing", "TARIFF")
    cell[4] = "Filed In: E-2 Sub XXXX" with docket link
    cell[5] = "Date Filed: MM/DD/YYYY"
    cell[6] = empty cell with href to PSCDocumentDetailsPageNCUC.aspx?DocumentId=...

ASP.NET field names (confirmed from live portal HTML 2026-03-18):
  companyName:              ...PSCDocumentSearchControl1$companyName
  docketNumber:             ...PSCDocumentSearchControl1$docketNumber
  filedOnOrAfterTextBox:    ...PSCDocumentSearchControl1$filedOnOrAfterTextBox
  filedOnOrBeforeTextBox:   ...PSCDocumentSearchControl1$filedOnOrBeforeTextBox
  filterByFilingTypesCheckBox: ...PSCDocumentSearchControl1$filterByFilingTypesCheckBox
  filingTypesList:          ...PSCDocumentSearchControl1$filingTypesList
  searchButton:             ...PSCDocumentSearchControl1$searchButton

Usage:
    pw, ctx, page = create_authenticated_context(settings)
    try:
        searcher = DocumentParamSearcher(settings)
        results = searcher.search(
            page,
            company_name="Duke Energy Progress",
            filing_types=["TARIFF", "RATESCED"],
        )
        for r in results:
            print(r.doc_type, r.description, r.date_filed, r.docket_number)
    finally:
        close_authenticated_context(pw, ctx)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from duke_rates.config import Settings

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

_PLACEHOLDER_DESCRIPTION_RE = re.compile(
    r"^click\s+the\s+to\s+view\s+the\s+document\.?$",
    re.I,
)

DOCUMENT_SEARCH_URL = (
    "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
)

# Filing type GUIDs — confirmed from live portal HTML 2026-03-18
FILING_TYPE_GUIDS: dict[str, str] = {
    "TARIFF": "e0418685-8231-4312-af73-e6fd4b08bb98",
    "RATESCED": "7ea9010b-5c1a-480c-a65a-ed402515cbde",
    "ORDER": "5e40c296-9c8e-4b15-8195-7cef6cf8144e",
    "INFOFILE": "54c59099-8c4a-46a6-8dfe-9cb2569984ea",
}

# ASP.NET control name suffix (everything after the naming-container prefix)
_CTL_SUFFIX = "PSCDocumentSearchControl1$"

# Confirmed field name suffixes (from live HTML 2026-03-18)
_FIELD_COMPANY       = f"{_CTL_SUFFIX}companyName"
_FIELD_DOCKET        = f"{_CTL_SUFFIX}docketNumber"
_FIELD_DATE_AFTER    = f"{_CTL_SUFFIX}filedOnOrAfterTextBox"
_FIELD_DATE_BEFORE   = f"{_CTL_SUFFIX}filedOnOrBeforeTextBox"
_FIELD_FT_CHECKBOX   = f"{_CTL_SUFFIX}filterByFilingTypesCheckBox"
_FIELD_FT_LIST       = f"{_CTL_SUFFIX}filingTypesList"
_FIELD_SEARCH_BTN    = f"{_CTL_SUFFIX}searchButton"


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class DocParamSearchResult:
    """A single document row returned from DocumentsParameterSearch."""
    description: str
    doc_type: str
    date_filed: str
    docket_number: str
    docket_id: str                    # GUID from DocketDetails URL
    company_name: str
    document_detail_url: str | None   # PSCDocumentDetailsPageNCUC.aspx?DocumentId=...
    document_id: str = ""             # GUID from document detail URL
    view_file_urls: list[str] = field(default_factory=list)
    view_file_labels: list[str] = field(default_factory=list)
    synopsis: str = ""

    # Enriched from description parsing
    extracted_schedule_codes: list[str] = field(default_factory=list)
    extracted_rider_codes: list[str] = field(default_factory=list)
    filing_classification: str = ""

    def is_tariff_related(self) -> bool:
        text = " ".join(
            part.strip()
            for part in (
                self.doc_type,
                self.description,
                self.synopsis,
                self.filing_classification,
                " ".join(self.view_file_labels),
            )
            if part and part.strip()
        ).lower()
        if any(
            kw in text
            for kw in (
                "tariff", "rate schedule", "rider", "schedule", "ratesced",
                "leaf", "canceling", "superseding",
            )
        ):
            return True

        if self.extracted_schedule_codes or self.extracted_rider_codes:
            return True

        # The portal sometimes returns placeholder rows that only resolve into
        # tariff/rider filenames after the detail-page enrichment step. Keep
        # those rows visible under tariff-only filtering rather than dropping
        # them as false negatives.
        desc = (self.description or "").strip()
        if self.document_detail_url and (
            not desc or _PLACEHOLDER_DESCRIPTION_RE.match(desc)
        ):
            return True

        return False

    def short_label(self) -> str:
        desc = self.description[:65].strip()
        return f"[{self.doc_type}] {desc}  docket={self.docket_number}  {self.date_filed}"


# ---------------------------------------------------------------------------
# Main searcher class
# ---------------------------------------------------------------------------

class DocumentParamSearcher:
    """
    Submits structured queries to the NCUC DocumentsParameterSearch portal.

    Requires an authenticated Playwright page (from session.create_authenticated_context).
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def search(
        self,
        page: "Page",
        *,
        company_name: str = "Duke Energy Progress",
        docket_number: str = "",
        filing_types: list[str] | None = None,
        date_after: str = "",
        date_before: str = "",
        max_results: int = 2000,
    ) -> list[DocParamSearchResult]:
        """
        Execute a DocumentsParameterSearch query and return results.

        Args:
            page: Authenticated Playwright page (from create_authenticated_context)
            company_name: Company name text filter (partial match)
            docket_number: Optional docket number filter (e.g. "E-2 Sub 1190")
            filing_types: List of filing type keys from FILING_TYPE_GUIDS
                          (e.g. ["TARIFF", "RATESCED"]). None = no filter.
            date_after: "MM/DD/YYYY" lower bound for filed date
            date_before: "MM/DD/YYYY" upper bound for filed date
            max_results: Maximum number of result rows to collect

        Returns:
            List of DocParamSearchResult objects
        """
        if filing_types is None:
            filing_types = ["TARIFF", "RATESCED"]

        logger.info(
            "DocumentParamSearch: company=%r types=%s docket=%r after=%r",
            company_name, filing_types, docket_number, date_after,
        )

        page.goto(DOCUMENT_SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)

        title = page.title()
        if "Just a moment" in title or "checking your browser" in page.content().lower():
            raise RuntimeError(
                f"Cloudflare blocked DocumentsParameterSearch (title={title!r}). "
                "Ensure authenticated context is active."
            )

        logger.info("DocumentsParameterSearch loaded: %r", title)

        # Discover the full ASP.NET naming-container prefix at runtime
        # (the ctl## portion can vary; the suffix _FIELD_* constants are fixed)
        html = page.content()
        prefix = _discover_prefix(html)
        logger.debug("ASP.NET naming prefix: %r", prefix)

        def sel(suffix: str) -> str:
            return f'[name="{prefix}{suffix}"]'

        # --- Fill form ---
        if company_name:
            try:
                page.fill(sel(_FIELD_COMPANY), company_name)
            except Exception as e:
                logger.warning("Could not fill companyName: %s", e)

        if docket_number:
            try:
                page.fill(sel(_FIELD_DOCKET), docket_number)
            except Exception as e:
                logger.warning("Could not fill docketNumber: %s", e)

        if date_after:
            try:
                page.fill(sel(_FIELD_DATE_AFTER), date_after)
            except Exception as e:
                logger.warning("Could not fill filedOnOrAfterTextBox: %s", e)

        if date_before:
            try:
                page.fill(sel(_FIELD_DATE_BEFORE), date_before)
            except Exception as e:
                logger.warning("Could not fill filedOnOrBeforeTextBox: %s", e)

        # Check the filing types filter checkbox, then select GUIDs
        if filing_types:
            try:
                cb_sel = sel(_FIELD_FT_CHECKBOX)
                if not page.is_checked(cb_sel):
                    page.check(cb_sel)
                    time.sleep(0.5)
            except Exception as e:
                logger.warning("Could not check filterByFilingTypesCheckBox: %s", e)

            guids = [FILING_TYPE_GUIDS[ft] for ft in filing_types if ft in FILING_TYPE_GUIDS]
            if guids:
                try:
                    page.select_option(sel(_FIELD_FT_LIST), value=guids)
                    logger.debug("Selected filing type GUIDs: %s", guids)
                except Exception as e:
                    logger.warning("Could not select filingTypesList: %s", e)

        # --- Submit ---
        try:
            page.click(sel(_FIELD_SEARCH_BTN))
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning("Search button click issue: %s", e)
        time.sleep(3)

        # --- Parse results (all on one page — no pagination observed) ---
        all_results: list[DocParamSearchResult] = []
        page_num = 1

        while True:
            html = page.content()
            page_results = _parse_results_page(html, company_name)
            logger.info(
                "Page %d: %d results (total so far: %d)",
                page_num, len(page_results),
                len(all_results) + len(page_results),
            )
            all_results.extend(page_results)

            if len(all_results) >= max_results:
                all_results = all_results[:max_results]
                break

            next_target = _find_next_page_target(html, current_page=page_num)
            if not next_target:
                break

            try:
                page.evaluate(f"__doPostBack('{next_target}', '')")
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                time.sleep(2)
            except Exception as e:
                logger.debug("Pagination postback failed: %s", e)
                break

            page_num += 1
            if page_num > 100:
                logger.warning("DocumentParamSearch: hit 100-page safety cap")
                break

        _enrich_results(all_results)
        logger.info("DocumentParamSearch: %d total results", len(all_results))
        return all_results

    def search_tariff_schedules(
        self,
        page: "Page",
        *,
        company_name: str = "Duke Energy Progress",
        date_after: str = "",
    ) -> list[DocParamSearchResult]:
        """Convenience wrapper: TARIFF + RATESCED filings for a company."""
        return self.search(
            page,
            company_name=company_name,
            filing_types=["TARIFF", "RATESCED"],
            date_after=date_after,
        )

    def enrich_with_document_details(
        self,
        page: "Page",
        results: list[DocParamSearchResult],
        *,
        delay_seconds: float = 0.5,
    ) -> list[DocParamSearchResult]:
        """Populate view-file links and synopsis from each document detail page."""
        for row in results:
            if not row.document_detail_url:
                continue
            try:
                detail = fetch_document_detail(page, row.document_detail_url)
            except Exception as exc:
                logger.warning(
                    "Could not enrich document detail %s: %s",
                    row.document_detail_url,
                    exc,
                )
                continue
            row.view_file_urls = detail["view_file_urls"]
            row.view_file_labels = detail["view_file_labels"]
            row.synopsis = detail["synopsis"]
            if delay_seconds:
                time.sleep(delay_seconds)
        return results


# ---------------------------------------------------------------------------
# ASP.NET prefix discovery
# ---------------------------------------------------------------------------

def _discover_prefix(html: str) -> str:
    """
    Find the ASP.NET naming-container prefix for the search control fields.

    The suffix is always 'PSCDocumentSearchControl1$companyName'.
    We search for any input whose name ends with that suffix and extract the prefix.
    """
    match = re.search(
        r'name="([^"]*PSCDocumentSearchControl1\$companyName)"',
        html,
    )
    if match:
        full_name = match.group(1)
        suffix = _FIELD_COMPANY
        return full_name[: len(full_name) - len(suffix)]
    # Fallback: try docketNumber
    match = re.search(
        r'name="([^"]*PSCDocumentSearchControl1\$docketNumber)"',
        html,
    )
    if match:
        full_name = match.group(1)
        return full_name[: len(full_name) - len(_FIELD_DOCKET)]
    return "ctl00$ContentPlaceHolder1$PortalPageControl1$ctl86$"


# ---------------------------------------------------------------------------
# Result page parser
# ---------------------------------------------------------------------------

def _parse_results_page(
    html: str, company_name: str
) -> list[DocParamSearchResult]:
    """
    Parse the DocumentsParameterSearch result page.

    Each result is rendered as its own nested <table> within the outer page.
    The outer container table has ~90 rows; each result sub-table has ~6 rows.

    Structure of first row of each result table (confirmed 2026-03-18):
      cell[1] = description text
      cell[2] = filing type code
      cell[4] = "Filed In: E-2 Sub XXXX" + docket link
      cell[5] = "Date Filed: MM/DD/YYYY"
      cell[6] = empty cell, href = PSCDocumentDetailsPageNCUC.aspx?DocumentId=GUID
    """
    soup = BeautifulSoup(html, "lxml")

    # Result sub-tables each contain exactly one PSCDocumentDetailsPage link
    result_tables = [
        t for t in soup.find_all("table")
        if t.find("a", href=re.compile(r"PSCDocumentDetailsPageNCUC", re.I))
    ]

    results: list[DocParamSearchResult] = []
    seen_ids: set[str] = set()

    for table in result_tables:
        rows = table.find_all("tr")
        if not rows:
            continue

        # Use the first data row (the one with the most cells)
        data_row = max(rows, key=lambda r: len(r.find_all(["td", "th"])))
        cells = data_row.find_all(["td", "th"])
        if len(cells) < 5:
            continue

        description = ""
        doc_type = ""
        date_filed = ""
        docket_number = ""
        docket_id = ""
        document_detail_url = None
        document_id = ""

        # cell[1] = description
        if len(cells) > 1:
            description = cells[1].get_text(" ", strip=True)[:300]

        # cell[2] = filing type
        if len(cells) > 2:
            doc_type = cells[2].get_text(" ", strip=True)[:40]

        # cell[4] = "Filed In: E-2 Sub XXXX" + docket link
        if len(cells) > 4:
            fi_text = cells[4].get_text(" ", strip=True)
            m = re.search(r"E-\d+(?:\s+Sub\s+\d+)?", fi_text)
            if m:
                docket_number = m.group(0).strip()
            docket_link = cells[4].find("a", href=re.compile(r"DocketId=", re.I))
            if docket_link:
                dm = re.search(r"DocketId=([a-f0-9-]{36})", docket_link["href"], re.I)
                if dm:
                    docket_id = dm.group(1)

        # cell[5] = "Date Filed: MM/DD/YYYY"
        if len(cells) > 5:
            dt_text = cells[5].get_text(" ", strip=True)
            m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", dt_text)
            if m:
                date_filed = m.group(1)

        # cell[6] = document detail link
        if len(cells) > 6:
            doc_link = cells[6].find("a", href=re.compile(r"PSCDocumentDetailsPageNCUC", re.I))
            if doc_link:
                document_detail_url = _make_absolute(doc_link["href"])
                dm = re.search(r"DocumentId=([a-f0-9-]{36})", document_detail_url, re.I)
                if dm:
                    document_id = dm.group(1)

        # Fallback: scan all cells for PSCDocumentDetails link
        if not document_detail_url:
            for cell in cells:
                doc_link = cell.find("a", href=re.compile(r"PSCDocumentDetailsPageNCUC", re.I))
                if doc_link:
                    document_detail_url = _make_absolute(doc_link["href"])
                    dm = re.search(r"DocumentId=([a-f0-9-]{36})", document_detail_url, re.I)
                    if dm:
                        document_id = dm.group(1)
                    break

        if not description and not document_detail_url:
            continue

        # Deduplicate by document_id
        key = document_id or description[:60]
        if key in seen_ids:
            continue
        seen_ids.add(key)

        results.append(DocParamSearchResult(
            description=description,
            doc_type=doc_type,
            date_filed=date_filed,
            docket_number=docket_number,
            docket_id=docket_id,
            company_name=company_name,
            document_detail_url=document_detail_url,
            document_id=document_id,
        ))

    return results


def _make_absolute(url: str) -> str:
    if url.startswith("http"):
        return url
    base = "https://starw1.ncuc.gov"
    return base + (url if url.startswith("/") else "/NCUC/" + url)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def _find_next_page_target(html: str, *, current_page: int) -> str | None:
    """
    Find the ASP.NET __doPostBack target for the next result page.

    The portal uses numbered pager links plus an ellipsis control instead of an
    explicit ``Next`` link.  The current page number is omitted from the pager,
    so we look for the next numeric page first and fall back to the ellipsis
    control when advancing past the visible page window.
    """
    soup = BeautifulSoup(html, "lxml")
    numeric_targets: dict[int, str] = {}
    ellipsis_target: str | None = None

    for a in soup.find_all("a"):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if "__doPostBack" not in href:
            continue
        m = re.search(r"__doPostBack\('([^']+)'", href)
        if not m:
            continue
        target = m.group(1)
        if text.isdigit():
            numeric_targets[int(text)] = target
        elif text == "...":
            ellipsis_target = target

    if current_page + 1 in numeric_targets:
        return numeric_targets[current_page + 1]
    return ellipsis_target


# ---------------------------------------------------------------------------
# Metadata enrichment
# ---------------------------------------------------------------------------

def _enrich_results(results: list[DocParamSearchResult]) -> None:
    from duke_rates.historical.ncuc.metadata import (
        extract_schedule_codes,
        extract_rider_codes,
        classify_filing,
    )
    for r in results:
        text = f"{r.description} {r.doc_type}"
        r.extracted_schedule_codes = extract_schedule_codes(text)
        r.extracted_rider_codes = extract_rider_codes(text)
        cls = classify_filing(text)
        r.filing_classification = cls.value if cls else ""


def fetch_document_detail(page: "Page", document_detail_url: str) -> dict:
    """
    Visit a PSCDocumentDetails page and extract linked ViewFile URLs plus synopsis.
    """
    page.goto(document_detail_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(1)
    soup = BeautifulSoup(page.content(), "lxml")

    view_file_urls: list[str] = []
    view_file_labels: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "ViewFile" not in href and "GetFile" not in href and not href.lower().endswith(".pdf"):
            continue
        full_href = _make_absolute(href)
        label = a.get_text(" ", strip=True)
        if full_href not in view_file_urls:
            view_file_urls.append(full_href)
            view_file_labels.append(label)

    synopsis = ""
    text = soup.get_text("\n", strip=True)
    match = re.search(r"Synopsis:\s*(.+?)\s*Files\s*:", text, re.I | re.S)
    if match:
        synopsis = " ".join(match.group(1).split())

    return {
        "view_file_urls": view_file_urls,
        "view_file_labels": view_file_labels,
        "synopsis": synopsis,
    }


# ---------------------------------------------------------------------------
# Display helper
# ---------------------------------------------------------------------------

def print_doc_param_results(
    results: list[DocParamSearchResult],
    top_n: int = 200,
    only_tariff_related: bool = False,
) -> None:
    if only_tariff_related:
        results = [r for r in results if r.is_tariff_related()]

    print(f"\n{'=' * 80}")
    print(f"DocumentsParameterSearch Results  ({len(results)} total)")
    print(f"{'=' * 80}")
    print(f"{'#':>4}  {'Date':^12}  {'Type':^10}  {'Docket':^16}  Description")
    print("-" * 80)

    for i, r in enumerate(results[:top_n], 1):
        desc = r.description[:52].strip()
        codes = ""
        if r.extracted_schedule_codes:
            codes = " [sched:" + ",".join(r.extracted_schedule_codes[:3]) + "]"
        if r.extracted_rider_codes:
            codes += " [rider:" + ",".join(r.extracted_rider_codes[:3]) + "]"
        print(f"{i:>4}  {r.date_filed:^12}  {r.doc_type[:10]:^10}  {r.docket_number[:16]:^16}  {desc}{codes}")

    print()
