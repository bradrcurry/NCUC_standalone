"""Verify the hd 2275 / hd 7265 rebind rehearsal candidate runs (Phase 2E D1).

    python bench/clues/rebind_check.py --db <rebind scratch db> --out report.json

Checks each candidate run against its rebound span: evidence pages inside the span,
one family, cents/kWh units on cents/kWh rows, duplicate natural keys, printed TOTAL
reconciliation (components vs TOTAL row per rate class), and redline-only (struck) rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

SPANS = {6908: (7265, 96, 101), 6056: (2275, 100, 105)}
#: Rows printed struck-through on the hd 2275 redline (p.105 Traffic Signal "RAL-2 Rider -0.009" and the
#: RAL-2 rows of the other classes); read from the page images, see the D1 report §10.
STRUCK_LABEL_RE = re.compile(r"RAL-?2", re.I)


def check(conn: sqlite3.Connection, version_id: int) -> dict:
    hd, start, end = SPANS[version_id]
    run = conn.execute(
        "SELECT * FROM extraction_runs WHERE version_id = ? ORDER BY id DESC LIMIT 1", (version_id,)).fetchone()
    rows = conn.execute("SELECT * FROM extracted_charges WHERE run_id = ?", (run["id"],)).fetchall()
    pages = Counter(r["page_number"] for r in rows)
    outside = [r["charge_label"] for r in rows if r["page_number"] is not None and not start <= r["page_number"] <= end]
    families = Counter(r["family_key"] for r in rows)
    units = Counter(r["rate_unit"] for r in rows)
    cents_labelled_per_kw = [
        r["charge_label"] for r in rows
        if (r["rate_unit"] or "").lower() in ("$/kw", "dollars/kw") and "kW" not in (r["source_quote"] or "")
        and "dollars" not in (r["extractor_snippet"] or "").lower() and "demand" not in (r["charge_label"] or "").lower()
    ]
    keys = Counter(r["natural_key"] for r in rows)
    dupes = [k for k, n in keys.items() if n > 1]
    totals = _total_reconciliation(rows)
    struck = [dict(label=r["charge_label"], value=r["rate_value"], page=r["page_number"])
              for r in rows if STRUCK_LABEL_RE.search(r["charge_label"] or "")]
    return {
        "historical_document_id": hd,
        "version_id": version_id,
        "run_id": run["id"],
        "run_status": run["status"],
        "validation_status": run["validation_status"],
        "span": [start, end],
        "charge_count": len(rows),
        "charges_by_page": dict(sorted((str(k), v) for k, v in pages.items())),
        "charges_without_page": pages.get(None, 0),
        "evidence_outside_span": outside,
        "families": dict(families),
        "units": dict(units),
        "per_kw_rows_needing_review": cents_labelled_per_kw,
        "duplicate_natural_keys": dupes,
        "total_reconciliation": totals,
        "rows_printed_struck_through": struck,
    }


def _total_reconciliation(rows) -> dict:
    """Per rate class and unit: net components (BA net, not its sub-lines) must sum to the printed TOTAL."""
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        label = r["charge_label"] or ""
        cls = re.split(r" - | Total Rider Adjustments", label)[0]
        groups[(cls, r["rate_unit"])].append(r)
    checked = ok = 0
    failures = []
    for (cls, unit), items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        total = [i for i in items if i["charge_type"] == "adjustment_total"]
        parts = [i for i in items if i["charge_type"] == "adjustment"
                 and not re.search(r" - BA-(Fuel|EMF|DSM|EE)$", i["charge_label"] or "")]
        if len(total) != 1:
            continue
        checked += 1
        s = round(sum(i["rate_value"] or 0 for i in parts), 6)
        t = round(total[0]["rate_value"] or 0, 6)
        if abs(s - t) <= 0.0000151:
            ok += 1
        else:
            failures.append({"class": cls, "unit": unit, "sum_components": s, "printed_total": t,
                             "diff": round(s - t, 6)})
    return {"classes_checked": checked, "reconciled": ok, "failures": failures}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{a.db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    report = {str(v): check(conn, v) for v in SPANS}
    report["legacy_tariff_charges_after_rebind"] = dict(conn.execute(
        "SELECT version_id, COUNT(*) FROM tariff_charges WHERE version_id IN (6908, 6056) GROUP BY 1").fetchall())
    report["active_results"] = [dict(r) for r in conn.execute(
        "SELECT * FROM active_results WHERE target_id IN (6908, 6056)")]
    if a.out:
        a.out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
