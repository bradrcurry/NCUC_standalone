"""Retire NC historical_documents and tariff_versions in noise/shingle families.

Background:
  The ncuc-import / discovery pipeline assigns each document a `family_key`
  by matching title text against known leaf/schedule/rider codes. When no
  short code matches, it falls back to a title-shingle hash of the document
  title's content words, producing family_keys like:

    nc-progress-doc-ICERTIFYTHATACOPYOFDUKEENERGYPROGRESSLLCSAMENDED
    nc-progress-program-DEMANDRESPONSEPROGRAM
    nc-carolinas-rider-NOTICEOFAPPLICATIONFORRIDERRATEADJUSTMENTSANDPUB

  These shingles are not real tariff families — they're title fragments of
  procedural documents, applications, certificates of service, etc. They
  clutter every audit and pollute the classifier training signal.

Retire criteria (all must hold):
  - family_key starts with 'nc-' (NC only)
  - family_key matches one of:
      * `*-doc-*`        (e.g. nc-progress-doc-SCHEDULE10, ...-ICERTIFYTHAT...)
      * `*-program-*`    (e.g. nc-progress-program-DSMEEPROGRAM)
      * `*-rider-<long>` where suffix length > 12 (real rider codes are short:
        BA, JAA, RDM, ESM, PIM, CAR, CPRE, EDPR, FCAR, STS, RECD, REPS, etc.)

Drops, in order:
  1. tariff_charges (where family_key matches the noise pattern OR
     version_id points to a tariff_version in noise family)
  2. tariff_versions (where family_key matches)
  3. historical_documents (where family_key matches)

Idempotent. Dry run by default; --apply to write.

Expected: ~496 hd rows + ~423 versions + ~13 stray charges removed on
2026-05-27.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")

# Matches noise/shingle family_key prefixes. Note: real rider codes are short
# (<=12 chars); anything longer is a title-shingle artifact.
NOISE_WHERE = """
    family_key LIKE 'nc-%'
    AND (
        family_key LIKE '%-doc-%'
        OR family_key LIKE '%-program-%'
        OR (family_key LIKE 'nc-progress-rider-%'
            AND LENGTH(REPLACE(family_key, 'nc-progress-rider-', '')) > 12)
        OR (family_key LIKE 'nc-carolinas-rider-%'
            AND LENGTH(REPLACE(family_key, 'nc-carolinas-rider-', '')) > 12)
    )
""".strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Inventory
    hd_rows = conn.execute(
        f"SELECT id, family_key, title FROM historical_documents WHERE {NOISE_WHERE}"
    ).fetchall()
    tv_rows = conn.execute(
        f"SELECT id, family_key FROM tariff_versions WHERE {NOISE_WHERE}"
    ).fetchall()
    # tariff_charges with noise family_key directly
    tc_direct = conn.execute(
        f"SELECT id, family_key FROM tariff_charges WHERE {NOISE_WHERE}"
    ).fetchall()
    # tariff_charges anchored on noise tariff_versions but with a different family_key
    tv_ids = {r["id"] for r in tv_rows}
    if tv_ids:
        placeholders = ",".join("?" * len(tv_ids))
        tc_anchored = conn.execute(
            f"SELECT id, family_key FROM tariff_charges WHERE version_id IN ({placeholders})",
            tuple(tv_ids),
        ).fetchall()
    else:
        tc_anchored = []
    tc_all = list({r["id"] for r in tc_direct} | {r["id"] for r in tc_anchored})

    print(f"Noise historical_documents:  {len(hd_rows)}")
    print(f"Noise tariff_versions:        {len(tv_rows)}")
    print(f"tariff_charges to drop:       {len(tc_all)}")
    print(f"  via family_key match:       {len(tc_direct)}")
    print(f"  via anchored version:       {len(tc_anchored)}")

    fam_counts = Counter(r["family_key"] for r in hd_rows)
    print()
    print(f"Top families being retired ({len(fam_counts)} distinct):")
    for fam, n in fam_counts.most_common(15):
        print(f"  {n:>4}  {fam}")

    if not args.apply:
        print(f"\nDRY RUN: would retire {len(hd_rows)} hd + {len(tv_rows)} tv + {len(tc_all)} tc. --apply to write.")
        return 0

    cur = conn.cursor()
    # Order matters: charges -> versions -> historical_documents.
    if tc_all:
        cur.executemany("DELETE FROM tariff_charges WHERE id = ?", [(i,) for i in tc_all])
    if tv_ids:
        cur.executemany("DELETE FROM tariff_versions WHERE id = ?", [(i,) for i in tv_ids])
    if hd_rows:
        cur.executemany("DELETE FROM historical_documents WHERE id = ?", [(r["id"],) for r in hd_rows])
    conn.commit()
    print(f"\nRetired {len(hd_rows)} historical_documents, {len(tv_rows)} tariff_versions, {len(tc_all)} tariff_charges.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
