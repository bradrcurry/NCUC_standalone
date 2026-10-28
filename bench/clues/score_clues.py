"""Score the clue ledger against the hand-verified sample (Phase 2E D1).

    python bench/clues/score_clues.py --db <scratch db with ledger> [--gold bench/clues/sample_50.yaml] [--out report.json]

Exit status 1 when the benchmark cannot verify its own source evidence:
a cited PDF is missing, its SHA-256 differs from the gold file, or the cited
page does not exist. A benchmark that cannot check its sources must not report
a score.

Scope: the regulatory clues a rate sheet prints in its header and footer:
revision, supersedes, effective_date, and the footer docket / order_date.
Body citations ("in Docket No. ...") are out of scope and excluded on both sides.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

KINDS = ("revision", "supersedes", "effective_date", "docket", "order_date")
FOOTER_ONLY = ("docket", "order_date")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _page_count(path: Path) -> int:
    import fitz

    with fitz.open(path) as doc:
        return doc.page_count


def _in_scope(row: sqlite3.Row) -> bool:
    if row["clue_kind"] not in KINDS or row["role"] == "index":
        return False
    return not (row["clue_kind"] in FOOTER_ONLY and row["role"] != "footer")


def _gold_key(clue: dict):
    return clue.get("value") if clue.get("value") is not None else ("AMBIGUOUS" if clue.get("ambiguous") else "BLANK")


def _ledger_key(row: sqlite3.Row):
    if row["normalized_value"] is not None:
        return row["normalized_value"]
    return "BLANK" if row["normalization"] == "blank" else "AMBIGUOUS"


def score(db: Path, gold_path: Path) -> dict:
    gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    evidence_failures: list[dict] = []
    tp: Counter = Counter()
    fp: Counter = Counter()
    fn: Counter = Counter()
    discrepancies: list[dict] = []
    attribution = Counter()
    normalization = defaultdict(Counter)
    identity_checks = Counter()
    by_layer = defaultdict(Counter)

    for doc in gold["documents"]:
        path = Path(doc["source_pdf"])
        if not path.is_file():
            evidence_failures.append({"sample": doc["sample"], "reason": "source PDF missing"})
            continue
        if _sha256(path) != doc["sha256"]:
            evidence_failures.append({"sample": doc["sample"], "reason": "SHA-256 differs from gold"})
            continue
        if not 1 <= doc["page"] <= _page_count(path):
            evidence_failures.append({"sample": doc["sample"], "reason": "page out of range"})
            continue
        rows = [r for r in conn.execute(
            "SELECT * FROM regulatory_clues WHERE file_sha256 = ? AND page = ?", (doc["sha256"], doc["page"]))
            if _in_scope(r)]
        scanned = conn.execute("SELECT text_layer FROM clue_scan_pages WHERE file_sha256 = ? AND page = ?",
                               (doc["sha256"], doc["page"])).fetchone()
        layer = scanned["text_layer"] if scanned else "not_scanned"
        gold_clues = [c for c in doc.get("clues", []) if c["kind"] in KINDS]
        remaining = list(rows)
        for g in gold_clues:
            key = _gold_key(g)
            hit = next((r for r in remaining if r["clue_kind"] == g["kind"] and _ledger_key(r) == key), None)
            if hit is None:
                fn[g["kind"]] += 1
                by_layer[layer]["fn"] += 1
                printed = next((r for r in remaining if r["clue_kind"] == g["kind"]), None)
                discrepancies.append({"sample": doc["sample"], "hd_id": doc["hd_id"], "type": "missed",
                                      "kind": g["kind"], "gold": g,
                                      "ledger_same_kind": (dict(printed) | {"normalized_json": None}) if printed else None})
                if printed is not None and " ".join(printed["printed_text"].split()) in " ".join(g["text"].split()):
                    normalization[g["kind"]]["wrong"] += 1
                continue
            remaining.remove(hit)
            tp[g["kind"]] += 1
            by_layer[layer]["tp"] += 1
            normalization[g["kind"]]["right"] += 1
            assoc = conn.execute(
                "SELECT 1 FROM clue_associations WHERE clue_id = ? AND historical_document_id = ?",
                (hit["id"], doc["hd_id"])).fetchone()
            attribution["attributed" if assoc else "not_attributed"] += 1
        for r in remaining:
            fp[r["clue_kind"]] += 1
            by_layer[layer]["fp"] += 1
            discrepancies.append({"sample": doc["sample"], "hd_id": doc["hd_id"], "type": "false_positive",
                                  "kind": r["clue_kind"], "printed_text": r["printed_text"],
                                  "normalized_value": r["normalized_value"], "normalization": r["normalization"]})
        if "sheet_of_family" in doc:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM tariff_versions WHERE historical_document_id = ?", (doc["hd_id"],))]
            identity = _identity(conn, ids)
            expected_single = bool(doc["sheet_of_family"])
            if expected_single and identity == "redline_only":
                # A redline header prints two revisions; withholding identity is the intended outcome.
                identity_checks["withheld_redline"] += 1
                continue
            identity_checks["correct" if (identity == "single") == expected_single else "wrong"] += 1
            if (identity == "single") != expected_single:
                discrepancies.append({"sample": doc["sample"], "hd_id": doc["hd_id"], "type": "family_identity",
                                      "sheet_of_family": doc["sheet_of_family"], "ledger_identity": identity})

    per_kind = {}
    for k in KINDS:
        p_den, r_den = tp[k] + fp[k], tp[k] + fn[k]
        per_kind[k] = {"tp": tp[k], "fp": fp[k], "fn": fn[k],
                       "precision": round(tp[k] / p_den, 4) if p_den else None,
                       "recall": round(tp[k] / r_den, 4) if r_den else None}
    links = _score_links(conn, gold.get("link_reviews", []))
    return {
        "documents": len(gold["documents"]),
        "evidence_failures": evidence_failures,
        "per_kind": per_kind,
        "page_attribution": dict(attribution),
        "normalization_accuracy": {k: dict(v) for k, v in normalization.items()},
        "by_text_layer": {k: dict(v) for k, v in by_layer.items()},
        "family_identity": dict(identity_checks),
        "links": links,
        "discrepancies": discrepancies,
    }


def _identity(conn: sqlite3.Connection, version_ids: list[int]) -> str:
    from duke_rates.historical.ncuc.clue_lineage import load_version_evidence

    if not version_ids:
        return "no_version"
    versions = load_version_evidence(conn)
    statuses = {versions[v].identity_status for v in version_ids if v in versions}
    return "single" if statuses == {"single"} else (",".join(sorted(statuses)) or "none")


def _score_links(conn: sqlite3.Connection, reviews: list[dict]) -> dict:
    tally = Counter()
    rows = []
    for review in reviews:
        link = conn.execute(
            "SELECT status, to_version_id FROM lineage_links WHERE from_version_id = ? AND to_version_id IS ?",
            (review["from_version_id"], review.get("to_version_id"))).fetchone()
        if link is None:
            tally["link_not_found"] += 1
            rows.append(review | {"found": False})
            continue
        tally[f"{link['status']}:{review['verdict']}"] += 1
        rows.append(review | {"found": True, "status": link["status"]})
    return {"tally": dict(tally), "reviews": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--gold", type=Path, default=Path(__file__).with_name("sample_50.yaml"))
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    report = score(a.db, a.gold)
    if a.out:
        a.out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    summary = {k: report[k] for k in ("documents", "evidence_failures", "per_kind", "page_attribution",
                                      "normalization_accuracy", "by_text_layer", "family_identity")}
    summary["links"] = report["links"]["tally"]
    summary["discrepancy_count"] = len(report["discrepancies"])
    print(json.dumps(summary, indent=1))
    sys.exit(1 if report["evidence_failures"] else 0)


if __name__ == "__main__":
    main()
