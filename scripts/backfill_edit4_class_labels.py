"""Backfill customer_class on leaf-604 EDIT-4 charges by re-parsing the source.

ProgressSingleValueRiderProfile (which currently handles leaf-604) emits each
table row as an unlabeled "Rider Adjustment" charge with customer_class=NULL.
For EDIT-4 specifically, that table is a multi-class breakdown:

    Rate Class                        | Schedules                    | Rate (¢/kWh)
    Residential                       | RES, R-TOUD, R-TOU, R-TOU-CPP| (0.249)
    General Service (Small)           | SGS, SGS-TOUE, SGS-TOU-CPP   | (0.259)
    General Service (Constant Load)   | SGS-TOU-CLR                  | (0.256)
    General Service (Medium)          | MGS, MGS-TOU, GS-TES, ...    | (0.145)
    General Service (Large)           | LGS, LGS-TOU, ..., LGS-HLF   | (0.093)
    Traffic Signal Service            | TSS, TFS                     | (0.191)
    Outdoor Lighting Service          | (various)                    | (0.801)
    Sports Field Lighting Schedule    | SFLS                         | (0.304)

The engine sums all unlabeled rows when computing residential rider total,
over-counting EDIT-4 by ~2 c/kWh. After this backfill, only the row with
class=residential will pass the audit's filter.

Approach: re-parse the active EDIT-4 PDF source text to extract (class -> rate)
mappings, then UPDATE existing tariff_charges by joining on rate_value.

Idempotent. Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")

# Map EDIT-4's text class labels to our normalized customer_class values.
CLASS_MAP = [
    (re.compile(r"^\s*Residential\b", re.I | re.M), "residential"),
    (re.compile(r"^\s*General Service \(Small\)", re.I | re.M), "commercial_small"),
    (re.compile(r"^\s*General Service \(Constant\s*Load\)", re.I | re.M | re.S), "commercial_small"),
    (re.compile(r"^\s*General Service \(Medium\)", re.I | re.M), "commercial_medium"),
    (re.compile(r"^\s*General Service \(Large\)", re.I | re.M), "commercial_large"),
    (re.compile(r"^\s*Traffic Signal Service", re.I | re.M), "traffic_signal"),
    (re.compile(r"^\s*Sports Field Lighting", re.I | re.M), "lighting"),
    (re.compile(r"^\s*Outdoor Lighting(?: Service)?", re.I | re.M), "lighting"),
    (re.compile(r"^\s*Seasonal(?:\s+and\s+Intermittent)?", re.I | re.M), "seasonal_intermittent"),
]

# Match parenthetical-negative rate: "(0.249)" => -0.249
_RATE_RE = re.compile(r"\(([\d.]+)\)")


def parse_edit4_table(text: str) -> dict[float, str]:
    """Walk the MONTHLY RATE table linearly; return {rate_dollars_per_kwh: class}."""
    # Slice from MONTHLY RATE to end of section
    m = re.search(r"MONTHLY RATE(.*?)(?:APPLICABILITY|TERMS|\Z)", text, re.I | re.S)
    body = m.group(1) if m else text

    mapping: dict[float, str] = {}
    # Find each class label and its following rate value
    for class_re, cls in CLASS_MAP:
        cm = class_re.search(body)
        if not cm:
            continue
        # Search for the next parenthetical rate within ~250 chars after the class label
        window = body[cm.end(): cm.end() + 250]
        rm = _RATE_RE.search(window)
        if not rm:
            continue
        try:
            val = -float(rm.group(1)) / 100.0  # ¢/kWh → $/kWh, parenthetical negative
        except ValueError:
            continue
        mapping[round(val, 7)] = cls
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Get all EDIT-4 tariff_versions with their text
    rows = conn.execute(
        """SELECT tv.id AS vid, hd.id AS hd_id, hd.effective_start, hd.raw_text_path
             FROM tariff_versions tv
             JOIN historical_documents hd ON hd.id = tv.historical_document_id
             WHERE tv.family_key = 'nc-progress-leaf-604'
               AND hd.raw_text_path IS NOT NULL"""
    ).fetchall()
    print(f"leaf-604 EDIT-4 versions to process: {len(rows)}")

    total_updates = 0
    total_skipped = 0
    for r in rows:
        rtp = r["raw_text_path"]
        try:
            text = Path(rtp).read_text(encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            print(f"  vid={r['vid']} skipped (text file missing): {rtp}")
            continue
        mapping = parse_edit4_table(text)
        if not mapping:
            print(f"  vid={r['vid']} eff={r['effective_start']}: no table parsed")
            total_skipped += 1
            continue
        print(f"  vid={r['vid']} eff={r['effective_start']}: parsed {len(mapping)} class->rate entries")
        # Update charges matching by rounded rate_value
        charges = conn.execute(
            """SELECT id, rate_value, customer_class FROM tariff_charges
                 WHERE version_id = ? AND family_key = 'nc-progress-leaf-604'""",
            (r["vid"],),
        ).fetchall()
        for ch in charges:
            if ch["rate_value"] is None:
                continue
            key = round(ch["rate_value"], 7)
            cls = mapping.get(key)
            if cls and ch["customer_class"] != cls:
                if args.apply:
                    conn.execute(
                        "UPDATE tariff_charges SET customer_class = ? WHERE id = ?",
                        (cls, ch["id"]),
                    )
                print(f"    ch={ch['id']} val={ch['rate_value']} -> class={cls}")
                total_updates += 1

    if args.apply:
        conn.commit()
        print(f"\nApplied {total_updates} class updates ({total_skipped} versions skipped).")
    else:
        print(f"\nDRY RUN: would update {total_updates} rows. --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
