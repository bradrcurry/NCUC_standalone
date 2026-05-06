"""
Register the 8 high-quality DEP documents found from NCUC portal search.
Analyzes potential charge contribution per family.
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from duke_rates.config import Settings
from duke_rates.db.sqlite import connect
from duke_rates.models.ncuc import NcucDiscoveryRecord
from duke_rates.db.repository import Repository

settings = Settings()

# Load search results
results_path = Path("data/dep_gap_search_results.json")
if not results_path.exists():
    print(f"Results file not found: {results_path}")
    exit(1)

with open(results_path) as f:
    found_docs = json.load(f)

print(f"\n{'='*70}")
print(f"Registering {len(found_docs)} high-quality DEP documents")
print(f"{'='*70}\n")

# Group by family
from collections import defaultdict
by_family = defaultdict(list)
for doc in found_docs:
    by_family[doc["family"]].append(doc)

conn = connect(settings.database_path)
cursor = conn.cursor()

try:
    # First, check current extraction status for each family
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
        print(f"  {name:<40} {count:>5} charges currently extracted")

    print(f"\n{'='*70}\n")

finally:
    conn.close()

# Now use Repository to register documents
repo = Repository(settings.database_path)

print("Registering documents:\n")
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
                acquisition_method="playwright",  # Valid enum value
                utility="Duke Energy Progress",
                filing_classification="tariff_sheets",  # Valid enum value
                family_keys_json=json.dumps([family_key]),
                content_hash=None,  # Don't have hash, will use URLs for dedup
                created_at=datetime.now(timezone.utc),
                doc_quality_tier="T2",
                search_confidence_score=0.85,
                search_ideality="probable",
            )
            result_id = repo.upsert_ncuc_discovery_record(record)
            print(f"  [OK] {doc['title'][:65]}")
            registered += 1
        except Exception as e:
            print(f"  [ERROR] {doc['title'][:65]} - {e}")
            failed.append((doc["title"], str(e)))

print(f"\n{'='*70}")
print(f"Registration complete:")
print(f"  Registered: {registered}")
print(f"  Failed: {len(failed)}")
if failed:
    print("\nFailures:")
    for title, error in failed:
        print(f"  - {title[:60]}: {error}")
print(f"{'='*70}\n")

# Impact analysis
print("Potential impact analysis:\n")
for family_key in sorted(family_charge_counts.keys()):
    family_name = by_family[family_key][0]["name"]
    doc_count = len(by_family[family_key])
    current_charges = family_charge_counts[family_key]

    # Conservative estimate: 1-2 documents per family might yield 5-15 charges each
    # assuming they're different versions with overlapping structure
    estimated_new_charges = doc_count * 8  # Conservative: 8 charges per new source on average

    print(
        f"  {family_name:<40} "
        f"Current: {current_charges:>3} | Found: {doc_count:>1} sources | "
        f"Est. gain: +{estimated_new_charges:>3} charges"
    )

print(f"\nNext step: Run bulk extraction on newly registered documents")
print(f"These documents can now be extracted with:")
print(f"  python -m duke_rates extract-bulk-ncuc-discovery --limit 20")
