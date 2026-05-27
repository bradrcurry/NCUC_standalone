"""Targeted fixes for undated bill-relevant historical_documents.

Findings from the 2026-05-27 historical sweep:

  hd=7782 (nc-progress-leaf-611 CAR, e-2-sub-1300-sept2023 bundle, span 143-143)
    Sidecar text reads: "Effective for service rendered from October 1, 2023
    through September 30, 2024" — clean leaf-611 CAR content with a recoverable
    effective_start of 2023-10-01.
    FIX: backfill effective_start = '2023-10-01'.

  hd=2504, 2505, 7613, 7669 (all tagged nc-progress-leaf-613 STS-2)
    Sidecar texts mention "Storm Securitization Rider" (singular STS, not
    STS-2) with effective dates from 2021-12-01 and 2022-12-01. STS-2
    (leaf-613) was created in 2023. None of these docs reference "Leaf No.
    613" — hd=7669 explicitly references "Leaf No. 607". These are clearly
    original-STS content misclassified to leaf-613.
    FIX: retire these 4 historical_documents (their leaf-607 equivalents
    already exist with correct dates).

Idempotent. Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")

# Verified findings: hd_id -> new effective_start
DATE_BACKFILLS = {
    7782: "2023-10-01",  # leaf-611 CAR
}

# Misclassified leaf-613 docs (actually leaf-607 content); retire to avoid
# duplicating correctly-anchored leaf-607 versions.
RETIRE_HD_IDS = [2504, 2505, 7613, 7669]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Inspect before
    print("Before:")
    for hd_id in list(DATE_BACKFILLS) + RETIRE_HD_IDS:
        r = conn.execute(
            "SELECT id, family_key, effective_start FROM historical_documents WHERE id=?",
            (hd_id,),
        ).fetchone()
        if r:
            print(f"  hd={r['id']} fam={r['family_key']} eff={r['effective_start']}")

    if not args.apply:
        print()
        print(f"DRY RUN: would backfill {len(DATE_BACKFILLS)} dates and retire {len(RETIRE_HD_IDS)} docs. --apply to write.")
        return 0

    cur = conn.cursor()
    # 1. Date backfill
    for hd_id, eff in DATE_BACKFILLS.items():
        cur.execute("UPDATE historical_documents SET effective_start = ? WHERE id = ?", (eff, hd_id))
        # Mirror into tariff_versions if a row exists for this hd
        cur.execute(
            "UPDATE tariff_versions SET effective_start = ? WHERE historical_document_id = ? AND (effective_start IS NULL OR effective_start = '')",
            (eff, hd_id),
        )

    # 2. Retire misclassified docs (and their dependent versions/charges)
    for hd_id in RETIRE_HD_IDS:
        # Find dependent tariff_version ids
        vids = [r[0] for r in cur.execute(
            "SELECT id FROM tariff_versions WHERE historical_document_id = ?", (hd_id,)
        ).fetchall()]
        for vid in vids:
            cur.execute("DELETE FROM tariff_charges WHERE version_id = ?", (vid,))
            cur.execute("DELETE FROM tariff_versions WHERE id = ?", (vid,))
        cur.execute("DELETE FROM historical_documents WHERE id = ?", (hd_id,))

    conn.commit()

    # Inspect after
    print()
    print("After:")
    for hd_id, eff in DATE_BACKFILLS.items():
        r = conn.execute(
            "SELECT id, family_key, effective_start FROM historical_documents WHERE id=?",
            (hd_id,),
        ).fetchone()
        if r:
            print(f"  hd={r['id']} fam={r['family_key']} eff={r['effective_start']}")
    for hd_id in RETIRE_HD_IDS:
        r = conn.execute("SELECT id FROM historical_documents WHERE id=?", (hd_id,)).fetchone()
        if r:
            print(f"  hd={hd_id} STILL EXISTS (delete failed)")
        else:
            print(f"  hd={hd_id} RETIRED")

    print(f"\nApplied {len(DATE_BACKFILLS)} date backfills and retired {len(RETIRE_HD_IDS)} misclassified docs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
