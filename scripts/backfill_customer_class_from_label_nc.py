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

# Rate-schedule families whose customer_class is determined by family_key alone.
# Used as a fallback when charge_label has no class info (e.g. "Energy Charge", "Basic Customer Charge").
FAMILY_CLASS = {
    # Progress NC residential
    "nc-progress-leaf-500": "residential", "nc-progress-leaf-501": "residential",
    "nc-progress-leaf-502": "residential", "nc-progress-leaf-503": "residential",
    "nc-progress-leaf-504": "residential",
    # Progress NC commercial
    "nc-progress-leaf-520": "commercial_small", "nc-progress-leaf-521": "commercial_small",
    "nc-progress-leaf-522": "commercial_small", "nc-progress-leaf-523": "commercial_small",
    "nc-progress-leaf-524": "commercial_small", "nc-progress-leaf-525": "commercial_small",
    "nc-progress-leaf-526": "commercial_small", "nc-progress-leaf-527": "commercial_small",
    "nc-progress-leaf-528": "commercial_small", "nc-progress-leaf-529": "commercial_small",
    "nc-progress-leaf-530": "commercial_medium",
    "nc-progress-leaf-532": "commercial_large", "nc-progress-leaf-533": "commercial_large",
    "nc-progress-leaf-534": "commercial_large", "nc-progress-leaf-536": "commercial_large",
    "nc-progress-leaf-535": "hourly_pricing_large",
    # Progress NC lighting/traffic
    "nc-progress-leaf-570": "lighting", "nc-progress-leaf-571": "lighting",
    "nc-progress-leaf-572": "lighting", "nc-progress-leaf-573": "lighting",
    "nc-progress-leaf-575": "lighting",
    "nc-progress-leaf-574": "lighting_sports_field",
    "nc-progress-leaf-590": "traffic_signal", "nc-progress-leaf-591": "traffic_signal",
    "nc-progress-leaf-592": "traffic_signal",
    # DEC NC schedules
    "nc-carolinas-schedule-RS": "residential", "nc-carolinas-schedule-RT": "residential",
    "nc-carolinas-schedule-RE": "residential", "nc-carolinas-schedule-RETC": "residential",
    "nc-carolinas-schedule-RSTC": "residential",
    "nc-carolinas-schedule-SGS": "commercial_small", "nc-carolinas-schedule-SGSTC": "commercial_small",
    "nc-carolinas-schedule-LGS": "commercial_large",
    "nc-carolinas-schedule-I": "industrial", "nc-carolinas-schedule-OPT-I": "industrial",
    "nc-carolinas-schedule-HP": "hourly_pricing_large",
    "nc-carolinas-schedule-OL": "lighting", "nc-carolinas-schedule-NL": "lighting",
    "nc-carolinas-schedule-FL": "lighting", "nc-carolinas-schedule-GL": "lighting",
    "nc-carolinas-schedule-PL": "lighting", "nc-carolinas-schedule-S": "lighting",
    "nc-carolinas-schedule-PG": "lighting",
    "nc-carolinas-schedule-TS": "traffic_signal",
    "nc-carolinas-schedule-WC": "commercial", "nc-carolinas-schedule-BC": "commercial",
    "nc-carolinas-schedule-PP": "commercial", "nc-carolinas-schedule-PPBE": "commercial",
    "nc-carolinas-schedule-ES": "commercial", "nc-carolinas-schedule-HLF": "commercial",
    "nc-carolinas-schedule-OPT-E": "commercial", "nc-carolinas-schedule-OPT-G": "commercial",
    "nc-carolinas-schedule-OPT-H": "commercial", "nc-carolinas-schedule-OPTV": "commercial",
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

    # Pull all NC charges joined to versions (also get family_key for fallback)
    c.execute("""
        SELECT tc.id, tc.charge_label, tc.customer_class, tv.family_key
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        WHERE tv.family_key LIKE 'nc-%'
    """)
    rows = c.fetchall()
    print(f"NC charges scanned: {len(rows)}")

    backfill_plan = []   # (id, new_class)
    normalize_plan = []  # (id, new_class)
    distribution = Counter()

    for cid, label, cls, family_key in rows:
        if cls in NORMALIZE and NORMALIZE[cls] != cls:
            normalize_plan.append((cid, NORMALIZE[cls]))
        if cls is None or cls == "":
            inferred = classify(label)
            # Fall back to family-key-derived class for rate schedules without class hints in label
            if inferred is None and family_key in FAMILY_CLASS:
                inferred = FAMILY_CLASS[family_key]
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
