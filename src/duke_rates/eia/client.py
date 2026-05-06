"""EIA Open Data API v2 HTTP client.

Handles authentication, pagination, retry with exponential backoff, optional
local JSON caching, and metadata/facet discovery.

Usage::

    from duke_rates.eia.client import EIAClient
    from duke_rates.config import get_settings

    client = EIAClient(api_key=get_settings().eia_api_key)

    # Fetch all NC + SC residential retail-sales annual records
    records = client.fetch_all(
        "electricity/retail-sales",
        frequency="annual",
        data_cols=["sales", "revenue", "price", "customers"],
        facets={"stateid": ["NC", "SC"], "sectorid": ["RES"]},
    )
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://api.eia.gov/v2/"
_MAX_PAGE = 5000  # EIA hard cap per request


class EIAError(RuntimeError):
    """Raised when the EIA API returns an error response."""


class EIAClient:
    """Thin client for EIA API v2.

    Parameters
    ----------
    api_key:
        EIA registered API key.  If None the client will attempt requests
        without a key (only useful for very light exploration; will 403 quickly).
    timeout:
        HTTP request timeout in seconds.
    max_retries:
        Number of retry attempts on transient errors (429, 5xx).
    retry_delay:
        Base delay in seconds between retries (doubles each attempt).
    request_delay:
        Seconds to sleep between sequential requests (rate-limit courtesy).
    cache_dir:
        If set, raw JSON responses are written here as ``<hash>.json`` files.
        Subsequent identical requests are served from cache without hitting the
        network.  Useful for development and reproducible backfills.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        request_delay: float = 0.25,
        cache_dir: Path | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._request_delay = request_delay
        self._cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_metadata(self, route: str) -> dict:
        """Return the metadata envelope for *route* (facets, frequencies, etc.).

        Example::

            meta = client.get_metadata("electricity/retail-sales")
            for facet in meta["facets"]:
                print(facet["id"], facet["description"])
        """
        url = _BASE_URL + route.lstrip("/")
        return self._get(url)

    def fetch_page(
        self,
        route: str,
        *,
        frequency: str = "annual",
        data_cols: list[str],
        facets: dict[str, list[str]] | None = None,
        start: str | None = None,
        end: str | None = None,
        sort_col: str = "period",
        sort_dir: str = "asc",
        offset: int = 0,
        length: int = _MAX_PAGE,
    ) -> dict:
        """Fetch a single page of data records.

        Returns the raw API *response* dict (includes ``total``, ``data``,
        ``warnings``, etc.).
        """
        url = _BASE_URL + route.lstrip("/").rstrip("/") + "/data/"
        params = self._build_params(
            frequency=frequency,
            data_cols=data_cols,
            facets=facets or {},
            start=start,
            end=end,
            sort_col=sort_col,
            sort_dir=sort_dir,
            offset=offset,
            length=length,
        )
        return self._get(url, params=params)

    def fetch_all(
        self,
        route: str,
        *,
        frequency: str = "annual",
        data_cols: list[str],
        facets: dict[str, list[str]] | None = None,
        start: str | None = None,
        end: str | None = None,
        sort_col: str = "period",
        sort_dir: str = "asc",
    ) -> list[dict]:
        """Fetch every record matching the query, paginating automatically.

        Returns a flat list of raw record dicts as returned by EIA (all numeric
        values are strings — callers should cast as needed).
        """
        records: list[dict] = []
        offset = 0

        while True:
            resp = self.fetch_page(
                route,
                frequency=frequency,
                data_cols=data_cols,
                facets=facets,
                start=start,
                end=end,
                sort_col=sort_col,
                sort_dir=sort_dir,
                offset=offset,
                length=_MAX_PAGE,
            )
            page_data = resp.get("data", [])
            records.extend(page_data)

            total = int(resp.get("total", 0))
            offset += len(page_data)

            if offset >= total or not page_data:
                break

            log.debug("EIA paginating: %d / %d fetched from %s", offset, total, route)
            time.sleep(self._request_delay)

        return records

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_params(
        self,
        *,
        frequency: str,
        data_cols: list[str],
        facets: dict[str, list[str]],
        start: str | None,
        end: str | None,
        sort_col: str,
        sort_dir: str,
        offset: int,
        length: int,
    ) -> list[tuple[str, str]]:
        """Build ordered query parameter list for EIA v2 data endpoint."""
        params: list[tuple[str, str]] = []
        if self._api_key:
            params.append(("api_key", self._api_key))
        params.append(("frequency", frequency))
        for i, col in enumerate(data_cols):
            params.append((f"data[{i}]", col))
        for facet_id, values in facets.items():
            for v in values:
                params.append((f"facets[{facet_id}][]", v))
        if start:
            params.append(("start", start))
        if end:
            params.append(("end", end))
        params.append((f"sort[0][column]", sort_col))
        params.append((f"sort[0][direction]", sort_dir))
        params.append(("offset", str(offset)))
        params.append(("length", str(length)))
        return params

    def _get(self, url: str, params: list[tuple[str, str]] | None = None) -> dict:
        """Execute a GET request with retry logic and optional caching."""
        # Build cache key from url + params
        cache_key = None
        if self._cache_dir is not None:
            import hashlib
            raw = url + ("?" + urlencode(params) if params else "")
            cache_key = self._cache_dir / (hashlib.sha256(raw.encode()).hexdigest() + ".json")
            if cache_key.exists():
                log.debug("EIA cache hit: %s", cache_key.name)
                return json.loads(cache_key.read_text(encoding="utf-8"))

        # Add api_key to metadata requests (no params list)
        if params is None and self._api_key:
            params = [("api_key", self._api_key)]

        delay = self._retry_delay
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = httpx.get(url, params=params, timeout=self._timeout)  # type: ignore[arg-type]

                if resp.status_code == 429:
                    wait = delay * (2 ** attempt)
                    log.warning("EIA rate limit (429) — waiting %.1fs before retry %d", wait, attempt + 1)
                    time.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    wait = delay * (2 ** attempt)
                    log.warning("EIA server error %d — waiting %.1fs before retry %d", resp.status_code, wait, attempt + 1)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                body = resp.json()

                # EIA wraps all responses in {"response": {...}}
                data = body.get("response", body)

                if cache_key is not None:
                    cache_key.write_text(json.dumps(data, indent=2), encoding="utf-8")

                return data

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                wait = delay * (2 ** attempt)
                log.warning("EIA network error (%s) — waiting %.1fs before retry %d", exc, wait, attempt + 1)
                time.sleep(wait)

        raise EIAError(f"EIA request failed after {self._max_retries} retries: {url}") from last_exc
