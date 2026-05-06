"""
Register the 3 documents found from Phase 4 search (EDIT-4, DSM, RES, PPM).
Update impact analysis.
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from duke_rates.config import Settings
from duke_rates.db.sqlite import connect
from duke_rates.models.ncuc import NcucDiscoveryRecord
from duke_rates.db.repository import Repository

settings = Settings()

# Load Phase 4 search results
results_path = Path("data/dep_gap_search_remaining.json")
if not results_path.exists():
    print(f"Results file not found: {results_path}")
    exit(1)

with open(results_path) as f:
    found_docs = json.load(f)

# Also load Phase 1 results for totals
phase1_path = Path("data/dep_gap_search_results.json")
phase1_docs = []
if phase1_path.exists():
    with open(phase1_path) as f:
        phase1_docs = json.load(f)

print(f"\n{'='*80}")
print(f"PHASE 4 REGISTRATION: Remaining DEP Family Sources")
print(f"{'='*80}\n")

from collections import defaultdict
by_family = defaultdict(list)
for doc in found_docs:
    by_family[doc["family"]].append(doc)

conn = connect(settings.database_path)
cursor = conn.cursor()

print("Current extraction status by family:\n")
family_charge_counts = {}
for family_key in sorted(by_family.keys()):
    count = cursor.execute(
        """
        SELECT COUNT(*)
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        WHERE tv.family_key = ?
        """,
        (family_key,),
    ).fetchone()[0]
    family_charge_counts[family_key] = count
    name = by_family[family_key][0]["name"] if by_family[family_key] else "?"
    print(f"  {name:<42} {count:>5} charges")

print(f"\n{'='*80}\n")
conn.close()

# Now use Repository to register documents
repo = Repository(settings.database_path)

print("Registering Phase 4 documents:\n")
registered = 0
skipped = 0
failed = []

for family_key, docs in sorted(by_family.items()):
    family_name = docs[0]["name"]
    print(f"{family_name}:")

    for doc in docs:
        try:
            record = NcucDiscoveryRecord(
                filing_title=doc["title"],
                filing_date=doc["date_filed"],
                docket_number=doc["docket"],
                discovered_url=doc["href"],
                attachment_url=doc["href"],
                acquisition_method="playwright",
                utility="Duke Energy Progress",
                filing_classification="tariff_sheets",
                family_keys_json=json.dumps([family_key]),
                content_hash=None,
                created_at=datetime.now(timezone.utc),
                doc_quality_tier="T2",
                search_confidence_score=0.80,  # Slightly lower: not from priority dockets
                search_ideality="probable",
            )
            result_id = repo.upsert_ncuc_discovery_record(record)
            print(f"  [OK] {doc['title'][:65]}")
            registered += 1
        except Exception as e:
            print(f"  [ERROR] {doc['title'][:65]} - {e}")
            failed.append((doc["title"], str(e)))

print(f"\n{'='*80}")
print(f"Phase 4 Registration Complete:")
print(f"  Registered: {registered}")
print(f"  Failed: {len(failed)}")
print(f"{'='*80}\n")

# Combined impact analysis
all_found = phase1_docs + found_docs
all_by_family = defaultdict(list)
for doc in all_found:
    all_by_family[doc["family"]].append(doc)

family_targets = {
    "nc-progress-leaf-602": "JAA (Joint Agency Asset Rider)",
    "nc-progress-leaf-604": "EDIT-4 (Excess Deferred Income Tax)",
    "nc-progress-leaf-606": "DSM (Demand-Side Management)",
    "nc-progress-leaf-607": "STS (Storm Securitization)",
    "nc-progress-leaf-608": "RDM (Revenue Decoupling)",
    "nc-progress-leaf-609": "RES (Renewable Energy Surcharge)",
    "nc-progress-leaf-610": "PPM (Purchased Power Adjustment)",
}

print("UPDATED IMPACT ANALYSIS: All 7 DEP Families\n")

conn = connect(settings.database_path)
cursor = conn.cursor()

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

    # In our newly found docs?
    found = len(all_by_family.get(family_key, []))

    # Conservative estimate of new charges (8 per source on average)
    est_new = found * 8 if found > 0 else 0

    status = "[FOUND]" if found > 0 else "[NO SOURCES]"

    total_current += current
    total_found_sources += found
    total_est_new += est_new

    print(
        f"{family_name:<42} Current: {current:>3} | Found: {found:>1} {status} | Est. gain: +{est_new:>2}"
    )

print(f"\n{'='*80}")
print(f"AGGREGATE IMPACT - ALL PHASES")
print(f"{'='*80}\n")
print(f"Total DEP families:              7")
print(f"Families with new sources:       {len([f for f in all_by_family if len(all_by_family[f]) > 0])}")
print(f"Total sources registered:        {len(all_found)} (Phases 1+4)")
print(f"Current charges (all 7):         {total_current}")
print(f"Est. new charges:                {total_est_new}")
print(f"Est. total after extraction:     {total_current + total_est_new}")
print(f"Improvement factor:              {((total_current + total_est_new) / total_current):.2f}x\n")

# Detailed breakdown
print(f"PHASE BREAKDOWN\n")
print(f"Phase 1 Results: 8 documents")
for family_key, docs in sorted(all_by_family.items()):
    if len(docs) <= 3:  # Only show phase 1 targets
        continue
    name = family_targets.get(family_key, family_key)
    phase1_count = len([d for d in docs if d in phase1_docs])
    phase4_count = len([d for d in docs if d in found_docs])
    if phase1_count > 0:
        print(f"  {name:<42} {phase1_count} source(s)")

print(f"\nPhase 4 Results: {len(found_docs)} documents")
for family_key, docs in sorted(all_by_family.items()):
    name = family_targets.get(family_key, family_key)
    phase4_count = len([d for d in docs if d in found_docs])
    if phase4_count > 0:
        print(f"  {name:<42} {phase4_count} source(s)")

print(f"\n{'='*80}")
print(f"NEXT STEPS\n")
print(f"1. Trigger extraction on all {len(all_found)} registered documents\n")
print(f"2. Monitor extraction and charge recovery\n")
print(f"3. Assess redline quality with fingerprinting\n")
print(f"4. Use HQ signal queries for additional searches\n")

try:
    total_current_updated = cursor.execute(
        """
        SELECT COUNT(*)
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        WHERE tv.family_key LIKE 'nc-progress-leaf%'
        """
    ).fetchone()[0]
    print(f"All DEP charges in DB: {total_current_updated}\n")
finally:
    conn.close()
