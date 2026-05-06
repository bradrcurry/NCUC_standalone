"""
Inspect a PSCDocumentDetailsPageNCUC page to find PDF download links.
Also explore how to get full DocumentIds from search results.
"""
import re
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

try:
    # First: go to search results for E-2 Sub 1354 and extract FULL document IDs from HTML
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    docket_input = page.query_selector("#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber")
    docket_input.fill("E-2 Sub 1354")
    submit = page.query_selector("input[value='Search']")
    submit.click()
    page.wait_for_timeout(3000)

    # Extract FULL document IDs from HTML
    content = page.content()
    doc_ids = re.findall(r'DocumentId=([0-9a-f\-]{36})', content, re.I)
    doc_ids = list(dict.fromkeys(doc_ids))  # deduplicate while preserving order
    print(f"Full DocumentIds found: {len(doc_ids)}")
    for did in doc_ids[:15]:
        print(f"  {did}")

    # Also check row data to see filing titles
    rows = page.query_selector_all("table tr")
    print(f"\nDocument rows (with content):")
    for row in rows:
        text = row.inner_text().strip()
        if text and ("Filed" in text or "Filing" in text or "Compliance" in text or "Tariff" in text or "Leaf" in text or "Rider" in text):
            print(f"  {text[:150]}")

    # Now explore a document detail page
    if doc_ids:
        first_doc_id = doc_ids[0]
        doc_detail_url = f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={first_doc_id}"
        print(f"\n\n=== Document Detail Page ===")
        print(f"  URL: {doc_detail_url}")
        page.goto(doc_detail_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        print(f"  Title: {page.title()}")

        # Get body text
        body = page.inner_text("body")
        print(f"\n  Body text:\n{body[:2000]}")

        # Get all links
        links = page.query_selector_all("a")
        print(f"\n  Links ({len(links)}):")
        for link in links:
            href = link.get_attribute("href") or ""
            text = link.inner_text().strip()[:60]
            if href and ("pdf" in href.lower() or "file" in href.lower() or "download" in href.lower() or "view" in href.lower()):
                print(f"    [{text}] -> {href}")

        # Get full page content
        detail_content = page.content()
        # Find any file/download URLs
        file_urls = re.findall(r'(https?://[^\s"\'<>]+\.pdf)', detail_content, re.I)
        getfile_urls = re.findall(r'href=["\']([^"\']*(?:GetFile|ViewFile|download|\.pdf)[^"\']*)["\']', detail_content, re.I)
        print(f"\n  PDF URLs in content: {file_urls[:5]}")
        print(f"  GetFile/ViewFile URLs: {getfile_urls[:10]}")

        # Show all links
        all_links = re.findall(r'href=["\']([^"\']+)["\']', detail_content)
        print(f"\n  ALL href links ({len(all_links)}):")
        for lnk in all_links:
            if lnk.startswith("#") or "javascript" in lnk or "portal.aspx" in lnk.lower():
                continue
            print(f"    {lnk[:120]}")

finally:
    close_authenticated_context(pw, ctx)
