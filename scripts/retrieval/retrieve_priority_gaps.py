"""
Targeted NCUC portal retrieval for priority coverage gaps.

Targets (in priority order):
1. E-2, Sub 1076 — DEP 2015 rate case (leaf-500 RES, leaf-520/521/522/532/533 base schedules)
2. E-2, Sub 1142 — find R-TOU-CPP leaf-503 compliance tariff within this docket
3. E-2, Sub 1076 companion subs (annual adjustments 2015-2022)

Usage:
    python scripts/retrieval/retrieve_priority_gaps.py

Downloads go to: data/historical/ncuc/<docket-slug>/
"""
import re
import time
import hashlib
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)

settings = Settings()

# Dockets to search, in priority order
# Format: "E-2 Sub XXXX" (no comma, one space — per working method docs)
SEARCH_TARGETS = [
    {
        "docket": "E-2 Sub 1076",
        "slug": "e-2-sub-1076",
        "description": "DEP 2015 rate case — base schedules (RES/SGS/LGS/R-TOUD 2015-2021)",
        "keywords": ["compliance tariff", "revised tariff", "leaf", "schedule"],
        "priority": 1,
    },
    {
        "docket": "E-2 Sub 1142",
        "slug": "e-2-sub-1142",
        "description": "DEP 2018 rate case — R-TOU-CPP leaf-503 compliance tariff",
        "keywords": ["R-TOU-CPP", "critical peak", "leaf 503", "503"],
        "priority": 2,
    },
    {
        "docket": "E-2 Sub 1293",
        "slug": "e-2-sub-1293",
        "description": "DEP 2022 compliance — all schedule compliance tariffs",
        "keywords": ["compliance tariff", "leaf", "schedule"],
        "priority": 3,
    },
    {
        "docket": "E-2 Sub 1296",
        "slug": "e-2-sub-1296",
        "description": "DEP compliance — rates filing with R-TOU-CPP",
        "keywords": ["R-TOU-CPP", "compliance", "leaf"],
        "priority": 4,
    },
]

SEARCH_URL = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
DETAIL_URL_BASE = "https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx"

# Document title filters — skip obviously non-rate documents
SKIP_TITLE_PATTERNS = [
    r"motion\b", r"brief\b", r"testimony\b", r"notice\b", r"certificate",
    r"letter\b", r"cover letter", r"service list", r"errata\b",
    r"application\b", r"petition\b", r"procedural", r"scheduling",
    r"discovery\b", r"data request", r"deposition", r"witness",
    r"annual report", r"audit\b", r"exhibit\s+[a-z](?!\s*(tariff|rate|leaf|schedule))",
]
SKIP_RE = re.compile("|".join(SKIP_TITLE_PATTERNS), re.I)

# Title patterns indicating rate documents
RATE_TITLE_PATTERNS = [
    r"compliance tariff", r"revised tariff", r"tariff sheet",
    r"leaf\s*no", r"leaf\s*\d+", r"schedule\s+[a-z]",
    r"R-TOU-CPP", r"R-TOUD", r"residential service",
    r"rate schedule", r"tariff filing", r"tariff compliance",
    r"rate supplement", r"tariff supplement",
]
RATE_RE = re.compile("|".join(RATE_TITLE_PATTERNS), re.I)


def extract_docs_from_page(page, page_num=1):
    """Extract document records from current search results page."""
    content = page.content()
    link_pattern = re.compile(
        r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+))["\']',
        re.I,
    )
    docs = []
    seen = set()
    for match in link_pattern.finditer(content):
        doc_id = match.group(2)
        doc_class = match.group(3)
        if doc_id in seen:
            continue
        seen.add(doc_id)
        docs.append({
            "doc_id": doc_id,
            "class_": doc_class,
            "href": f"{DETAIL_URL_BASE}?DocumentId={doc_id}&Class={doc_class}",
            "title": "",
            "date_filed": "",
        })

    # Enrich with titles from table rows
    rows = page.query_selector_all("table tr")
    for row in rows:
        row_links = row.query_selector_all("a[href*='PSCDocumentDetailsPageNCUC']")
        if not row_links:
            continue
        href = row_links[0].get_attribute("href") or ""
        m = re.search(r"DocumentId=([0-9a-f\-]{36})", href, re.I)
        if not m:
            continue
        doc_id = m.group(1)
        row_text = row.inner_text()
        # Extract title (first non-metadata line)
        lines = [l.strip() for l in row_text.split("\n") if l.strip()]
        title = ""
        for line in lines:
            if not re.match(r"^(Filing|Order|Other|Filed In:|Date Filed:|Items Count:)", line):
                if len(line) > 5:
                    title = line
                    break
        date_m = re.search(r"Date Filed:\s*(\d{1,2}/\d{1,2}/\d{4})", row_text)
        date_filed = date_m.group(1) if date_m else ""
        for doc in docs:
            if doc["doc_id"] == doc_id:
                doc["title"] = title
                doc["date_filed"] = date_filed
                break

    return docs


