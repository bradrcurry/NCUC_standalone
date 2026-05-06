"""Google Custom Search Engine (CSE) API client.

Wraps the JSON API at https://customsearch.googleapis.com/customsearch/v1
Docs: https://developers.google.com/custom-search/v1/reference/rest/v1/cse/list

To use:
    1. Create a Programmable Search Engine at https://programmablesearchengine.google.com/
    2. Enable "Search the entire web" and set domains/site-restrict as desired
    3. Obtain an API key from Google Cloud Console (Custom Search JSON API)
    4. Set DUKE_RATES_GOOGLE_API_KEY and DUKE_RATES_GOOGLE_CSE_ID in .env

Free tier: 100 queries/day.  Paid: $5 per 1000 queries up to 10k/day.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

_CSE_ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"
_MAX_RESULTS_PER_PAGE = 10  # API hard limit


@dataclass
class CseSearchResult:
    query: str
    title: str
    url: str
    snippet: str
    file_format: str | None  # "PDF" when Google identifies the file type
    mime_type: str | None
    hostname: str
    path: str
    filename: str


@dataclass
class CseSearchResponse:
    query: str
    total_results: int
    items: list[CseSearchResult] = field(default_factory=list)
    next_page_start: int | None = None
    quota_exhausted: bool = False


class GoogleCseClient:
    """Thin client for Google Custom Search JSON API."""

    def __init__(
        self,
        *,
        api_key: str,
        cse_id: str,
        timeout: float = 20.0,
        rate_limit_seconds: float = 1.1,
        user_agent: str = "duke-rates/0.1",
    ):
        self.api_key = api_key
        self.cse_id = cse_id
        self.rate_limit_seconds = rate_limit_seconds
        self._last_call: float = 0.0
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        start: int = 1,
        num: int = 10,
    ) -> CseSearchResponse:
        """Execute one page of results (up to 10 items)."""
        self._throttle()
        params: dict[str, str | int] = {
            "key": self.api_key,
            "cx": self.cse_id,
            "q": query,
            "start": start,
            "num": min(num, _MAX_RESULTS_PER_PAGE),
        }
        response = self.client.get(_CSE_ENDPOINT, params=params)

        # 429 = quota exhausted
        if response.status_code == 429:
            return CseSearchResponse(query=query, total_results=0, quota_exhausted=True)

        response.raise_for_status()
        data = response.json()

        total = int(data.get("searchInformation", {}).get("totalResults", 0))
        items = [_parse_item(query, item) for item in data.get("items", [])]

        next_start: int | None = None
        queries = data.get("queries", {})
        next_page = queries.get("nextPage", [{}])
        if next_page:
            next_start = next_page[0].get("startIndex")

        return CseSearchResponse(
            query=query,
            total_results=total,
            items=items,
            next_page_start=next_start,
        )

    def search_all_pages(
        self,
        query: str,
        *,
        max_results: int = 30,
    ) -> CseSearchResponse:
        """Fetch up to max_results across multiple pages (max 3 pages = 30 results)."""
        combined: list[CseSearchResult] = []
        total = 0
        start = 1
        while start and len(combined) < max_results:
            page = self.search(query, start=start)
            if page.quota_exhausted:
                return CseSearchResponse(
                    query=query,
                    total_results=0,
                    items=combined,
                    quota_exhausted=True,
                )
            total = page.total_results
            combined.extend(page.items)
            start = page.next_page_start if page.next_page_start else 0  # type: ignore[assignment]
        return CseSearchResponse(query=query, total_results=total, items=combined[:max_results])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)
        self._last_call = time.monotonic()


def _parse_item(query: str, item: dict) -> CseSearchResult:
    url = item.get("link", "")
    parsed = urlparse(url)
    path = parsed.path
    filename = path.rsplit("/", 1)[-1] if "/" in path else path

    # Google sets fileFormat = "PDF" for PDF hits
    file_format = item.get("fileFormat")
    mime_type: str | None = None
    page_map = item.get("pagemap", {})
    meta_tags = page_map.get("metatags", [{}])
    if meta_tags:
        mime_type = meta_tags[0].get("og:type")

    return CseSearchResult(
        query=query,
        title=item.get("title", ""),
        url=url,
        snippet=item.get("snippet", ""),
        file_format=file_format,
        mime_type=mime_type,
        hostname=parsed.hostname or "",
        path=path,
        filename=filename,
    )
