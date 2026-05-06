"""
Enhanced DEP Gap Search with Portal Metadata Analysis

Searches NCUC portal AND filters results using quality metadata signals
to identify high-quality tariff documents before downloading.
"""
import json
from pathlib import Path
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import (
    create_authenticated_context,
    close_authenticated_context,
)
from duke_rates.historical.ncuc.portal_metadata_analyzer import (
    score_portal_result,
    filter_high_quality_results,
)

settings = Settings()

# Enhanced DEP targets with quality scoring
DEP_GAP_TARGETS = [
    {
        "family": "nc-progress-leaf-602",
        "name": "JAA (Joint Agency Asset Rider)",
        "dockets": ["E-2 Sub 1354", "E-2 Sub 1143"],
        "leaf": "602",
        "keywords": ["leaf 602", "JAA", "joint agency", "annual adjustment"],
        "quality_filters": {
            "min_confidence": 0.75,
            "prefer_compliance": True,
            "prefer_recent": True,  # 2023+
        },
    },
    {
        "family": "nc-progress-leaf-607",
        "name": "STS (Storm Securitization)",
        "dockets": ["E-2 Sub 1204"],
        "leaf": "607",
        "keywords": ["leaf 607", "STS", "storm securitization"],
        "quality_filters": {
            "min_confidence": 0.75,
            "prefer_compliance": True,
            "prefer_recent": False,  # Historical versions OK
        },
    },
    {
        "family": "nc-progress-leaf-608",
        "name": "RDM (Revenue Decoupling)",
        "dockets": ["E-2 Sub 1294"],
        "leaf": "608",
        "keywords": ["leaf 608", "RDM", "revenue decoupling"],
        "quality_filters": {
            "min_confidence": 0.75,
            "prefer_compliance": True,
            "prefer_recent": True,
        },
    },
    {
        "family": "nc-progress-leaf-604",
        "name": "EDIT-4 (Excess Deferred Income Tax)",
        "dockets": ["E-2 Sub 1196", "E-2 Sub 1160"],
        "leaf": "604",
        "keywords": ["leaf 604", "EDIT", "income tax", "deferred"],
        "quality_filters": {
            "min_confidence": 0.70,
            "prefer_compliance": True,
            "prefer_recent": False,
        },
    },
    {
        "family": "nc-progress-leaf-606",
        "name": "DSM (Demand-Side Management)",
        "dockets": ["E-2 Sub 1204", "E-2 Sub 1276"],
        "leaf": "606",
        "keywords": ["leaf 606", "DSM", "efficiency", "programs"],
        "quality_filters": {
            "min_confidence": 0.65,  # Lower threshold due to scarcity
            "prefer_compliance": True,
            "prefer_recent": True,
        },
    },
    {
        "family": "nc-progress-leaf-609",
        "name": "RES (Renewable Energy Surcharge)",
        "dockets": ["E-2 Sub 1204", "E-2 Sub 1324"],
        "leaf": "609",
        "keywords": ["leaf 609", "RES", "renewable energy", "surcharge"],
        "quality_filters": {
            "min_confidence": 0.70,
            "prefer_compliance": True,
            "prefer_recent": True,
        },
    },
    {
        "family": "nc-progress-leaf-610",
        "name": "PPM (Purchased Power Adjustment)",
        "dockets": ["E-2 Sub 1204"],
        "leaf": "610",
        "keywords": ["leaf 610", "PPM", "purchased power"],
        "quality_filters": {
            "min_confidence": 0.70,
            "prefer_compliance": True,
            "prefer_recent": True,
        },
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
metadata_analysis = {}

try:
    for target in DEP_GAP_TARGETS:
        family = target["family"]
        name = target["name"]
        leaf = target["leaf"]
        dockets = target["dockets"]
        quality_filters = target.get("quality_filters", {})
        min_confidence = quality_filters.get("min_confidence", 0.75)

        print(f"\n{'='*70}")
        print(f"  {name} ({leaf})")
        print(f"  Target dockets: {', '.join(dockets)}")
        print(f"  Quality threshold: {min_confidence}")
        print(f"{'='*70}")

        docket_results = []

        for docket_str in dockets:
            print(f"  Searching {docket_str}...", end=" ", flush=True)
            docs = search_docket(page, docket_str)
            print(f"({len(docs)} docs found)")

            # Score all documents based on portal metadata
            scored_docs = []
            for doc in docs:
                score = score_portal_result(
                    doc["title"],
                    doc["date_filed"],
                )
                scored_docs.append({**doc, **score})

            # Filter to high-quality candidates
            high_quality = [d for d in scored_docs if d["confidence"] >= min_confidence]

            print(f"    -> {len(high_quality)}/{len(docs)} meet quality threshold ({min_confidence})")

            # Also check keyword matching
            tariff_keywords = set(target["keywords"])
            for doc in high_quality:
                title = (doc["title"] or "").lower()
                is_tariff = any(kw.lower() in title for kw in tariff_keywords)
                if is_tariff or doc["quality_tier"] == "high":
                    output.append(
                        {
                            "family": family,
                            "name": name,
                            "docket": docket_str,
                            "doc_id": doc["doc_id"],
                            "title": doc["title"],
                            "date_filed": doc["date_filed"],
                            "href": doc["href"],
                            "quality_confidence": doc["confidence"],
                            "quality_tier": doc["quality_tier"],
                            "filing_type": doc["filing_type"],
                            "reason": doc["reason"],
                        }
                    )
                    quality_icon = "[HIGH]" if doc["confidence"] >= 0.85 else "[MED]"
                    print(
                        f"    {quality_icon} [{doc['date_filed']}] {doc['title'][:55]}"
                    )

            docket_results.extend(high_quality)

        # Store metadata analysis for this family
        metadata_analysis[name] = {
            "total_found": len(docket_results),
            "registered": len([d for d in output if d["name"] == name]),
            "avg_confidence": sum(d["confidence"] for d in docket_results) / len(docket_results) if docket_results else 0,
        }

finally:
    close_authenticated_context(pw, ctx)

# Save results
out_path = Path("data/dep_gap_search_enhanced.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w") as f:
    json.dump(output, f, indent=2)

print(f"\n{'='*70}")
print(f"SUMMARY: Found {len(output)} high-quality DEP tariff documents")
print(f"Saved to: {out_path}")
print(f"{'='*70}\n")

# Show summary by family
from collections import defaultdict

by_family = defaultdict(list)
for item in output:
    by_family[item["name"]].append(item)

print("Results by Family:")
for name in sorted(by_family.keys()):
    docs = by_family[name]
    avg_conf = sum(d["quality_confidence"] for d in docs) / len(docs) if docs else 0
    print(f"  {name:<42} {len(docs):>2} docs (confidence: {avg_conf:.2f})")

print(f"\nMetadata Analysis Summary:")
for name, stats in sorted(metadata_analysis.items()):
    print(f"  {name:<42} avg confidence: {stats['avg_confidence']:.2f}")