def search_docket(page, docket_str, description):
    """Search for all documents in a docket. Returns list of doc records."""
    print(f"\n  Searching: {docket_str}")
    print(f"  ({description})")

    try:
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"  ERROR navigating: {e}")
        return []

    try:
        docket_input = page.query_selector(
            "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
        )
        if not docket_input:
            print("  ERROR: docket input not found")
            return []
        docket_input.fill(docket_str)
        submit = page.query_selector("input[value='Search']")
        submit.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
    except Exception as e:
        print(f"  ERROR submitting search: {e}")
        return []

    all_docs = []
    seen_ids = set()
    page_num = 1

    while True:
        docs = extract_docs_from_page(page, page_num)
        new_docs = [d for d in docs if d["doc_id"] not in seen_ids]
        for d in new_docs:
            seen_ids.add(d["doc_id"])
        all_docs.extend(new_docs)
        print(f"    Page {page_num}: {len(new_docs)} new docs ({len(all_docs)} total)")

        # Check for next page
        next_page_links = page.query_selector_all("a[href*='__doPostBack']")
        next_nums = []
        for link in next_page_links:
            txt = link.inner_text().strip()
            try:
                num = int(txt)
                if num > page_num:
                    next_nums.append((num, link))
            except ValueError:
                pass

        if not next_nums:
            break

        next_num, next_link = min(next_nums, key=lambda x: x[0])
        next_link.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        page_num = next_num

    return all_docs


def is_rate_document(doc):
    """Return True if this document is likely a rate sheet worth downloading."""
    title = doc.get("title", "")
    if not title:
        return True  # Unknown title — include by default, check after download

    if SKIP_RE.search(title):
        return False
    return True  # Include unless explicitly skipped — better to over-include


def download_document(page, doc, dest_dir):
    """Download a document to dest_dir. Returns (path, success)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_id = doc["doc_id"].replace("-", "")[:32]
    dest_path = dest_dir / f"{safe_id}.pdf"

    if dest_path.exists():
        print(f"    Already exists: {dest_path.name}")
        return dest_path, True

    try:
        with page.expect_download(timeout=45000) as dl_info:
            try:
                page.goto(doc["href"], wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass  # Expected "Download is starting" exception

        download = dl_info.value
        download.save_as(str(dest_path))
        size_kb = dest_path.stat().st_size // 1024
        print(f"    Downloaded: {dest_path.name} ({size_kb} KB)")
        return dest_path, True

    except Exception as e:
        print(f"    ERROR downloading {doc['doc_id']}: {e}")
        return dest_path, False


def main():
    print("=" * 70)
    print("NCUC Portal — Priority Gap Retrieval")
    print("=" * 70)

    pw, ctx, page = create_authenticated_context(settings)
    results = {}

    try:
        for target in SEARCH_TARGETS:
            docket = target["docket"]
            slug = target["slug"]
            description = target["description"]
            dest_dir = Path("data/historical/ncuc") / slug

            print(f"\n{'='*70}")
            print(f"DOCKET: {docket}")
            print(f"Description: {description}")
            print(f"Destination: {dest_dir}")
            print(f"{'='*70}")

            # Discover documents
            docs = search_docket(page, docket, description)
            if not docs:
                print(f"  No documents found in {docket}")
                results[docket] = {"found": 0, "downloaded": 0, "skipped": 0}
                continue

            print(f"\n  Found {len(docs)} total documents")

            # Filter to rate-relevant docs
            rate_docs = [d for d in docs if is_rate_document(d)]
            skipped = len(docs) - len(rate_docs)
            print(f"  Rate-relevant: {len(rate_docs)} (skipped {skipped} procedural)")

            # Show titles for manual review
            print(f"\n  Document titles:")
            for d in docs[:30]:
                flag = "[RATE]" if is_rate_document(d) else "[SKIP]"
                print(f"    {flag} {d['date_filed']:10s} {d['title'][:80]}")
            if len(docs) > 30:
                print(f"    ... and {len(docs)-30} more")

            # Download rate-relevant docs
            downloaded = 0
            failed = 0
            already_existed = 0
            for doc in rate_docs:
                existed = (dest_dir / f"{doc['doc_id'].replace('-', '')[:32]}.pdf").exists()
                path, ok = download_document(page, doc, dest_dir)
                if ok:
                    if existed:
                        already_existed += 1
                    else:
                        downloaded += 1
                else:
                    failed += 1
                time.sleep(0.5)  # polite delay

            results[docket] = {
                "found": len(docs),
                "rate_docs": len(rate_docs),
                "downloaded": downloaded,
                "already_existed": already_existed,
                "failed": failed,
            }
            print(f"\n  Summary: {downloaded} new downloads, {already_existed} already existed, {failed} failed")

    finally:
        close_authenticated_context(pw, ctx)

    print(f"\n{'='*70}")
    print("RETRIEVAL COMPLETE")
    print(f"{'='*70}")
    for docket, r in results.items():
        print(f"  {docket}: {r.get('downloaded', 0)} new, {r.get('already_existed', 0)} existed, {r.get('failed', 0)} failed")


if __name__ == "__main__":
    main()
