"""
Search NCUC E-7 docket for DEC base rate schedule compliance tariff books.

Target: RS/SGS/LGS compliance tariff books from 2015-2020.
DEC had rate cases around E-7 Sub 1100 (2013) and a later sub-docket.
This script searches for compliance tariff books containing RS/SGS/LGS schedules.
"""
import re
import time
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)

settings = Settings()

# Known E-7 sub-dockets that might contain DEC base rate compliance books
# for the 2015-2020 period. These need to be searched.
E7_SEARCH_TARGETS = [
    # DEC 2013 rate case follow-on compliance
    "E-7 Sub 1100",
    # DEC proceedings in the 2015-2020 window
    "E-7 Sub 1110",
    "E-7 Sub 1113",
    "E-7 Sub 1144",
    "E-7 Sub 1146",
    # Also try keyword searches
]

# Keyword searches to find compliance tariff books
KEYWORD_SEARCHES = [
    "Duke Energy Carolinas compliance tariff residential schedule RS 2016",
    "Duke Energy Carolinas compliance tariff RS SGS 2017",
    "Duke Energy Carolinas compliance tariff 2018 base rate schedule",
    "Duke Energy Carolinas base rate compliance 2019",
    "DEC compliance tariff RS residential service",
]

DOCKET_SEARCH_URL = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
DOCKET_FIELD = "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
SUBMIT_BTN = "input[value='Search']"


def search_docket(page, docket_str: str, max_results: int = 50) -> list[dict]:
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
        print(f"  ERROR: Could not find Submit button")
        return []

    submit.click()
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    results = extract_results(page, docket_str)
    print(f"  Found {len(results)} documents")
    return results[:max_results]


def keyword_search(page, query: str, max_results: int = 30) -> list[dict]:
    """Keyword search on the NCUC portal."""
    print(f"\n--- Keyword search: {query[:60]} ---")
    # Use the document search with just text
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    # Find and fill keyword field
    kw_field = page.query_selector(
        "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_fullTextSearchTextBox"
    )
    if not kw_field:
        print("  ERROR: Could not find keyword field")
        return []

    kw_field.fill(query)
    submit = page.query_selector(SUBMIT_BTN)
    if submit:
        submit.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

    results = extract_results(page, query)
    print(f"  Found {len(results)} documents")
    return results[:max_results]


def extract_results(page, query_label: str) -> list[dict]:
    """Extract document entries from search results page."""
    html = page.content()
    results = []

    # Extract document links
    link_pattern = re.compile(
        r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&(?:amp;)?Class=(\w+))["\']',
        re.I,
    )
    title_pattern = re.compile(r'DocumentId=[0-9a-f\-]{36}&(?:amp;)?Class=\w+["\'][^>]*>([^<]+)<', re.I)

    seen_ids = set()
    for match in link_pattern.finditer(html):
        doc_id = match.group(2)
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        doc_class = match.group(3)
        href = f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}"
        results.append({
            "doc_id": doc_id,
            "doc_class": doc_class,
            "href": href,
            "query": query_label,
        })

    # Try to extract titles from surrounding text
    # Look for filing dates and titles near document links
    date_pattern = re.compile(r'(\d{1,2}/\d{1,2}/\d{4})')
    filing_text_pattern = re.compile(
        r'DocumentId=[0-9a-f\-]{36}[^>]+>([^<]{10,200})</a>',
        re.I,
    )
    titles = {}
    for m in filing_text_pattern.finditer(html):
        title_text = m.group(1).strip()
        # Try to find this near a doc_id
        start = max(0, m.start() - 500)
        nearby = html[start:m.end()]
        for lm in link_pattern.finditer(nearby):
            did = lm.group(2)
            if title_text and len(title_text) > 5:
                titles[did] = title_text[:100]

    for r in results:
        r["filing_title"] = titles.get(r["doc_id"], "")

    return results


def is_dec_base_rate_tariff(result: dict) -> bool:
    """Filter: does this look like a DEC base rate schedule compliance tariff?"""
    title = (result.get("filing_title") or "").lower()
    keywords = ["compliance tariff", "rate schedule", "rs", "sgs", "lgs", "residential"]
    return any(kw in title for kw in keywords)


def main():
    pw, ctx, page = create_authenticated_context(settings)
    all_results = []

    try:
        # Search by docket number
        for docket in E7_SEARCH_TARGETS:
            results = search_docket(page, docket, max_results=100)
            # Filter for likely compliance tariff books
            for r in results:
                r["search_type"] = "docket"
                r["docket_hint"] = docket
            all_results.extend(results)
            time.sleep(1)

        # Keyword searches
        for query in KEYWORD_SEARCHES:
            results = keyword_search(page, query, max_results=30)
            for r in results:
                r["search_type"] = "keyword"
                r["docket_hint"] = "E-7"
            all_results.extend(results)
            time.sleep(1)

    finally:
        close_authenticated_context(pw, ctx)

    # Deduplicate by doc_id
    seen = set()
    unique = []
    for r in all_results:
        if r["doc_id"] not in seen:
            seen.add(r["doc_id"])
            unique.append(r)

    print(f"\n=== RESULTS SUMMARY ===")
    print(f"Total unique documents found: {len(unique)}")

    # Print filtered results (likely base rate tariffs)
    candidates = [r for r in unique if is_dec_base_rate_tariff(r)]
    print(f"Filtered candidates (likely DEC base rate tariffs): {len(candidates)}")

    print("\nAll unique results:")
    for r in unique:
        marker = "***" if is_dec_base_rate_tariff(r) else "   "
        print(f"  {marker} [{r['search_type'][:3]}] {r['docket_hint']:15} | {r.get('filing_title', '')[:70]}")
        print(f"       {r['href']}")

    # Save results
    output_path = Path("docs/reports/dec_base_rate_search_results.txt")
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w") as f:
        for r in unique:
            marker = "CANDIDATE" if is_dec_base_rate_tariff(r) else "other"
            f.write(f"[{marker}] {r['search_type']} | {r['docket_hint']} | {r.get('filing_title', '')}\n")
            f.write(f"  {r['href']}\n\n")

    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()
