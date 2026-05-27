"""Delete tariff_charges where the charge's family_key disagrees with its
tariff_version's family_key — provided the family has at least one correctly-
anchored charge elsewhere.

Why this works as a cleanup:

A bundle PDF gets registered as N historical_documents (one per family_key
detected in its title via shingle matching). Each registration creates a
tariff_version. When extract-rates-nc runs each version, it extracts ALL
recognizable rate content from the PDF, tagging charges with the family_key
detected from the *content* (which is often correct) but anchoring them on
the version_id of the *registration* (which is N-deep duplicated).

Result: charge has correct `family_key` but wrong `version_id`. The correctly-
anchored copy already exists on the legitimate version.

The "correct-anchor-exists-elsewhere" guard avoids deleting orphan-only
families that exist solely via cross-attribution — those need different
treatment (re-anchor, not delete).

Companion to fix_misattributed_leaf600_rows.py (which is leaf-600/Progress
specific). This is the general form.

Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Build the set of families that have at least one correctly-anchored charge.
    legit_fams = {
        r[0]
        for r in conn.execute(
            """SELECT DISTINCT tc.family_key
                 FROM tariff_charges tc
                 JOIN tariff_versions v ON v.id = tc.version_id
                 WHERE tc.family_key = v.family_key"""
        )
    }
    print(f"Families with at least one correct anchor: {len(legit_fams)}")

    # Find mismatched rows where the family is "legit elsewhere" (safe to delete)
    rows = conn.execute(
        """SELECT tc.id, tc.family_key, v.family_key AS v_family, tc.version_id, tc.charge_label, tc.rate_value, tc.rate_unit
             FROM tariff_charges tc
             JOIN tariff_versions v ON v.id = tc.version_id
             WHERE tc.family_key != v.family_key"""
    ).fetchall()
    to_delete = [r for r in rows if r["family_key"] in legit_fams]
    keep_orphans = [r for r in rows if r["family_key"] not in legit_fams]

    print(f"Total mismatches: {len(rows)}")
    print(f"  ...where family has legit-anchor elsewhere (DELETE candidates): {len(to_delete)}")
    print(f"  ...orphan-only (KEEP, needs re-anchor): {len(keep_orphans)}")

    by_pair = Counter((r["family_key"], r["v_family"]) for r in to_delete)
    print()
    print("Top 15 (tc.family, v.family) pairs to delete:")
    for (tcf, vf), n in by_pair.most_common(15):
        print(f"  n={n:>5}  {tcf:<40s} -> v.family={vf}")

    print()
    print("Sample (top abs values):")
    for r in sorted(to_delete, key=lambda x: -abs(x["rate_value"] or 0))[:8]:
        print(f"  ch={r['id']} tc.fam={r['family_key']:<35s} v.fam={r['v_family']:<35s} val={r['rate_value']!s:>10} {r['rate_unit']:<10} | {(r['charge_label'] or '')[:60]}")

    if not args.apply:
        print(f"\nDRY RUN: would delete {len(to_delete)}.  --apply to write.")
        return 0

    cur = conn.cursor()
    cur.executemany("DELETE FROM tariff_charges WHERE id=?", [(r["id"],) for r in to_delete])
    conn.commit()
    print(f"\nDeleted {len(to_delete)} cross-attributed rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
