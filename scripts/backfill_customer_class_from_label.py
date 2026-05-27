"""Backfill tariff_charges.customer_class by parsing it from charge_label.

The progress_billing_adjustments and similar parsers extract per-class rider
rate rows like:
    "Billing Adjustment - Residential"   value=0.01549 $/kWh
    "Billing Adjustment - Large General Service"  value=0.00954 $/kWh
    "Billing Adjustment - Lighting"  value=0.00214 $/kWh
... but never populate the `customer_class` column. The label carries the
class info; the column stays NULL.

This causes the tariff completeness audit's engine to over-count: when
`_sum_per_kwh_rate(rider_ver.id, 'residential')` filters `if c.customer_class
and c.customer_class not in ("all", customer_class): continue`, a NULL class
short-circuits the filter and ALL rows are summed. Result: engine claims ~5×
the true residential rider total, producing the "Leaf-600 mismatch" warning.

This script regex-extracts the class from the label and updates the column.

Patterns recognized:
    Residential                          -> residential
    Small General Service                -> commercial_small
    Medium General Service               -> commercial_medium
    Large General Service                -> commercial_large
    Industrial                           -> industrial
    Lighting / Outdoor Lighting          -> lighting

Idempotent. Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")

# Order matters: more specific patterns first.
CLASS_PATTERNS = [
    (re.compile(r"\bLarge\s+General\s+Service\b", re.I), "commercial_large"),
    (re.compile(r"\bMedium\s+General\s+Service\b", re.I), "commercial_medium"),
    (re.compile(r"\bSmall\s+General\s+Service\b", re.I), "commercial_small"),
    (re.compile(r"\b(?:Sports\s+Field|Traffic\s+Signal|Outdoor)\s+Lighting\b", re.I), "lighting"),
    (re.compile(r"\bTraffic\s+Signal\b", re.I), "traffic_signal"),
    (re.compile(r"\bLighting\b", re.I), "lighting"),
    (re.compile(r"\bResidential\b", re.I), "residential"),
    (re.compile(r"\bIndustrial\b", re.I), "industrial"),
    (re.compile(r"\bSeasonal\s+(?:and|or)\s+Intermittent\b", re.I), "seasonal_intermittent"),
]


def class_from_label(label: str | None) -> str | None:
    if not label:
        return None
    for pat, cls in CLASS_PATTERNS:
        if pat.search(label):
            return cls
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--family-prefix", default="nc-progress-leaf-",
                    help="Only operate on tariff_charges whose family_key starts with this (default: nc-progress-leaf-).")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT id, family_key, charge_label, customer_class
             FROM tariff_charges
             WHERE (customer_class IS NULL OR customer_class = '')
               AND family_key LIKE ? || '%'""",
        (args.family_prefix,),
    ).fetchall()
    print(f"Rows with NULL customer_class under {args.family_prefix}*: {len(rows)}")

    updates: list[tuple[int, str]] = []
    by_class: Counter[str] = Counter()
    no_match: int = 0
    for r in rows:
        cls = class_from_label(r["charge_label"])
        if cls:
            updates.append((r["id"], cls))
            by_class[cls] += 1
        else:
            no_match += 1

    print(f"  matched to a class: {len(updates)}")
    print(f"  no class in label:  {no_match}")
    print()
    print("Class distribution:")
    for cls, n in by_class.most_common():
        print(f"  {n:>5}  {cls}")

    if not args.apply:
        print(f"\nDRY RUN: would update {len(updates)} rows. --apply to write.")
        return 0

    cur = conn.cursor()
    cur.executemany(
        "UPDATE tariff_charges SET customer_class = ? WHERE id = ?",
        [(cls, rid) for rid, cls in updates],
    )
    conn.commit()
    print(f"\nUpdated {len(updates)} tariff_charges rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
