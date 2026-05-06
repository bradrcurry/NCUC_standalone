"""
Try NCUC text search for DEC EDPR and DEP EDIT-4 tariff filings.
These dockets return wrong results with parameter search.
"""
import re
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

SEARCHES = [
    {"query": "Leaf No. 64", "label": "DEC Leaf 64 (EDPR)"},
    {"query": "Rider EDPR", "label": "DEC Rider EDPR"},
    {"query": "Leaf No. 604", "label": "DEP Leaf 604 (EDIT-4)"},
    {"query": "EDIT-4 compliance tariff", "label": "DEP EDIT-4 compliance"},
]

try:
    for search in SEARCHES:
        query = search["query"]
        label = search["label"]

        print(f"\n=== {label} (query: {query!r}) ===")
        page.goto("https://starw1.ncuc.gov/NCUC/page/DocumentsTextSearch/portal.aspx",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # Find the text search input
        inputs = page.query_selector_all("input[type='text']")
        print(f"  Text inputs: {[inp.get_attribute('name') or inp.get_attribute('id') for inp in inputs]}")

        # Find a search box
        text_input = None
        for inp in inputs:
            name = inp.get_attribute("name") or ""
            if "search" in name.lower() or "text" in name.lower() or "query" in name.lower() or "keyword" in name.lower():
                text_input = inp
                break
        if not text_input and inputs:
            text_input = inputs[0]

        if not text_input:
            print("  No text input found")
            body = page.inner_text("body")
            print(f"  Page body: {body[:500]}")
            continue

        text_input.fill(query)
        submit = page.query_selector("input[value='Search'], button[type='submit'], input[type='submit']")
        if submit:
            submit.click()
        else:
            text_input.press("Enter")

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        content = page.content()
        doc_ids = re.findall(r'DocumentId=([0-9a-f\-]{36})', content, re.I)
        doc_ids = list(dict.fromkeys(doc_ids))
        print(f"  Results: {len(doc_ids)} docs")

        rows = page.query_selector_all("table tr")
        tariff_rows = 0
        for row in rows:
            text = row.inner_text().strip()
            if "tariff" in text.lower() or "leaf" in text.lower() or "compliance" in text.lower():
                tariff_rows += 1
                print(f"    Tariff row: {text[:100]}")
                doc_links = row.query_selector_all("a[href*='PSCDocumentDetailsPageNCUC']")
                for dl in doc_links:
                    href = dl.get_attribute("href") or ""
                    print(f"      Link: {href[:100]}")

        if tariff_rows == 0:
            # Show first few rows
            non_empty = [r.inner_text().strip() for r in rows if len(r.inner_text().strip()) > 10]
            for r in non_empty[:5]:
                print(f"    Row: {r[:100]}")

finally:
    close_authenticated_context(pw, ctx)
