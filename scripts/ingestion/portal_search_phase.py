#!/usr/bin/env python3
"""
Portal Search Phase: Search priority dockets for missing tariff documents.

Priority dockets (from gap analysis):
- E-2 Sub 1342 (Demand Side Management)
- E-2 Sub 1345 (Rider/Adjustment)
- E-2 Sub 1347 (Fuel Cost Recovery)
- E-7 (Electric Company)

Expected: 50-100 documents per docket, 80%+ tariff content
Goal: Reach 40% coverage (need 430 versions)
"""

import subprocess
import sys
from pathlib import Path

# Repository root
REPO_ROOT = Path(__file__).parent.parent

def run_cmd(cmd, description):
    """Run a command and report status."""
    print(f"\n{'='*70}")
    print(f"{description}")
    print(f"Command: {cmd}")
    print(f"{'='*70}")
    result = subprocess.run(cmd, shell=True, cwd=REPO_ROOT)
    return result.returncode == 0

def main():
    """Execute portal searches for priority dockets."""

    priority_dockets = [
        ("E-2 Sub 1342", "Demand Side Management"),
        ("E-2 Sub 1345", "Rider/Adjustment"),
        ("E-2 Sub 1347", "Fuel Cost Recovery"),
        ("E-7", "Electric Company"),
    ]

    print("""
========================================================================
        NCUC PORTAL SEARCH - PRIORITY DOCKETS

  Target: Reach 40% coverage (need 430 more versions)
  Current: 12.2% coverage (188/1,546 versions)
  Expected documents: 50-100 per docket
  Tariff quality: 80%+ (portal is pre-filtered)
========================================================================
""")

    results = []

    for docket_num, docket_desc in priority_dockets:
        query = f"docket:{docket_num}"
        cmd = (
            f'python -m duke_rates ncuc search '
            f'"{query}" '
            f'--docket-hint "{docket_num}" '
            f'--max-results 100'
        )

        success = run_cmd(cmd, f"Searching {docket_num} ({docket_desc})")
        results.append((docket_num, docket_desc, success))

        if not success:
            print(f"WARNING: Search for {docket_num} failed")

    print(f"\n{'='*70}")
    print("SEARCH SUMMARY")
    print(f"{'='*70}")
    for docket_num, docket_desc, success in results:
        status = "[OK]" if success else "[FAILED]"
        print(f"{status} {docket_num:20s} {docket_desc}")

    # Count discovered records
    print(f"\n{'='*70}")
    print("Checking discovered records...")
    print(f"{'='*70}")

    import sqlite3
    db_path = REPO_ROOT / "data/db/duke_rates.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        for docket_num, _, _ in results:
            c.execute(
                "SELECT COUNT(*) FROM ncuc_discovery_records WHERE docket_number = ?",
                (docket_num,)
            )
            count = c.fetchone()[0]
            print(f"{docket_num:20s} : {count:3d} discovered records")

        conn.close()

    print(f"\n{'='*70}")
    print("NEXT STEP: Fetch and extract from discovered documents")
    print(f"{'='*70}")
    print("""
Run these commands to fetch and extract:

  1. Fetch document details from portal:
     python -m duke_rates ncuc fetch-portal --limit 100 --dep-only

  2. Process documents with Docling:
     python -m duke_rates doc-intel process-docling-batch --classification tariff_sheets

  3. Mine Docling artifacts:
     python -m duke_rates doc-intel mine-docling --accelerator cuda

  4. Extract charges:
     python -m duke_rates extract-rates-nc

  5. Check final coverage:
     python << 'EOF'
import sqlite3
conn = sqlite3.connect("data/db/duke_rates.db")
c = conn.cursor()
c.execute("SELECT COUNT(DISTINCT version_id) FROM tariff_charges WHERE version_id IS NOT NULL")
versions = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM tariff_versions")
total = c.fetchone()[0]
pct = versions / total * 100 if total > 0 else 0
print(f"Final coverage: {versions}/{total} ({pct:.1f}%)")
print(f"Target: 40% ({int(total * 0.4)} versions)")
print(f"Gap: {max(0, int(total * 0.4) - versions)} versions")
EOF
""")

if __name__ == "__main__":
    main()
