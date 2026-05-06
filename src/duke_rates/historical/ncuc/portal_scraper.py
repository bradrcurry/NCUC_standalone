"""
NCUC portal.aspx scraper and Wayback docket harvester.

Cloudflare analysis (confirmed via live probing 2026-03-15):
- starw1.ncuc.gov/NCUC/portal.aspx  → accessible via Playwright (200)
- starw1.ncuc.gov/NCUC/PSC/*        → Cloudflare-blocked (403) even from portal session
- starw1.ncuc.gov/NCUC/page/*       → Cloudflare-blocked (403) even from portal session
- starw1.ncuc.gov/NCUC/ViewFile.aspx → Cloudflare-blocked (403)
- All direct httpx requests to starw1 → 403

Working strategies:
1. portal.aspx recent orders panel (E-2 dockets visible)
2. MS Ajax postback to paginate portal.aspx recent orders
3. Wayback CDX + snapshot fetching for historical docket detail pages
4. ncuc.gov public Zoom search (returns navigation links only, not documents)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

import httpx
from bs4 import BeautifulSoup

from duke_rates.config import Settings
from duke_rates.historical.ncuc.metadata import (
    classify_filing,
    extract_docket_from_text,
    extract_leaf_nos,
    extract_rider_codes,
    extract_schedule_codes,
    normalize_filing_date,
    score_relevance,
)
from duke_rates.models.ncuc import (
    NcucAcquisitionMethod,
    NcucDiscoveryRecord,
    NcucFetchStatus,
)

logger = logging.getLogger(__name__)

PORTAL_URL = "https://starw1.ncuc.gov/NCUC/portal.aspx"
WAYBACK_CDX_URL = "http://web.archive.org/cdx/search/cdx"

# MS Ajax postback target for the recent orders grid
RECENT_ORDERS_GRID = (
    "ctl00$ContentPlaceHolder1$PortalPageControl1$ctl81$resultsGridView"
)

# Utility codes used by NCUC for Duke Energy Progress
DUKE_PROGRESS_UTILITY_CODES = {"E-2"}

# Keywords that signal relevance to Duke Energy Progress rate/rider content
DUKE_PROGRESS_KEYWORDS = {
    "duke energy progress",
    "progress energy carolinas",
    "carolina power",
    "cp&l",
    "joint agency",
    "rider",
    "rate case",
    "tariff",
    "DSM",
    "storm cost",
    "EDIT",
    "renewable energy",
    "clean energy",
}


@dataclass
class PortalEntry:
    """A single row from the NCUC portal.aspx recent orders/filings table."""

    order_date: str | None
    docket_number: str | None
    docket_description: str | None
    order_description: str | None
    docket_id: str | None  # GUID
    document_id: str | None  # GUID
    document_class: str | None
    docket_url: str | None
    document_url: str | None
    is_e2: bool = False


def _parse_portal_html(html: str, page_url: str = PORTAL_URL) -> list[PortalEntry]:
    """Parse the portal.aspx HTML and extract all table rows."""
    soup = BeautifulSoup(html, "lxml")
    entries: list[PortalEntry] = []

    # The recent orders table has pairs of rows: docket row + order description row
    # Structure: <td>date</td><td><a href=DocketDetails>docket</a>...</td><td><a href=PSCDocument>order</a></td>
    table = soup.find("table", id=re.compile(r"resultsGridView", re.I))
    if not table:
        # Try any table with docket links
        tables = soup.find_all("table")
        for t in tables:
            if t.find("a", href=re.compile(r"DocketDetails")):
                table = t
                break

    if not table:
        return entries

    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        date_text = cells[0].get_text(strip=True)
        docket_cell = cells[1]
        order_cell = cells[2]

        # Extract docket info
        docket_link = docket_cell.find("a", href=re.compile(r"DocketDetails"))
        docket_number = None
        docket_id = None
        docket_url = None
        docket_description = None

        if docket_link:
            docket_url = docket_link["href"]
            docket_text = docket_link.get_text(strip=True)
            # "E-2 Sub 1354" style
            docket_number = docket_text
            # Extract GUID from URL
            m = re.search(r"DocketId=([a-f0-9-]{36})", docket_url, re.I)
            if m:
                docket_id = m.group(1)
            # Get the rest of the cell text as description
            full_cell_text = docket_cell.get_text(" ", strip=True)
            desc_match = re.sub(re.escape(docket_text), "", full_cell_text, count=1).strip()
            docket_description = desc_match if desc_match else None

        # Extract document/order info
        order_link = order_cell.find("a", href=re.compile(r"PSCDocumentDetails"))
        document_id = None
        document_url = None
        document_class = None
        order_description = order_cell.get_text(strip=True)

        if order_link:
            document_url = order_link["href"]
            m2 = re.search(r"DocumentId=([a-f0-9-]{36})", document_url, re.I)
            if m2:
                document_id = m2.group(1)
            class_m = re.search(r"Class=(\w+)", document_url)
            if class_m:
                document_class = class_m.group(1)

        # Detect E-2 dockets
        is_e2 = bool(docket_number and re.match(r"E-2\b", docket_number))

        entry = PortalEntry(
            order_date=normalize_filing_date(date_text),
            docket_number=docket_number,
            docket_description=docket_description,
            order_description=order_description,
            docket_id=docket_id,
            document_id=document_id,
            document_class=document_class,
            docket_url=docket_url,
            document_url=document_url,
            is_e2=is_e2,
        )
        entries.append(entry)

    return entries


def _entry_to_discovery_record(entry: PortalEntry) -> NcucDiscoveryRecord:
    """Convert a PortalEntry to an NcucDiscoveryRecord."""
    combined_text = " ".join(
        filter(None, [entry.docket_number, entry.docket_description, entry.order_description])
    )
    schedule_codes = extract_schedule_codes(combined_text)
    rider_codes = extract_rider_codes(combined_text)
    leaf_nos = extract_leaf_nos(combined_text)
    classification = classify_filing(
        (entry.document_class or "") + " " + (entry.order_description or "")
    )

    sub_m = re.search(r"Sub\s+(\d+)", entry.docket_number or "", re.I)
    sub_number = sub_m.group(1) if sub_m else None

    return NcucDiscoveryRecord(
        docket_number=entry.docket_number,
        sub_number=sub_number,
        filing_title=entry.order_description,
        filing_date=entry.order_date,
        proceeding_type=entry.document_class,
        filing_classification=classification,
        referenced_schedule_codes=schedule_codes,
        referenced_rider_codes=rider_codes,
        referenced_leaf_nos=leaf_nos,
        discovered_url=entry.document_url or entry.docket_url,
        viewer_url=entry.document_url,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
        fetch_status=NcucFetchStatus.PENDING,
        provenance_notes=[
            f"source=portal.aspx",
            f"docket_id={entry.docket_id}",
            f"document_id={entry.document_id}",
            f"docket_desc={entry.docket_description or ''}",
        ],
        page_title=entry.order_description,
        metadata_json=json.dumps(
            {
                "docket_id": entry.docket_id,
                "document_id": entry.document_id,
                "document_class": entry.document_class,
                "docket_url": entry.docket_url,
                "document_url": entry.document_url,
            },
            sort_keys=True,
        ),
    )


class NcucPortalScraper:
    """
    Scrape NCUC portal.aspx using Playwright to collect docket and document entries.

    portal.aspx is the only URL on starw1.ncuc.gov accessible without Cloudflare
    challenge. All PSC/* and page/* sub-paths return 403.

    This scraper:
    1. Loads portal.aspx to get recent orders
    2. Uses MS Ajax postback to paginate through pages
    3. Filters for E-2 (Duke Energy Progress) dockets
    4. Returns NcucDiscoveryRecord entries for each document found
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def scrape_recent_orders(
        self,
        *,
        max_pages: int = 20,
        e2_only: bool = True,
        all_e_dockets: bool = False,
    ) -> list[NcucDiscoveryRecord]:
        """
        Scrape recent orders from portal.aspx, paginating through the result grid.

        Returns list of NcucDiscoveryRecord for each discovered document entry.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("Playwright not installed. Run: pip install 'duke-rates[browser]'")
            return []

        records: list[NcucDiscoveryRecord] = []
        seen_doc_ids: set[str] = set()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=self.settings.user_agent,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = ctx.new_page()

            logger.info("Loading NCUC portal: %s", PORTAL_URL)
            resp = page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
            if not resp or resp.status >= 400:
                logger.error("Failed to load portal: %s", resp.status if resp else "no response")
                browser.close()
                return records

            time.sleep(2)

            for page_num in range(1, max_pages + 1):
                logger.info("Scraping portal page %d", page_num)
                html = page.content()
                entries = _parse_portal_html(html)

                if not entries:
                    logger.info("No entries found on page %d — stopping", page_num)
                    break

                # Filter entries
                filtered = entries
                if e2_only:
                    filtered = [e for e in entries if e.is_e2]
                elif all_e_dockets:
                    filtered = [
                        e
                        for e in entries
                        if e.docket_number and re.match(r"E-\d+", e.docket_number)
                    ]

                for entry in filtered:
                    # Deduplicate by document_id
                    key = entry.document_id or entry.docket_id or entry.order_description
                    if key and key in seen_doc_ids:
                        continue
                    if key:
                        seen_doc_ids.add(key)

                    rec = _entry_to_discovery_record(entry)
                    rel = score_relevance(
                        rec.filing_title,
                        rec.docket_number,
                        rec.referenced_schedule_codes,
                        rec.referenced_rider_codes,
                    )
                    logger.info(
                        "  [%.2f] %s | %s | %s",
                        rel,
                        entry.docket_number,
                        entry.order_date,
                        entry.order_description[:60] if entry.order_description else "",
                    )
                    records.append(rec)

                # Check if there's a next page
                next_page = self._get_next_page_number(page, page_num)
                if next_page is None:
                    logger.info("No more pages after page %d", page_num)
                    break

                # Navigate to next page via click
                if not self._click_page(page, next_page):
                    logger.info("Could not navigate to page %d", next_page)
                    break
                time.sleep(2)

            browser.close()

        logger.info("Portal scrape complete: %d records collected", len(records))
        return records

    def _get_next_page_number(self, page, current: int) -> int | None:
        """Return the next page number if a pager link exists, else None."""
        next_num = current + 1
        # Check for a page link with that number
        selector = f'a:text-is("{next_num}")'
        try:
            link = page.query_selector(selector)
            return next_num if link else None
        except Exception:
            return None

    def _click_page(self, page, page_num: int) -> bool:
        """Click the page number link in the grid pager."""
        selector = f'a:text-is("{page_num}")'
        try:
            link = page.query_selector(selector)
            if not link:
                return False
            link.click()
            # Wait for the MS Ajax update
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                time.sleep(3)  # fallback wait if networkidle times out
            return True
        except Exception as exc:
            logger.warning("Page click failed for page %d: %s", page_num, exc)
            return False


class NcucWaybackHarvester:
    """
    Harvest NCUC document URLs from Wayback Machine CDX index.

    Uses the CDX API to find archived NCUC DocketDetails pages, then
    fetches Wayback snapshots of those pages to extract filing lists.

    The content on these pages is JS-rendered so actual document extraction
    requires Playwright on the archived snapshot URLs.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=self.settings.request_timeout,
            headers={"User-Agent": self.settings.user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def harvest_docket_guids(
        self,
        *,
        url_pattern: str = "starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx*",
        limit: int = 500,
    ) -> list[dict]:
        """
        Query Wayback CDX for archived NCUC DocketDetails URLs.
        Returns list of dicts: {original_url, timestamp, docket_id}.
        """
        params = {
            "url": url_pattern,
            "output": "json",
            "limit": str(limit),
            "fl": "original,timestamp,statuscode",
            "filter": "statuscode:200",
            "collapse": "original",
        }
        logger.info("Querying Wayback CDX for NCUC docket URLs")
        try:
            resp = self._client.get(WAYBACK_CDX_URL, params=params)
        except Exception as exc:
            logger.warning("Wayback CDX query failed: %s", exc)
            return []

        if resp.status_code != 200:
            return []

        try:
            rows = json.loads(resp.text)
        except Exception:
            return []

        results = []
        for row in rows[1:]:  # skip header
            original_url = row[0]
            timestamp = row[1]
            # Extract DocketId GUID
            m = re.search(r"DocketId=([a-f0-9-]{36})", original_url, re.I)
            docket_id = m.group(1) if m else None
            results.append(
                {
                    "original_url": original_url,
                    "timestamp": timestamp,
                    "docket_id": docket_id,
                    "wayback_url": f"https://web.archive.org/web/{timestamp}/{original_url}",
                }
            )
        return results

    def fetch_wayback_snapshot_with_playwright(
        self,
        wayback_url: str,
        *,
        docket_hint: str | None = None,
    ) -> list[NcucDiscoveryRecord]:
        """
        Fetch a Wayback Machine snapshot of a DocketDetails page via Playwright
        and extract document links.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not installed")
            return []

        records: list[NcucDiscoveryRecord] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=self.settings.user_agent,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            try:
                resp = page.goto(wayback_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(4)
            except Exception as exc:
                logger.warning("Playwright snapshot fetch failed: %s", exc)
                browser.close()
                return records

            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "lxml")
        # Check if the page has actual content (not blank JS-rendered)
        visible_text = soup.get_text(" ", strip=True)
        if len(visible_text) < 200:
            logger.info("Wayback snapshot appears blank (JS-rendered): %s", wayback_url)
            return records

        # Extract document links from DocketDetails page
        doc_links = soup.find_all("a", href=re.compile(r"PSCDocumentDetails|ViewFile|GetFile|\.pdf", re.I))
        for a in doc_links:
            href = a["href"]
            title = a.get_text(strip=True)
            if not href.startswith("http"):
                href = "https://starw1.ncuc.gov" + href

            combined = (title + " " + (docket_hint or "")).strip()
            docket, sub = extract_docket_from_text(combined)
            if not docket and docket_hint:
                docket = docket_hint

            m = re.search(r"DocumentId=([a-f0-9-]{36})", href, re.I)
            document_id = m.group(1) if m else None

            rec = NcucDiscoveryRecord(
                docket_number=docket,
                filing_title=title,
                discovered_url=href,
                viewer_url=href if "Document" in href or "ViewFile" in href else None,
                download_url=href if ".pdf" in href.lower() else None,
                acquisition_method=NcucAcquisitionMethod.DOCKET_SCRAPE,
                fetch_status=NcucFetchStatus.PENDING,
                filing_classification=classify_filing(title + " " + href),
                provenance_notes=[
                    f"source=wayback_snapshot",
                    f"wayback_url={wayback_url}",
                    f"document_id={document_id}",
                ],
                metadata_json=json.dumps({"document_id": document_id, "wayback_url": wayback_url}),
            )
            records.append(rec)

        return records


def scrape_portal_and_persist(
    settings: Settings,
    repository,
    *,
    max_pages: int = 20,
    e2_only: bool = True,
) -> list[NcucDiscoveryRecord]:
    """
    Convenience function: scrape portal.aspx and persist all records.
    Returns persisted records with ids.
    """
    scraper = NcucPortalScraper(settings)
    raw_records = scraper.scrape_recent_orders(max_pages=max_pages, e2_only=e2_only)

    persisted = []
    for rec in raw_records:
        rec_id = repository.upsert_ncuc_discovery_record(rec)
        persisted.append(rec.model_copy(update={"id": rec_id}))

    return persisted
