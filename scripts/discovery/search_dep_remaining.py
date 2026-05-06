"""
Targeted search for the 4 remaining DEP families with no sources found:
EDIT-4 (leaf-604), DSM (leaf-606), RES (leaf-609), PPM (leaf-610)
"""
import json
from pathlib import Path
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)

settings = Settings()

# DEP families still needing sources
REMAINING_TARGETS = [
    {
        "family": "nc-progress-leaf-604",
        "name": "EDIT-4 (Excess Deferred Income Tax)",
        "dockets": ["E-2 Sub 1196"],
        "leaf": "604",
        "keywords": ["leaf 604", "EDIT", "income tax", "excess deferred"],
    },
    {
        "family": "nc-progress-leaf-606",
        "name": "DSM (Demand-Side Management)",
        "dockets": ["E-2 Sub 1204"],
        "leaf": "606",
        "keywords": ["leaf 606", "DSM", "demand side", "efficiency", "programs"],
    },
    {
        "family": "nc-progress-leaf-609",
        "name": "RES (Renewable Energy Surcharge)",
        "dockets": ["E-2 Sub 1204", "E-2 Sub 1324"],
        "leaf": "609",
        "keywords": ["leaf 609", "RES", "renewable energy", "surcharge"],
    },
    {
        "family": "nc-progress-leaf-610",
        "name": "PPM (Purchased Power Adjustment)",
        "dockets": ["E-2 Sub 1204"],
        "leaf": "610",
        "keywords": ["leaf 610", "PPM", "purchased power", "adjustment"],
    },
]


def extract_title_from_row(text):
    """Extract document title from search result row."""
    skip_patterns = [
        r"^(Filing|Order|Other)$",
        r"^Filed In:",
        r"^Date Filed:",
        r"^Items Count:",
    ]
    import re

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        skip = any(re.match(pat, line, re.I) for pat in skip_patterns)
        if not skip and len(line) > 5:
            return line
    return ""


def search_docket(page, docket_str):
    """Search for documents in a docket."""
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"    ERROR navigating to search: {e}")
        return []

    try:
        docket_input = page.query_selector(
            "#ctl00_ContentPlaceHolder1_PortalPageControl1_ctl86_PSCDocumentSearchControl1_docketNumber"
        )
        docket_input.fill(docket_str)

        submit = page.query_selector("input[value='Search']")
        submit.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"    ERROR submitting search: {e}")
        return []

    all_docs = []
    seen_ids = set()
    page_num = 1

    while True:
        try:
            content = page.content()
            import re

            link_pattern = re.compile(
                r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+))["\']',
                re.I,
            )
            for match in link_pattern.finditer(content):
                href_raw = match.group(1).replace("&amp;", "&")
                doc_id = match.group(2)
                doc_class = match.group(3)

                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                all_docs.append(
                    {
                        "doc_id": doc_id,
                        "title": "",
                        "date_filed": "",
                        "class_": doc_class,
                        "href": f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}",
                    }
                )

            # Extract titles and dates from rendered rows
            rows = page.query_selector_all("table tr")
            for row in rows:
                row_links = row.query_selector_all("a[href*='PSCDocumentDetailsPageNCUC']")
                if not row_links:
                    continue
                href = row_links[0].get_attribute("href") or ""
                doc_id_match = re.search(r"DocumentId=([0-9a-f\-]{36})", href, re.I)
                if not doc_id_match:
                    continue
                doc_id = doc_id_match.group(1)

                row_text = row.inner_text()
                title = extract_title_from_row(row_text)
                date_match = re.search(r"Date Filed:\s*(\d{1,2}/\d{1,2}/\d{4})", row_text)
                date_filed = date_match.group(1) if date_match else ""

                # Update matching doc entry
                for doc in all_docs:
                    if doc["doc_id"] == doc_id and not doc["title"]:
                        doc["title"] = title
                        doc["date_filed"] = date_filed
                        break

            # Check for next page
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
            next_link.click()
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(2000)
            page_num = next_num

        except Exception as e:
            print(f"    ERROR on page {page_num}: {e}")
            break

    return all_docs


pw, ctx, page = create_authenticated_context(settings)
output = []

try:
    for target in REMAINING_TARGETS:
        family = target["family"]
        name = target["name"]
        leaf = target["leaf"]
        dockets = target["dockets"]

        print(f"\n{'='*70}")
        print(f"  {name} ({leaf})")
        print(f"  Target dockets: {', '.join(dockets)}")
        print(f"{'='*70}")

        for docket_str in dockets:
            print(f"  Searching {docket_str}...", end=" ", flush=True)
            docs = search_docket(page, docket_str)
            print(f"({len(docs)} docs found)")

            # Filter to likely tariff documents
            tariff_keywords = set(target["keywords"])
            for doc in docs:
                title = (doc["title"] or "").lower()
                is_tariff = any(kw.lower() in title for kw in tariff_keywords)
                if is_tariff or "tariff" in title or "compliance" in title:
                    output.append(
                        {
                            "family": family,
                            "name": name,
                            "docket": docket_str,
                            "doc_id": doc["doc_id"],
                            "title": doc["title"],
                            "date_filed": doc["date_filed"],
                            "href": doc["href"],
                        }
                    )
                    print(
                        f"    [{doc['date_filed']}] {doc['title'][:60] if doc['title'] else '(no title)'}"
                    )

finally:
    close_authenticated_context(pw, ctx)

# Save results
out_path = Path("data/dep_gap_search_remaining.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w") as f:
    json.dump(output, f, indent=2)

print(f"\n{'='*70}")
print(f"SUMMARY: Found {len(output)} documents for remaining DEP families")
print(f"Saved to: {out_path}")
print(f"{'='*70}\n")

# Show summary by family
from collections import defaultdict

by_family = defaultdict(list)
for item in output:
    by_family[item["name"]].append(item)

for name in sorted(by_family.keys()):
    docs = by_family[name]
    print(f"{name:<50} {len(docs):>3} docs")
