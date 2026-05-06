#!/usr/bin/env python
"""
Harvest targeted DEP and DEC tariff/rider documents from the authenticated NCUC portal.

This script is intentionally narrower than a full portal crawl. It searches a curated
set of DEP/DEC dockets that are currently high-value for historical tariff/rider
backfill, enriches result rows with attachment labels, filters to likely tariff
documents, downloads matching PDFs, and writes a manifest/report for follow-up
registration and mining.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.duplicate_detector import calculate_file_checksum, find_duplicate_by_checksum
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.document_param_search import DocumentParamSearcher
from duke_rates.historical.ncuc.session import (
    close_authenticated_context,
    create_authenticated_context,
    download_view_file,
    get_docket_documents,
    resolve_docket_ids,
)


@dataclass(frozen=True)
class HarvestTarget:
    docket: str
    company: str
    utility_slug: str
    focus: str
    terms: tuple[str, ...]


TARGETS: tuple[HarvestTarget, ...] = (
    HarvestTarget("E-2 Sub 1142", "Duke Energy Progress", "dep", "DEP Rider BA historical", ("billing adjustment", "rider ba", "leaf 601", "summary of riders", "fuel", "reps")),
    HarvestTarget("E-2 Sub 1143", "Duke Energy Progress", "dep", "DEP Rider JAA historical", ("jaa", "jaar", "joint agency", "leaf 602", "summary of riders", "fuel", "reps")),
    HarvestTarget("E-2 Sub 1196", "Duke Energy Progress", "dep", "DEP Rider EDIT-4 historical", ("edit-4", "edit 4", "excess deferred income", "leaf 604")),
    HarvestTarget("E-2 Sub 1204", "Duke Energy Progress", "dep", "DEP storm riders", ("storm", "storm securitization", "sts", "sts-2", "leaf 607", "leaf 613", "summary of riders")),
    HarvestTarget("E-2 Sub 1294", "Duke Energy Progress", "dep", "DEP Rider RDM historical", ("rdm", "revenue decoupling", "decoupling", "leaf 608", "summary of riders")),
    HarvestTarget("E-2 Sub 1354", "Duke Energy Progress", "dep", "DEP Rider JAA current", ("jaa", "jaar", "joint agency", "leaf 602")),
    HarvestTarget("E-7 Sub 487", "Duke Energy Carolinas", "dec", "DEC Rider EDPR historical", ("edpr", "existing dsm", "dsm program")),
    HarvestTarget("E-7 Sub 828", "Duke Energy Carolinas", "dec", "DEC Rider EDPR historical", ("edpr", "existing dsm", "dsm program")),
    HarvestTarget("E-7 Sub 1026", "Duke Energy Carolinas", "dec", "DEC Rider EDPR historical", ("edpr", "existing dsm", "dsm program")),
    HarvestTarget("E-7 Sub 1146", "Duke Energy Carolinas", "dec", "DEC EDPR and EDIT-4 historical", ("edpr", "existing dsm", "edit-4", "edit 4", "excess deferred income", "leaf 131")),
    HarvestTarget("E-7 Sub 1152", "Duke Energy Carolinas", "dec", "DEC Rider EDIT-4 historical", ("edit-4", "edit 4", "excess deferred income", "leaf 131")),
    HarvestTarget("E-7 Sub 1165", "Duke Energy Carolinas", "dec", "DEC Rider EDPR historical", ("edpr", "existing dsm", "dsm program")),
    HarvestTarget("E-7 Sub 1168", "Duke Energy Carolinas", "dec", "DEC Rider PM historical", ("rider pm", "performance mechanism", "pim", "pm")),
    HarvestTarget("E-7 Sub 1187", "Duke Energy Carolinas", "dec", "DEC Rider EDIT-4 historical", ("edit-4", "edit 4", "excess deferred income", "leaf 131")),
    HarvestTarget("E-7 Sub 1213", "Duke Energy Carolinas", "dec", "DEC Rider EDIT-4 historical", ("edit-4", "edit 4", "excess deferred income", "leaf 131")),
    HarvestTarget("E-7 Sub 1214", "Duke Energy Carolinas", "dec", "DEC Rider EDIT-4 and EDPR historical", ("edit-4", "edit 4", "excess deferred income", "leaf 131", "edpr", "existing dsm")),
    HarvestTarget("E-7 Sub 1243", "Duke Energy Carolinas", "dec", "DEC storm riders", ("storm", "storm securitization", "rider sts", "sts")),
    HarvestTarget("E-7 Sub 1276", "Duke Energy Carolinas", "dec", "DEC annual compliance riders and schedules", ("edpr", "existing dsm", "schedule bc", "schedule re", "schedule pp", "rider car", "rider summary", "compliance tariffs")),
    HarvestTarget("E-7 Sub 1321", "Duke Energy Carolinas", "dec", "DEC storm rider Debby", ("storm", "storm securitization", "rider sts", "sts", "debby")),
    HarvestTarget("E-7 Sub 1325", "Duke Energy Carolinas", "dec", "DEC storm rider Helene", ("storm", "storm securitization", "rider sts", "sts", "helene")),
)

EXCLUDE_TERMS = (
    "cover letter",
    "redlined",
    "redline",
    "customer notice",
    "notice of",
    "notice ",
    "confidential",
    "motion",
    "brief",
    "testimony",
    "certificate",
    "procedural",
    "service list",
    "data request",
    "discovery request",
    "application",
    "petition",
    "order",
    "cos",
)

PLACEHOLDER_TEXT = "click the to view the document."


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_excluded(text: str) -> bool:
    lowered = norm(text)
    return any(term in lowered for term in EXCLUDE_TERMS)


def matches_terms(text: str, terms: tuple[str, ...]) -> bool:
    lowered = norm(text)
    return any(term in lowered for term in terms)


def slugify(text: str, limit: int = 90) -> str:
    cleaned = re.sub(r"[^\w\s\-\.]", "_", text or "")
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:limit] or "document"


def build_dest_path(root: Path, target: HarvestTarget, date_filed: str, view_url: str, title: str) -> Path:
    view_id_match = re.search(r"Id=([0-9a-f\-]{36})", view_url, re.I)
    view_id = view_id_match.group(1)[:8] if view_id_match else "unknown"
    docket_slug = slugify(target.docket.replace(" ", "_"), 40)
    date_slug = slugify(date_filed.replace("/", "-"), 20) if date_filed else "undated"
    file_name = f"{date_slug}_{view_id}_{slugify(title)}.pdf"
    return root / target.utility_slug / docket_slug / file_name


def absolute_detail_url(detail_url: str) -> str:
    if not detail_url:
        return ""
    if detail_url.startswith("http://") or detail_url.startswith("https://"):
        return detail_url
    return "https://starw1.ncuc.gov" + detail_url


def candidate_attachments(row, target: HarvestTarget) -> list[tuple[str, str]]:
    description = row.description or ""
    labels = row.view_file_labels or []
    urls = row.view_file_urls or []
    matches: list[tuple[str, str]] = []

    for label, url in zip_longest(labels, urls, fillvalue=""):
        combined = f"{description} {label}".strip()
        if not url:
            continue
        if is_excluded(combined):
            continue
        if matches_terms(combined, target.terms):
            matches.append((label or description or target.focus, url))

    # Some rows have a single attachment and a generic/empty label. If the filing
    # description is targeted, keep that one attachment.
    if not matches and len(urls) == 1 and description and description.lower() != PLACEHOLDER_TEXT:
        if not is_excluded(description) and matches_terms(description, target.terms):
            matches.append((labels[0] if labels else description, urls[0]))

    return matches


def load_detail_attachments(page, detail_url: str) -> list[tuple[str, str]]:
    page.goto(absolute_detail_url(detail_url), wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(800)
    links = page.locator("a[href*='ViewFile.aspx']").evaluate_all(
        """
        els => els.map(e => ({
          text: (e.innerText || '').trim(),
          href: e.href || ''
        }))
        """
    )
    results: list[tuple[str, str]] = []
    for link in links:
        href = link.get("href", "")
        if not href:
            continue
        results.append((link.get("text", "").strip(), href))
    return results


def fallback_docket_candidates(page, target: HarvestTarget) -> list[dict[str, str]]:
    docket_matches = resolve_docket_ids(page, target.docket)
    if not docket_matches:
        return []

    docs = get_docket_documents(page, docket_matches[0]["docket_id"])
    results: list[dict[str, str]] = []
    for doc in docs:
        if norm(doc.get("doc_type", "")) == "order":
            continue
        detail_url = doc.get("document_url", "")
        if not detail_url:
            continue
        try:
            attachments = load_detail_attachments(page, detail_url)
        except Exception:
            continue

        for label, url in attachments:
            combined = f"{doc.get('description', '')} {label}".strip()
            if is_excluded(combined):
                continue
            if not matches_terms(combined, target.terms):
                continue
            results.append(
                {
                    "date_filed": doc.get("date_filed", ""),
                    "description": doc.get("description", ""),
                    "attachment_label": label or doc.get("description", "") or target.focus,
                    "view_url": url,
                }
            )
    return results


def write_outputs(manifest: list[dict], summary: dict[str, dict[str, int]], report_md: Path, report_json: Path) -> None:
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps({"generated_at": datetime.now().isoformat(), "summary": summary, "documents": manifest}, indent=2))

    lines = [
        "# NCUC Target Harvest 2026-04-07",
        "",
        f"Generated: {datetime.now().isoformat()}",
        "",
        "## Summary",
        "",
        "| Target | Matched | Downloaded | Skipped | Failed | Duplicates |",
        "|--------|---------|------------|---------|--------|------------|",
    ]
    for focus, stats in summary.items():
        lines.append(f"| {focus} | {stats['matched']} | {stats['downloaded']} | {stats['skipped']} | {stats['failed']} | {stats['duplicates']} |")

    lines.extend([
        "",
        "## Downloads",
        "",
        "| Docket | Focus | Date Filed | Attachment | Status | Path |",
        "|-------|-------|------------|------------|--------|------|",
    ])

    for item in manifest:
        path = item.get("path", "")
        lines.append(
            f"| {item['docket']} | {item['focus']} | {item['date_filed']} | "
            f"{item['attachment_label'][:100]} | {item['status']} | {path} |"
        )

    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def download_with_duplicate_guard(
    conn: sqlite3.Connection,
    *,
    page,
    url: str,
    dest_path: Path,
) -> tuple[str, dict]:
    """
    Download one attachment and return a manifest payload.

    Duplicate downloads are deleted immediately and reported as skipped so the
    manifest does not promote redundant docket copies into registration.
    """
    size = download_view_file(page, url, dest_path)
    content_hash = calculate_file_checksum(dest_path)
    duplicate = find_duplicate_by_checksum(conn, content_hash)
    payload: dict[str, object] = {
        "size_bytes": size,
        "content_hash": content_hash,
        "duplicate_of": duplicate,
    }
    if duplicate:
        dest_path.unlink(missing_ok=True)
        payload["path"] = ""
        return "skipped_duplicate", payload
    payload["path"] = str(dest_path)
    return "downloaded", payload


def main() -> None:
    settings = Settings()
    searcher = DocumentParamSearcher(settings)
    conn: sqlite3.Connection = connect(settings.database_path)

    download_root = settings.data_dir / "downloads" / "ncuc_tariff" / "authenticated_portal"
    report_json = Path("data/ncuc_target_harvest_2026_04_07.json")
    report_md = Path("docs/reports/NCUC_TARGET_HARVEST_2026_04_07.md")

    manifest: list[dict] = []
    summary: dict[str, dict[str, int]] = defaultdict(lambda: {"matched": 0, "downloaded": 0, "skipped": 0, "failed": 0, "duplicates": 0})

    pw, ctx, page = create_authenticated_context(settings)
    try:
        for target in TARGETS:
            print(f"\n=== {target.docket} | {target.focus} ===", flush=True)
            rows = searcher.search(
                page,
                company_name=target.company,
                docket_number=target.docket,
                filing_types=["TARIFF", "RATESCED"],
                max_results=250,
            )
            rows = [row for row in rows if norm(row.description) != PLACEHOLDER_TEXT]
            print(f"  search results: {len(rows)}", flush=True)

            rows = searcher.enrich_with_document_details(page, rows, delay_seconds=0.2)

            seen_urls: set[str] = set()
            docket_matches = 0
            for row in rows:
                attachments = candidate_attachments(row, target)
                for label, url in attachments:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    docket_matches += 1
                    summary[target.focus]["matched"] += 1

                    title = label or row.description or target.focus
                    dest_path = build_dest_path(download_root, target, row.date_filed, url, title)

                    item = {
                        "docket": target.docket,
                        "focus": target.focus,
                        "date_filed": row.date_filed or "",
                        "description": row.description or "",
                        "attachment_label": title,
                        "view_url": url,
                        "path": str(dest_path),
                        "status": "",
                        "duplicate_of": None,
                    }

                    if dest_path.exists():
                        item["status"] = "exists"
                        summary[target.focus]["skipped"] += 1
                        manifest.append(item)
                        continue

                    try:
                        status, payload = download_with_duplicate_guard(
                            conn,
                            page=page,
                            url=url,
                            dest_path=dest_path,
                        )
                        item["status"] = status
                        item["size_bytes"] = payload.get("size_bytes")
                        item["content_hash"] = payload.get("content_hash")
                        item["duplicate_of"] = payload.get("duplicate_of")
                        if payload.get("path") == "":
                            item["path"] = ""
                        if item["duplicate_of"]:
                            summary[target.focus]["duplicates"] += 1
                        if status == "downloaded":
                            summary[target.focus]["downloaded"] += 1
                        else:
                            summary[target.focus]["skipped"] += 1
                        manifest.append(item)
                        print(f"  {status}: {dest_path.name}", flush=True)
                    except Exception as exc:  # pragma: no cover - live portal behavior
                        item["status"] = f"failed: {exc}"
                        summary[target.focus]["failed"] += 1
                        manifest.append(item)
                        print(f"  failed: {title[:70]} :: {exc}", flush=True)

            if docket_matches == 0:
                fallback_matches = fallback_docket_candidates(page, target)
                if not fallback_matches:
                    print("  no matched attachments", flush=True)
                    continue

                print(f"  fallback matches: {len(fallback_matches)}", flush=True)
                fallback_seen: set[str] = set()
                for match in fallback_matches:
                    url = match["view_url"]
                    if url in fallback_seen:
                        continue
                    fallback_seen.add(url)
                    summary[target.focus]["matched"] += 1

                    dest_path = build_dest_path(
                        download_root,
                        target,
                        match["date_filed"],
                        url,
                        match["attachment_label"],
                    )
                    item = {
                        "docket": target.docket,
                        "focus": target.focus,
                        "date_filed": match["date_filed"],
                        "description": match["description"],
                        "attachment_label": match["attachment_label"],
                        "view_url": url,
                        "path": str(dest_path),
                        "status": "",
                        "duplicate_of": None,
                    }

                    if dest_path.exists():
                        item["status"] = "exists"
                        summary[target.focus]["skipped"] += 1
                        manifest.append(item)
                        continue

                    try:
                        status, payload = download_with_duplicate_guard(
                            conn,
                            page=page,
                            url=url,
                            dest_path=dest_path,
                        )
                        item["status"] = status
                        item["size_bytes"] = payload.get("size_bytes")
                        item["content_hash"] = payload.get("content_hash")
                        item["duplicate_of"] = payload.get("duplicate_of")
                        if payload.get("path") == "":
                            item["path"] = ""
                        if item["duplicate_of"]:
                            summary[target.focus]["duplicates"] += 1
                        if status == "downloaded":
                            summary[target.focus]["downloaded"] += 1
                        else:
                            summary[target.focus]["skipped"] += 1
                        manifest.append(item)
                        print(f"  {status}: {dest_path.name}", flush=True)
                    except Exception as exc:  # pragma: no cover - live portal behavior
                        item["status"] = f"failed: {exc}"
                        summary[target.focus]["failed"] += 1
                        manifest.append(item)
                        print(f"  failed: {match['attachment_label'][:70]} :: {exc}", flush=True)
    finally:
        close_authenticated_context(pw, ctx)
        conn.close()

    write_outputs(manifest, summary, report_md, report_json)

    print("\n=== Harvest Summary ===")
    for focus, stats in summary.items():
        print(
            f"{focus}: matched={stats['matched']} downloaded={stats['downloaded']} "
            f"skipped={stats['skipped']} failed={stats['failed']} duplicates={stats['duplicates']}"
        )
    print(f"\nManifest: {report_json}")
    print(f"Report:   {report_md}")


if __name__ == "__main__":
    main()
