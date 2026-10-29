"""Apply or revert a source-verified corrections manifest against a SCRATCH database.

Every item carries a precondition (the stored "before" value, the current row count, or
the JSON entries to remove).  If a precondition does not hold the item is skipped and
reported, never forced.  Applying writes a receipt holding the exact prior state, and
--revert replays that receipt.  Inserted rows are tagged ``notes = 'correction:<id>'``.

Usage::

    python scripts/corrections/apply_corrections.py --db <scratch.db> \
        --manifest scripts/corrections/2026-10-27_billing_integrity_corrections.json \
        --receipt <receipt.json> [--dry-run]
    python scripts/corrections/apply_corrections.py --db <scratch.db> --receipt <receipt.json> --revert

Refuses the production database.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRODUCTION_DB = (REPO / "data" / "db" / "duke_rates.db").resolve()


def _matches(entry: dict, match: dict) -> bool:
    return all(entry.get(k) == v for k, v in match.items())


def _apply_item(conn: sqlite3.Connection, item: dict, manifest_id: str) -> dict:
    kind = item["kind"]
    if kind == "update_charges":
        prior = []
        for upd in item["updates"]:
            col = upd["column"]
            row = conn.execute(f"SELECT {col} FROM tariff_charges WHERE id = ?", (upd["id"],)).fetchone()
            if row is None or row[0] != upd["before"]:
                return {"status": "skipped", "reason": f"id {upd['id']} {col}={row[0] if row else None!r}, expected {upd['before']!r}"}
            prior.append({"id": upd["id"], "column": col, "value": row[0]})
        for upd in item["updates"]:
            conn.execute(f"UPDATE tariff_charges SET {upd['column']} = ? WHERE id = ?", (upd["after"], upd["id"]))
        return {"status": "applied", "prior": prior}

    if kind == "insert_charges":
        tag = f"correction:{manifest_id}:{item['id']}"
        if conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE notes = ?", (tag,)).fetchone()[0]:
            return {"status": "skipped", "reason": "already applied"}
        count = conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (item["version_id"],)).fetchone()[0]
        if count != item["precondition_count"]:
            return {"status": "skipped", "reason": f"version {item['version_id']} has {count} rows, expected {item['precondition_count']}"}
        now = datetime.now(UTC).isoformat()
        for r in item["rows"]:
            conn.execute(
                "INSERT INTO tariff_charges (version_id, family_key, charge_type, charge_label, rate_value, rate_unit,"
                " tou_period, season, customer_class, source_snippet, confidence_score, notes, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["version_id"], item["family_key"], r["charge_type"], r["charge_label"], r["rate_value"],
                 r["rate_unit"], r["tou_period"], r["season"], r["customer_class"], r["source_snippet"], 1.0, tag, now),
            )
        return {"status": "applied", "tag": tag}

    if kind == "parsed_json_remove":
        row = conn.execute("SELECT parsed_result_json FROM historical_documents WHERE id = ?", (item["row_id"],)).fetchone()
        if row is None or not row[0]:
            return {"status": "skipped", "reason": "no parsed_result_json"}
        original = row[0]
        data = json.loads(original)
        for rm in item["remove"]:
            section, key = rm["path"].split(".")
            entries = data[section][key]
            hits = [i for i, e in enumerate(entries) if _matches(e, rm["match"])]
            if len(hits) != 1:
                return {"status": "skipped", "reason": f"{rm['path']} match {rm['match']} found {len(hits)} entries, expected 1"}
            entries.pop(hits[0])
        conn.execute("UPDATE historical_documents SET parsed_result_json = ? WHERE id = ?", (json.dumps(data), item["row_id"]))
        return {"status": "applied", "prior_json": original}

    if kind == "bill_statement_patch":
        row = conn.execute("SELECT statement_json FROM bill_statements WHERE id = ?", (item["row_id"],)).fetchone()
        if row is None:
            return {"status": "skipped", "reason": "no bill statement"}
        original = row[0]
        data = json.loads(original)
        electric = data["electric_section"]
        labels = {li["label"] for li in electric["line_items"]}
        if electric.get("total_current_charges") != item["before_total"] or labels & {li["label"] for li in item["append_line_items"]}:
            return {"status": "skipped", "reason": f"total={electric.get('total_current_charges')!r} or lines already present"}
        electric["line_items"].extend(item["append_line_items"])
        electric["total_current_charges"] = item["after_total"]
        conn.execute("UPDATE bill_statements SET statement_json = ? WHERE id = ?", (json.dumps(data), item["row_id"]))
        return {"status": "applied", "prior_json": original}

    return {"status": "skipped", "reason": f"unknown kind {kind}"}


def _revert_item(conn: sqlite3.Connection, item: dict, result: dict) -> None:
    if result.get("status") != "applied":
        return
    if item["kind"] == "update_charges":
        for p in result["prior"]:
            conn.execute(f"UPDATE tariff_charges SET {p['column']} = ? WHERE id = ?", (p["value"], p["id"]))
    elif item["kind"] == "insert_charges":
        conn.execute("DELETE FROM tariff_charges WHERE notes = ?", (result["tag"],))
    elif item["kind"] == "parsed_json_remove":
        conn.execute("UPDATE historical_documents SET parsed_result_json = ? WHERE id = ?", (result["prior_json"], item["row_id"]))
    elif item["kind"] == "bill_statement_patch":
        conn.execute("UPDATE bill_statements SET statement_json = ? WHERE id = ?", (result["prior_json"], item["row_id"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--manifest")
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()
    if Path(args.db).resolve() == PRODUCTION_DB:
        raise SystemExit("refusing to run against the production database")

    conn = sqlite3.connect(args.db)
    try:
        if args.revert:
            receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
            for entry in reversed(receipt["items"]):
                _revert_item(conn, entry["item"], entry["result"])
                print(f"reverted {entry['item']['id']}: {entry['result'].get('status')}")
            conn.commit()
            return

        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        items = []
        for item in manifest["corrections"]:
            result = _apply_item(conn, item, manifest["manifest_id"])
            items.append({"item": item, "result": result})
            print(f"{item['id']}: {result['status']}" + (f" ({result['reason']})" if "reason" in result else ""))
        if args.dry_run:
            conn.rollback()
            print("dry run: rolled back")
            return
        conn.commit()
        Path(args.receipt).write_text(
            json.dumps({"manifest_id": manifest["manifest_id"], "db": str(Path(args.db).resolve()),
                        "applied_at": datetime.now(UTC).isoformat(), "items": items}, indent=1),
            encoding="utf-8",
        )
        print(f"receipt: {args.receipt}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
