"""Reproduce D6 local research measurements against explicit scratch snapshots.

The baseline is read-only. --apply installs additive research schema and runs the
existing clue/lineage resolver on the target copy. No network or promotion path
is called. Existing original archives are only read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from duke_rates.db.provenance_schema import ensure_provenance_schema
from duke_rates.historical.ncuc import clue_lineage
from duke_rates.historical.ncuc import local_gap_resolution as local
from duke_rates.historical.ncuc import remote_discovery as remote

CANONICAL_TABLES = (
    "historical_documents",
    "tariff_versions",
    "tariff_families",
    "tariff_charges",
    "rider_summary_sources",
    "rider_summary_blocks",
    "rider_line_items",
)


def open_snapshot(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    path = path.resolve(strict=True)
    production = Path(__file__).resolve().parents[1] / "data/db/duke_rates.db"
    if writable and (
        path == production.resolve() or (production.exists() and path.samefile(production))
    ):
        raise ValueError("refusing to write production")
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True)
    connection.row_factory = sqlite3.Row
    if not writable:
        connection.execute("PRAGMA query_only=ON")
    return connection


def table_fingerprints(connection: sqlite3.Connection) -> dict:
    result = {}
    for table in CANONICAL_TABLES:
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            digest.update(json.dumps(tuple(row), default=str).encode())
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="Write research ledgers in the target copy."
    )
    args = parser.parse_args()
    if args.db.resolve() == args.baseline.resolve() or args.db.samefile(args.baseline):
        parser.error("target and preserved baseline must be separate snapshots")
    baseline = open_snapshot(args.baseline)
    connection = open_snapshot(args.db, writable=args.apply)
    try:
        before = table_fingerprints(connection)
        report = {
            "network_requests": 0,
            "pdf_downloads": 0,
            "machine_research_only": True,
            "timings": {},
            "canonical_before": before,
        }
        if args.apply:
            ensure_provenance_schema(connection)
            report["lineage"] = clue_lineage.rebuild_lineage(connection)
            for key in ("local_resolution", "unchanged_repeat"):
                started = time.perf_counter()
                report[key] = local.resolve_gaps_locally(
                    connection, created_by="D6 machine benchmark"
                )
                report["timings"][key] = time.perf_counter() - started
        base_uids = {r[0] for r in baseline.execute("SELECT candidate_uid FROM gap_candidates")}
        candidates = [
            dict(r) for r in connection.execute("SELECT * FROM gap_candidates ORDER BY id")
        ]
        report["new_local_candidates"] = [
            r for r in candidates if r["candidate_uid"] not in base_uids
        ]
        report["baseline_candidates"] = len(base_uids)
        report["current_candidates"] = len(candidates)
        report["remaining_gaps"] = local.resolution_summary(connection)
        started = time.perf_counter()
        report["network_plan"] = remote.plan_network_searches(connection)
        report["timings"]["network_planning"] = time.perf_counter() - started
        report["canonical_after"] = table_fingerprints(connection)
        report["canonical_preserved"] = before == report["canonical_after"]
        report["canonical_matches_baseline"] = (
            table_fingerprints(baseline) == report["canonical_after"]
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "current_candidates": len(candidates),
                    "new_candidates": len(report["new_local_candidates"]),
                    "canonical_preserved": report["canonical_preserved"],
                    "canonical_matches_baseline": report["canonical_matches_baseline"],
                    "timings": report["timings"],
                },
                indent=2,
            )
        )
    finally:
        baseline.close()
        connection.close()


if __name__ == "__main__":
    main()
