"""Retire empty tariff_versions whose effective_start displaces a charged version.

The post-bootstrap re-extraction can create new tariff_version rows for
documents that don't produce extractable charges (e.g., compliance bundle
PDFs that contain the rider name but not its rate table). When such empty
versions have an effective_start that's newer than the latest correctly-
charged version in the same family, the tariff completeness audit's
`_select_version` (ORDER BY effective_start DESC LIMIT 1) picks the empty
version as "active" — silently masking the legitimate older version's
charges.

Example caught on 2026-05-27:
  family nc-progress-leaf-604 (EDIT-4):
    v=5399 eff=2023-10-01 charges=8  (correctly-extracted)
    v=7271 eff=2026-01-01 charges=0  (from compliance bundle, displaces v=5399)

This script deletes empty `tariff_versions` rows where:
  - The version has 0 tariff_charges anchored on it
  - effective_start IS NOT NULL
  - effective_start >= the latest effective_start of a *charged* version in
    the same family
  - The family has at least one charged version (so we don't delete the only
    version of a not-yet-extracted family)

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
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Family -> latest effective_start among charged versions
    charged_latest = {
        r["family_key"]: r["latest"]
        for r in conn.execute(
            """SELECT tv.family_key, MAX(hd.effective_start) AS latest
                 FROM tariff_versions tv
                 JOIN historical_documents hd ON hd.id = tv.historical_document_id
                 JOIN tariff_charges tc ON tc.version_id = tv.id
                 WHERE tv.family_key LIKE 'nc-%'
                   AND hd.effective_start IS NOT NULL
                 GROUP BY tv.family_key""",
        )
    }

    # Empty versions where eff_start >= latest_charged in their family
    rows = conn.execute(
        """SELECT tv.id, tv.family_key, hd.effective_start, hd.id AS hd_id,
                  hd.local_path
             FROM tariff_versions tv
             JOIN historical_documents hd ON hd.id = tv.historical_document_id
             WHERE tv.family_key LIKE 'nc-%'
               AND hd.effective_start IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM tariff_charges tc WHERE tc.version_id = tv.id)"""
    ).fetchall()

    to_retire = [
        r for r in rows
        if r["family_key"] in charged_latest
        and r["effective_start"] >= charged_latest[r["family_key"]]
    ]

    by_fam = Counter(r["family_key"] for r in to_retire)
    print(f"Empty displacers found: {len(to_retire)}")
    print(f"Distinct families:       {len(by_fam)}")
    print()
    print("Top affected families:")
    for fam, n in by_fam.most_common(15):
        print(f"  {n:>3}  {fam}  (latest_charged={charged_latest[fam]})")

    if not args.apply:
        print(f"\nDRY RUN: would retire {len(to_retire)} tariff_versions. --apply to write.")
        return 0

    cur = conn.cursor()
    tv_ids = [r["id"] for r in to_retire]
    # Defensive: clean tariff_charges anchored to these versions (should be 0 by definition).
    cur.executemany("DELETE FROM tariff_charges WHERE version_id = ?", [(i,) for i in tv_ids])
    cur.executemany("DELETE FROM tariff_versions WHERE id = ?", [(i,) for i in tv_ids])
    conn.commit()
    print(f"\nRetired {len(tv_ids)} empty displacing tariff_versions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
