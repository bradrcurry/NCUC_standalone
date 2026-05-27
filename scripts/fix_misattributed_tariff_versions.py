"""Delete tariff_versions whose family_key disagrees with their historical_document's family_key.

This is the third cross-attribution layer found in the 2026-05-27 session:

  fix_misattributed_leaf600_rows.py     — charges with leaf-600 labels on rider versions
  fix_cross_attribution_general.py      — charges where tc.family_key != tv.family_key
  fix_misattributed_tariff_versions.py  — versions where tv.family_key != hd.family_key  (this)

Pattern: a bundle PDF gets registered as one family (often a "noise" shingle
or a top-level bundle family), but a tariff_version row was created against
that historical_document with a *different* family_key — typically because
the parser detected content for another family inside the bundle and made a
new version row for it. The resulting charges anchor on the wrong PDF and
often duplicate (or replace) the correct version's charges in
date-based "active version" lookups.

Example caught on 2026-05-27:
  tv.id=6062 family=nc-progress-leaf-604 eff=2024-10-01
    -> hd.id=2275 family=nc-progress-leaf-600 (a multi-leaf bundle PDF)
  The 2 charges anchored on this tv were residential schedule rates
  ("A. Customer Charge $28.50/mo" + "Energy Charge $0.15975/kWh") — neither
  leaf-604 (EDIT-4 rider) content nor leaf-600 (rider summary) content. The
  parser had landed on a residential schedule sheet within the bundle and
  tagged its output to family 604.

  Removing this version causes the EDIT-4 audit to fall back to the
  correctly-anchored hd=364 (effective 2023-10-01, 8 real EDIT-4 charges).

This script also deletes the tariff_charges anchored on the deleted versions
(via ON DELETE CASCADE or explicit cleanup).

Idempotent. Dry run by default; --apply to write.
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
    ap.add_argument("--family-prefix", default="nc-",
                    help="Only consider versions whose family starts with this (default: nc-).")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT tv.id AS tv_id, tv.family_key AS tv_family,
                  hd.family_key AS hd_family,
                  hd.id AS hd_id, hd.effective_start,
                  (SELECT COUNT(*) FROM tariff_charges tc WHERE tc.version_id = tv.id) AS n_charges
             FROM tariff_versions tv
             JOIN historical_documents hd ON hd.id = tv.historical_document_id
             WHERE tv.family_key != hd.family_key
               AND tv.family_key LIKE ? || '%' """,
        (args.family_prefix,),
    ).fetchall()

    print(f"Mismatched versions: {len(rows)}")
    n_charges_total = sum(r["n_charges"] for r in rows)
    print(f"Charges anchored on these versions: {n_charges_total}")

    pair_counts = Counter((r["tv_family"], r["hd_family"]) for r in rows)
    print()
    print("Top 15 (tv.family, hd.family) pairs:")
    for (tv_f, hd_f), n in pair_counts.most_common(15):
        print(f"  n={n:>3}  tv.fam={tv_f:<35s} hd.fam={hd_f}")

    print()
    print("Sample versions (largest charge counts first):")
    for r in sorted(rows, key=lambda x: -x["n_charges"])[:6]:
        print(f"  tv={r['tv_id']} tv.fam={r['tv_family']} hd={r['hd_id']} hd.fam={r['hd_family']} eff={r['effective_start']} charges={r['n_charges']}")

    if not args.apply:
        print(f"\nDRY RUN: would delete {len(rows)} tariff_versions and {n_charges_total} dependent charges.")
        return 0

    cur = conn.cursor()
    tv_ids = [r["tv_id"] for r in rows]
    # Delete charges first
    cur.executemany("DELETE FROM tariff_charges WHERE version_id = ?", [(i,) for i in tv_ids])
    n_ch_deleted = cur.rowcount  # may be -1 on executemany; ignore
    # Delete versions
    cur.executemany("DELETE FROM tariff_versions WHERE id = ?", [(i,) for i in tv_ids])
    conn.commit()
    print(f"\nDeleted {len(tv_ids)} tariff_versions (and {n_charges_total} anchored tariff_charges).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
