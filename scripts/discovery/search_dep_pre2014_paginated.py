"""
Paginated search of large NCUC E-2 dockets for DEP compliance tariff books (2008-2013).

The non-paginated discovery script found 0 candidates in the 838-1023 range because:
1. Large rate case dockets (E-2 Sub 1023, the 2012 DEP general rate case) have 50+ docs
2. The portal only shows 10 docs per page — compliance tariff filings are buried on pages 3-5+
3. The non-paginated script never saw those pages

This script adds pagination support and targets:
- E-2 Sub 1023: the 2012 DEP general rate case (compliance tariffs filed June 2013)
  The July 18, 2013 corrections filing (sub1023_2013_corrections.pdf) was on page 1.
  The original June 3, 2013 compliance filing is likely on a later page.
- E-2 Sub 938: the estimated 2008 DEP general rate case (PEC filed ~2008)
  This sub-docket number is a guess — the FCAR chain noted the 2008 rate case.
  We'll search a range of likely dockets with pagination to find it.

Known rate case timeline (reconstructed from FCAR cross-references and compliance filings):
  2012 rate case: E-2 Sub 1023 (Order May 30, 2013; compliance June 3, 2013)
  2008 rate case: unknown sub-docket — likely E-2 Sub ~908-935 range
  Prior rate case: E-2 Sub ~697-850 range (we have 1996-09-15 data from Sub 697)

Strategy:
  1. Paginate through E-2 Sub 1023 to find ALL docs (including compliance tariffs)
  2. Sweep E-2 Sub 905-950 range with pagination to find the 2008 rate case
  3. Download any residential compliance tariff candidates found
"""
import re
import time
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import (
    close_authenticated_context,
    create_authenticated_context,
    download_view_file,
)

settings = Settings()

DOCKET_SEARCH_URL = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
DOCKET_FIELD = "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
SUBMIT_BTN = "input[value='Search']"

DOWNLOAD_DIR = Path("data/downloads/dep_pre2014_paginated")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Dockets to paginate through fully.
# Session 28 found RES-10B/RES-12 in E-2 Sub 929 (2008 fuel adjustment compliance).
# Sub 905-949 sweep complete — 0 compliance tariff books for 2009-2013 found.
# This sweep targets Sub 932-1022 for the 2009-2013 annual fuel/rider compliance books
# (RES-13 through RES-30 revisions, filed annually in the years between 2008 and 2013).
PAGINATED_DOCKETS = [
    # Dense sweep of Sub 932-1022 — compliance tariff books for 2009-2013 should land here
    *[f"E-2 Sub {n}" for n in range(932, 1023)],
]
PAGINATED_DOCKETS = list(dict.fromkeys(PAGINATED_DOCKETS))


def extract_docs_from_html(html: str, docket_str: str) -> list[dict]:
    """Extract document entries from portal search result HTML."""
    desc_link_pattern = re.compile(
        r'<td colspan="2">([^<]{5,300})</td>.*?'
        r'PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+)',
        re.I | re.DOTALL,
    )
    documents = []
    seen_ids = set()
    for match in desc_link_pattern.finditer(html):
        if match.end() - match.start() >= 3000:
            continue
        doc_id = match.group(2)
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        doc_class = match.group(3)
        title = match.group(1).strip()
        documents.append({
            "doc_id": doc_id,
            "doc_class": doc_class,
            "title": title,
            "docket": docket_str,
            "url": f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}",
        })
    return documents


def search_docket_paginated(page, docket_str: str) -> list[dict]:
    """Search a docket with pagination to retrieve ALL documents, not just first 10."""
    print(f"\n--- Searching (paginated): {docket_str} ---")
    page.goto(DOCKET_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    docket_input = page.query_selector(DOCKET_FIELD)
    if not docket_input:
        print(f"  ERROR: Could not find docket input field")
        return []

    docket_input.fill(docket_str)
    submit = page.query_selector(SUBMIT_BTN)
    if not submit:
        print(f"  ERROR: Could not find submit button")
        return []

    submit.click()
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    all_docs = []
    page_num = 1

    while True:
        html = page.content()

        if "No documents found" in html or "no documents" in html.lower():
            print(f"  No documents found in {docket_str}")
            break

        docs = extract_docs_from_html(html, docket_str)
        if not docs and page_num == 1:
            break

        all_docs.extend(docs)
        print(f"  Page {page_num}: {len(docs)} docs (total so far: {len(all_docs)})")

        # Look for next-page links — ASP.NET paging uses __doPostBack links
        # The links typically show page numbers: 1 2 3 ... or "Next"
        next_links = page.locator("a[href*='__doPostBack']").all()
        next_clicked = False

        for link in next_links:
            try:
                text = link.inner_text().strip()
                # Look for the next sequential page number or "Next" / ">"
                if text == str(page_num + 1) or text in ("Next", ">", ">>"):
                    link.click()
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                    page_num += 1
                    next_clicked = True
                    break
            except Exception:
                continue

        if not next_clicked:
            break  # No more pages

        if page_num > 20:
            print(f"  WARNING: Stopping after 20 pages to avoid runaway pagination")
            break

    print(f"  Total docs in {docket_str}: {len(all_docs)}")
    return all_docs


def is_compliance_tariff_candidate(title: str) -> bool:
    """Is this document likely to be a compliance tariff book with residential schedule?"""
    lower = title.lower()
    # Exclude false positives
    excluded = (
        "neighborhood energy saver", "nes-1", "lighting program", "appliance recycling",
        "order approving tariff filing", "approval of tariff", "order approving",
        "regulatory asset", "petition to intervene", "notice of intervention",
        "motion to dismiss", "answer and motion",
    )
    if any(term in lower for term in excluded):
        return False
    return any(kw in lower for kw in [
        "compliance tariff", "tariff book", "revised tariff", "tariff compliance",
        "compliance filing", "rate schedule", "general rate", "rate increase",
        "schedule r", "residential service", "leaf no.", "leaf 500", "leaf 501", "leaf 502",
    ])


def get_viewfile_urls(page, detail_url: str) -> list[str]:
    """Return ViewFile.aspx URLs from a document detail page."""
    page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)
    links = page.locator("a[href*='ViewFile.aspx']").evaluate_all(
        "els => els.map(e => e.href || e.getAttribute('href') || '').filter(Boolean)"
    )
    deduped: list[str] = []
    seen: set[str] = set()
    for href in links:
        if href.startswith("/"):
            href = "https://starw1.ncuc.gov" + href
        if href not in seen:
            seen.add(href)
            deduped.append(href)
    return deduped


