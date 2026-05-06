"""
Search NCUC E-2 docket for DEP residential base rate filings before December 2014.

Target: DEP Schedule R (leaf-500), R-TOU (leaf-501), R-ES (leaf-502) before 2014-12-01.
Currently earliest versions: leaf-500 starts 2014-12-01, leaf-501 starts 2013-06-01 (E-2 Sub 1023),
leaf-502 starts 2014-12-01 — nothing for 2008-2013 period for leaf-500/502.

Known context:
- DEP residential = leaf-500 (Schedule R), leaf-501 (R-TOU), leaf-502 (R-ES)
- 2014-12-01 versions come from E-2 Sub 1044 compliance tariff book
- E-2 Sub 1023 is the 2012 DEP general rate increase proceeding; we already have leaf-501 2013-06-01
- E-2 Sub 902 is the 2008 DEP general rate case (PEC Application for Rate Increase)
- E-2 Sub 838 is an earlier DEP rate case (~2004-2006 era)
- Compliance tariff books following a rate case order are filed in separate small sub-dockets
  typically within a few sub-docket numbers of the main rate case
- Large rate case sub-dockets (902, 838) have 50+ docs; portal only shows 10 — compliance
  tariff filings are often buried. But dedicated compliance sub-dockets (small, 2-5 docs) ARE
  fully visible in search results.

Strategy:
  1. E-2 Sub 838-902 range: the 2008 rate case window (dense sweep in intervals of 5-10)
  2. E-2 Sub 902-1023 range: post-2008 compliance window (intervals of 10-15)
  3. Previously searched 1095-1154 range is complete and returned 0 candidates (keep in list
     for completeness but won't re-download existing files)

NOTE: The portal search returns at most 10 docs per result page. Large dockets will miss
buried compliance filings, but dedicated compliance sub-dockets (2-5 docs total) are safe.
Confirmed docket hits so far:
  E-2 Sub 1023: sub1023_2013_corrections.pdf -> leaf-501 2013-06-01 (hd=3114, tv=6159)
  E-2 Sub 1044: earliest leaf-500/502 at 2014-12-01
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

# Target dockets for DEP pre-2014 residential rates.
#
# Priority block 1: E-2 Sub 838-902 — the 2008 DEP rate case window.
#   Sub 838 is the main 2004-2006 era rate case; Sub 902 is ~2008.
#   Compliance tariff sub-dockets land just after each rate case number.
#   Dense sweep with intervals of 5 to catch any 2-5-doc compliance sub-dockets.
#
# Priority block 2: E-2 Sub 902-1023 — post-2008 through 2012 rate case.
#   Larger intervals (10-15) since compliance windows are spread wider.
#   Also check Sub 1044 (known 2014-12-01 compliance tariff source for leaf-500/502).
#
# Already-confirmed dockets (keep for idempotency; downloads skip existing files):
#   E-2 Sub 1023: found sub1023_2013_corrections.pdf -> leaf-501 2013-06-01 (hd=3114)
#   E-2 Sub 1044: found leaf-500/502 at 2014-12-01 (already in DB)
#
# Previously-searched range 1095-1154 returned 0 candidates; kept for completeness.
DEP_SEARCH_DOCKETS = [
    # === Priority 1: 2008 rate case window (E-2 Sub 838 → 902) ===
    "E-2 Sub 838",
    *[f"E-2 Sub {n}" for n in range(840, 905, 5)],
    # === Priority 2: post-2008 compliance through 2012 rate case (E-2 Sub 902 → 1023) ===
    "E-2 Sub 902",
    *[f"E-2 Sub {n}" for n in range(910, 1025, 10)],
    # === Known compliance anchors ===
    "E-2 Sub 1023",
    "E-2 Sub 1044",
    # === Previously-searched 1095-1154 range (already clean — 0 candidates) ===
    *[f"E-2 Sub {n}" for n in range(1095, 1155, 5)],
    "E-2 Sub 1140",
    "E-2 Sub 1141",
    "E-2 Sub 1142",
    "E-2 Sub 1143",
    "E-2 Sub 1144",
    "E-2 Sub 1145",
    "E-2 Sub 1146",
    "E-2 Sub 1147",
    "E-2 Sub 1148",
    "E-2 Sub 1149",
    "E-2 Sub 1150",
    "E-2 Sub 1151",
    "E-2 Sub 1152",
    "E-2 Sub 1153",
    "E-2 Sub 1154",
    # Pre-1023 sub-dockets checked previously
    "E-2 Sub 1005",
    "E-2 Sub 1010",
    "E-2 Sub 1015",
    "E-2 Sub 1020",
]
DEP_SEARCH_DOCKETS = list(dict.fromkeys(DEP_SEARCH_DOCKETS))

DOCKET_SEARCH_URL = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
DOCKET_FIELD = "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
SUBMIT_BTN = "input[value='Search']"

DOWNLOAD_DIR = Path("data/downloads/dep_pre2014_discovery")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def search_docket(page, docket_str: str) -> list[dict]:
    """Search for documents in a specific docket number."""
    print(f"\n--- Searching docket: {docket_str} ---")
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

    html = page.content()
    if "No documents found" in html or "no documents" in html.lower():
        print(f"  No documents found in {docket_str}")
        return []

    # Document descriptions are in <td colspan="2">description</td> elements
    # followed within ~2000 chars by the document link with the DocumentId.
    desc_link_pattern = re.compile(
        r'<td colspan="2">([^<]{5,300})</td>.*?'
        r'PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+)',
        re.I | re.DOTALL,
    )
    documents = []
    seen_ids = set()
    for match in desc_link_pattern.finditer(html):
        span = match.end() - match.start()
        if span >= 3000:
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

    print(f"  Found {len(documents)} documents")
    for d in documents[:10]:
        print(f"    {d['title']!r}")
    return documents


def is_residential_tariff_relevant(title: str) -> bool:
    """Heuristic: is this document likely to contain DEP residential tariff schedules?"""
    lower = title.lower()
    excluded_terms = (
        "neighborhood energy saver",
        "nes-1",
        "program",
        "order approving tariff filing",
        "approval of tariff",
        "tariff filing",
        "order approving",
    )
    if any(term in lower for term in excluded_terms):
        return False
    return any(kw in lower for kw in [
        "schedule r", "residential service", "leaf no.", "leaf-no",
        "compliance tariff", "tariff book", "rate schedule",
        "revised tariff", "tariff compliance",
        "r-tou", "r-es", "r-iqhu", "r tou", "r es",
        "leaf 500", "leaf 501", "leaf 502",
    ])


def get_viewfile_urls(page, detail_url: str) -> list[str]:
    """Return ViewFile.aspx URLs found on an authenticated document detail page."""
    page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)

    links = page.locator("a[href*='ViewFile.aspx']").evaluate_all(
        """
        els => els
          .map(e => e.href || e.getAttribute('href') || '')
          .filter(Boolean)
        """
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
            print("  Download failed: no ViewFile link found on detail page")
            return None
        download_view_file(page, view_urls[0], dest_path)
        print(f"  Downloaded: {dest_path.name}")
        return dest_path
    except Exception as e:
        print(f"  Download failed: {e}")
        return None


def main():
    print("DEP Pre-2014 Residential Rate Discovery Search")
    print("=" * 55)
    print(f"Searching {len(DEP_SEARCH_DOCKETS)} E-2 sub-dockets for pre-2014 DEP residential filings")
    print(f"Download dir: {DOWNLOAD_DIR}")
    print(f"Target families: nc-progress-leaf-500 (R), leaf-501 (R-TOU), leaf-502 (R-ES)")
    print()

    pw, ctx, page = create_authenticated_context(settings)

    try:
        all_documents = []
        residential_candidates = []
        docket_counts = {}

        for docket in DEP_SEARCH_DOCKETS:
            docs = search_docket(page, docket)
            all_documents.extend(docs)
            docket_counts[docket] = len(docs)

            # Filter for likely residential tariff filings
            for doc in docs:
                if is_residential_tariff_relevant(doc['title']):
                    residential_candidates.append(doc)
                    print(f"  *** RESIDENTIAL CANDIDATE: {doc['title']!r} in {docket}")

            time.sleep(1.0)

        print(f"\n{'=' * 55}")
        print(f"Search complete: {len(all_documents)} total docs, {len(residential_candidates)} residential candidates")
        print()

        # Show docket summary
        print("Docket document counts:")
        for docket, count in docket_counts.items():
            if count > 0:
                print(f"  {docket}: {count} docs")

        if not residential_candidates:
            print("\nNo residential candidates found.")
            print("Manual search suggestions:")
            print("  - Navigate to NCUC portal, search E-2 Sub 838 / 902 for 'compliance tariff'")
            print("  - Look for 'DEP Compliance Tariff' or 'Duke Energy Progress compliance'")
            print("  - Try text search: 'Schedule R Residential Service' or 'Leaf 500'")
            print("  - Large rate case dockets (838, 902) have 50+ docs — portal only shows 10.")
            print("    Browse manually via: https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx")
            return

        print("\nResidential candidates to download:")
        for doc in residential_candidates:
            print(f"  [{doc['docket']}] {doc['title']!r}")

        # Download all residential candidates
        print("\nDownloading residential tariff candidates...")
        downloaded = []
        for doc in residential_candidates:
            path = download_document(page, doc)
            if path:
                downloaded.append((doc, path))
            time.sleep(0.5)

        print(f"\nDownloaded {len(downloaded)}/{len(residential_candidates)} files")
        print("\nNext steps:")
        print("  1. Review downloads in:", DOWNLOAD_DIR)
        print("  2. Find the 'Schedule R' / 'Residential Service' pages in each PDF")
        print("  3. Register the specific pages:")
        print("     python -m duke_rates add-historical-document-nc \\")
        print("       --family-key nc-progress-leaf-500 \\")
        print("       --local-path <pdf_path> \\")
        print("       --start-page <n> --end-page <n> \\")
        print("       --effective-start YYYY-MM-DD")
        print("  4. Run: python -m duke_rates bootstrap-missing-versions-nc")
        print("  5. Run: python -m duke_rates enqueue-reprocess-nc --hd-id <id>")
        print("  6. Run: python -m duke_rates process-reprocess-queue-nc")

    finally:
        close_authenticated_context(pw, ctx)


if __name__ == "__main__":
    main()
