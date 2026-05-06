"""
Scrape NCUC portal for compliance tariff filings from target dockets.

Navigation strategy (discovered 2026-03-28):
1. POST to DocumentsParameterSearch/portal.aspx with docket number + optional company name
2. Each result row has PSCDocumentDetailsPageNCUC.aspx?DocumentId=GUID&Class=Filing link
3. Document detail page has ViewFile.aspx?Id=GUID link for actual file download
4. The detail page requires a session cookie — must navigate by clicking/href within same session

Key URL patterns:
- Search: https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx
- Detail: https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={GUID}&Class=Filing
- File:   https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id={GUID}

Pagination: uses ASP.NET __doPostBack with page numbers visible as links [2], [3], etc.

The portal is Cloudflare-protected -- direct HTTP returns 403.
Playwright with an authenticated session (create_authenticated_context) is required.
Login uses NCID credentials from .env (DUKE_RATES_NCID_USERNAME / DUKE_RATES_NCID_PASSWORD).

Document row structure in search results (inner_text pattern):
  "TITLE_LINE\\nFiling\\n[icon]\\nFiled In: DOCKET\\nDate Filed: MM/DD/YYYY"

Title is extracted by skipping lines that are "Filing", "Order", "Filed In:...", "Date Filed:...",
and taking the first meaningful line.
"""
import re
import json
from pathlib import Path
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()

# Target dockets — priority compliance filing dockets for Duke Energy riders
TARGETS = [
    # DEP (E-2) — Joint Agency Asset Rider (JAA)
    {"docket": "E-2 Sub 1354", "label": "DEP JAA current", "family": "nc-progress-leaf-602",
     "company": "Duke Energy Progress",
     "keywords": ["tariff", "leaf", "rider", "compliance", "JAA", "JAAR", "annual adjustment"]},
    {"docket": "E-2 Sub 1143", "label": "DEP JAA historical", "family": "nc-progress-leaf-602",
     "company": "Duke Energy Progress",
     "keywords": ["tariff", "leaf", "rider", "compliance", "JAA", "JAAR", "annual adjustment"]},
    # DEP (E-2) — Storm Securitization
    {"docket": "E-2 Sub 1204", "label": "DEP STS", "family": "nc-progress-leaf-607",
     "company": "Duke Energy Progress",
     "keywords": ["tariff", "leaf", "STS", "storm", "compliance", "annual adjustment"]},
    # DEP (E-2) — Revenue Decoupling
    {"docket": "E-2 Sub 1294", "label": "DEP RDM", "family": "nc-progress-leaf-608",
     "company": "Duke Energy Progress",
     "keywords": ["tariff", "leaf", "RDM", "decoupling", "compliance", "annual adjustment"]},
    # DEP (E-2) — Excess Deferred Income Tax
    {"docket": "E-2 Sub 1196", "label": "DEP EDIT-4", "family": "nc-progress-leaf-604",
     "company": "Duke Energy Progress",
     "keywords": ["tariff", "leaf", "EDIT", "compliance", "annual adjustment"]},
    # DEC (E-7) — Storm Securitization
    {"docket": "E-7 Sub 1243", "label": "DEC STS current", "family": "nc-carolinas-rider-sts",
     "company": "Duke Energy Carolinas",
     "keywords": ["tariff", "leaf", "STS", "storm", "securitization", "compliance", "annual adjustment"]},
    {"docket": "E-7 Sub 1321", "label": "DEC STS Debby", "family": "nc-carolinas-rider-sts",
     "company": "Duke Energy Carolinas",
     "keywords": ["tariff", "leaf", "STS", "Debby", "storm", "compliance"]},
    {"docket": "E-7 Sub 1325", "label": "DEC STS Helene", "family": "nc-carolinas-rider-sts",
     "company": "Duke Energy Carolinas",
     "keywords": ["tariff", "leaf", "STS", "Helene", "storm", "compliance"]},
    # DEC (E-7) — Existing DSM Program Costs Rider (EDPR)
    {"docket": "E-7 Sub 1276", "label": "DEC EDPR current", "family": "nc-carolinas-rider-edpr",
     "company": "Duke Energy Carolinas",
     "keywords": ["tariff", "leaf", "EDPR", "DSM", "compliance", "annual adjustment"]},
    {"docket": "E-7 Sub 1146", "label": "DEC EDPR historical", "family": "nc-carolinas-rider-edpr",
     "company": "Duke Energy Carolinas",
     "keywords": ["tariff", "leaf", "EDPR", "DSM", "compliance", "annual adjustment"]},
]

