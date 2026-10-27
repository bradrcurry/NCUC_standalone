"""Backfill DEC Schedule RS base-rate versions for the 2021-2022 gap.

CONTEXT
-------
The canonical ``nc-carolinas-schedule-RS`` history had base energy charges only
for 2013-2015 and 2025-2026, leaving a 2016-2024 hole.  The DEC RS base leaf
(Leaf No. 11, "Electricity No. 4") was parsed into ``ncuc_ingest_segments`` for
this window but never promoted into ``tariff_versions`` / ``tariff_charges``.

Source PDFs were inspected directly (see reports/_verify_rs_2018_pdf.txt).  Each
prints "Basic Facilities Charge per month $14.00" and "Energy Charges ... per
kWh 9.3826c" (both seasons identical -> flat schedule).  The base energy held at
9.3826c across the whole span; rate movement in this period was in the riders.

The legacy segments carried one mis-dated row (seg 5973 tagged 2018-09-01 but the
PDF leaf reads "Effective August 1, 2022" -- a duplicate of the 2022-08-01 leaf),
so the true distinct base effective dates are:

    2021-12-01   Leaf 52nd Revised   E-7 Sub 1243
    2022-02-01   Leaf 53rd Revised   E-7 Sub 1214
    2022-08-01   Leaf 54th Revised   E-7 Sub 1146

VALUES (verified against the printed leaf)
    energy_block  0.093826  $/kWh   (season 'all_year'; both printed seasons equal)
    fixed         14.00     $/month (Basic Facilities Charge)

This script is IDEMPOTENT: it inserts a version only if no
``nc-carolinas-schedule-RS`` version already exists for that effective_start, and
re-running is a no-op.  Reverse with:
    DELETE FROM tariff_charges WHERE version_id IN
      (SELECT id FROM tariff_versions WHERE family_key='nc-carolinas-schedule-RS'
       AND effective_start IN ('2021-12-01','2022-02-01','2022-08-01')
       AND source_type='backfill_legacy_segment');
    DELETE FROM tariff_versions WHERE family_key='nc-carolinas-schedule-RS'
      AND effective_start IN ('2021-12-01','2022-02-01','2022-08-01')
      AND source_type='backfill_legacy_segment';

Run:  python scripts/backfill_dec_rs_2021_2022.py          (dry-run, prints plan)
      python scripts/backfill_dec_rs_2021_2022.py --apply  (writes to the DB)
"""
from __future__ import annotations
import sqlite3, sys, datetime
from pathlib import Path

DB = Path("data/db/duke_rates.db")
FAMILY = "nc-carolinas-schedule-RS"
ENERGY_RATE = 0.093826
FIXED_RATE = 14.00
VER_NOTE = ("Backfilled from ncuc_ingest_segments Leaf-11; values verified against "
            "printed PDF (energy 9.3826c/kWh, Basic Facilities Charge $14.00).")
ENERGY_NOTE = "Backfill; both printed seasons equal 9.3826c/kWh (flat schedule)."
FIXED_NOTE = "Backfill from Leaf-11 Basic Facilities Charge."

# (effective_start, leaf_revision, docket, source_pdf)
ROWS = [
    ("2021-12-01", "52nd Revised", "E-7, Sub 1243",
     r"data\historical\ncuc\e-7-sub-1243\4e0c2c78-3f6f-47b5-8ae0-25aed12fe82f.pdf"),
    ("2022-02-01", "53rd Revised", "E-7, Sub 1214",
     r"data\historical\ncuc\e-7-sub-1214\bd980ae4-9963-4ee3-ae4e-b043653227c8.pdf"),
    ("2022-08-01", "54th Revised", "E-7, Sub 1146",
     r"data\historical\ncuc\e-7-sub-1146\c59be01b-89e9-4c21-8ccc-95dc97d2b244.pdf"),
]

_INSERT_VERSION = """
    INSERT INTO tariff_versions
        (family_key, effective_start, revision_label, source_type,
         confidence_score, notes, created_at, docket_number, leaf_no,
         source_pdf, status)
    VALUES (?, ?, ?, 'backfill_legacy_segment', 0.9, ?, ?, ?, '11', ?, 'approved')
"""

_INSERT_CHARGE = """
    INSERT INTO tariff_charges
        (version_id, family_key, charge_type, charge_label, rate_value,
         rate_unit, season, customer_class, confidence_score, notes, created_at)
    VALUES (?, ?, ?, ?, ?, ?, 'all_year', 'residential', 0.9, ?, ?)
"""


def main(apply: bool) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        existing = {
            str(r["effective_start"])[:10]
            for r in conn.execute(
                "SELECT effective_start FROM tariff_versions WHERE family_key=?",
                (FAMILY,),
            )
        }
        print(f"existing RS versions: {sorted(existing)}")
        planned = [(eff, rev, dk, pdf) for eff, rev, dk, pdf in ROWS if eff not in existing]
        for eff, rev, dk, pdf in ROWS:
            tag = "PLAN insert" if eff not in existing else "SKIP (exists)"
            print(f"  {tag} {eff}  leaf={rev}  {dk}")
        if not planned:
            print("Nothing to do.")
            return
        if not apply:
            print(f"\nDRY-RUN. Re-run with --apply to write {len(planned)} versions "
                  f"+ {2 * len(planned)} charges.")
            return

        for eff, rev, docket, pdf in planned:
            cur = conn.execute(
                _INSERT_VERSION, (FAMILY, eff, rev, VER_NOTE, now, docket, pdf)
            )
            vid = cur.lastrowid
            conn.execute(
                _INSERT_CHARGE,
                (vid, FAMILY, "energy_block", "Kilowatt-Hour Charge",
                 ENERGY_RATE, "$/kWh", ENERGY_NOTE, now),
            )
            conn.execute(
                _INSERT_CHARGE,
                (vid, FAMILY, "fixed", "Basic Facilities Charge",
                 FIXED_RATE, "$/month", FIXED_NOTE, now),
            )
            print(f"  WROTE version id={vid} for {eff} (+2 charges)")
        conn.commit()
        print(f"\nCOMMITTED {len(planned)} versions.")
    finally:
        conn.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
