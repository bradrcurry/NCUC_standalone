"""Set status on a historical_document (and its tariff_versions).

Companion to the projected-rates schema migration (status / requested_effective_date /
approved_document_id columns added to historical_documents and tariff_versions).

Usage:
    # Mark a single doc as proposed with a requested effective date
    python scripts/set_doc_status.py --hd-id 1234 --status proposed --requested-effective-date 2026-07-01

    # Mark all docs from a docket as proposed (e.g. an application's exhibits)
    python scripts/set_doc_status.py --docket "E-7 Sub 1329" --status proposed --requested-effective-date 2026-07-01

    # Link a proposed doc to the approved version once it lands
    python scripts/set_doc_status.py --hd-id 1234 --approved-document-id 5678

    # Show current status distribution
    python scripts/set_doc_status.py --report

Valid status values: approved | proposed | withdrawn | superseded

Idempotent. Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")
VALID_STATUSES = {"approved", "proposed", "withdrawn", "superseded"}


def _select_targets(conn: sqlite3.Connection, args: argparse.Namespace) -> list[int]:
    """Return list of historical_document.id to operate on."""
    if args.hd_id is not None:
        return [args.hd_id]
    if args.docket:
        rows = conn.execute(
            """SELECT hd.id FROM historical_documents hd
                 WHERE EXISTS (
                     SELECT 1 FROM ncuc_discovery_records dr
                     WHERE dr.local_path = hd.local_path AND dr.docket_number = ?
                 )""",
            (args.docket,),
        ).fetchall()
        return [r[0] for r in rows]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hd-id", type=int, help="Target a single historical_document by id")
    ap.add_argument("--docket", help="Target all docs from a specific NCUC docket")
    ap.add_argument("--status", choices=sorted(VALID_STATUSES), help="New status value")
    ap.add_argument("--requested-effective-date", help="Requested effective date for proposed docs (YYYY-MM-DD)")
    ap.add_argument("--approved-document-id", type=int, help="Lineage pointer to approved version (used when a proposed doc lands)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", action="store_true", help="Print status distribution and exit")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    if args.report:
        print("=== historical_documents.status distribution ===")
        for r in conn.execute("SELECT status, COUNT(*) FROM historical_documents GROUP BY status").fetchall():
            print(f"  {r[0]}: {r[1]:,}")
        print()
        print("=== tariff_versions.status distribution ===")
        for r in conn.execute("SELECT status, COUNT(*) FROM tariff_versions GROUP BY status").fetchall():
            print(f"  {r[0]}: {r[1]:,}")
        print()
        n_with_req = conn.execute("SELECT COUNT(*) FROM historical_documents WHERE requested_effective_date IS NOT NULL").fetchone()[0]
        n_linked = conn.execute("SELECT COUNT(*) FROM historical_documents WHERE approved_document_id IS NOT NULL").fetchone()[0]
        print(f"hd with requested_effective_date: {n_with_req}")
        print(f"hd with approved_document_id (lineage):   {n_linked}")
        return 0

    targets = _select_targets(conn, args)
    if not targets:
        print("No targets selected. Use --hd-id or --docket. Or --report to see current state.")
        return 0

    print(f"Targets: {len(targets)} historical_documents")
    for hd_id in targets[:8]:
        r = conn.execute(
            "SELECT id, family_key, status, effective_start, requested_effective_date FROM historical_documents WHERE id = ?",
            (hd_id,),
        ).fetchone()
        if r:
            print(f"  hd={r['id']} fam={r['family_key']} status={r['status']} eff={r['effective_start']} req={r['requested_effective_date']}")
    if len(targets) > 8:
        print(f"  ... and {len(targets) - 8} more")

    updates = []
    if args.status:
        updates.append(("status", args.status))
    if args.requested_effective_date:
        updates.append(("requested_effective_date", args.requested_effective_date))
    if args.approved_document_id is not None:
        updates.append(("approved_document_id", args.approved_document_id))

    if not updates:
        print("\nNothing to do. Specify --status, --requested-effective-date, or --approved-document-id.")
        return 0

    set_clause = ", ".join(f"{c} = ?" for c, _ in updates)
    params = [v for _, v in updates]

    if not args.apply:
        print(f"\nDRY RUN: would SET {set_clause} on {len(targets)} hd rows + mirror to their tariff_versions.")
        return 0

    cur = conn.cursor()
    for hd_id in targets:
        cur.execute(
            f"UPDATE historical_documents SET {set_clause} WHERE id = ?",
            (*params, hd_id),
        )
    # Mirror status + requested_effective_date to dependent tariff_versions
    tv_updates = [(c, v) for c, v in updates if c in ("status", "requested_effective_date")]
    if tv_updates:
        tv_set = ", ".join(f"{c} = ?" for c, _ in tv_updates)
        tv_params = [v for _, v in tv_updates]
        for hd_id in targets:
            cur.execute(
                f"UPDATE tariff_versions SET {tv_set} WHERE historical_document_id = ?",
                (*tv_params, hd_id),
            )
    # Mirror approved_document_id -> tariff_versions.approved_version_id only if a lineage exists
    if args.approved_document_id is not None:
        approved_vids = [
            r[0] for r in cur.execute(
                "SELECT id FROM tariff_versions WHERE historical_document_id = ?",
                (args.approved_document_id,),
            ).fetchall()
        ]
        if approved_vids:
            for hd_id in targets:
                cur.execute(
                    "UPDATE tariff_versions SET approved_version_id = ? WHERE historical_document_id = ?",
                    (approved_vids[0], hd_id),
                )
    conn.commit()
    print(f"\nUpdated {len(targets)} historical_documents (and their tariff_versions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
