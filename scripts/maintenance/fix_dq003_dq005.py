"""
Fix DQ-003: Delete BPM Prospective Rider phantom rows from tariff_charges.
Fix DQ-005: Normalize non-ISO effective_start dates in tariff_versions.

Run with --dry-run to preview changes without committing.
Run with --execute to apply changes.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime

DB_PATH = "data/db/duke_rates.db"

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_fuzzy_date(s: str) -> str | None:
    """Parse human-readable date string to ISO YYYY-MM-DD. Returns None on failure."""
    s = s.strip().replace("\n", " ").replace("  ", " ")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.match(r"^(\w+)\s+(\d{1,2}),?\s+(\d{4})$", s, re.I)
    if m:
        month_name, day, year = m.groups()
        month_num = MONTH_MAP.get(month_name.lower())
        if month_num:
            return f"{int(year):04d}-{month_num:02d}-{int(day):02d}"
    return None


# ---------------------------------------------------------------------------
# DQ-003: Delete BPM phantom rows
# ---------------------------------------------------------------------------

def fix_dq003(conn: sqlite3.Connection, dry_run: bool) -> None:
    cur = conn.cursor()

    # Count phantom rows to delete
    cur.execute(
        """
        SELECT COUNT(*) FROM tariff_charges
        WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'
          AND (
            charge_label LIKE '%Electricity No.%'
            OR charge_label LIKE '%Effective November%'
            OR rate_value >= 0.02
          )
        """
    )
    phantom_count = cur.fetchone()[0]

    # Count surviving rows
    cur.execute(
        """
        SELECT COUNT(*) FROM tariff_charges
        WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'
          AND NOT (
            charge_label LIKE '%Electricity No.%'
            OR charge_label LIKE '%Effective November%'
            OR rate_value >= 0.02
          )
        """
    )
    surviving_count = cur.fetchone()[0]

    print(f"\n--- DQ-003: BPM Phantom Rows ---")
    print(f"  Phantom rows to DELETE: {phantom_count}")
    print(f"  Legitimate rows that survive: {surviving_count}")

    if dry_run:
        print("  [DRY RUN] No changes made.")
        return

    cur.execute(
        """
        DELETE FROM tariff_charges
        WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'
          AND (
            charge_label LIKE '%Electricity No.%'
            OR charge_label LIKE '%Effective November%'
            OR rate_value >= 0.02
          )
        """
    )
    deleted = cur.rowcount
    print(f"  Deleted {deleted} phantom rows.")

    # Verify
    cur.execute(
        "SELECT COUNT(*) FROM tariff_charges WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'"
    )
    remaining = cur.fetchone()[0]
    print(f"  Remaining rows in BPM family: {remaining}")

    bad_remaining = cur.execute(
        """
        SELECT COUNT(*) FROM tariff_charges
        WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'
          AND (
            charge_label LIKE '%Electricity No.%'
            OR charge_label LIKE '%Effective November%'
            OR rate_value >= 0.02
          )
        """
    ).fetchone()[0]
    if bad_remaining == 0:
        print("  [OK] All phantom rows deleted. Acceptance criteria met.")
    else:
        print(f"  [FAIL] WARNING: {bad_remaining} phantom rows remain!")


# ---------------------------------------------------------------------------
# DQ-005: Normalize non-ISO effective_start date strings
# ---------------------------------------------------------------------------

def fix_dq005(conn: sqlite3.Connection, dry_run: bool) -> None:
    cur = conn.cursor()

    # Get all non-ISO date rows
    cur.execute(
        """
        SELECT id, family_key, effective_start
        FROM tariff_versions
        WHERE effective_start IS NOT NULL
          AND effective_start NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
        ORDER BY family_key, effective_start
        """
    )
    rows = cur.fetchall()

    print(f"\n--- DQ-005: Non-ISO effective_start Normalization ---")
    print(f"  Total non-ISO rows: {len(rows)}")

    to_delete: list[int] = []                   # string-date version ids to DELETE (ISO dupe exists)
    iso_stubs_to_delete: list[int] = []         # ISO stub version ids to DELETE (string-date version has more charges)
    to_update: list[tuple[str, int]] = []       # (iso_date, version_id) to UPDATE
    seen_update_ids: set[int] = set()           # avoid duplicate update entries

    for vid, fkey, raw_start in rows:
        parsed = parse_fuzzy_date(raw_start)
        if not parsed:
            print(f"  [WARN] Could not parse {raw_start!r} for version {vid} ({fkey}) -- SKIPPING")
            continue

        # Check for existing ISO-format version for same family+date
        cur.execute(
            """
            SELECT id, effective_start FROM tariff_versions
            WHERE family_key = ?
              AND effective_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
              AND effective_start LIKE ?
              AND id != ?
            """,
            (fkey, parsed + "%", vid),
        )
        iso_dupes = cur.fetchall()

        if iso_dupes:
            # Has ISO duplicate — get charge counts to decide which to keep
            cur.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (vid,))
            string_charges = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (iso_dupes[0][0],))
            iso_charges = cur.fetchone()[0]

            if string_charges > iso_charges:
                # String-date version has more charges: normalize its date and delete the ISO stub
                print(
                    f"  [WARN->UPDATE+DELETE-STUB] id={vid} ({fkey}) has {string_charges} charges "
                    f"vs ISO stub id={iso_dupes[0][0]} with {iso_charges}. "
                    f"Updating string-date to ISO, deleting stub."
                )
                if vid not in seen_update_ids:
                    to_update.append((parsed, vid))
                    seen_update_ids.add(vid)
                # Mark the ISO stub for deletion too
                iso_stubs_to_delete.append(iso_dupes[0][0])
            else:
                # ISO version has equal or more charges: delete the string-date version
                to_delete.append(vid)
                print(
                    f"  DELETE id={vid} ({fkey} {raw_start!r}) -- "
                    f"ISO dupe id={iso_dupes[0][0]} has {iso_charges} charges, "
                    f"string version has {string_charges}"
                )
        else:
            if vid not in seen_update_ids:
                to_update.append((parsed, vid))
                seen_update_ids.add(vid)
            print(f"  UPDATE id={vid} ({fkey} {raw_start!r} -> {parsed})")

    print(f"\n  Plan: {len(to_delete)} string-date DELETEs, {len(iso_stubs_to_delete)} ISO stub DELETEs, {len(to_update)} UPDATEs")

    if dry_run:
        print("  [DRY RUN] No changes made.")
        return

    # Execute DELETEs (string-date versions replaced by ISO stubs): delete charges first, then version
    delete_charge_count = 0
    for vid in to_delete:
        cur.execute("DELETE FROM tariff_charges WHERE version_id = ?", (vid,))
        delete_charge_count += cur.rowcount
        cur.execute("DELETE FROM tariff_versions WHERE id = ?", (vid,))

    print(f"\n  Deleted {len(to_delete)} string-date versions (+ {delete_charge_count} orphaned charges)")

    # Execute UPDATES (string-date → ISO, keeping these versions as the canonical ones)
    for iso_date, vid in to_update:
        cur.execute(
            "UPDATE tariff_versions SET effective_start = ? WHERE id = ?",
            (iso_date, vid),
        )

    print(f"  Updated {len(to_update)} versions to ISO dates")

    # Delete ISO stubs made redundant by updates above (these had fewer charges)
    iso_stub_charge_count = 0
    for vid in iso_stubs_to_delete:
        cur.execute("DELETE FROM tariff_charges WHERE version_id = ?", (vid,))
        iso_stub_charge_count += cur.rowcount
        cur.execute("DELETE FROM tariff_versions WHERE id = ?", (vid,))

    if iso_stubs_to_delete:
        print(f"  Deleted {len(iso_stubs_to_delete)} ISO stub versions (+ {iso_stub_charge_count} charges)")

    # Verify: count remaining non-ISO rows
    cur.execute(
        """
        SELECT COUNT(*) FROM tariff_versions
        WHERE effective_start IS NOT NULL
          AND effective_start NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
        """
    )
    remaining = cur.fetchone()[0]
    if remaining == 0:
        print("  [OK] All effective_start values are now ISO-8601. Acceptance criteria met.")
    else:
        print(f"  [FAIL] WARNING: {remaining} non-ISO rows remain!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fix DQ-003 and DQ-005 data quality issues.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB changes")
    parser.add_argument("--execute", action="store_true", help="Apply changes to DB")
    parser.add_argument("--dq003-only", action="store_true", help="Only run DQ-003 fix")
    parser.add_argument("--dq005-only", action="store_true", help="Only run DQ-005 fix")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("ERROR: Must specify --dry-run or --execute")
        parser.print_help()
        return

    dry_run = args.dry_run

    conn = sqlite3.connect(DB_PATH)
    try:
        if not args.dq005_only:
            fix_dq003(conn, dry_run)
        if not args.dq003_only:
            fix_dq005(conn, dry_run)

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
