"""Backfill tariff_charges.customer_class from charge_label patterns (NC).

Many charge_labels embed the customer class textually
("JAA Rate - Residential", "Residential Service Schedules Total Rider Adjustments",
"Large General Service Schedules - JAA", ...), but the dedicated customer_class
column is NULL. The engine then can't filter properly, so rider sums for a given
schedule pick the wrong row.

This script pattern-matches charge_label and sets customer_class accordingly.
Idempotent: leaves non-NULL values untouched, normalizes existing
title-case values (e.g. 'Residential' -> 'residential', 'General Service' -> 'general_service').

Run with --apply to commit. Without it, prints a dry-run plan.
"""
import argparse
import re
import sqlite3
from collections import Counter

DB = "data/db/duke_rates.db"


# Order matters: more-specific patterns first.
PATTERNS = [
    (re.compile(r"sports\s*field\s*lighting", re.I), "lighting_sports_field"),
    (re.compile(r"traffic\s*signal", re.I), "traffic_signal"),
    (re.compile(r"outdoor\s*lighting|street\s*lighting", re.I), "lighting"),
    (re.compile(r"seasonal\s*(?:or|and|/)?\s*intermittent|intermittent\s*service", re.I), "seasonal_intermittent"),
    (re.compile(r"hourly\s*pricing|schedule\s*HP\b|LGS\s*-\s*RTP", re.I), "hourly_pricing_large"),
    (re.compile(r"large\s*general\s*service", re.I), "commercial_large"),
    (re.compile(r"\bLGS\b", re.I), "commercial_large"),
    (re.compile(r"medium\s*general\s*service", re.I), "commercial_medium"),
    (re.compile(r"\bMGS\b", re.I), "commercial_medium"),
    (re.compile(r"small\s*general\s*service", re.I), "commercial_small"),
    (re.compile(r"\bSGS\b", re.I), "commercial_small"),
    (re.compile(r"industrial\s*service|schedule\s*I\b", re.I), "industrial"),
    (re.compile(r"\bresidential\b", re.I), "residential"),
    (re.compile(r"\bcommercial\b", re.I), "commercial"),
    (re.compile(r"\bgeneral\s*service\b", re.I), "general_service"),
]

# Normalize existing values that are stored title-case / inconsistent.
NORMALIZE = {
    "Residential": "residential",
    "General Service": "general_service",
    "Lighting": "lighting",
    "Industrial": "industrial",
    "Commercial": "commercial",
    "all": "all",
}


def classify(label):
    if not label:
        return None
    for pat, cls in PATTERNS:
        if pat.search(label):
            return cls
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    c = db.cursor()

    # Pull all NC charges joined to versions
    c.execute("""
        SELECT tc.id, tc.charge_label, tc.customer_class
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        WHERE tv.family_key LIKE 'nc-%'
    """)
    rows = c.fetchall()
    print(f"NC charges scanned: {len(rows)}")

    backfill_plan = []   # (id, new_class)
    normalize_plan = []  # (id, new_class)
    distribution = Counter()

    for cid, label, cls in rows:
        if cls in NORMALIZE and NORMALIZE[cls] != cls:
            normalize_plan.append((cid, NORMALIZE[cls]))
        if cls is None or cls == "":
            inferred = classify(label)
            if inferred:
                backfill_plan.append((cid, inferred))
                distribution[inferred] += 1

    print(f"\nBackfill plan: {len(backfill_plan)} rows would gain a customer_class")
    for cls, n in distribution.most_common():
        print(f"  {cls}: {n}")
    print(f"\nNormalize plan: {len(normalize_plan)} rows have title-case values to lowercase")

    if not args.apply:
        print("\n--dry run-- use --apply to commit")
        return

    for cid, new_class in backfill_plan:
        c.execute("UPDATE tariff_charges SET customer_class=? WHERE id=?", (new_class, cid))
    for cid, new_class in normalize_plan:
        c.execute("UPDATE tariff_charges SET customer_class=? WHERE id=?", (new_class, cid))
    db.commit()
    print(f"\nApplied: backfilled {len(backfill_plan)}, normalized {len(normalize_plan)}")


if __name__ == "__main__":
    main()
