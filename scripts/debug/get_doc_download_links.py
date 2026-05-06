"""
Navigate to document detail pages via clicking from search results.
Find the actual PDF download URLs.
"""
import re
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

try:
    # Search for E-2 Sub 1143 (JAA historical — more filings with compliance tariffs)
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    docket_input = page.query_selector("#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber")
    docket_input.fill("E-2 Sub 1143")
    submit = page.query_selector("input[value='Search']")
    submit.click()
    page.wait_for_timeout(3000)

    content = page.content()
    doc_ids = re.findall(r'DocumentId=([0-9a-f\-]{36})', content, re.I)
    doc_ids = list(dict.fromkeys(doc_ids))
    print(f"Found {len(doc_ids)} document IDs in search results")

    # Try clicking the first document icon link (no text, just icon)
    # These appear as blank links with PSCDocumentDetailsPageNCUC
    doc_links = page.query_selector_all(f"a[href*='PSCDocumentDetailsPageNCUC']")
    print(f"Document detail links found: {len(doc_links)}")
    if doc_links:
        first_href = doc_links[0].get_attribute("href")
        print(f"First doc href: {first_href}")

        # Navigate by clicking within same page context
        doc_links[0].click()
        page.wait_for_timeout(3000)

        print(f"After click - Title: {page.title()}")
        print(f"After click - URL: {page.url}")

        body = page.inner_text("body")
        print(f"\nBody text:\n{body[:3000]}")

        detail_content = page.content()
        # Look for GetFile or ViewFile URLs
        getfile = re.findall(r'href=["\']([^"\']*(?:GetFile|ViewFile|getfile|viewfile)[^"\']*)["\']', detail_content)
        pdf_links = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', detail_content, re.I)
        all_doc_links = re.findall(r'href=["\']([^"\']+(?:file|document|download|view)[^"\']*)["\']', detail_content, re.I)
        print(f"\nGetFile/ViewFile links: {getfile[:10]}")
        print(f"PDF links: {pdf_links[:10]}")
        print(f"All doc links: {all_doc_links[:15]}")

        # Look for all links on the page
        links = page.query_selector_all("a")
        print(f"\nAll links ({len(links)}):")
        for link in links:
            href = link.get_attribute("href") or ""
            text = link.inner_text().strip()[:80]
            if href and not href.startswith("javascript") and "portal.aspx" not in href.lower():
                print(f"  [{text}] -> {href[:120]}")

finally:
    close_authenticated_context(pw, ctx)
