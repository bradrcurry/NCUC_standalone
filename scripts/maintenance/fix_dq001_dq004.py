"""
Fix DQ-001: Delete phantom Rider Adjustment rows (-1.0 and -8.0 $/kWh) from
            Carolinas schedule families.
Fix DQ-004: Delete all 1,722 corrupted charge rows from nc-progress-leaf-501 v5302,
            and reset the version for re-extraction.

Run with --dry-run to preview, --execute to apply.
"""
from __future__ import annotations

import argparse
import sqlite3

DB_PATH = "data/db/duke_rates.db"

DQ001_FAMILIES = (
    "nc-carolinas-schedule-SGS",
    "nc-carolinas-schedule-I",
    "nc-carolinas-schedule-PG",
    "nc-carolinas-schedule-TS",
    "nc-carolinas-doc-SCHEDULEWC",
    "nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE",
    "nc-carolinas-doc-SCHEDULEOPTE",
)


# ---------------------------------------------------------------------------
# DQ-001
# ---------------------------------------------------------------------------

def fix_dq001(conn: sqlite3.Connection, dry_run: bool) -> None:
    cur = conn.cursor()

    placeholders = ",".join("?" * len(DQ001_FAMILIES))

    cur.execute(
        f"""
        SELECT COUNT(*) FROM tariff_charges
        WHERE family_key IN ({placeholders})
          AND charge_type = 'adjustment'
          AND rate_value IN (-1.0, -8.0)
          AND rate_unit = '$/kWh'
        """,
        DQ001_FAMILIES,
    )
    phantom_count = cur.fetchone()[0]

    cur.execute(
        f"""
        SELECT COUNT(*) FROM tariff_charges
        WHERE family_key IN ({placeholders})
          AND charge_type != 'adjustment'
        """,
        DQ001_FAMILIES,
    )
    other_charges = cur.fetchone()[0]

    cur.execute(
        f"""
        SELECT COUNT(*) FROM tariff_charges
        WHERE family_key IN ({placeholders})
          AND charge_type = 'adjustment'
          AND rate_value NOT IN (-1.0, -8.0)
        """,
        DQ001_FAMILIES,
    )
    legit_adjustment_count = cur.fetchone()[0]

    print(f"\n--- DQ-001: Phantom Rider Adjustment rows (-1.0, -8.0) ---")
    print(f"  Phantom rows to DELETE: {phantom_count}")
    print(f"  Other (non-adjustment) charges preserved: {other_charges}")
    print(f"  Legitimate adjustment rows (other values): {legit_adjustment_count}")

    if legit_adjustment_count > 0:
        print("  [WARN] Some non-(-1.0)/(-8.0) adjustment rows exist — review before proceeding!")
        cur.execute(
            f"""
            SELECT family_key, charge_label, rate_value, rate_unit, COUNT(*) as cnt
            FROM tariff_charges
            WHERE family_key IN ({placeholders})
              AND charge_type = 'adjustment'
              AND rate_value NOT IN (-1.0, -8.0)
            GROUP BY family_key, charge_label, rate_value, rate_unit
            """,
            DQ001_FAMILIES,
        )
        for r in cur.fetchall():
            print(f"    {r[0]} | {r[1]} | {r[2]} {r[3]} | cnt={r[4]}")

    if dry_run:
        print("  [DRY RUN] No changes made.")
        return

    cur.execute(
        f"""
        DELETE FROM tariff_charges
        WHERE family_key IN ({placeholders})
          AND charge_type = 'adjustment'
          AND rate_value IN (-1.0, -8.0)
          AND rate_unit = '$/kWh'
        """,
        DQ001_FAMILIES,
    )
    deleted = cur.rowcount
    print(f"  Deleted {deleted} phantom rows.")

    # Verify
    cur.execute(
        f"""
        SELECT COUNT(*) FROM tariff_charges
        WHERE family_key IN ({placeholders})
          AND charge_type = 'adjustment'
          AND rate_value IN (-1.0, -8.0)
        """,
        DQ001_FAMILIES,
    )
    remaining = cur.fetchone()[0]
    if remaining == 0:
        print("  [OK] All phantom rows deleted. Acceptance criteria met.")
    else:
        print(f"  [FAIL] WARNING: {remaining} phantom rows remain!")


# ---------------------------------------------------------------------------
# DQ-004
# ---------------------------------------------------------------------------

