"""
Enhanced DEP Gap Search with Streaming Output

Searches NCUC portal with metadata quality filtering.
Saves results incrementally to avoid data loss if portal connection drops.
"""
import json
from pathlib import Path
from collections import defaultdict
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)
from duke_rates.historical.ncuc.portal_metadata_analyzer import score_portal_result

settings = Settings()

# DEP targets with quality filtering thresholds
DEP_GAP_TARGETS = [
    {
        "family": "nc-progress-leaf-602",
        "name": "JAA (Joint Agency Asset Rider)",
        "dockets": ["E-2 Sub 1354", "E-2 Sub 1143"],
        "min_confidence": 0.75,
    },
    {
        "family": "nc-progress-leaf-607",
        "name": "STS (Storm Securitization)",
        "dockets": ["E-2 Sub 1204"],
        "min_confidence": 0.75,
    },
    {
        "family": "nc-progress-leaf-608",
        "name": "RDM (Revenue Decoupling)",
        "dockets": ["E-2 Sub 1294"],
        "min_confidence": 0.75,
    },
    {
        "family": "nc-progress-leaf-604",
        "name": "EDIT-4 (Excess Deferred Income Tax)",
        "dockets": ["E-2 Sub 1196", "E-2 Sub 1160"],
        "min_confidence": 0.70,
    },
    {
        "family": "nc-progress-leaf-606",
        "name": "DSM (Demand-Side Management)",
        "dockets": ["E-2 Sub 1204", "E-2 Sub 1276"],
        "min_confidence": 0.65,
    },
    {
        "family": "nc-progress-leaf-609",
        "name": "RES (Renewable Energy Surcharge)",
        "dockets": ["E-2 Sub 1204"],
        "min_confidence": 0.70,
    },
    {
        "family": "nc-progress-leaf-610",
        "name": "PPM (Purchased Power Adjustment)",
        "dockets": ["E-2 Sub 1204"],
        "min_confidence": 0.70,
    },
]


def extract_title_from_row(text):
    """Extract document title from search result row."""
    import re
    skip_patterns = [
        r"^(Filing|Order|Other)$",
        r"^Filed In:",
        r"^Date Filed:",
        r"^Items Count:",
    ]

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        skip = any(re.match(pat, line, re.I) for pat in skip_patterns)
        if not skip and len(line) > 5:
            return line
    return ""


def search_docket_safe(page, docket_str, timeout=45000):
    """Search for documents in a docket with timeout and error handling."""
    import re
    search_url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"

    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=timeout)
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
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"    ERROR submitting search: {e}")
        return []

    all_docs = []
    seen_ids = set()
    page_num = 1
    max_pages = 10  # Limit pages to prevent hanging

    while page_num <= max_pages:
        try:
            content = page.content()
            link_pattern = re.compile(
                r'href=["\']([^"\']*PSCDocumentDetailsPageNCUC\.aspx\?DocumentId=([0-9a-f\-]{36})&amp;Class=(\w+))["\']',
                re.I,
            )

            for match in link_pattern.finditer(content):
                doc_id = match.group(2)
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    doc_class = match.group(3)
                    all_docs.append(
                        {
                            "doc_id": doc_id,
                            "title": "",
                            "date_filed": "",
                            "class_": doc_class,
                            "href": f"https://starw1.ncuc.gov/NCUC/PSC/PSCDocumentDetailsPageNCUC.aspx?DocumentId={doc_id}&Class={doc_class}",
                        }
                    )

            # Extract titles and dates
            rows = page.query_selector_all("table tr")
            for row in rows:
                try:
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

                    for doc in all_docs:
                        if doc["doc_id"] == doc_id and not doc["title"]:
                            doc["title"] = title
                            doc["date_filed"] = date_filed
                            break
                except Exception as e:
                    print(f"    WARNING parsing row: {e}")
                    continue

            # Check for next page
            try:
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
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
                page.wait_for_timeout(2000)
                page_num = next_num
            except Exception as e:
                print(f"    ERROR checking for next page: {e}")
                break

        except Exception as e:
            print(f"    ERROR on page {page_num}: {e}")
            break

    return all_docs


def save_results(output, out_path):
    """Save results to JSON file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)


pw = None
ctx = None
page = None
output = []

try:
    pw, ctx, page = create_authenticated_context(settings)

    for target in DEP_GAP_TARGETS:
        family = target["family"]
        name = target["name"]
        dockets = target["dockets"]
        min_confidence = target["min_confidence"]

        print(f"\n{'='*70}")
        print(f"  {name}")
        print(f"  Dockets: {', '.join(dockets)}")
        print(f"  Quality threshold: {min_confidence}")
        print(f"{'='*70}")

        for docket_str in dockets:
            try:
                print(f"  Searching {docket_str}...", end=" ", flush=True)
                docs = search_docket_safe(page, docket_str)
                print(f"({len(docs)} docs found)")

                high_quality_count = 0
                for doc in docs:
                    score = score_portal_result(doc["title"], doc["date_filed"])

                    if score["confidence"] >= min_confidence:
                        high_quality_count += 1
                        # Check keyword match
                        title_lower = (doc["title"] or "").lower()
                        if any(k in title_lower for k in ["tariff", "compliance", "order", "rider", "schedule", "exhibit"]):
                            output.append(
                                {
                                    "family": family,
                                    "name": name,
                                    "docket": docket_str,
                                    "doc_id": doc["doc_id"],
                                    "title": doc["title"],
                                    "date_filed": doc["date_filed"],
                                    "href": doc["href"],
                                    "quality_confidence": score["confidence"],
                                    "quality_tier": score["quality_tier"],
                                    "filing_type": score["filing_type"],
                                }
                            )

                            tier_icon = "[H]" if score["confidence"] >= 0.85 else "[M]"
                            print(f"    {tier_icon} {doc['title'][:65]}")

                print(f"    -> {high_quality_count}/{len(docs)} high-confidence")

                # Save incrementally after each docket
                save_results(output, Path("data/dep_gap_search_enhanced.json"))

            except Exception as e:
                print(f"  ERROR searching {docket_str}: {e}")
                continue

finally:
    if pw and ctx:
        close_authenticated_context(pw, ctx)

print(f"\n{'='*70}")
print(f"SUMMARY: {len(output)} high-quality documents found")
print(f"Saved to: data/dep_gap_search_enhanced.json")
print(f"{'='*70}\n")

# Summary by family
by_family = defaultdict(list)
for item in output:
    by_family[item["name"]].append(item)

for name in sorted(by_family.keys()):
    docs = by_family[name]
    print(f"{name:<42} {len(docs):>2} docs")
