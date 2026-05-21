"""
Fix two REPS docs misclassified by the NCUC family classifier as EDIT-4 / CPRE.

Backstory: docket E-2 Sub 1109 contains proposed-order documents for the REPS
(Renewable Energy and Energy Efficiency Portfolio Standard Cost Recovery)
Rider. Two PDFs in this docket were ingested twice:

  1. ncuc-dep-604 "REPS Rider" (hd_id=16)         <-> nc-progress-leaf-604 "EDIT-4" (hd_id=7369)
  2. ncuc-dep-605 "REPS EMF Rider" (hd_id=19)     <-> nc-progress-leaf-605 "CPRE"   (hd_id=7311)

Same content_hash + same span on each side, but the NCUC canonical-path
classifier picked the wrong family. The orphan-path doc has the correct
title (read from the filename); the canonical-path doc has wrong title
and wrong family_key.

REPS canonically lives at nc-progress-leaf-603 per the profile allowlist
in parser_profiles.py (line 2570).

Fix (canonical-side only):
  - Update hd_id=7369 family_key -> nc-progress-leaf-603, title -> "REPS Rider..."
  - Update hd_id=7311 family_key -> nc-progress-leaf-603, title -> "REPS EMF Rider..."

The orphan rows hd_id=16 and hd_id=19 are NOT deleted — they have 70+44
processing_runs + tariff_versions + 14+14 classifications + reprocess_queue
entries each. The audit trail is more valuable than cosmetic dedupe, and
the orphans don't cause routing issues since `ncuc-dep-N` isn't in any
profile allowlist (they just stay 'skipped').

No charges change — all four docs currently extract 0 charges (these are
proposed orders, not tariff sheets). The fix is metadata-only but prevents
future audits from listing these as 'empty docs in leaf-604/605'.

Run dry-run first. --execute applies.

Usage:
    python scripts/maintenance/fix_reps_misclassification_e2_sub_1109.py --dry-run
    python scripts/maintenance/fix_reps_misclassification_e2_sub_1109.py --execute
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH_DEFAULT = REPO_ROOT / "data" / "db" / "duke_rates.db"

# (hd_id_to_update, new_family_key, new_title)
PLAN = [
    (
        7369,
        "nc-progress-leaf-603",
        "REPS Rider (E-2 Sub 1109 Proposed Order, Span 1-6)",
    ),
    (
        7311,
        "nc-progress-leaf-603",
        "REPS EMF Rider (E-2 Sub 1109 Proposed Order, Span 1-52)",
    ),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true",
                        help="Apply changes. Default is dry-run.")
    args = parser.parse_args(argv)

    if args.execute:
        args.dry_run = False

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"{'DRY RUN' if args.dry_run else 'EXECUTING'} REPS classification fix")
    print("=" * 60)
    for hd_id, new_fk, new_title in PLAN:
        row = conn.execute(
            "SELECT id, family_key, title FROM historical_documents WHERE id=?",
            (hd_id,),
        ).fetchone()
        if not row:
            print(f"  SKIP: hd_id={hd_id} not found")
            continue
        print(f"  hd_id={hd_id}:")
        print(f"    family_key {row['family_key']!r} -> {new_fk!r}")
        print(f"    title      {row['title'][:60]!r}")
        print(f"           ->  {new_title!r}")
        print()

    if args.dry_run:
        print("DRY RUN — no changes made. Re-run with --execute to apply.")
        return 0

    cur = conn.cursor()
    applied = 0
    for hd_id, new_fk, new_title in PLAN:
        cur.execute(
            "UPDATE historical_documents SET family_key=?, title=? WHERE id=?",
            (new_fk, new_title, hd_id),
        )
        applied += cur.rowcount
    conn.commit()
    print(f"Applied {applied} update(s) to historical_documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
