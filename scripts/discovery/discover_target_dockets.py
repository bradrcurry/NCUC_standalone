"""
Targeted NCUC docket discovery using authenticated Playwright session.
Searches specific dockets for high-priority tariff sheet filings.
"""
import sqlite3
import json
import re
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

# Target dockets — priority order
TARGETS = [
    # DEP (E-2)
    {"docket": "E-2, Sub 1354", "utility": "E", "case_sub": "2", "sub": "1354",
     "family": "nc-progress-leaf-602", "label": "DEP JAA current"},
    {"docket": "E-2, Sub 1143", "utility": "E", "case_sub": "2", "sub": "1143",
     "family": "nc-progress-leaf-602", "label": "DEP JAA historical"},
    {"docket": "E-2, Sub 1204", "utility": "E", "case_sub": "2", "sub": "1204",
     "family": "nc-progress-leaf-607", "label": "DEP STS historical"},
    {"docket": "E-2, Sub 1294", "utility": "E", "case_sub": "2", "sub": "1294",
     "family": "nc-progress-leaf-608", "label": "DEP RDM historical"},
    {"docket": "E-2, Sub 1196", "utility": "E", "case_sub": "2", "sub": "1196",
     "family": "nc-progress-leaf-604", "label": "DEP EDIT-4 historical"},
    # DEC (E-7)
    {"docket": "E-7, Sub 1243", "utility": "E", "case_sub": "7", "sub": "1243",
     "family": "nc-carolinas-rider-STS", "label": "DEC STS"},
    {"docket": "E-7, Sub 1276", "utility": "E", "case_sub": "7", "sub": "1276",
     "family": "nc-carolinas-rider-EDPR", "label": "DEC EDPR current"},
    {"docket": "E-7, Sub 1146", "utility": "E", "case_sub": "7", "sub": "1146",
     "family": "nc-carolinas-rider-EDPR", "label": "DEC EDPR historical"},
    {"docket": "E-7, Sub 1321", "utility": "E", "case_sub": "7", "sub": "1321",
     "family": "nc-carolinas-rider-STS", "label": "DEC STS Storm Debby"},
    {"docket": "E-7, Sub 1325", "utility": "E", "case_sub": "7", "sub": "1325",
     "family": "nc-carolinas-rider-STS", "label": "DEC STS Storm Helene"},
]

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

results = []
try:
    for target in TARGETS:
        docket_url = (
            f"https://starw1.ncuc.gov/NCUC/page/Dockets/portal.aspx"
            f"?Utility={target['utility']}&CaseSub={target['case_sub']}&Sub={target['sub']}"
        )
        print(f"\n=== {target['label']} ({target['docket']}) ===")
        print(f"  URL: {docket_url}")

        try:
            page.goto(docket_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            title = page.title()
            url = page.url
            content = page.content()

            print(f"  Title: {title}")
            print(f"  Final URL: {url}")

            # Look for filing links / document links
            links = page.query_selector_all("a[href*='ViewFile'], a[href*='FilingDetail'], a[href*='document']")
            print(f"  Doc links found: {len(links)}")

            for link in links[:10]:
                href = link.get_attribute("href") or ""
                text = link.inner_text().strip()[:80]
                print(f"    [{text}] -> {href[:100]}")

            # Look for filing list items
            rows = page.query_selector_all("tr.gridRow, tr.altGridRow, .filing-row, table tr")
            print(f"  Table rows: {len(rows)}")

            # Check for filing entries in the content
            filing_matches = re.findall(
                r'(Compliance Tariff|Annual Adjustment|Revised Tariff|Rider \w+|Leaf No\. \d+)'
                r'.{0,200}?(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})',
                content, re.I | re.S
            )
            for m in filing_matches[:5]:
                print(f"    Filing: {m[0][:60]} | date: {m[1]}")

            results.append({
                "docket": target["docket"],
                "label": target["label"],
                "family": target["family"],
                "url": url,
                "title": title,
                "link_count": len(links),
                "row_count": len(rows),
            })

        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({
                "docket": target["docket"],
                "label": target["label"],
                "error": str(e),
            })

finally:
    close_authenticated_context(pw, ctx)

print("\n\n=== SUMMARY ===")
for r in results:
    links = r.get("link_count", "ERR")
    rows = r.get("row_count", "ERR")
    err = r.get("error", "")
    print(f"  {r['label']}: links={links} rows={rows} {err}")