# Titles that indicate a compliance tariff exhibit
HIGH_VALUE_TITLE_PATTERNS = [
    r"compliance tariff",
    r"revised tariff",
    r"leaf no",
    r"annual adjustment",
    r"tariff sheet",
    r"revised leaf",
    r"annual compliance",
    r"tariff filing",
    r"revised rider",
]


def extract_title_from_row_text(text):
    """
    Extract the document title from search result row text.
    Row format: "TITLE\nFiling\n[whitespace/icon]\nFiled In: ...\nDate Filed: ..."
    Skip boilerplate lines to get the real title.
    """
    skip_patterns = [
        r"^(Filing|Order|Other)$",
        r"^Filed In:",
        r"^Date Filed:",
        r"^Search for Document",
        r"^Sorted By",
        r"^Ascending$",
        r"^Descending$",
        r"^Click the",
        r"^Items Count:",
    ]
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for line in lines:
        skip = False
        for pat in skip_patterns:
            if re.match(pat, line, re.I):
                skip = True
                break
        if not skip and len(line) > 5:
            return line
    return ""


def is_tariff_related(title, keywords):
    """Check if a document title is likely a compliance tariff exhibit."""
    title_lower = title.lower()
    for pat in HIGH_VALUE_TITLE_PATTERNS:
        if re.search(pat, title_lower):
            return True, "high"
    for kw in keywords:
        if kw.lower() in title_lower:
            return True, "medium"
    return False, None


def search_and_get_docs(page, docket_str, company_name=""):
    """
    Search for documents in a docket, paginating through all result pages.
    Returns list of {doc_id, title, date_filed, class_, href} dicts.
    """
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    docket_input = page.query_selector(
        "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
    )
    docket_input.fill(docket_str)

    if company_name:
        company_input = page.query_selector(
            "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_companyName"
        )
        if company_input:
            company_input.fill(company_name)

    submit = page.query_selector("input[value='Search']")
    submit.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)

    all_docs = []
    seen_ids = set()
    page_num = 1

    while True:
        content = page.content()

        # Extract document detail links with full GUIDs
        # Pattern: PSCDocumentDetailsPageNCUC.aspx?DocumentId=GUID&Class=TYPE
        link_pattern = re.compile(
            r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+))["\']',
            re.I
        )
        for match in link_pattern.finditer(content):
            href_raw = match.group(1).replace("&amp;", "&")
            doc_id = match.group(2)
            doc_class = match.group(3)

            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)

            # Find the corresponding row in the rendered page to get title and date
            all_docs.append({
                "doc_id": doc_id,
                "title": "",  # will fill in from rendered text below
                "date_filed": "",
                "class_": doc_class,
                "href": f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}",
            })

        # Now get titles and dates from rendered rows
        rows = page.query_selector_all("table tr")
        for row in rows:
            row_links = row.query_selector_all("a[href*='PSCDocumentDetailsPageNCUC']")
            if not row_links:
                continue
            href = row_links[0].get_attribute("href") or ""
            doc_id_match = re.search(r'DocumentId=([0-9a-f\-]{36})', href, re.I)
            if not doc_id_match:
                continue
            doc_id = doc_id_match.group(1)

            row_text = row.inner_text()
            title = extract_title_from_row_text(row_text)
            date_match = re.search(r'Date Filed:\s*(\d{1,2}/\d{1,2}/\d{4})', row_text)
            date_filed = date_match.group(1) if date_match else ""

            # Update matching doc entry
            for doc in all_docs:
                if doc["doc_id"] == doc_id and not doc["title"]:
                    doc["title"] = title
                    doc["date_filed"] = date_filed
                    break

        # Check for next page link (numbers > current page)
        next_page_links = page.query_selector_all("a[href*='__doPostBack']")
        next_nums = []
        for link in next_page_links:
            txt = link.inner_text().strip()
            try:
                num = int(txt)
                if num > page_num:
                    next_nums.append((num, link))
            except ValueError:
                pass

        if not next_nums:
            break

        next_num, next_link = min(next_nums, key=lambda x: x[0])
        print(f"    -> Page {next_num}...", end=" ", flush=True)
        next_link.click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        page_num = next_num

    # Deduplicate by doc_id (keep first occurrence with best title)
    deduped = {}
    for doc in all_docs:
        if doc["doc_id"] not in deduped or not deduped[doc["doc_id"]]["title"]:
            deduped[doc["doc_id"]] = doc
    return list(deduped.values())


