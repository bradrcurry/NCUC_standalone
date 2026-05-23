"""
Search NCUC E-7 docket for DEC Fuel Cost Adjustment Rider (FCAR) historical filings.

COMPLETED CHAIN (as of 2026-04-17):
- E-7 Sub 982: Sep 2011 (hd=3126) — Residential 2.4304¢/kWh
- E-7 Sub 1002: Sep 2012 (hd=3125) — Residential 2.3754¢/kWh; predecessor = Sub 982
- E-7 Sub 1033: Sep 2013 (hd=3121) — Residential 2.1877¢/kWh; predecessor = Sub 1002
- E-7 Sub 1051: Sep 2014 (hd=3118) — Residential 2.2140¢/kWh; predecessor = Sub 1033
- E-7 Sub 1072: Sep 2015 (hd=3117) — Residential 2.1182¢/kWh; predecessor = Sub 1051
- E-7 Sub 1104: Sep 2016 (hd=3115) — Residential 1.7014¢/kWh; predecessor = Sub 1072
- E-7 Sub 1129: Sep 2017 (hd=3116) — Residential 1.8296¢/kWh; predecessor = Sub 1104
- E-7 Sub 1163: Sep 2018 (hd=3119) — Residential 1.6830¢/kWh; predecessor = Sub 1129
- E-7 Sub 1190: Sep 2019 (hd=3120) — Residential 1.9051¢/kWh; predecessor = Sub 1163
- E-7 Sub 1228: Sep 2020 (hd=3122) — Residential 1.7533¢/kWh (proposed; approved=1.6391¢)
- E-7 Sub 1250: Sep 2021 (hd=3124) — Residential 1.4456¢/kWh; predecessor = Sub 1228
- E-7 Sub 1263: Sep 2022 (hd=3123) — Residential 2.3100¢/kWh; predecessor = Sub 1250
- hd=483: Jan 2024 — tariff sheet format; already in DB

REMAINING GAPS:
- Sep 2023: E-7 Sub 1304 found but 1-page FULLY REDACTED (confidential). No public version.
  To find: look for NCUC Order approving Sub 1304 for the Sep 2023 composite factors,
  or use the actual tariff sheet (Leaf No. 60) filing in Sub 1304 or nearby dockets.

NOTE: Rates stored are from APPLICATIONS (proposed), not Commission ORDERS (approved).
  The Commission may approve different rates. Cross-reference example:
  Sub 1228 proposed 1.7533¢ but Sub 1250 shows Commission approved 1.6391¢.

NOTE: Application+testimony bundles (~50-190 pages). Composite approved fuel factors
  appear on the last application page (typically page 3-5) under heading
  "composite fuel and fuel-related costs factors" with lines like:
    "Residential - 2.2140¢ per kWh"
  Multiple "Version A / Version B" alternatives may appear; Version A is the primary rate.
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

# Confirmed annual FCAR sub-docket chain (from application cross-references):
# Sub 1033 (Sep 2013) -> Sub 1051 (Sep 2014) -> Sub 1072 (Sep 2015)
#   -> Sub 1104 (Sep 2016) -> Sub 1129 (Sep 2017) -> Sub 1163 (Sep 2018)
#   -> Sub 1190 (Sep 2019)
# Spacing: 18-34 dockets per year.
# Sub 1033's predecessor (2012) is unknown; estimate ~Sub 1012.
# Sub 1228 confirmed as 2020 FCAR (spacing jumped to ~38 dockets post-2019 due to COVID-era filings)
# Sub 1228's successor (2021) unknown; estimate ~Sub 1260-1269.
FCAR_SEARCH_DOCKETS = [
    # Already downloaded and registered — kept here so any new docs surface
    "E-7 Sub 1051",  # Sep 2014 CONFIRMED
    "E-7 Sub 1072",  # Sep 2015 CONFIRMED
    "E-7 Sub 1104",  # Sep 2016 CONFIRMED
    "E-7 Sub 1129",  # Sep 2017 CONFIRMED
    "E-7 Sub 1163",  # Sep 2018 CONFIRMED
    "E-7 Sub 1190",  # Sep 2019 CONFIRMED
    # Sep 2013: Sub 1033 CONFIRMED (already registered hd=3121)
    "E-7 Sub 1033",  # Sep 2013 CONFIRMED
    # Sep 2012: Sub 1002 CONFIRMED (already registered hd=3125)
    "E-7 Sub 1002",  # Sep 2012 CONFIRMED
    # Sep 2011: Sub 982 CONFIRMED (referenced in Sub 1002 as predecessor)
    "E-7 Sub 982",   # Sep 2011 CONFIRMED predecessor of Sub 1002
    # Sep 2020: Sub 1228 CONFIRMED (already registered hd=3122)
    "E-7 Sub 1228",  # Sep 2020 CONFIRMED
    # Sep 2021: Sub 1250 CONFIRMED (referenced in Sub 1263 as predecessor)
    "E-7 Sub 1250",  # Sep 2021 CONFIRMED predecessor of Sub 1263
    # Sep 2022: Sub 1263 CONFIRMED (already registered hd=3123)
    "E-7 Sub 1263",  # Sep 2022 CONFIRMED
    # Sep 2023: Sub 1304 has 7 docs (redacted), Sub 1311 or 1314 have 9/8 docs
    # Check Sub 1311 and 1314 for Sep 2023 FCAR (Sub 1263 is Sep 2022, ~38 spacing → ~1301)
    "E-7 Sub 1304",  # Sep 2023 candidate (redacted)
    "E-7 Sub 1311",  # Sep 2023 alt.
    "E-7 Sub 1314",  # Sep 2023 alt.
    "E-7 Sub 1317",  # Sep 2023 alt.
    "E-7 Sub 1320",  # Sep 2023 alt.
]
FCAR_SEARCH_DOCKETS = list(dict.fromkeys(FCAR_SEARCH_DOCKETS))

DOCKET_SEARCH_URL = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
DOCKET_FIELD = "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
SUBMIT_BTN = "input[value='Search']"

DOWNLOAD_DIR = Path("data/downloads/fcar_discovery")
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


def is_fcar_relevant(title: str) -> bool:
    """Heuristic: is this document likely to be an FCAR tariff filing?"""
    lower = title.lower()
    if "confidential" in lower:
        return False
    # Annual application format: "DEC's Application ... Kim H. Smith ... Swati V. Daji"
    # These are fuel case testimony bundles containing the proposed rates
    if "application" in lower and ("testimony" in lower or "exhibits" in lower) and (
        "smith" in lower or "daji" in lower or "mcgee" in lower or "fuel" in lower
    ):
        return True
    # R8-55 filings (newer format): "R8-55 Relating to Fuel and Fuel-Related Charge Adjustments"
    if "r8-55" in lower and ("fuel" in lower):
        return True
    # "Application to Adjust the Fuel and Fuel-Related Cost Component of Its Electric Rates"
    if "adjust" in lower and "fuel" in lower and ("cost component" in lower or "electric rates" in lower):
        return True
    # "Application of Duke Energy Pursuant to G.S. 62-133.2 & NCUC Rule R8-55"
    if "62-133.2" in lower and ("fuel" in lower or "r8-55" in lower or "testimony" in lower):
        return True
    # "Application ... G.S. 62-133.2" with testimony = fuel adjustment proceeding
    if "g.s. 62-133.2" in lower and ("testimony" in lower or "exhibits" in lower):
        return True
    return any(kw in lower for kw in [
        "fuel cost", "fuel charge adjustment", "fuel adjustment", "fuel-cost",
        "ncfuelcost", "compliance tariff", "leaf no. 60", "leaf 60",
        "fuel rider", "tariff compliance", "revised tariff",
        "annual fuel",
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
    print("FCAR Historical Discovery Search")
    print("=" * 50)
    print(f"Searching {len(FCAR_SEARCH_DOCKETS)} E-7 sub-dockets for annual FCAR filings")
    print(f"Download dir: {DOWNLOAD_DIR}")
    print()

    pw, ctx, page = create_authenticated_context(settings)

    try:
        all_documents = []
        fcar_candidates = []

        for docket in FCAR_SEARCH_DOCKETS:
            docs = search_docket(page, docket)
            all_documents.extend(docs)

            # Filter for likely FCAR filings
            for doc in docs:
                if is_fcar_relevant(doc['title']):
                    fcar_candidates.append(doc)
                    print(f"  *** FCAR CANDIDATE: {doc['title']!r} in {docket}")

            time.sleep(1.0)

        print(f"\n{'=' * 50}")
        print(f"Search complete: {len(all_documents)} total docs, {len(fcar_candidates)} FCAR candidates")
        print()

        if not fcar_candidates:
            print("No FCAR candidates found. Try these manual searches in the portal:")
            print("  - Search for 'Duke Energy Carolinas fuel cost' in text search")
            print("  - Check dockets E-7 Sub 877, E-7 Sub 897, E-7 Sub 919, E-7 Sub 939")
            return

        print("FCAR candidates to download:")
        for doc in fcar_candidates:
            print(f"  [{doc['docket']}] {doc['title']!r}")

        # Download all FCAR candidates
        print("\nDownloading FCAR candidates...")
        downloaded = []
        for doc in fcar_candidates:
            path = download_document(page, doc)
            if path:
                downloaded.append((doc, path))
            time.sleep(0.5)

        print(f"\nDownloaded {len(downloaded)}/{len(fcar_candidates)} files")
        print("\nNext steps:")
        print("  1. Review downloads in:", DOWNLOAD_DIR)
        print("  2. For each FCAR tariff sheet: find the 'Fuel Cost Adjustment Rider' page")
        print("  3. Register: python -m duke_rates add-historical-document-nc --family-key nc-carolinas-rider-FCAR ...")
        print("  4. Enqueue: python -m duke_rates reprocess enqueue-nc --hd-id <id>")
        print("  5. Process: python -m duke_rates reprocess process-queue-nc")

    finally:
        close_authenticated_context(pw, ctx)


if __name__ == "__main__":
    main()
