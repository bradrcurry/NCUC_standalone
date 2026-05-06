"""
Search NCUC portal for filings in specific dockets and extract document download links.
"""
import json
import re
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

DOCKET_SEARCHES = [
    {"docket": "E-2 Sub 1354", "label": "DEP JAA current"},
    {"docket": "E-2 Sub 1143", "label": "DEP JAA historical"},
    {"docket": "E-2 Sub 1204", "label": "DEP STS historical"},
    {"docket": "E-7 Sub 1243", "label": "DEC STS current"},
    {"docket": "E-7 Sub 1276", "label": "DEC EDPR current"},
    {"docket": "E-7 Sub 1321", "label": "DEC STS Debby"},
    {"docket": "E-7 Sub 1325", "label": "DEC STS Helene"},
]

def search_docket(docket_str, label):
    """Search for compliance tariff filings in a docket."""
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    # Fill in docket number
    docket_input = page.query_selector("#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber")
    if not docket_input:
        print(f"  ERROR: Could not find docket input")
        return []

    docket_input.fill(docket_str)

    # Submit search
    submit = page.query_selector("input[value='Search']")
    submit.click()
    page.wait_for_timeout(3000)

    print(f"\n=== {label} ({docket_str}) ===")
    print(f"  Result URL: {page.url}")
    print(f"  Title: {page.title()}")

    # Get all links - look for ViewFile or document download links
    content = page.content()

    # Find document view links
    view_links = re.findall(r'href=["\']([^"\']*ViewFile[^"\']*)["\']', content, re.I)
    doc_links = re.findall(r'href=["\']([^"\']*(?:getfile|download|document)[^"\']*)["\']', content, re.I)

    # Get table rows
    rows = page.query_selector_all("table tr")
    print(f"  Table rows: {len(rows)}")

    # Show first several rows
    results = []
    for i, row in enumerate(rows[:20]):
        text = row.inner_text().strip()
        if text and len(text) > 5:
            # Find any links in this row
            links_in_row = row.query_selector_all("a")
            row_links = []
            for link in links_in_row:
                href = link.get_attribute("href") or ""
                link_text = link.inner_text().strip()
                if href:
                    row_links.append(f"[{link_text[:40]}]->{href[:80]}")

            if i < 10 or row_links:
                print(f"  Row {i}: {text[:100]}")
                for rl in row_links:
                    print(f"    link: {rl}")
                    results.append(rl)

    print(f"  ViewFile links found: {len(view_links)}")
    for vl in view_links[:5]:
        print(f"    {vl[:100]}")

    # Also look at the body text to understand pagination
    body = page.inner_text("body")
    # Find text mentioning filings count
    count_match = re.search(r'(\d+)\s+(?:records?|results?|document)', body, re.I)
    if count_match:
        print(f"  Count mention: {count_match.group(0)}")

    return view_links

all_results = {}
try:
    for item in DOCKET_SEARCHES:
        links = search_docket(item["docket"], item["label"])
        all_results[item["docket"]] = links

finally:
    close_authenticated_context(pw, ctx)

print("\n\n=== SUMMARY ===")
for docket, links in all_results.items():
    print(f"  {docket}: {len(links)} ViewFile links")
