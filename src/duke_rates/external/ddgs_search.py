"""DuckDuckGo search client wrapping the `ddgs` package.

Drop-in replacement for GoogleCseClient — same result shape, no API key required.

Install: pip install ddgs
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException


@dataclass
class DdgsSearchResult:
    query: str
    title: str
    url: str
    snippet: str
    file_format: str | None   # "PDF" when URL ends .pdf
    mime_type: str | None
    hostname: str
    path: str
    filename: str


@dataclass
class DdgsSearchResponse:
    query: str
    total_results: int
    items: list[DdgsSearchResult] = field(default_factory=list)
    quota_exhausted: bool = False
    rate_limited: bool = False


class DdgsSearchClient:
    """DuckDuckGo text search client with the same interface as GoogleCseClient."""

    def __init__(
        self,
        *,
        rate_limit_seconds: float = 2.0,
        max_retries: int = 2,
    ):
        self.rate_limit_seconds = rate_limit_seconds
        self.max_retries = max_retries
        self._last_call: float = 0.0

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
    ) -> DdgsSearchResponse:
        self._throttle()
        for attempt in range(self.max_retries + 1):
            try:
                with DDGS() as ddgs:
                    raw = ddgs.text(
                        query,
                        max_results=max_results,
                        safesearch="off",
                        region="us-en",
                    ) or []
                items = [_parse_item(query, r) for r in raw]
                return DdgsSearchResponse(
                    query=query,
                    total_results=len(items),
                    items=items,
                )
            except RatelimitException:
                if attempt < self.max_retries:
                    time.sleep(5.0 * (attempt + 1))
                else:
                    return DdgsSearchResponse(
                        query=query,
                        total_results=0,
                        rate_limited=True,
                    )
            except DDGSException:
                return DdgsSearchResponse(query=query, total_results=0)

    # Alias so dork_runner can call the same method name as GoogleCseClient
    def search_all_pages(
        self,
        query: str,
        *,
        max_results: int = 30,
    ) -> DdgsSearchResponse:
        return self.search(query, max_results=max_results)

    def close(self) -> None:
        pass  # DDGS uses context manager internally; nothing to close

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)
        self._last_call = time.monotonic()


def _parse_item(query: str, item: dict) -> DdgsSearchResult:
    url = item.get("href", "")
    parsed = urlparse(url)
    path = parsed.path
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    file_format = "PDF" if url.lower().endswith(".pdf") else None

    return DdgsSearchResult(
        query=query,
        title=item.get("title", ""),
        url=url,
        snippet=item.get("body", ""),
        file_format=file_format,
        mime_type=None,
        hostname=parsed.hostname or "",
        path=path,
        filename=filename,
    )
