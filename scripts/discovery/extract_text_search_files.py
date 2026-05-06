"""
Extract ViewFile URLs directly from NCUC text search results.
Text search shows ViewFile links inline, unlike parameter search.

Download strategy for EDPR + EDIT-4 compliance tariffs.
"""
import re
import json
from pathlib import Path
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

SEARCHES = [
    {"query": "Rider EDPR", "label": "DEC EDPR", "family": "nc-carolinas-rider-edpr"},
    {"query": "Leaf No. 604", "label": "DEP EDIT-4 Leaf 604", "family": "nc-progress-leaf-604"},
    {"query": "EDIT-4", "label": "DEP EDIT-4", "family": "nc-progress-leaf-604"},
    {"query": "Joint Agency Asset Rider compliance", "label": "DEP JAA compliance", "family": "nc-progress-leaf-602"},
]

HIGH_VALUE_DOC_TITLE_PATTERNS = [
    r"compliance tariff",
    r"revised tariff",
    r"compliance exhibit",
    r"compliance filing",
    r"annual adjustment",
    r"rider edpr",
    r"edpr",
    r"edit-4",
    r"edit4",
    r"leaf no",
    r"tariff filing",
]

def parse_text_search_results(content):
    """
    Parse NCUC full text search HTML to extract document titles + ViewFile URLs.

    Structure:
    - Each result has <span class="documentTitle">TITLE</span>
    - Immediately followed by ViewFile.aspx links
    - Also has DocketDetails link for docket context
    """
    # Find all result blocks
    # Pattern: documentTitle span followed by ViewFile links
    title_pattern = re.compile(r'<span class="documentTitle">([^<]+)</span>', re.I)
    # Full result block: from documentTitle to next documentTitle or end

    results = []
    # Split content around document title spans
    parts = re.split(r'<span class="documentTitle">', content)

    for i, part in enumerate(parts[1:], 1):  # skip preamble
        # Extract title
        title_match = re.match(r'([^<]+)</span>', part)
        title = title_match.group(1).strip() if title_match else ""

        # Extract ViewFile links in this block (up to next result)
        view_urls = re.findall(
            r'href=["\']?(https://starw1\.ncuc\.gov/NCUC/ViewFile\.aspx\?Id=[0-9a-f\-]{36})["\']?',
            part
        )

        # Extract docket context
        docket_links = re.findall(
            r'DocketDetails\.aspx\?DocketId=[0-9a-f\-]{36}[^"\']*',
            part
        )

        # Extract date (may appear in various formats)
        date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', part)
        date_filed = date_match.group(1) if date_match else ""

        if title and view_urls:
            results.append({
                "title": title,
                "view_urls": view_urls,
                "date_filed": date_filed,
                "docket_links_count": len(docket_links),
            })

    return results


all_files = []

try:
    for search in SEARCHES:
        query = search["query"]
        label = search["label"]
        family = search["family"]

        print(f"\n=== {label} (query: {query!r}) ===")
        page.goto("https://starw1.ncuc.gov/NCUC/page/DocumentsTextSearch/portal.aspx",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        text_input = page.query_selector(
            "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentFullTextSearchControl1_searchPhrase"
        )
        text_input.fill(query)
        submit = page.query_selector("input[value='Search']")
        submit.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        content = page.content()
        results = parse_text_search_results(content)

        print(f"  Results: {len(results)} documents with files")
        for r in results:
            is_hv = any(re.search(p, r["title"].lower()) for p in HIGH_VALUE_DOC_TITLE_PATTERNS)
            marker = "[HV]" if is_hv else "    "
            print(f"  {marker} [{r['date_filed']}] {r['title'][:80]} ({len(r['view_urls'])} files)")

        # Collect high-value entries
        for r in results:
            if any(re.search(p, r["title"].lower()) for p in HIGH_VALUE_DOC_TITLE_PATTERNS):
                for url in r["view_urls"]:
                    # Extract file ID from URL
                    file_id_match = re.search(r'Id=([0-9a-f\-]{36})', url, re.I)
                    file_id = file_id_match.group(1) if file_id_match else "unknown"
                    all_files.append({
                        "query": query,
                        "label": label,
                        "family": family,
                        "doc_title": r["title"],
                        "date_filed": r["date_filed"],
                        "filename": f"{file_id}_text_search",
                        "view_url": url,
                        "doc_id": file_id,
                    })

finally:
    close_authenticated_context(pw, ctx)

# Deduplicate by view_url
seen_urls = set()
deduped = []
for f in all_files:
    if f["view_url"] not in seen_urls:
        seen_urls.add(f["view_url"])
        deduped.append(f)

out_path = Path("data/ncuc_edpr_edit4_filings.json")
with open(out_path, "w") as f_out:
    json.dump(deduped, f_out, indent=2)

print(f"\n\n=== SUMMARY ===")
print(f"High-value files found: {len(deduped)}")
for item in deduped:
    print(f"  [{item['family']}] {item['date_filed']} | {item['doc_title'][:60]}")
    print(f"    {item['view_url']}")
print(f"\nSaved to {out_path}")
