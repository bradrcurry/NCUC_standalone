"""
Dynamic NCUC Search Strategy Builder

Learn from successful searches to build better queries for gap families.
Analyzes what worked (found docs) vs. what didn't, then generates
targeted queries with higher quality signals.
"""
from dataclasses import dataclass
from typing import Optional
import json
from pathlib import Path


@dataclass
class SearchPattern:
    """Successful search pattern."""
    docket_number: str
    family_key: str
    family_name: str
    leaf_no: str
    found_count: int          # How many quality docs found
    quality_signal: str       # What keyword worked
    year_range: Optional[tuple[int, int]]  # (start_year, end_year)
    confidence: float         # Pattern reliability


# Learned patterns from successful Phase 1 searches
SUCCESSFUL_PATTERNS = [
    SearchPattern(
        docket_number="E-2 Sub 1354",
        family_key="nc-progress-leaf-602",
        family_name="JAA",
        leaf_no="602",
        found_count=6,
        quality_signal="Compliance Tariffs + recent dates (2025)",
        year_range=(2017, 2025),
        confidence=0.95,
    ),
    SearchPattern(
        docket_number="E-2 Sub 1143",
        family_key="nc-progress-leaf-602",
        family_name="JAA",
        leaf_no="602",
        found_count=3,
        quality_signal="Historical orders (2017)",
        year_range=(2017, 2017),
        confidence=0.90,
    ),
    SearchPattern(
        docket_number="E-2 Sub 1204",
        family_key="nc-progress-leaf-607",
        family_name="STS",
        leaf_no="607",
        found_count=1,
        quality_signal="Specific rider in order (2020)",
        year_range=(2019, 2020),
        confidence=0.85,
    ),
    SearchPattern(
        docket_number="E-2 Sub 1294",
        family_key="nc-progress-leaf-608",
        family_name="RDM",
        leaf_no="608",
        found_count=1,
        quality_signal="Recent compliance tariff (2023)",
        year_range=(2023, 2023),
        confidence=0.85,
    ),
]

# Unsuccessful patterns (no docs found)
UNSUCCESSFUL_PATTERNS = [
    {
        "docket": "E-2 Sub 1196",
        "family": "EDIT-4 (leaf-604)",
        "reason": "Wrong docket - found 3 docs but 0 matched",
        "try_next": ["E-2 Sub 1160", "Broader E-2 searches", "Rate case exhibits"],
    },
    {
        "docket": "E-2 Sub 1204 (with DSM keywords)",
        "family": "DSM (leaf-606)",
        "reason": "0 charges in DB suggests no docs extracted - need new source",
        "try_next": ["E-2 Sub 1276", "E-2 Sub 1160", "Manual website search"],
    },
]


def build_refined_search_queries(gap_family_key: str, gap_family_name: str) -> list[dict]:
    """
    Build refined search queries for a family with gaps.

    Uses patterns from successful searches to infer likely dockets and keywords.
    """
    queries = []

    # Strategy 1: Try similar docket patterns
    if gap_family_key == "nc-progress-leaf-604":  # EDIT-4
        # Similar to EDIT patterns - try broader E-2 Sub dockets
        queries.extend([
            {
                "docket": "E-2 Sub 1160",
                "keywords": ["EDIT", "income tax", "deferred"],
                "priority": 1.0,
                "rationale": "Common E-2 Sub for EDIT riders",
            },
            {
                "docket": "E-2",  # Broader search
                "keywords": ["Leaf 604", "EDIT", "compliance"],
                "priority": 0.8,
                "rationale": "Broader E-2 search with explicit leaf number",
            },
        ])

    elif gap_family_key == "nc-progress-leaf-606":  # DSM
        queries.extend([
            {
                "docket": "E-2 Sub 1276",
                "keywords": ["DSM", "demand side", "efficiency"],
                "priority": 1.0,
                "rationale": "Common E-2 Sub for DSM/EE programs",
            },
            {
                "docket": "E-2 Sub 1204",
                "keywords": ["leaf 606", "DSM", "program"],
                "priority": 0.9,
                "rationale": "Multi-docket, try with explicit leaf number",
            },
        ])

    elif gap_family_key == "nc-progress-leaf-609":  # RES
        queries.extend([
            {
                "docket": "E-2 Sub 1324",
                "keywords": ["RES", "renewable", "surcharge"],
                "priority": 1.0,
                "rationale": "RES docket - found 2 docs before",
            },
            {
                "docket": "E-2 Sub 1204",
                "keywords": ["renewable", "surcharge", "leaf 609"],
                "priority": 0.85,
                "rationale": "Multi-docket, try with explicit keywords",
            },
        ])

    elif gap_family_key == "nc-progress-leaf-610":  # PPM
        queries.extend([
            {
                "docket": "E-2 Sub 1204",
                "keywords": ["PPM", "purchased power", "fuel"],
                "priority": 1.0,
                "rationale": "Fuel/PPM adjustment typically in Sub 1204",
            },
            {
                "docket": "E-2 Sub 1173",
                "keywords": ["purchased power", "adjustment"],
                "priority": 0.85,
                "rationale": "Alternative fuel cost docket",
            },
        ])

    return queries


