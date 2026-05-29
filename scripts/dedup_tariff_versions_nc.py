"""Dedupe NC tariff_versions: collapse rows with the same (family_key, normalized effective_start).

Strategy per dedupe group:
1. Normalize literal dates ("April 1, 2025") to ISO ("2025-04-01") on the canonical row.
2. Pick a canonical row: prefer (a) non-null historical_document_id, then (b) most charges,
   then (c) lowest id.
3. Re-point tariff_charges.version_id and llm_rate_charge_promotion_proposals.version_id
   from non-canonical rows to canonical.
4. Drop charge rows on canonical that are now duplicates of one another
   (same family_key, charge_label, rate_value, rate_unit, tier_min, tier_max, tou_period, season).
5. Delete the non-canonical version rows.

Idempotent; run with --apply to commit, otherwise prints plan only.
"""
import argparse
import re
import sqlite3
from collections import defaultdict

DB = "data/db/duke_rates.db"
MONTHS = {
    "January": "01", "February": "02", "March": "03", "April": "04",
    "May": "05", "June": "06", "July": "07", "August": "08",
    "September": "09", "October": "10", "November": "11", "December": "12",
}


def to_iso(es):
    if not es:
        return es
    if re.match(r"^\d{4}-\d{2}-\d{2}$", es):
        return es
    m = re.match(r"^(\w+)\s+(\d+),\s*(\d{4})$", es)
    if m and m.group(1) in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(1)]}-{int(m.group(2)):02d}"
    return es


def pick_canonical(rows):
    # rows: list of (id, has_doc, n_charges, effective_start)
    rows = sorted(rows, key=lambda r: (0 if r[1] else 1, -r[2], r[0]))
    return rows[0][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--family-prefix", default="nc-")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    c = db.cursor()

    c.execute(
        """SELECT id, family_key, effective_start, historical_document_id,
                  (SELECT COUNT(*) FROM tariff_charges WHERE version_id=tariff_versions.id) AS n_charges
           FROM tariff_versions
           WHERE family_key LIKE ? AND effective_start IS NOT NULL""",
        (args.family_prefix + "%",),
    )
    rows = c.fetchall()

    groups = defaultdict(list)
    literal_to_normalize = []  # (id, current_es, new_es) for rows we need to update
    for r in rows:
        iso = to_iso(r["effective_start"])
        if iso != r["effective_start"]:
            literal_to_normalize.append((r["id"], r["effective_start"], iso))
        groups[(r["family_key"], iso)].append((r["id"], r["historical_document_id"] is not None, r["n_charges"], r["effective_start"]))

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"Found {len(dup_groups)} duplicate groups across {sum(len(v)-1 for v in dup_groups.values())} redundant rows")
    print(f"Literal-date rows to normalize: {len(literal_to_normalize)}")

    if not args.apply:
        print("\n--dry run-- (use --apply to commit)")
        for (fk, iso), members in sorted(dup_groups.items())[:10]:
            canon = pick_canonical(members)
            others = [m[0] for m in members if m[0] != canon]
            print(f"  {fk:40s}  {iso}  canon={canon}  drop={others}")
        return

    moved_charges = 0
    moved_proposals = 0
    deleted_versions = 0
    deleted_dup_charges = 0
    normalized = 0

    # First: normalize literal-date rows that are NOT going to be dropped as dups
    # (rows in a dup group: only normalize the canonical; rows not in a dup group: normalize directly)
    rows_in_dup_groups = set()
    for members in dup_groups.values():
        for m in members:
            rows_in_dup_groups.add(m[0])

    for vid, cur, new in literal_to_normalize:
        if vid not in rows_in_dup_groups:
            c.execute("UPDATE tariff_versions SET effective_start=? WHERE id=?", (new, vid))
            normalized += 1

    # Process duplicate groups
    for (fk, iso), members in dup_groups.items():
        canon = pick_canonical(members)
        others = [m[0] for m in members if m[0] != canon]

        # Set canonical to ISO effective_start
        c.execute("UPDATE tariff_versions SET effective_start=? WHERE id=?", (iso, canon))

        # Move charges from others to canonical
        for oid in others:
            c.execute("UPDATE tariff_charges SET version_id=? WHERE version_id=?", (canon, oid))
            moved_charges += c.rowcount
            c.execute(
                "UPDATE llm_rate_charge_promotion_proposals SET version_id=? WHERE version_id=?",
                (canon, oid),
            )
            moved_proposals += c.rowcount

        # Delete the non-canonical version rows
        c.executemany("DELETE FROM tariff_versions WHERE id=?", [(oid,) for oid in others])
        deleted_versions += len(others)

        # Dedupe charge rows on canonical
        c.execute(
            """DELETE FROM tariff_charges
               WHERE id NOT IN (
                   SELECT MIN(id) FROM tariff_charges
                   WHERE version_id=?
                   GROUP BY COALESCE(charge_label,''), COALESCE(rate_value,-9999),
                            COALESCE(rate_unit,''), COALESCE(tier_min,-9999),
                            COALESCE(tier_max,-9999), COALESCE(tou_period,''),
                            COALESCE(season,''), COALESCE(customer_class,'')
               ) AND version_id=?""",
            (canon, canon),
        )
        deleted_dup_charges += c.rowcount

    db.commit()
    print(f"\nApplied:")
    print(f"  literal-date rows normalized: {normalized}")
    print(f"  charges re-pointed to canonical: {moved_charges}")
    print(f"  promotion-proposals re-pointed: {moved_proposals}")
    print(f"  duplicate version rows deleted: {deleted_versions}")
    print(f"  duplicate charge rows deleted on canonicals: {deleted_dup_charges}")


if __name__ == "__main__":
    main()
