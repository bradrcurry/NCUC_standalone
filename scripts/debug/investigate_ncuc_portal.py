#!/usr/bin/env python
"""
Investigate the NCUC portal to understand how PDFs are served.

This script will:
1. Load the portal page
2. Capture all network requests
3. Log what response types we get
4. Help us understand the correct way to fetch the PDF
"""

import logging
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def investigate_ncuc_portal():
    """Investigate how the NCUC portal serves PDFs."""

    url = "https://starw1.ncuc.gov/NCUC/PSC/ViewFile.aspx?DocumentID=37985119-bc3e-4fa0-9d52-17a11d0ef2f0"

    print("=" * 70)
    print("INVESTIGATING NCUC PORTAL")
    print("=" * 70)
    print(f"\nURL: {url}\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)  # Show browser to see what's happening
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = context.new_page()

        # Track all network activity
        responses_captured = []

        def capture_response(response):
            ct = response.headers.get("content-type", "")
            url_path = response.url.split("?")[0].split("/")[-1]
            responses_captured.append({
                'url': response.url,
                'status': response.status,
                'content_type': ct,
                'method': response.request.method,
            })
            print(f"Response: {response.status} | {url_path[:40]:40s} | {ct[:30]}")

        page.on("response", capture_response)

        print("Loading page...\n")
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"Goto failed: {e}")

        print("\nWaiting 5 seconds for any delayed requests...")
        page.wait_for_timeout(5000)

        # Check page content
        print("\n" + "=" * 70)
        print("PAGE ANALYSIS")
        print("=" * 70)

        title = page.title()
        print(f"Page title: {title}")

        # Check for iframes
        iframes = page.query_selector_all("iframe")
        print(f"\nIframes found: {len(iframes)}")
        for i, iframe in enumerate(iframes):
            src = iframe.get_attribute("src")
            print(f"  {i+1}. {src}")

        # Check for objects/embeds
        objects = page.query_selector_all("object, embed")
        print(f"\nObjects/Embeds found: {len(objects)}")
        for i, obj in enumerate(objects):
            data = obj.get_attribute("data") or obj.get_attribute("src")
            print(f"  {i+1}. {data}")

        # Check for download links
        links = page.query_selector_all("a")
        print(f"\nDownload-related links:")
        for link in links:
            href = link.get_attribute("href")
            text = link.text_content()
            if href and ("download" in href.lower() or "pdf" in href.lower()):
                print(f"  {text[:30]:30s} -> {href}")

        # Try to get PDF via network inspection
        print("\n" + "=" * 70)
        print("NETWORK ACTIVITY SUMMARY")
        print("=" * 70)

        pdf_responses = [r for r in responses_captured if 'pdf' in r['content_type'].lower()]
        if pdf_responses:
            print(f"\nPDF responses found: {len(pdf_responses)}")
            for r in pdf_responses:
                print(f"  {r['url']}")
        else:
            print("\nNo direct PDF responses found")

        print(f"\nTotal responses captured: {len(responses_captured)}")
        print("\nTop 10 responses:")
        for r in responses_captured[:10]:
            method = r['method'] if r['method'] else 'GET'
            url_short = r['url'][-50:] if len(r['url']) > 50 else r['url']
            print(f"  {r['status']} | {method:4s} | {url_short}")

        # Keep browser open for manual inspection
        print("\n" + "=" * 70)
        print("Browser window is open. Inspect the page manually if needed.")
        print("Press ENTER to close...")
        # input()

        browser.close()


if __name__ == "__main__":
    investigate_ncuc_portal()