def download_document(page, doc: dict) -> Path | None:
    """Download the first ViewFile attachment from a document detail page."""
    safe_title = re.sub(r'[^\w\-.]', '_', doc['title'])[:60]
    dest_path = DOWNLOAD_DIR / f"{doc['docket'].replace(' ', '_')}_{safe_title}_{doc['doc_id'][:8]}.pdf"

    if dest_path.exists():
        print(f"  Already exists: {dest_path.name}")
        return dest_path

    try:
        view_urls = get_viewfile_urls(page, doc["url"])
        if not view_urls:
            print(f"  No ViewFile link found for: {doc['title']!r}")
            return None
        download_view_file(page, view_urls[0], dest_path)
        print(f"  Downloaded: {dest_path.name}")
        return dest_path
    except Exception as e:
        print(f"  Download failed for {doc['title']!r}: {e}")
        return None


def main():
    print("DEP Pre-2014 Residential Rate Discovery — PAGINATED Search")
    print("=" * 60)
    print(f"Searching {len(PAGINATED_DOCKETS)} dockets with full pagination")
    print(f"Target: compliance tariff books with Schedule R / leaf-500 / leaf-502")
    print(f"Download dir: {DOWNLOAD_DIR}")
    print()

    pw, ctx, page = create_authenticated_context(settings)

    try:
        all_candidates = []
        docket_totals = {}

        for docket in PAGINATED_DOCKETS:
            docs = search_docket_paginated(page, docket)
            docket_totals[docket] = len(docs)

            candidates = []
            for doc in docs:
                if is_compliance_tariff_candidate(doc['title']):
                    candidates.append(doc)
                    all_candidates.append(doc)
                    print(f"  *** CANDIDATE: {doc['title']!r} [{docket}]")

            time.sleep(0.8)

        print(f"\n{'=' * 60}")
        print(f"Search complete: {len(all_candidates)} candidates across {len(PAGINATED_DOCKETS)} dockets")
        print()

        print("Dockets with documents:")
        for docket, count in docket_totals.items():
            if count > 0:
                print(f"  {docket}: {count} total docs")

        if not all_candidates:
            print("\nNo compliance tariff candidates found.")
            print("Interpretation:")
            print("  - The 2008 DEP rate case may not be in the E-2 Sub 905-950 range")
            print("  - Try searching E-2 Sub 920-960 or reviewing NCUC docket index manually")
            print("  - The compliance tariff book may be in the MAIN rate case docket under a later page")
            return

        print("\nCandidates to download:")
        for doc in all_candidates:
            print(f"  [{doc['docket']}] {doc['title']!r}")

        print("\nDownloading candidates...")
        downloaded = []
        for doc in all_candidates:
            path = download_document(page, doc)
            if path:
                downloaded.append((doc, path))
            time.sleep(0.5)

        print(f"\nDownloaded {len(downloaded)}/{len(all_candidates)} files")
        if downloaded:
            print("\nNext steps:")
            print("  1. Review downloads in:", DOWNLOAD_DIR)
            print("  2. Find Schedule R pages; note start/end page numbers")
            print("  3. Register with:")
            print("     python -m duke_rates add-historical-document-nc \\")
            print("       --family-key nc-progress-leaf-500 \\")
            print("       --local-path <pdf_path> \\")
            print("       --start-page <n> --end-page <n> \\")
            print("       --effective-start YYYY-MM-DD")
            print("  4. python -m duke_rates bootstrap-missing-versions-nc")
            print("  5. python -m duke_rates enqueue-reprocess-nc --hd-id <id>")
            print("  6. python -m duke_rates process-reprocess-queue-nc")

    finally:
        close_authenticated_context(pw, ctx)


if __name__ == "__main__":
    main()
