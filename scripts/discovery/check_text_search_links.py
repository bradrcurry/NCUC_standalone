"""Check actual link structure on text search results page."""
import re
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

try:
    page.goto("https://starw1.ncuc.gov/NCUC/page/DocumentsTextSearch/portal.aspx",
              wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    text_input = page.query_selector(
        "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentFullTextSearchControl1_searchPhrase"
    )
    text_input.fill("Rider EDPR")
    submit = page.query_selector("input[value='Search']")
    submit.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)

    content = page.content()
    print(f"Page length: {len(content)}")

    # Find ALL links
    all_links = re.findall(r'href=["\']([^"\']+)["\']', content)
    unique_links = list(dict.fromkeys(all_links))
    print(f"\nAll unique links ({len(unique_links)}):")
    for l in unique_links:
        if not l.startswith("#") and "javascript" not in l and "portal.aspx" not in l.lower():
            print(f"  {l[:120]}")

    # Also look at raw HTML around EDPR mentions
    edpr_ctx = re.findall(r'.{200}EDPR.{200}', content, re.S)
    print(f"\nContext around 'EDPR' ({len(edpr_ctx)} occurrences):")
    for ctx_snippet in edpr_ctx[:3]:
        print(f"  ...{ctx_snippet[:300]}...")

finally:
    close_authenticated_context(pw, ctx)
