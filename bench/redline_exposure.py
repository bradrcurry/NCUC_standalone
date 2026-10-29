"""Read-only wrapper around the existing shared source audit; never migrates a DB.

Run with --db PATH --out PATH. This report creates no queue/catalog tables.
Use the existing evidence review queue on an explicit scratch copy for triage.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from duke_rates.historical.ncuc.ingestion_validation import VALIDATOR_VERSION, audit_version_sources
from duke_rates.parse.redline_detector import PAGE_EVIDENCE_VERSION


def run(database: Path) -> dict:
    started = time.perf_counter()
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        audit = audit_version_sources(conn)
    finally:
        conn.close()
    return {
        "read_only": True,
        "label_source": "machine:shared-source-integrity-audit",
        "human_verified": False,
        "validator": VALIDATOR_VERSION,
        "detector": PAGE_EVIDENCE_VERSION,
        "versions": audit,
        "seconds": time.perf_counter() - started,
        "summary": {
            "versions_with_charges": len(audit),
            "charges": sum(v["charges"] for v in audit),
            "status_counts": dict(Counter(v["status"] for v in audit)),
            "failed_check_counts": dict(Counter(check for v in audit for check in v["failed"])),
        },
        "limitations": [
            "Source exposure is not proof that a charge is incorrect or was used in a bill.",
            "Absence of detected markup does not establish regulatory approval.",
            "Utility-web source exceptions remain reported as not_applicable.",
            "No data repair, binding, promotion, status change, or queue decision is performed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report["summary"] | {"seconds": report["seconds"]}, indent=2))


if __name__ == "__main__":
    main()