def get_viewfile_urls(page, doc_href):
    """
    Navigate to a document detail page and return list of ViewFile URLs.
    Returns list of {filename, view_url} dicts.
    """
    page.goto(doc_href, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    title_text = page.title()
    if "Error" in title_text or "error" in title_text.lower():
        body_preview = page.inner_text("body")[:200]
        if "Object reference" in body_preview or "Server Error" in body_preview:
            return []

    detail_content = page.content()
    view_urls = re.findall(
        r'href=["\']?(https://starw1\.ncuc\.gov/NCUC/ViewFile\.aspx\?Id=[0-9a-f\-]{36})["\']?',
        detail_content, re.I
    )
    file_links = page.query_selector_all("a[href*='ViewFile.aspx']")
    results = []
    for link in file_links:
        href = link.get_attribute("href") or ""
        filename = link.inner_text().strip()
        if href and "Id=" in href:
            results.append({"filename": filename, "view_url": href})
    return results


pw, ctx, page = create_authenticated_context(settings)
output = []

try:
    for target in TARGETS:
        docket = target["docket"]
        label = target["label"]
        family = target["family"]

        print(f"\n=== {label} ({docket}) ===")
        print(f"  Searching...", end=" ", flush=True)

        docs = search_and_get_docs(page, docket, target.get("company", ""))
        print(f"({len(docs)} docs found)")

        # Classify each document
        tariff_docs = []
        other_docs = []
        for doc in docs:
            is_tariff, priority = is_tariff_related(doc["title"], target["keywords"])
            if is_tariff:
                tariff_docs.append((doc, priority))
            else:
                other_docs.append(doc)

        print(f"  Tariff-related: {len(tariff_docs)}, Other: {len(other_docs)}")

        # Show tariff docs
        for doc, priority in tariff_docs:
            print(f"  [{priority}] [{doc['date_filed']}] {doc['title'][:80]}")

        # Show a sample of other docs for context
        if other_docs:
            print(f"  Other (first 5):")
            for doc in other_docs[:5]:
                print(f"    [{doc['date_filed']}] {doc['title'][:80]}")

        # Get file download links for tariff docs
        for doc, priority in tariff_docs:
            print(f"\n  Getting files: {doc['title'][:60]}...")
            try:
                file_links = get_viewfile_urls(page, doc["href"])
                for fl in file_links:
                    print(f"    File: [{fl['filename'][:60]}]")
                    print(f"      -> {fl['view_url']}")
                    output.append({
                        "docket": docket,
                        "label": label,
                        "family": family,
                        "priority": priority,
                        "doc_title": doc["title"],
                        "date_filed": doc["date_filed"],
                        "filename": fl["filename"],
                        "view_url": fl["view_url"],
                        "doc_id": doc["doc_id"],
                    })
            except Exception as e:
                print(f"    ERROR: {e}")

finally:
    close_authenticated_context(pw, ctx)

# Save results
out_path = Path("data/ncuc_tariff_filings.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w") as f:
    json.dump(output, f, indent=2)

print(f"\n\n=== SUMMARY ===")
print(f"Total tariff file links found: {len(output)}")
by_docket = {}
for item in output:
    by_docket.setdefault(item["label"], []).append(item)
for label, items in by_docket.items():
    print(f"  {label}: {len(items)} files")
    for item in items:
        print(f"    [{item['priority']}] {item['date_filed']} | {item['filename'][:50]}")

print(f"\nResults saved to {out_path}")
