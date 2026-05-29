"""Clean cross-attributed charges from nc-carolinas-schedule-NL.

Background:
  One DEC compliance bundle PDF (e-7-nodate-...compliance-filing-of-approved-tar.pdf)
  was registered as `nc-carolinas-schedule-NL` but contains tariff sheets for
  MANY schedules (Schedule IIP, General Service, Residential, etc.).  When
  extract-rates-nc processed the NL version of this bundle, the parser
  extracted ALL the content and tagged it with family_key='schedule-NL'
  AND version_id pointing to the NL version.

  General cross-attribution cleanup (fix_cross_attribution_general.py) misses
  these because tc.family_key matches v.family_key (both NL). But the
  charge_label content reveals the rows belong to other schedules.

Strategy:
  Identify legitimate NL content (outdoor lighting: mast-arm pole, luminaire,
  Monthly Services Payment for fixtures) and delete everything else from
  the NL family.

  We use a DEFINITELY_NOT_NL pattern list (inverse approach) because the
  cross-attributed content has clearer signals than the legit content has.

Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")

# Patterns that REVEAL a label is from another schedule (not NL).
# Order matters only for sample-output prioritization.
NOT_NL_PATTERNS = [
    re.compile(r"\bSchedule\s+IIP\b", re.I),
    re.compile(r"\bGeneral\s+Service\b", re.I),
    re.compile(r"\bResidential\s+(?:Service|Schedule)\b", re.I),
    re.compile(r"\bSchedule\s+LGS\b", re.I),
    re.compile(r"\bSchedule\s+RS\b", re.I),
    re.compile(r"\bSchedule\s+SGS\b", re.I),
    re.compile(r"\bSchedule\s+I\b(?!\w)", re.I),  # Schedule I (industrial), not II
    re.compile(r"\bIndustrial\s+(?:Service|Schedule)\b", re.I),
    re.compile(r"\bSchedule\s+OPT-[A-Z]\b", re.I),
    re.compile(r"\bSchedule\s+HP\b", re.I),
    re.compile(r"\bSchedule\s+RE\b", re.I),
    re.compile(r"\bSchedule\s+TS\b", re.I),
    re.compile(r"\bSchedule\s+BC\b", re.I),
    re.compile(r"\bSchedule\s+ES\b", re.I),
    re.compile(r"\bSchedule\s+FL\b", re.I),
    re.compile(r"\bSchedule\s+PG\b", re.I),
    re.compile(r"\bSchedule\s+PL\b", re.I),
    re.compile(r"\bSmall\s+General\s+Service\b", re.I),
    re.compile(r"\bLarge\s+General\s+Service\b", re.I),
    re.compile(r"\bOutdoor\s+Lighting\s+Schedule\b", re.I),
    re.compile(r"\bSports?\s+Field\s+Lighting\b", re.I),
    re.compile(r"\bTraffic\s+Signal\b", re.I),
    re.compile(r"\bWater\s+Heating\s+Schedule\b", re.I),
    re.compile(r"\bSeasonal\s+(?:and|or)\s+Intermittent\b", re.I),
    re.compile(r"\bHigh\s+Load\s+Factor\b", re.I),
    # OCR-mangled variants where "v" reads as "n" or similar
    re.compile(r"\bGeneral\s+Sen\s*ice\b", re.I),
    re.compile(r"\bResidential\s+Sen\s*ice\b", re.I),
    re.compile(r"\bIndustrial\s+Sen\s*ice\b", re.I),
]


def label_is_not_nl(label: str | None) -> str | None:
    """Return the matching pattern source if label is clearly from another schedule, else None."""
    if not label:
        return None
    for pat in NOT_NL_PATTERNS:
        if pat.search(label):
            return pat.pattern
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT id, charge_label, rate_value, rate_unit, version_id
             FROM tariff_charges
             WHERE family_key = 'nc-carolinas-schedule-NL'"""
    ).fetchall()
    print(f"Total nc-carolinas-schedule-NL charges: {len(rows)}")

    to_delete: list[tuple[int, str | None, float, str, int, str]] = []
    keep: list[tuple[int, str | None]] = []
    for r in rows:
        pat = label_is_not_nl(r["charge_label"])
        if pat:
            to_delete.append((r["id"], r["charge_label"], r["rate_value"], r["rate_unit"], r["version_id"], pat))
        else:
            keep.append((r["id"], r["charge_label"]))

    print(f"  DELETE candidates (not-NL content):  {len(to_delete)}")
    print(f"  KEEP (looks legit-NL):                {len(keep)}")

    pat_counts = Counter(t[5] for t in to_delete)
    print()
    print("Top matching patterns:")
    for pat, n in pat_counts.most_common(10):
        print(f"  {n:>5}  {pat}")

    print()
    print("Sample DELETE candidates (top abs value):")
    for r in sorted(to_delete, key=lambda x: -abs(x[2] or 0))[:6]:
        print(f"  ch={r[0]} val={r[2]!s:>10} {r[3]:<8s} | {(r[1] or '')[:75]}")

    print()
    print("Sample KEEP candidates (distinct labels):")
    seen: set[str] = set()
    for cid, lbl in keep:
        key = (lbl or "")[:40]
        if key in seen:
            continue
        seen.add(key)
        print(f"  ch={cid} | {(lbl or '')[:80]}")
        if len(seen) >= 8:
            break

    if not args.apply:
        print(f"\nDRY RUN: would delete {len(to_delete)} rows. --apply to write.")
        return 0

    cur = conn.cursor()
    cur.executemany("DELETE FROM tariff_charges WHERE id=?", [(r[0],) for r in to_delete])
    conn.commit()
    print(f"\nDeleted {len(to_delete)} cross-attributed schedule-NL rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
