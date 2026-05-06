"""
Analyze the impact of the 8 newly registered DEP documents on gap closure.
Shows current status and expected gains after extraction.
"""
import json
from pathlib import Path
from duke_rates.config import Settings
from duke_rates.db.sqlite import connect

settings = Settings()
conn = connect(settings.database_path)
cursor = conn.cursor()

# Load search results for context
results_path = Path("data/dep_gap_search_results.json")
if not results_path.exists():
    print(f"Results file not found: {results_path}")
    exit(1)

with open(results_path) as f:
    found_docs = json.load(f)

from collections import defaultdict
by_family = defaultdict(list)
for doc in found_docs:
    by_family[doc["family"]].append(doc)

print(f"\n{'='*80}")
print(f"DEP GAP CLOSURE IMPACT ANALYSIS")
print(f"{'='*80}\n")

print(f"NEWLY REGISTERED DOCUMENTS: {len(found_docs)} sources across 3 families\n")

family_targets = {
    "nc-progress-leaf-602": "JAA (Joint Agency Asset Rider)",
    "nc-progress-leaf-604": "EDIT-4 (Excess Deferred Income Tax)",
    "nc-progress-leaf-606": "DSM (Demand-Side Management)",
    "nc-progress-leaf-607": "STS (Storm Securitization)",
    "nc-progress-leaf-608": "RDM (Revenue Decoupling)",
    "nc-progress-leaf-609": "RES (Renewable Energy Surcharge)",
    "nc-progress-leaf-610": "PPM (Purchased Power Adjustment)",
}

print("FAMILY-BY-FAMILY GAP ANALYSIS\n")
print("Status Key: [REGISTERED] = in ncuc_discovery_records, [EXTRACTED] = charges in DB\n")

total_current = 0
total_found_sources = 0
total_est_new = 0

for family_key, family_name in sorted(family_targets.items()):
    # Current charges
    current = cursor.execute(
        """
        SELECT COUNT(*)
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        WHERE tv.family_key = ?
        """,
        (family_key,),
    ).fetchone()[0]

    # Registered discovery records (potentially extractable)
    registered = cursor.execute(
        """
        SELECT COUNT(*)
        FROM ncuc_discovery_records
        WHERE family_keys_json LIKE ?
        """,
        (f'%{family_key}%',),
    ).fetchone()[0]

    # In our newly found docs?
    found = len(by_family.get(family_key, []))

    # Conservative estimate of new charges (8 per source on average)
    est_new = found * 8 if found > 0 else 0

    status = "[FOUND]" if found > 0 else "[NO SOURCES FOUND]"

    total_current += current
    total_found_sources += found
    total_est_new += est_new

    print(f"{family_name:<42}")
    print(f"  Current charges:          {current:>5}")
    print(f"  New sources found:        {found:>5} {status}")
    print(f"  Est. new charges:         {est_new:>5} (conservative: 8/source)")
    print(f"  Total after extraction:   {current + est_new:>5}")
    print()

print(f"{'='*80}")
print(f"AGGREGATE IMPACT")
print(f"{'='*80}\n")
print(f"DEP Leaf Families targeted:        7")
print(f"Families with new sources found:   3 (JAA, STS, RDM)")
print(f"New sources registered:            {total_found_sources}")
print(f"Current charges (3 families):      {total_current}")
print(f"Est. new charges (conservative):   {total_est_new}")
print(f"Est. total after extraction:       {total_current + total_est_new}")
print(f"\nGain factor: {((total_current + total_est_new) / total_current):.2f}x\n")

# Show which sources are registered for extraction
print(f"{'='*80}")
print(f"EXTRACTION READINESS: {len(found_docs)} Documents Ready\n")

for family_key, docs in sorted(by_family.items()):
    family_name = family_targets.get(family_key, family_key)
    print(f"{family_name}:")
    for doc in docs:
        print(f"  - {doc['title'][:70]}")
        print(f"    Date: {doc['date_filed']} | Docket: {doc['docket']}")
    print()

print(f"{'='*80}")
print(f"NEXT STEPS\n")
print(f"1. Trigger extraction on registered documents:")
print(f"   python -m duke_rates extract-rates-nc --ncuc-only --limit 20\n")
print(f"2. Monitor extraction progress and charge recovery\n")
print(f"3. Run gap analysis after extraction completes\n")
print(f"4. Continue targeted searches for remaining families:")
print(f"   - EDIT-4 (leaf-604): No sources found yet")
print(f"   - DSM (leaf-606): No sources found yet")
print(f"   - RES (leaf-609): No sources found yet")
print(f"   - PPM (leaf-610): No sources found yet\n")

conn.close()