def fix_dq004(conn: sqlite3.Connection, dry_run: bool) -> None:
    cur = conn.cursor()

    VERSION_ID = 5302
    FAMILY_KEY = "nc-progress-leaf-501"

    cur.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (VERSION_ID,))
    row_count = cur.fetchone()[0]

    # What does the version look like?
    cur.execute("SELECT id, family_key, effective_start, source_type, notes, historical_document_id FROM tariff_versions WHERE id = ?", (VERSION_ID,))
    v = cur.fetchone()

    print(f"\n--- DQ-004: nc-progress-leaf-501 v{VERSION_ID} runaway extractor ---")
    print(f"  Version: id={v[0]} family={v[1]} eff_start={v[2]!r}")
    print(f"           source_type={v[3]!r} doc_id={v[5]!r}")
    print(f"           notes={v[4]!r}")
    print(f"  Rows to DELETE: {row_count}")

    # Check neighboring version counts to understand what's expected
    cur.execute(
        """
        SELECT tv.id, tv.effective_start, COUNT(tc.id) as cnt
        FROM tariff_versions tv
        LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
        WHERE tv.family_key = ?
        GROUP BY tv.id ORDER BY tv.effective_start
        """,
        (FAMILY_KEY,),
    )
    print(f"  Neighboring versions for context:")
    for r in cur.fetchall():
        marker = " <<< TARGET" if r[0] == VERSION_ID else ""
        print(f"    id={r[0]:5d}  eff_start={r[1]!r:15s}  charges={r[2]:4d}{marker}")

    if dry_run:
        print("  [DRY RUN] No changes made.")
        return

    # Delete all charges for this version
    cur.execute("DELETE FROM tariff_charges WHERE version_id = ?", (VERSION_ID,))
    deleted = cur.rowcount
    print(f"  Deleted {deleted} rows from version {VERSION_ID}.")

    # Update version notes to indicate it needs re-extraction
    cur.execute(
        """
        UPDATE tariff_versions
        SET notes = 'Bootstrapped for historical reprocess queue. Charges cleared 2026-04-02 (DQ-004: 1,722 corrupted rows from 178-page span; needs re-extraction with narrower span or page-aware segmentation).'
        WHERE id = ?
        """,
        (VERSION_ID,),
    )
    print(f"  Updated version notes to indicate re-extraction needed.")

    # Enqueue for reprocessing (add to the reprocess queue if possible)
    # Check what table/structure is used for reprocessing
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%reprocess%'")
    reprocess_tables = [r[0] for r in cur.fetchall()]
    print(f"  Reprocess tables found: {reprocess_tables}")

    if "reprocess_queue" in reprocess_tables:
        # Check if already queued
        cur.execute("SELECT id FROM reprocess_queue WHERE version_id = ?", (VERSION_ID,))
        existing = cur.fetchone()
        if not existing:
            cur.execute(
                """
                INSERT INTO reprocess_queue (version_id, family_key, reason, created_at)
                VALUES (?, ?, 'DQ-004: corrupted 1722-row extraction from 178-page span; needs page-aware re-extraction', datetime('now'))
                """,
                (VERSION_ID, FAMILY_KEY),
            )
            print(f"  Added version {VERSION_ID} to reprocess_queue.")
        else:
            print(f"  Version {VERSION_ID} already in reprocess_queue (id={existing[0]}).")
    else:
        print("  [NOTE] No reprocess_queue table found. Version notes updated; enqueue manually.")

    # Verify
    cur.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (VERSION_ID,))
    remaining = cur.fetchone()[0]
    if remaining == 0:
        print(f"  [OK] All corrupted rows deleted from v{VERSION_ID}.")
    else:
        print(f"  [FAIL] {remaining} rows remain in v{VERSION_ID}!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fix DQ-001 and DQ-004 data quality issues.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB changes")
    parser.add_argument("--execute", action="store_true", help="Apply changes to DB")
    parser.add_argument("--dq001-only", action="store_true")
    parser.add_argument("--dq004-only", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("ERROR: Must specify --dry-run or --execute")
        parser.print_help()
        return

    dry_run = args.dry_run
    conn = sqlite3.connect(DB_PATH)
    try:
        if not args.dq004_only:
            fix_dq001(conn, dry_run)
        if not args.dq001_only:
            fix_dq004(conn, dry_run)

        if not dry_run:
            conn.commit()
            print("\n[OK] All changes committed.")
        else:
            print("\n[DRY RUN complete -- no changes committed]")
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
