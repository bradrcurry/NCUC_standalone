"""Deduplicate tariff_charges rows that share (version_id, family_key, charge_label, rate_value, rate_unit, customer_class, tier_min, tier_max, tou_period).

Some parser profiles (notably progress_billing_adjustments) emit each charge
row twice — once per table-pass iteration. The duplicates don't matter for
display but they double the engine's per-rider sum, distorting bill
reconciliation totals.

Keeps the lowest `id` in each duplicate group; deletes the rest. Idempotent.
Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--family-prefix", default="nc-",
                    help="Only operate on tariff_charges whose family_key starts with this (default: nc-).")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT MIN(id) AS keep_id, GROUP_CONCAT(id) AS all_ids, COUNT(*) AS n,
                  version_id, family_key, charge_label, rate_value, rate_unit,
                  customer_class, tier_min, tier_max, tou_period
             FROM tariff_charges
             WHERE family_key LIKE ? || '%'
             GROUP BY version_id, family_key, charge_label, rate_value, rate_unit,
                      customer_class, tier_min, tier_max, tou_period
             HAVING n > 1""",
        (args.family_prefix,),
    ).fetchall()

    to_delete: list[int] = []
    for r in rows:
        ids = [int(x) for x in r["all_ids"].split(",")]
        keep = r["keep_id"]
        for i in ids:
            if i != keep:
                to_delete.append(i)

    print(f"Duplicate groups: {len(rows)}")
    print(f"Rows to delete (keeping one per group): {len(to_delete)}")
    print()
    print("Sample groups (top 5 by group size):")
    for r in sorted(rows, key=lambda x: -x["n"])[:5]:
        print(f"  n={r['n']} fam={r['family_key']} v={r['version_id']} val={r['rate_value']} class={r['customer_class']!r} | {(r['charge_label'] or '')[:60]}")

    if not args.apply:
        print(f"\nDRY RUN: would delete {len(to_delete)} rows. --apply to write.")
        return 0

    cur = conn.cursor()
    cur.executemany("DELETE FROM tariff_charges WHERE id = ?", [(i,) for i in to_delete])
    conn.commit()
    print(f"\nDeleted {len(to_delete)} duplicate rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
