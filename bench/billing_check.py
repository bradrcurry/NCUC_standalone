"""Re-run the 12-bill DEP reconciliation against a scratch DB with a given code tree.

Writes per-bill estimated totals and deltas to JSON so two runs (baseline vs
repaired code, or original vs re-extracted scratch DB) can be diffed.  Refuses
the production DB; ``Repository`` runs migrations, so pass a scratch copy.

Usage::

    python bench/billing_check.py --src src --db <scratch.db> --label repaired \
        --out reports/phase0/billing_repaired.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRODUCTION_DB = (REPO / "data" / "db" / "duke_rates.db").resolve()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--usage", default=str(REPO / "data" / "usage" / "Energy Usage.xml"))
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--read-only",
        action="store_true",
        help="Use one read-only scratch snapshot connection; skip repeated migrations.",
    )
    args = ap.parse_args()
    if Path(args.db).resolve() == PRODUCTION_DB:
        raise SystemExit("refusing to run against the production database")
    sys.path.insert(0, str(Path(args.src).resolve()))

    from duke_rates.analytics.bill_accuracy import build_progress_nc_bill_accuracy_report
    from duke_rates.db.repository import Repository

    repository = Repository(Path(args.db))
    snapshot = None
    if args.read_only:
        snapshot = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
        snapshot.row_factory = sqlite3.Row
        snapshot.execute("PRAGMA query_only=ON")
        # The report only reads an already-migrated scratch snapshot. Reuse its
        # connection without changing any calculation or repository query.
        repository._connect = lambda: snapshot
    try:
        report = build_progress_nc_bill_accuracy_report(
            repository=repository,
            usage_xml_path=Path(args.usage),
        )
    finally:
        if snapshot is not None:
            snapshot.close()
    keep = (
        "bill_date",
        "rate_code",
        "bill_kwh",
        "actual_electric_total_fallback",
        "estimated_electric_total",
        "total_delta",
        "summary_rider_estimated",
        "summary_rider_delta",
        "status",
    )
    bills = [{k: row.get(k) for k in keep} for row in report.rows]
    Path(args.out).write_text(
        json.dumps(
            {
                "label": args.label,
                "read_only_snapshot": args.read_only,
                "summary": report.summary,
                "bills": bills,
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    print(json.dumps(report.summary, indent=1, default=str))


if __name__ == "__main__":
    main()
