from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


@dataclass
class WaybackSnapshot:
    timestamp: str
    original_url: str
    status_code: str
    mimetype: str

    @property
    def archive_url(self) -> str:
        return f"https://web.archive.org/web/{self.timestamp}/{self.original_url}"


class WaybackClient:
    def __init__(self, *, timeout: float = 30.0, user_agent: str = "duke-rates/0.1"):
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self.client.close()

    def lookup_pdf_revisions(
        self,
        duke_url: str,
        *,
        from_year: int = 2023,
        limit: int = 50,
    ) -> list[WaybackSnapshot]:
        parsed = urlparse(duke_url)
        return self.lookup_snapshots(
            f"https://{parsed.netloc}{parsed.path}",
            from_year=from_year,
            limit=limit,
            wildcard=True,
        )

    def lookup_page_snapshots(
        self,
        url: str,
        *,
        from_year: int = 2023,
        limit: int = 25,
    ) -> list[WaybackSnapshot]:
        return self.lookup_snapshots(
            url,
            from_year=from_year,
            limit=limit,
            wildcard=False,
        )

    def lookup_capture_history(
        self,
        url: str,
        *,
        from_year: int = 2023,
        limit: int = 25,
    ) -> list[WaybackSnapshot]:
        return self.lookup_snapshots(
            url,
            from_year=from_year,
            limit=limit,
            wildcard=False,
            dedupe_originals=False,
        )

    def lookup_snapshots(
        self,
        url: str,
        *,
        from_year: int = 2023,
        limit: int = 50,
        wildcard: bool = False,
        dedupe_originals: bool = True,
    ) -> list[WaybackSnapshot]:
        parsed = urlparse(url)
        target = f"https://{parsed.netloc}{parsed.path}"
        if wildcard:
            target += "*"
        elif parsed.query:
            target += f"?{parsed.query}"
        lookup_url = (
            "https://web.archive.org/cdx/search/cdx?"
            f"url={target}"
            "&output=json&fl=timestamp,original,statuscode,mimetype"
            f"&filter=statuscode:200&limit={limit}&from={from_year}"
        )
        response = self.client.get(lookup_url)
        response.raise_for_status()
        payload = response.json()
        if len(payload) <= 1:
            return []

        snapshots: list[WaybackSnapshot] = []
        seen_originals: set[str] = set()
        for row in payload[1:]:
            snapshot = WaybackSnapshot(
                timestamp=row[0],
                original_url=row[1],
                status_code=row[2],
                mimetype=row[3],
            )
            if dedupe_originals and snapshot.original_url in seen_originals:
                continue
            seen_originals.add(snapshot.original_url)
            snapshots.append(snapshot)
        return snapshots


def normalize_archived_target(url: str) -> str:
    marker = "/web/"
    if "web.archive.org" not in url or marker not in url:
        return url
    suffix = url.split(marker, maxsplit=1)[1]
    if "/" not in suffix:
        return url
    target = suffix.split("/", maxsplit=1)[1]
    return target if target.startswith("http") else url
