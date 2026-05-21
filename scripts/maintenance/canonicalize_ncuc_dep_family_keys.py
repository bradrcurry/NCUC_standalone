"""
Canonicalize 4 orphan historical_documents with `ncuc-dep-N` family-key shape.

Each orphan has an identical-content + identical-span sibling in the
canonical `nc-progress-leaf-N` family. The orphans are stuck on the
non-canonical family-key, so no profile matches them and they stay
'skipped' or route to 'unknown'.

The canonical numbering for these 4 PDFs (confirmed from PDF content):
  hd_id=16 (ncuc-dep-604 "REPS Rider")      -> nc-progress-leaf-603 (REPS)
  hd_id=19 (ncuc-dep-605 "REPS EMF Rider")  -> nc-progress-leaf-603 (REPS)
  hd_id=60 (ncuc-dep-721 "TOB")             -> nc-progress-leaf-721 (TOB)
  hd_id=61 (ncuc-dep-723 "Smart $aver TOBR") -> nc-progress-leaf-723

Note 1: hd_id=16 and hd_id=19 are both REPS proposed orders (E-2 Sub 1109)
and map to leaf-603 (not 604/605 — those are EDIT-4 and CPRE respectively).
This was the same fix applied to their canonical-path siblings on 2026-05-16.

Note 2: After this, each family will have 2 rows for the same PDF — one from
the orphan import path, one from the canonical NCUC path. Acceptable: same
pattern already exists for compliance-book splits, and dedupe-by-content
downstream handles it.

No rows are deleted; only family_key is updated. Preserves all FK fanout
(70+44 processing_runs + tariff_versions + 14+14 classifications + queue
entries per orphan).

Usage:
    python scripts/maintenance/canonicalize_ncuc_dep_family_keys.py --dry-run
    python scripts/maintenance/canonicalize_ncuc_dep_family_keys.py --execute
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH_DEFAULT = REPO_ROOT / "data" / "db" / "duke_rates.db"

PLAN = [
    # Round 1: paired with same-content canonical sibling
    (16, "nc-progress-leaf-603"),  # REPS Rider (E-2 Sub 1109)
    (19, "nc-progress-leaf-603"),  # REPS EMF Rider (E-2 Sub 1109)
    (60, "nc-progress-leaf-721"),  # Residential TOB
    (61, "nc-progress-leaf-723"),  # Smart $aver Early Replacement+Retrofit TOBR
    # Round 2: unique orphans (no same-content sibling), title-confirmed
    (10, "nc-progress-leaf-605"),  # Summary of CPRE Proposed Rider (Sub 1108 supplemental)
    (14, "nc-progress-leaf-602"),  # Joint Agency Adjustment Rider (JAA)
    (20, "nc-progress-leaf-606"),  # Demand Side Management Rider (DSM)
    (63, "nc-progress-leaf-802"),  # Line Extension Plan LEP
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument("--execute", action="store_true",
                        help="Apply changes. Default is dry-run.")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"{'EXECUTE' if args.execute else 'DRY RUN'} family-key canonicalization")
    print("=" * 60)
    for hd_id, new_fk in PLAN:
        r = conn.execute(
            "SELECT id, family_key, title FROM historical_documents WHERE id=?",
            (hd_id,),
        ).fetchone()
        if not r:
            print(f"  SKIP: hd_id={hd_id} not found")
            continue
        if r["family_key"] == new_fk:
            print(f"  hd_id={hd_id}: already {new_fk}, no change")
            continue
        print(f"  hd_id={hd_id}: family_key {r['family_key']!r} -> {new_fk!r}")
        print(f"    title: {(r['title'] or '')[:80]}")

    if not args.execute:
        print("\nDRY RUN — re-run with --execute to apply.")
        return 0

    cur = conn.cursor()
    applied = 0
    for hd_id, new_fk in PLAN:
        cur.execute(
            "UPDATE historical_documents SET family_key=? "
            "WHERE id=? AND family_key != ?",
            (new_fk, hd_id, new_fk),
        )
        applied += cur.rowcount
    conn.commit()
    print(f"\nApplied {applied} family_key update(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
