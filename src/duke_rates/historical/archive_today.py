from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from duke_rates.utils.files import ensure_parent

ARCHIVE_TODAY_BASE = "https://archive.ph"
ARCHIVE_LINK_RE = re.compile(r"https://archive\.[a-z]+/[A-Za-z0-9]+")


class ArchiveTodayProbeResult(BaseModel):
    title: str
    source_url: str
    search_url: str
    direct_url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    archive_links: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ArchiveTodayClient:
    def __init__(self, *, timeout: float = 20.0, user_agent: str = "duke-rates/0.1"):
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self.client.close()

    def probe(self, *, title: str, source_url: str) -> ArchiveTodayProbeResult:
        search_url = f"{ARCHIVE_TODAY_BASE}/?url={quote(source_url, safe='')}"
        direct_url = f"{ARCHIVE_TODAY_BASE}/{source_url}"
        notes: list[str] = []
        try:
            response = self.client.get(search_url)
        except httpx.TimeoutException:
            return ArchiveTodayProbeResult(
                title=title,
                source_url=source_url,
                search_url=search_url,
                direct_url=direct_url,
                status="timeout",
                notes=["archive.today timed out from this environment"],
            )
        except httpx.HTTPError as exc:
            return ArchiveTodayProbeResult(
                title=title,
                source_url=source_url,
                search_url=search_url,
                direct_url=direct_url,
                status="error",
                notes=[str(exc)],
            )

        archive_links = sorted(set(ARCHIVE_LINK_RE.findall(response.text)))
        status = "ok"
        if response.status_code == 429:
            status = "rate_limited"
            notes.append("archive.today returned HTTP 429")
        elif response.status_code >= 400:
            status = "http_error"
            notes.append(f"archive.today returned HTTP {response.status_code}")
        elif not archive_links:
            status = "no_links_found"
            notes.append("No archive.today snapshot links were parsed from the response")

        return ArchiveTodayProbeResult(
            title=title,
            source_url=source_url,
            search_url=search_url,
            direct_url=direct_url,
            status=status,
            http_status=response.status_code,
            final_url=str(response.url),
            archive_links=archive_links,
            notes=notes,
        )


def write_archive_today_markdown_report(
    *,
    results: list[ArchiveTodayProbeResult],
    output_path: Path,
) -> None:
    ensure_parent(output_path)
    lines = [
        "# Progress NC archive.today Probe",
        "",
        "| Title | Status | HTTP | Source URL | Search URL | Archive Links | Notes |",
        "|---|---|---:|---|---|---|---|",
    ]
    for result in results:
        archive_links = "<br>".join(result.archive_links) if result.archive_links else ""
        notes = "; ".join(result.notes)
        row = (
            "| {title} | {status} | {http_status} | {source_url} | "
            "{search_url} | {archive_links} | {notes} |"
        )
        lines.append(
            row.format(
                title=_md_cell(result.title),
                status=_md_cell(result.status),
                http_status=_md_cell("" if result.http_status is None else str(result.http_status)),
                source_url=_md_cell(result.source_url),
                search_url=_md_cell(result.search_url),
                archive_links=_md_cell(archive_links),
                notes=_md_cell(notes),
            )
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