def extract_successful_patterns(found_docs: list[dict]) -> list[SearchPattern]:
    """
    Analyze found documents to extract patterns for future searches.

    Extract what worked: docket, keywords, date ranges, etc.
    """
    patterns = []
    from collections import defaultdict

    by_family = defaultdict(list)
    for doc in found_docs:
        family = doc.get("family", "")
        by_family[family].append(doc)

    for family_key, docs in by_family.items():
        if not docs:
            continue

        # Extract metadata
        docket = docs[0].get("docket", "")
        title = docs[0].get("title", "")
        name = docs[0].get("name", "")

        # Extract leaf number
        leaf_match = None
        if "leaf" in title.lower():
            import re
            m = re.search(r'leaf[- ]?(\d+)', title, re.I)
            if m:
                leaf_match = m.group(1)

        # Date range
        dates = []
        for doc in docs:
            d = doc.get("date_filed", "")
            if d:
                import re
                m = re.search(r'(\d{4})', d)
                if m:
                    dates.append(int(m.group(1)))

        year_range = (min(dates), max(dates)) if dates else None

        pattern = SearchPattern(
            docket_number=docket,
            family_key=family_key,
            family_name=name or family_key,
            leaf_no=leaf_match or "unknown",
            found_count=len(docs),
            quality_signal=f"Found {len(docs)} docs with keyword match",
            year_range=year_range,
            confidence=0.85 + (len(docs) * 0.05),  # More finds = higher confidence
        )
        patterns.append(pattern)

    return patterns


def analyze_search_effectiveness(results_path: Path) -> dict:
    """
    Analyze search results to identify effective patterns.
    """
    if not results_path.exists():
        return {}

    with open(results_path) as f:
        results = json.load(f)

    from collections import defaultdict
    by_docket = defaultdict(lambda: defaultdict(int))
    by_family = defaultdict(int)

    for doc in results:
        docket = doc.get("docket", "Unknown")
        family = doc.get("family", "Unknown")
        by_docket[docket][family] += 1
        by_family[family] += 1

    analysis = {
        "total_docs": len(results),
        "dockets_searched": list(by_docket.keys()),
        "by_docket": {
            docket: dict(families) for docket, families in by_docket.items()
        },
        "by_family": dict(by_family),
        "effectiveness": {}
    }

    # Calculate effectiveness per docket
    for docket, families in by_docket.items():
        total_in_docket = sum(families.values())
        analysis["effectiveness"][docket] = {
            "docs_found": total_in_docket,
            "families": families,
            "avg_per_family": total_in_docket / len(families) if families else 0,
        }

    return analysis


def recommend_next_searches(gap_analysis: dict) -> list[dict]:
    """
    Recommend next dockets/keywords based on gap analysis and successful patterns.

    gap_analysis should have structure: {family: {current: N, gap: N, ...}}
    """
    recommendations = []

    # EDIT-4 (leaf-604)
    if gap_analysis.get("nc-progress-leaf-604", {}).get("gap"):
        recommendations.append({
            "family": "EDIT-4 (leaf-604)",
            "priority": 1,
            "next_searches": [
                {
                    "docket": "E-2 Sub 1160",
                    "keywords": ["leaf 604", "EDIT", "tax"],
                    "rationale": "Standard EDIT docket",
                },
                {
                    "docket": "E-2 Sub 1196",
                    "keywords": ["leaf 604", "deferred"],
                    "rationale": "Previous search found 3 docs - refine keywords",
                },
                {
                    "docket": "E-2 Sub 1196",
                    "search_method": "manual_exhibits",
                    "rationale": "Try finding exhibit documents manually",
                },
            ],
        })

    # DSM (leaf-606)
    if gap_analysis.get("nc-progress-leaf-606", {}).get("current") == 0:
        recommendations.append({
            "family": "DSM (leaf-606)",
            "priority": 2,  # Critical - 0 charges
            "next_searches": [
                {
                    "docket": "E-2 Sub 1276",
                    "keywords": ["DSM", "efficiency", "programs"],
                    "rationale": "Standard DSM docket",
                },
                {
                    "docket": "E-2 Sub 1204",
                    "keywords": ["leaf 606", "DSM", "compliance"],
                    "rationale": "Multi-docket - try with explicit leaf",
                },
            ],
        })

    return recommendations
