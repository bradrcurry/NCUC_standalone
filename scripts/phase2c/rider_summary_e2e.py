"""Phase 2C rehearsal: versioned rider-summary runs on a scratch copy.

Reparses one live rider-summary source PDF, records it as a candidate run,
compares it with the live blocks, promotes, verifies, rolls back and
verifies again. Refuses the production database.

    python scripts/phase2c/rider_summary_e2e.py --db <scratch.db> [--source-pdf <path>] --out <report.json>
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
from pathlib import Path

from duke_rates.config import get_settings
from duke_rates.db import provenance as pv
from duke_rates.db import rider_summary_provenance as rsp
from duke_rates.parse.rider_summary import parse_rider_summary_from_pdf


def _live(conn, docket_dir, source_pdf):
    blocks = [tuple(r) for r in conn.execute(
        "SELECT * FROM rider_summary_blocks WHERE docket_dir = ? AND source_pdf = ? ORDER BY id",
        (docket_dir, source_pdf))]
    items = [tuple(r) for r in conn.execute(
        "SELECT li.* FROM rider_line_items li JOIN rider_summary_blocks b ON b.id = li.block_id "
        "WHERE b.docket_dir = ? AND b.source_pdf = ? ORDER BY li.id", (docket_dir, source_pdf))]
    return blocks, items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--source-pdf")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if Path(args.db).resolve() == get_settings().database_path.resolve():
        raise SystemExit("refusing the production database")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if args.source_pdf:
        row = conn.execute("SELECT docket_dir, source_pdf FROM rider_summary_blocks WHERE source_pdf = ? LIMIT 1", (args.source_pdf,)).fetchone()
    else:
        row = next(
            r for r in conn.execute(
                "SELECT docket_dir, source_pdf, COUNT(*) n FROM rider_summary_blocks GROUP BY 1, 2 ORDER BY n DESC"
            ) if Path(r["source_pdf"]).exists()
        )
    docket_dir, source_pdf = row["docket_dir"], row["source_pdf"]
    before = _live(conn, docket_dir, source_pdf)
    record = dataclasses.asdict(parse_rider_summary_from_pdf(source_pdf))
    run_ids = rsp.record_rider_summary_runs(conn, [record], created_by="phase2c-e2e", parser_name="rider_summary:text")
    run_id = run_ids[0]
    run = conn.execute("SELECT * FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()
    sid = run["target_id"]
    report = {
        "source_pdf": source_pdf,
        "docket_dir": docket_dir,
        "run_id": run_id,
        "run_target_matches_live_docket": conn.execute(
            "SELECT docket_dir FROM rider_summary_sources WHERE id = ?", (sid,)).fetchone()[0] == docket_dir,
        "live_blocks": len(before[0]),
        "live_items": len(before[1]),
        "validation_status": run["validation_status"],
        "review_findings": pv.review_findings(conn, run_id),
        "read_model_unchanged_after_record": _live(conn, docket_dir, source_pdf) == before,
        "compare": pv.compare_run(conn, run_id)["summary"],
    }
    promoted = pv.promote(conn, run_id, promoted_by="phase2c-e2e", reason="scratch rehearsal", acknowledge_review=True)
    report["promote"] = {k: promoted[k] for k in ("deleted_blocks", "deleted_items", "inserted_blocks", "inserted_items")}
    report["verify_after_promote"] = pv.verify_materialization(conn)["mismatches"]
    report["revisions_logged_by_promote"] = conn.execute(
        "SELECT COUNT(*) FROM rider_summary_revisions WHERE operation = 'delete'").fetchone()[0]
    pv.rollback(conn, "rider_summary_source", sid, by="phase2c-e2e", reason="scratch rehearsal")
    report["read_model_restored_after_rollback"] = _live(conn, docket_dir, source_pdf) == before
    report["verify_after_rollback"] = pv.verify_materialization(conn)["mismatches"]
    report["review_state"] = pv.run_review_status(conn, run_id)
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
