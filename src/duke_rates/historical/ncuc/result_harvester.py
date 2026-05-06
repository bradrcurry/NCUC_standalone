"""
Stage 3: Search result harvesting.

Submits generated search queries against the NCUC public Zoom search
(https://www.ncuc.gov/search/search.php), parses returned result rows,
and yields structured SearchResult objects.

Also handles deduplication across multiple queries in a single session.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from duke_rates.config import Settings
from duke_rates.historical.ncuc.query_builder import QuerySpec
from duke_rates.historical.ncuc.query_syntax import sanitize_ncuc_query
from duke_rates.historical.ncuc.metadata import (
    classify_filing,
    extract_docket_from_text,
    extract_docket_from_url,
    extract_leaf_nos,
    extract_rider_codes,
    extract_schedule_codes,
    normalize_filing_date,
)

logger = logging.getLogger(__name__)

NCUC_ZOOM_SEARCH = "https://www.ncuc.gov/search/search.php"
NCUC_PUBLIC_BASE = "https://www.ncuc.gov"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """A single result row from the NCUC full-text search."""
    url: str
    title: str | None
    snippet: str | None
    filing_date: str | None
    docket_number: str | None
    sub_number: str | None
    source_query: str
    source_template: str
    utility_hint: str | None
    doc_type_hint: str | None
    schedule_code_hint: str | None
    rider_code_hint: str | None

    # Extracted metadata
    extracted_schedule_codes: list[str] = field(default_factory=list)
    extracted_rider_codes: list[str] = field(default_factory=list)
    extracted_leaf_nos: list[str] = field(default_factory=list)
    filing_classification: str = "other"

    # Session tracking
    discovered_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    found_by_queries: list[str] = field(default_factory=list)  # all queries that returned this

    # Content fingerprint for deduplication
    url_hash: str = ""

    def __post_init__(self):
        if not self.url_hash:
            self.url_hash = hashlib.sha1(self.url.encode()).hexdigest()[:16]

    def text_for_scoring(self) -> str:
        """Combined text blob for local scoring."""
        parts = [
            self.title or "",
            self.snippet or "",
            self.url,
            self.docket_number or "",
        ]
        return " ".join(p for p in parts if p)


def _result_key(url: str) -> str:
    """Canonical deduplication key for a URL."""
    parsed = urlparse(url)
    # Normalize: lowercase scheme+host+path, keep query
    return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path}?{parsed.query}".rstrip("?")


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _parse_zoom_results(html: str, page_url: str) -> list[dict]:
    """
    Parse NCUC Zoom search result HTML into raw dicts.

    The NCUC Zoom search (www.ncuc.gov/search/search.php) structures each result as:
      <div class="result_block"> or <div class="result_altblock">
        <div class="result_title"><b>N.</b> <a href="URL">TITLE</a></div>
        <div class="context"> ...snippet with <span class="highlight"> tags... </div>
        <div class="infoline">Terms matched: N - Score: N - DD Mon YYYY - URL: ...</div>
      </div>

    Returns list of {title, url, snippet, date} dicts.
    """
    soup = BeautifulSoup(html, "lxml")
    raw: list[dict] = []

    # Primary: target the exact NCUC Zoom result block classes
    for container in soup.find_all(
        "div",
        class_=re.compile(r"^result_(alt)?block$", re.I),
    ):
        entry = _extract_ncuc_result_block(container, page_url)
        if entry:
            raw.append(entry)

    # Fallback: generic Zoom class names or link scan
    if not raw:
        for container in soup.find_all(
            ["div", "li"],
            class_=re.compile(r"zoom_result|result[-_]item", re.I),
        ):
            entry = _extract_zoom_result_entry(container, page_url)
            if entry:
                raw.append(entry)

    if not raw:
        raw = _fallback_link_scan(soup, page_url)

    return raw


def _extract_ncuc_result_block(container: Tag, page_url: str) -> dict | None:
    """
    Extract a result from an NCUC Zoom result_block or result_altblock div.

    Structure:
      div.result_title > a[href]       → URL + title
      div.context                       → snippet (may contain highlight spans)
      div.infoline                      → date and URL info line
    """
    # Title + URL from result_title div
    title_div = container.find("div", class_="result_title")
    if not title_div:
        return None
    link = title_div.find("a", href=True)
    if not link:
        return None

    href = link.get("href", "").strip()
    if not href or href.startswith("#") or href.startswith("javascript"):
        return None

    url = href if href.startswith("http") else urljoin(page_url, href)

    # Skip search-internal navigation links
    if "/search/search.php" in url or "zoom_query=" in url:
        return None

    # Title: link text (the filename or page title)
    title = link.get_text(strip=True) or None

    # Snippet from context div — strip HTML tags, keep highlight text
    snippet = None
    context_div = container.find("div", class_="context")
    if context_div:
        # Replace <b>...</b> with context, strip <span> tags
        snippet = context_div.get_text(" ", strip=True)[:600]

    # Date and additional URL from infoline div
    filing_date = None
    infoline_url = None
    infoline_div = container.find("div", class_="infoline")
    if infoline_div:
        infoline_text = infoline_div.get_text(" ", strip=True)
        # Format: "Terms matched: N - Score: N - DD Mon YYYY - URL: ..."
        # Also handles: "23 Jul 2021" or "5 Mar 2024" style dates
        date_m = re.search(
            r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})\b",
            infoline_text,
            re.I,
        )
        if date_m:
            try:
                from dateutil import parser as _dp
                filing_date = _dp.parse(date_m.group(1)).strftime("%Y-%m-%d")
            except Exception:
                pass

        # Also try URL from infoline (sometimes more canonical than the link href)
        url_m = re.search(r"URL:\s*(https?://\S+)", infoline_text)
        if url_m:
            infoline_url = url_m.group(1).rstrip(".")

    # Prefer the infoline URL if it differs (sometimes the link is relative but infoline is absolute)
    final_url = infoline_url or url

    return {
        "url": final_url,
        "title": title,
        "snippet": snippet,
        "filing_date": filing_date,
    }


def _extract_zoom_result_entry(container: Tag, page_url: str) -> dict | None:
    """Generic Zoom result entry extractor (fallback for non-NCUC Zoom variants)."""
    link = container.find("a", href=True)
    if not link:
        return None

    href = link.get("href", "")
    if not href or href.startswith("#") or href.startswith("javascript"):
        return None

    url = href if href.startswith("http") else urljoin(page_url, href)

    skip_patterns = ["/search/search.php", "zoom_query=", "javascript:", "mailto:"]
    if any(p in url for p in skip_patterns):
        return None

    title = link.get_text(strip=True) or None

    snippet_parts = []
    for elem in container.children:
        if isinstance(elem, Tag) and elem.name == "a":
            continue
        text = elem.get_text(strip=True) if isinstance(elem, Tag) else str(elem).strip()
        if text:
            snippet_parts.append(text)
    snippet = " ".join(snippet_parts)[:500] if snippet_parts else None

    container_text = container.get_text(" ", strip=True)
    date_m = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b", container_text)
    filing_date = normalize_filing_date(date_m.group(1)) if date_m else None

    return {"url": url, "title": title, "snippet": snippet, "filing_date": filing_date}


def _fallback_link_scan(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """
    Fallback: scan all <a> tags for document-like links.
    More permissive, produces more noise.
    """
    results = []
    doc_patterns = [
        r"DocketDetails", r"ViewFile", r"GetDocument", r"\.pdf",
        r"docid=", r"fileid=", r"PSC/", r"NCUC/",
    ]
    pat = re.compile("|".join(doc_patterns), re.IGNORECASE)

    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pat.search(href):
            continue
        url = href if href.startswith("http") else urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)

        title = a.get_text(strip=True) or None
        parent_text = ""
        parent = a.find_parent(["tr", "div", "li", "p"])
        if parent:
            parent_text = parent.get_text(" ", strip=True)

        date_m = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b", parent_text)
        filing_date = normalize_filing_date(date_m.group(1)) if date_m else None

        snippet = parent_text[:300] if parent_text else None
        results.append({
            "url": url,
            "title": title,
            "snippet": snippet,
            "filing_date": filing_date,
        })
    return results


def _detect_search_error(html: str) -> tuple[bool, str]:
    """Return (error, snippet) from the response body."""
    error_pats = [
        re.compile(r"sql\s+(?:syntax\s+)?error", re.I),
        re.compile(r"you have an error in your sql", re.I),
        re.compile(r"parse\s+error", re.I),
        re.compile(r"internal\s+server\s+error", re.I),
        re.compile(r"query\s+failed", re.I),
        re.compile(r"error\s+executing\s+query", re.I),
    ]
    for pat in error_pats:
        m = pat.search(html)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(html), m.end() + 100)
            return True, html[start:end].strip()
    return False, ""


# ---------------------------------------------------------------------------
# The harvester
# ---------------------------------------------------------------------------

class SearchResultHarvester:
    """
    Submits queries to the NCUC Zoom search and yields SearchResult objects.
    Deduplicates across multiple queries in a session.
    """

    def __init__(self, settings: Settings, delay_seconds: float = 1.0):
        self.settings = settings
        self.delay_seconds = delay_seconds
        self._session_cache: dict[str, SearchResult] = {}  # url_key → result
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Referer": "https://www.ncuc.gov/search/",
            },
        )
        self._safe_pattern_types = {"single_term", "two_term"}

    def set_safe_pattern_types(self, safe_pattern_types: set[str]) -> None:
        if safe_pattern_types:
            self._safe_pattern_types = set(safe_pattern_types)

    def close(self) -> None:
        self._client.close()

    def reset_session(self) -> None:
        """Clear per-session deduplication cache."""
        self._session_cache.clear()

    def harvest_query(
        self,
        query_spec: QuerySpec,
        *,
        per_page: int = 20,
        max_pages: int = 3,
    ) -> tuple[list[SearchResult], bool, str]:
        """
        Execute a single query spec and return (results, had_error, error_snippet).

        Returns:
            results: List of SearchResult objects (deduplicated within session)
            had_error: True if the search UI returned an error response
            error_snippet: Excerpt of the error text if had_error
        """
        all_results: list[SearchResult] = []
        had_error = False
        error_snippet = ""

        for page_num in range(max_pages):
            safe_query = sanitize_ncuc_query(
                query_spec.query_text,
                safe_pattern_types=self._safe_pattern_types,
            )
            start = page_num * per_page
            params = {
                "zoom_query": safe_query or query_spec.query_text,
                "zoom_sort": "0",
                "zoom_cat[]": "-1",
                "zoom_per_page": str(per_page),
                "zoom_start": str(start),
            }

            if page_num > 0:
                time.sleep(self.delay_seconds)

            try:
                resp = self._client.get(NCUC_ZOOM_SEARCH, params=params)
            except Exception as exc:
                logger.warning("Search request failed for query %r: %s", query_spec.query_text, exc)
                had_error = True
                error_snippet = str(exc)[:200]
                break

            if resp.status_code != 200:
                logger.warning(
                    "NCUC search HTTP %s for query %r",
                    resp.status_code, query_spec.query_text,
                )
                had_error = True
                error_snippet = f"HTTP {resp.status_code}"
                break

            html = resp.text
            error_found, err_snip = _detect_search_error(html)
            if error_found:
                logger.warning(
                    "SQL/parse error detected for query %r: %s",
                    query_spec.query_text, err_snip[:80],
                )
                had_error = True
                error_snippet = err_snip
                break

            raw_rows = _parse_zoom_results(html, str(resp.url))
            if not raw_rows:
                # No more results on this page
                break

            page_results = []
            for row in raw_rows:
                result = self._build_result(row, query_spec)
                if result is None:
                    continue

                url_key = _result_key(result.url)
                if url_key in self._session_cache:
                    # Already seen — just record this query found it too
                    existing = self._session_cache[url_key]
                    if query_spec.query_text not in existing.found_by_queries:
                        existing.found_by_queries.append(query_spec.query_text)
                else:
                    self._session_cache[url_key] = result
                    page_results.append(result)

            all_results.extend(page_results)

            # If we got fewer results than per_page, we've seen all pages
            if len(raw_rows) < per_page:
                break

        logger.info(
            "Query %r → %d new results (had_error=%s)",
            query_spec.query_text[:60], len(all_results), had_error,
        )
        return all_results, had_error, error_snippet

    def harvest_all(
        self,
        query_specs: list[QuerySpec],
        *,
        delay_between_queries: float | None = None,
        per_page: int = 20,
        max_pages: int = 3,
    ) -> "HarvestSession":
        """
        Execute all queries and return a HarvestSession with all results.
        """
        delay = delay_between_queries if delay_between_queries is not None else self.delay_seconds
        session = HarvestSession()

        for i, qs in enumerate(query_specs, 1):
            logger.info("[%d/%d] Searching: %r", i, len(query_specs), qs.query_text[:70])
            results, had_error, err_snip = self.harvest_query(qs, per_page=per_page, max_pages=max_pages)

            session.record_query(
                query_spec=qs,
                new_results=results,
                had_error=had_error,
                error_snippet=err_snip,
            )

            if i < len(query_specs):
                time.sleep(delay)

        return session

    def _build_result(self, row: dict, query_spec: QuerySpec) -> SearchResult | None:
        """Convert a raw result dict to a SearchResult."""
        url = row.get("url", "").strip()
        if not url:
            return None

        title = row.get("title")
        snippet = row.get("snippet")
        filing_date = row.get("filing_date")

        # Extract metadata from title + snippet + URL
        combined = " ".join(filter(None, [title, snippet, url]))
        docket, sub = extract_docket_from_url(url)
        if not docket:
            docket, sub = extract_docket_from_text(combined)

        schedule_codes = extract_schedule_codes(combined)
        rider_codes = extract_rider_codes(combined)
        leaf_nos = extract_leaf_nos(combined)
        classification = classify_filing((title or "") + " " + (snippet or ""))

        return SearchResult(
            url=url,
            title=title,
            snippet=snippet,
            filing_date=filing_date,
            docket_number=docket,
            sub_number=sub,
            source_query=query_spec.query_text,
            source_template=query_spec.template_name,
            utility_hint=query_spec.utility_hint,
            doc_type_hint=query_spec.doc_type_hint,
            schedule_code_hint=query_spec.schedule_code_hint,
            rider_code_hint=query_spec.rider_code_hint,
            extracted_schedule_codes=schedule_codes,
            extracted_rider_codes=rider_codes,
            extracted_leaf_nos=leaf_nos,
            filing_classification=classification.value if hasattr(classification, "value") else str(classification),
            found_by_queries=[query_spec.query_text],
        )


# ---------------------------------------------------------------------------
# Session container
# ---------------------------------------------------------------------------

@dataclass
class QuerySessionRecord:
    """Summary of one query's execution."""
    query_text: str
    template_name: str
    new_result_count: int
    had_error: bool
    error_snippet: str
    executed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class HarvestSession:
    """
    Container for all results from a multi-query harvest session.
    Provides aggregated access and statistics.
    """

    def __init__(self):
        self._results: dict[str, SearchResult] = {}  # url_key → SearchResult
        self._query_records: list[QuerySessionRecord] = []

    def record_query(
        self,
        query_spec: QuerySpec,
        new_results: list[SearchResult],
        had_error: bool,
        error_snippet: str,
    ) -> None:
        for r in new_results:
            key = _result_key(r.url)
            self._results[key] = r

        self._query_records.append(QuerySessionRecord(
            query_text=query_spec.query_text,
            template_name=query_spec.template_name,
            new_result_count=len(new_results),
            had_error=had_error,
            error_snippet=error_snippet,
        ))

    def merge(self, other: "HarvestSession") -> None:
        """Merge another HarvestSession into this one, deduplicating by canonical URL."""
        for result in other.all_results:
            key = _result_key(result.url)
            if key in self._results:
                existing = self._results[key]
                for query in result.found_by_queries:
                    if query not in existing.found_by_queries:
                        existing.found_by_queries.append(query)
            else:
                self._results[key] = result
        self._query_records.extend(other.query_records)

    @property
    def all_results(self) -> list[SearchResult]:
        return list(self._results.values())

    @property
    def query_records(self) -> list[QuerySessionRecord]:
        return list(self._query_records)

    @property
    def total_unique(self) -> int:
        return len(self._results)

    @property
    def error_queries(self) -> list[QuerySessionRecord]:
        return [r for r in self._query_records if r.had_error]

    def print_summary(self) -> None:
        total = len(self._query_records)
        errors = len(self.error_queries)
        print(f"\n=== Harvest Session Summary ===")
        print(f"Queries executed:  {total}")
        print(f"Queries with errors: {errors}")
        print(f"Unique results:    {self.total_unique}")
        print()
        print(f"{'Query':<60} {'New':>5} {'Error':>6}")
        print("-" * 75)
        for r in self._query_records:
            q = r.query_text[:57] + "..." if len(r.query_text) > 60 else r.query_text
            err_tag = "YES" if r.had_error else ""
            print(f"{q:<60} {r.new_result_count:>5} {err_tag:>6}")
