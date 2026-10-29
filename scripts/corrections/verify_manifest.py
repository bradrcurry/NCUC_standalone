"""Re-verify every item of a corrections manifest against its source PDF and a DB.

Read-only.  For each correction it checks, and reports as JSON:

* the source PDF exists, with its SHA-256;
* each ``evidence.tokens`` string appears (whitespace-normalised) on the listed
  pages, and no ``evidence.absent_tokens`` string does;
* the ``evidence.effective_phrase`` is printed on those pages, and the target
  version's stored ``effective_start`` matches it;
* the stored "before" state still matches the manifest's precondition.

The DB is opened ``mode=ro``; production is acceptable here because nothing is
written.

Usage::

    python scripts/corrections/verify_manifest.py \
        --manifest scripts/corrections/2026-10-27_billing_integrity_corrections.json \
        --db data/db/duke_rates.db --out reports/phase1c/manifest_verification.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import fitz

REPO = Path(__file__).resolve().parents[2]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("¢", "c").replace("�", "c")).strip()


def _effective_iso(phrase: str | None) -> str | None:
    match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", phrase or "")
    return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat() if match else None


def _stored_state(conn: sqlite3.Connection, item: dict) -> dict:
    kind = item["kind"]
    if kind == "update_charges":
        rows = []
        for upd in item["updates"]:
            row = conn.execute(
                f"SELECT tc.{upd['column']}, tv.effective_start FROM tariff_charges tc"
                " JOIN tariff_versions tv ON tv.id = tc.version_id WHERE tc.id = ?",
                (upd["id"],),
            ).fetchone()
            rows.append({"id": upd["id"], "stored": row[0] if row else None, "before": upd["before"],
                         "version_effective_start": row[1] if row else None})
        return {"precondition_holds": all(r["stored"] == r["before"] for r in rows), "rows": rows,
                "version_effective_start": rows[0]["version_effective_start"] if rows else None}
    if kind == "insert_charges":
        count = conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (item["version_id"],)).fetchone()[0]
        start = conn.execute("SELECT effective_start FROM tariff_versions WHERE id = ?", (item["version_id"],)).fetchone()
        return {"precondition_holds": count == item["precondition_count"], "row_count": count,
                "expected_count": item["precondition_count"], "version_effective_start": start[0] if start else None}
    if kind == "parsed_json_remove":
        row = conn.execute("SELECT parsed_result_json, effective_start FROM historical_documents WHERE id = ?",
                           (item["row_id"],)).fetchone()
        data = json.loads(row[0]) if row and row[0] else {}
        hits = []
        for rm in item["remove"]:
            section, key = rm["path"].split(".")
            entries = data.get(section, {}).get(key, [])
            hits.append(sum(1 for e in entries if all(e.get(k) == v for k, v in rm["match"].items())))
        return {"precondition_holds": all(h == 1 for h in hits), "match_counts": hits,
                "version_effective_start": row[1] if row else None}
    if kind == "bill_statement_patch":
        row = conn.execute("SELECT statement_json FROM bill_statements WHERE id = ?", (item["row_id"],)).fetchone()
        electric = json.loads(row[0])["electric_section"] if row else {}
        labels = {li["label"] for li in electric.get("line_items", [])}
        missing = [li["label"] for li in item["append_line_items"] if li["label"] not in labels]
        return {"precondition_holds": electric.get("total_current_charges") == item["before_total"]
                and len(missing) == len(item["append_line_items"]),
                "stored_total": electric.get("total_current_charges"), "labels_missing": missing,
                "version_effective_start": None}
    return {"precondition_holds": False, "reason": f"unknown kind {kind}"}


def verify(manifest: dict, conn: sqlite3.Connection) -> list[dict]:
    results = []
    for item in manifest["corrections"]:
        evidence = item.get("evidence", {})
        pdf = REPO / item["source_pdf"]
        result: dict = {"id": item["id"], "source_pdf": item["source_pdf"], "pdf_exists": pdf.exists()}
        if pdf.exists():
            result["pdf_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
            doc = fitz.open(pdf)
            text = _norm(" ".join(doc[p - 1].get_text() for p in evidence.get("pdf_page", [])))
            result["tokens_found"] = {t: _norm(t) in text for t in evidence.get("tokens", [])}
            result["absent_tokens_absent"] = {t: _norm(t) not in text for t in evidence.get("absent_tokens", [])}
            phrase = evidence.get("effective_phrase")
            result["effective_phrase_found"] = None if phrase is None else _norm(phrase) in text
        state = _stored_state(conn, item)
        printed = _effective_iso(evidence.get("effective_phrase"))
        state["effective_date_matches"] = (
            None if printed is None else state.get("version_effective_start") == printed
        )
        result.update(state)
        result["verified"] = bool(
            result["pdf_exists"]
            and all(result.get("tokens_found", {}).values())
            and all(result.get("absent_tokens_absent", {}).values())
            and result.get("effective_phrase_found") is not False
            and state["effective_date_matches"] is not False
            and state["precondition_holds"]
        )
        results.append(result)
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    try:
        results = verify(manifest, conn)
    finally:
        conn.close()
    for r in results:
        print(f"{r['id']}: {'VERIFIED' if r['verified'] else 'NOT VERIFIED'}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"manifest_id": manifest["manifest_id"], "items": results}, indent=1) + "\n",
                                  encoding="utf-8")


if __name__ == "__main__":
    main()
