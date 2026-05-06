"""
Find DEC EDPR and DEP EDIT-4 compliance tariff documents via text search.
Extract ViewFile download URLs.
"""
import re
import json
from pathlib import Path
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

SEARCHES = [
    {"query": "Rider EDPR", "label": "DEC EDPR compliance tariffs", "family": "nc-carolinas-rider-edpr"},
    {"query": "Leaf No. 604", "label": "DEP EDIT-4 Leaf 604", "family": "nc-progress-leaf-604"},
    {"query": "EDIT-4", "label": "DEP EDIT-4", "family": "nc-progress-leaf-604"},
]

HIGH_VALUE_TITLE_PATTERNS = [
    r"compliance tariff",
    r"revised tariff",
    r"leaf no",
    r"annual adjustment",
    r"tariff sheet",
    r"revised leaf",
    r"annual compliance",
    r"tariff filing",
    r"compliance filing",
    r"compliance exhibit",
    r"rider edpr",
    r"rider jaa",
    r"edit-4",
    r"edit4",
]

all_results = []

try:
    for search in SEARCHES:
        query = search["query"]
        label = search["label"]
        family = search["family"]

        print(f"\n=== {label} ===")
        page.goto("https://starw1.ncuc.gov/NCUC/page/DocumentsTextSearch/portal.aspx",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # Fill search
        text_input = page.query_selector(
            "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentFullTextSearchControl1_searchPhrase"
        )
        text_input.fill(query)
        submit = page.query_selector("input[value='Search']")
        submit.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        content = page.content()
        # Extract document detail links with GUIDs
        link_pattern = re.compile(
            r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+))["\']',
            re.I
        )
        doc_ids_seen = set()
        docs = []
        for match in link_pattern.finditer(content):
            href_raw = match.group(1).replace("&amp;", "&")
            doc_id = match.group(2)
            doc_class = match.group(3)
            if doc_id not in doc_ids_seen:
                doc_ids_seen.add(doc_id)
                docs.append({
                    "doc_id": doc_id,
                    "href": f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}",
                    "class_": doc_class,
                })

        # Get titles from rows
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

            # Extract title (skip boilerplate)
            lines = [l.strip() for l in row_text.split('\n') if l.strip()]
            skip_pats = [r"^(Filing|Order)$", r"^Filed In:", r"^Date Filed:", r"^Match in file",
                         r"^\d+%$", r"^Full-Text Search"]
            title = ""
            for line in lines:
                skip = any(re.match(p, line, re.I) for p in skip_pats)
                if not skip and len(line) > 5:
                    title = line
                    break

            date_match = re.search(r'Date Filed:\s*(\d{1,2}/\d{1,2}/\d{4})', row_text)
            date_filed = date_match.group(1) if date_match else ""

            # Update matching doc
            for doc in docs:
                if doc["doc_id"] == doc_id and "title" not in doc:
                    doc["title"] = title
                    doc["date_filed"] = date_filed
                    break

        print(f"  Docs found: {len(docs)}")
        for doc in docs[:20]:
            title = doc.get("title", "")
            date = doc.get("date_filed", "")
            is_hv = any(re.search(p, title.lower()) for p in HIGH_VALUE_TITLE_PATTERNS)
            marker = "[HV]" if is_hv else "    "
            print(f"  {marker} [{date}] {title[:80]}")

        # Get files for high-value docs
        for doc in docs:
            title = doc.get("title", "")
            if not any(re.search(p, title.lower()) for p in HIGH_VALUE_TITLE_PATTERNS):
                continue

            print(f"\n  Getting files: {title[:60]}...")
            try:
                page.goto(doc["href"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)

                if "Error" in page.title():
                    print(f"    Error loading page")
                    continue

                file_links = page.query_selector_all("a[href*='ViewFile.aspx']")
                for fl in file_links:
                    href = fl.get_attribute("href") or ""
                    filename = fl.inner_text().strip()
                    print(f"    File: [{filename[:60]}]")
                    print(f"      -> {href}")
                    all_results.append({
                        "query": query,
                        "label": label,
                        "family": family,
                        "doc_title": title,
                        "date_filed": doc.get("date_filed", ""),
                        "filename": filename,
                        "view_url": href,
                        "doc_id": doc["doc_id"],
                    })
            except Exception as e:
                print(f"    ERROR: {e}")

finally:
    close_authenticated_context(pw, ctx)

# Save
out_path = Path("data/ncuc_edpr_edit4_filings.json")
with open(out_path, "w") as f:
    json.dump(all_results, f, indent=2)

print(f"\n\n=== SUMMARY ===")
print(f"Total high-value files: {len(all_results)}")
for item in all_results:
    print(f"  {item['label']} | {item['date_filed']} | {item['filename'][:50]}")
    print(f"    {item['view_url']}")
print(f"\nSaved to {out_path}")
