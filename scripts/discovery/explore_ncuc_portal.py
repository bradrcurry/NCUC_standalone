"""
Explore NCUC portal page structure to find document links.
"""
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

try:
    # Try the dockets page and inspect the actual content
    url = "https://starw1.ncuc.gov/NCUC/page/Dockets/portal.aspx?Utility=E&CaseSub=2&Sub=1354"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    print("=== Page title:", page.title())
    print("=== Page URL:", page.url)

    # Get all links on the page
    links = page.query_selector_all("a")
    print(f"\n=== All links ({len(links)}) ===")
    for link in links[:30]:
        href = link.get_attribute("href") or ""
        text = link.inner_text().strip()[:60]
        if text or href:
            print(f"  [{text}] -> {href[:100]}")

    # Look at table structure
    tables = page.query_selector_all("table")
    print(f"\n=== Tables: {len(tables)} ===")
    for i, table in enumerate(tables[:3]):
        rows = table.query_selector_all("tr")
        print(f"  Table {i}: {len(rows)} rows")
        for j, row in enumerate(rows[:3]):
            print(f"    Row {j}: {row.inner_text()[:100].strip()}")

    # Get page content snippet to understand structure
    content = page.content()
    print(f"\n=== Content length: {len(content)} ===")

    # Look for filing-related content in the HTML
    import re
    filing_hints = re.findall(r'(FilingDetail|ViewFile|DocketDetails|DocumentList|\.aspx[^"\']{0,100})', content)
    for h in list(set(filing_hints))[:15]:
        print(f"  {h[:100]}")

    # Try DocketDetails page
    print("\n\n=== Trying DocketDetails URL ===")
    # The login test revealed DocketId format
    docket_url = "https://starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx?DocketId=9b3614b6-11d6-4703-bca9-60fe8e5a5b42"
    page.goto(docket_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    print(f"Title: {page.title()}, URL: {page.url}")

    links2 = page.query_selector_all("a")
    print(f"Links: {len(links2)}")
    for link in links2[:20]:
        href = link.get_attribute("href") or ""
        text = link.inner_text().strip()[:60]
        if text:
            print(f"  [{text}] -> {href[:100]}")

    # Try to find specific filings for E-2 Sub 1354
    # NCUC portal uses a search form — try finding it
    print("\n\n=== Looking for search/filter form ===")
    inputs = page.query_selector_all("input, select")
    for inp in inputs[:15]:
        name = inp.get_attribute("name") or ""
        val = inp.get_attribute("value") or ""
        inp_type = inp.get_attribute("type") or ""
        print(f"  input name={name} type={inp_type} value={val[:40]}")

finally:
    close_authenticated_context(pw, ctx)
