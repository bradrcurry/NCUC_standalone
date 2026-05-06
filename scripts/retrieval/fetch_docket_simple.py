#!/usr/bin/env python
"""
Simple NCUC docket search and download.

Target: E-2, Sub 1076 (DEP 2015 rate case)

Uses create_authenticated_context() to log in, then searches the docket
and downloads rate-relevant PDFs.

Usage:
    python scripts/retrieval/fetch_docket_simple.py
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
docket = "E-2 Sub 1076"
dest_dir = Path("data/historical/ncuc/e-2-sub-1076")

print(f"\nFetching documents from: {docket}")
print(f"Destination: {dest_dir}\n")

pw, ctx, page = create_authenticated_context(settings)
print("[OK] Authenticated with NCUC portal")

try:
    # Navigate to document search
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    print(f"Navigating to search page...")
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    # Fill in docket number
    print(f"Searching for docket: {docket}")
    docket_input = page.query_selector(
        "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
    )
    docket_input.fill(docket)

    # Submit search
    submit = page.query_selector("input[value='Search']")
    submit.click()
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    # Extract and display found documents
    content = page.content()
    link_pattern = re.compile(
        r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+))["\']',
        re.I,
    )

    docs = {}
    for match in link_pattern.finditer(content):
        doc_id = match.group(2)
        doc_class = match.group(3)
        docs[doc_id] = {
            "id": doc_id,
            "class": doc_class,
            "href": f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}",
        }

    print(f"\nFound {len(docs)} documents")

    # Extract titles from table
    rows = page.query_selector_all("table tr")
    for row in rows:
        row_links = row.query_selector_all("a[href*='PSCDocumentDetailsPageNCUC']")
        if not row_links:
            continue
        href = row_links[0].get_attribute("href") or ""
        m = re.search(r"DocumentId=([0-9a-f\-]{36})", href, re.I)
        if not m:
            continue
        doc_id = m.group(1)
        row_text = row.inner_text()
        lines = [l.strip() for l in row_text.split("\n") if l.strip()]
        title = ""
        for line in lines:
            if not re.match(r"^(Filing|Order|Other|Filed In:|Date Filed:|Items Count:)", line):
                if len(line) > 5:
                    title = line
                    break
        date_m = re.search(r"Date Filed:\s*(\d{1,2}/\d{1,2}/\d{4})", row_text)
        date_filed = date_m.group(1) if date_m else ""
        if doc_id in docs:
            docs[doc_id]["title"] = title
            docs[doc_id]["date"] = date_filed

    # Show documents
    print("\nDocuments found:")
    for doc_id, doc in list(docs.items())[:50]:
        print(f"  {doc['date']:10s} {doc.get('title', 'unknown')[:70]}")
    if len(docs) > 50:
        print(f"  ... and {len(docs)-50} more")

    # Download rate-relevant documents
    dest_dir.mkdir(parents=True, exist_ok=True)
    skip_patterns = [
        r"motion\b", r"brief\b", r"testimony\b", r"notice\b", r"certificate",
        r"letter\b", r"application\b", r"petition\b", r"procedural",
        r"discovery\b", r"data request", r"exhibit\s+[a-z](?!\s*(tariff|rate|leaf))",
    ]
    skip_re = re.compile("|".join(skip_patterns), re.I)

    print(f"\nDownloading rate-relevant documents...")
    downloaded = 0
    for doc_id, doc in docs.items():
        title = doc.get("title", "")
        if skip_re.search(title):
            continue

        safe_id = doc_id.replace("-", "")[:32]
        dest_path = dest_dir / f"{safe_id}.pdf"
        if dest_path.exists():
            continue

        try:
            with page.expect_download(timeout=45000) as dl_info:
                try:
                    page.goto(doc["href"], wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass

            download = dl_info.value
            download.save_as(str(dest_path))
            size_kb = dest_path.stat().st_size // 1024
            print(f"  [OK] {dest_path.name} ({size_kb} KB)")
            downloaded += 1
        except Exception as e:
            print(f"  ✗ {doc_id}: {e}")

        time.sleep(1)

    print(f"\nDownloaded {downloaded} documents to {dest_dir}")

finally:
    close_authenticated_context(pw, ctx)
    print("[OK] Closed browser context\n")
