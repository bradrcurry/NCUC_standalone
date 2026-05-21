"""
Backfill missing historical_documents.start_page/end_page from existing
ncuc_span_artifacts rows.

The NCUC archival ingest path computes spans and writes start/end_page to
historical_documents. The non-NCUC ingest paths (manual_import, website
crawler, direct-URL) do not — they leave start_page/end_page NULL even
when ncuc_span_artifacts has spans for the same source_pdf.

This script finds those orphans and links them to the best matching span.

Selection rule, in order:
  1. If ``hd.leaf_no`` is set and a span's extracted_leaf_nos contains it,
     prefer that span (with confidence as tiebreaker).
  2. Else if hd.family_key suggests a leaf number, same rule.
  3. Else if hd has rider/schedule codes in the family_key, match against
     extracted_schedule_titles.
  4. Else among tariff-type spans, pick the highest-confidence one; if all
     are 0-confidence, pick the largest span.
  5. If still nothing matched, pick the only span (if 1) or skip (if 0/many).

Dry-run by default. Writes ``--report-path`` JSON either way.

Usage:
    python scripts/maintenance/link_orphan_spans_nc.py \\
        --dry-run \\
        --report-path docs/reports/nc_document_gap_audit/span_linker_dry_run.json
    python scripts/maintenance/link_orphan_spans_nc.py --execute
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH_DEFAULT = REPO_ROOT / "data" / "db" / "duke_rates.db"


# Family keys come in several shapes — covered by these patterns:
#   nc-progress-leaf-602          -> leaf 602
#   ncuc-dep-602                   -> leaf 602 (no explicit -leaf- prefix)
#   ncuc-dep-721                   -> leaf 721
#   nc-progress-rider-RECOVERYRIDER -> rider RECOVERYRIDER
#   nc-carolinas-schedule-OPTV     -> schedule OPTV (no hyphen)
#   nc-carolinas-rider-EDIT4       -> rider EDIT4
_LEAF_FAMILY_RES = [
    re.compile(r"-leaf-(\d+)\b", re.IGNORECASE),
    re.compile(r"^ncuc-[a-z]+-(\d+)$", re.IGNORECASE),
]
_RIDER_FAMILY_RE = re.compile(r"-rider-([A-Z0-9]+)\b", re.IGNORECASE)
_SCHED_FAMILY_RE = re.compile(r"-schedule-([A-Z0-9]+)\b", re.IGNORECASE)


def _extract_leaf_from_family(family_key: str | None) -> str | None:
    if not family_key:
        return None
    for pat in _LEAF_FAMILY_RES:
        m = pat.search(family_key)
        if m:
            return m.group(1)
    return None


def _normalize_code(code: str) -> str:
    """Strip hyphens, spaces, and uppercase. OPT-V -> OPTV, EDIT-4 -> EDIT4."""
    return re.sub(r"[\s\-_]", "", code).upper()


def _extract_rider_from_family(family_key: str | None) -> str | None:
    if not family_key:
        return None
    m = _RIDER_FAMILY_RE.search(family_key)
    return _normalize_code(m.group(1)) if m else None


def _extract_schedule_from_family(family_key: str | None) -> str | None:
    if not family_key:
        return None
    m = _SCHED_FAMILY_RE.search(family_key)
    return _normalize_code(m.group(1)) if m else None


def _find_orphans(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT DISTINCT hd.id, hd.local_path, hd.family_key, hd.leaf_no, hd.title
        FROM historical_documents hd
        JOIN ncuc_span_artifacts sa ON sa.source_pdf = hd.local_path
        WHERE hd.state = 'NC' AND hd.start_page IS NULL
        ORDER BY hd.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _load_latest_spans(
    conn: sqlite3.Connection, source_pdf: str
) -> list[dict]:
    """Return spans from the latest artifact_version for the given source_pdf."""
    v_row = conn.execute(
        "SELECT MAX(artifact_version) AS v FROM ncuc_span_artifacts WHERE source_pdf=?",
        (source_pdf,),
    ).fetchone()
    if not v_row or not v_row[0]:
        return []
    version = v_row[0]
    rows = conn.execute(
        """
        SELECT span_index, start_page, end_page, doc_type, confidence,
               extracted_leaf_nos_json, extracted_schedule_titles_json
        FROM ncuc_span_artifacts
        WHERE source_pdf=? AND artifact_version=?
        ORDER BY span_index ASC
        """,
        (source_pdf, version),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            leaves = json.loads(r["extracted_leaf_nos_json"] or "[]")
        except Exception:
            leaves = []
        try:
            titles = json.loads(r["extracted_schedule_titles_json"] or "[]")
        except Exception:
            titles = []
        out.append({
            "span_index": r["span_index"],
            "start_page": r["start_page"],
            "end_page": r["end_page"],
            "doc_type": r["doc_type"],
            "confidence": float(r["confidence"] or 0.0),
            "leaves": [str(l).strip() for l in leaves if str(l).strip()],
            "titles_upper": [str(t).strip().upper() for t in titles if str(t).strip()],
            "version": version,
        })
    return out


def _pick_best_span(
    hd: dict, spans: list[dict]
) -> tuple[dict | None, str]:
    """Return (chosen_span, rule_name). chosen_span is None if no pick possible."""
    if not spans:
        return None, "no_spans"
    if len(spans) == 1:
        return spans[0], "single_span"

    # Compute target keys from hd
    target_leaf = (hd.get("leaf_no") or "").strip() or _extract_leaf_from_family(hd.get("family_key"))
    target_rider = _extract_rider_from_family(hd.get("family_key"))
    target_schedule = _extract_schedule_from_family(hd.get("family_key"))

    # Rule 1 — leaf number match
    if target_leaf:
        matches = [s for s in spans if target_leaf in s["leaves"]]
        if matches:
            matches.sort(key=lambda s: (s["doc_type"] != "tariff",
                                        -s["confidence"],
                                        -(s["end_page"] - s["start_page"])))
            return matches[0], "leaf_match"

    # Rule 2 — rider code anywhere in titles (normalized, hyphen-insensitive)
    if target_rider:
        matches = [
            s for s in spans
            if any(target_rider in _normalize_code(t) for t in s["titles_upper"])
        ]
        if matches:
            matches.sort(key=lambda s: (s["doc_type"] != "tariff",
                                        -s["confidence"],
                                        -(s["end_page"] - s["start_page"])))
            return matches[0], "rider_match"

    # Rule 3 — schedule code anywhere in titles (normalized, hyphen-insensitive)
    if target_schedule:
        matches = [
            s for s in spans
            if any(target_schedule in _normalize_code(t) for t in s["titles_upper"])
        ]
        if matches:
            matches.sort(key=lambda s: (s["doc_type"] != "tariff",
                                        -s["confidence"],
                                        -(s["end_page"] - s["start_page"])))
            return matches[0], "schedule_match"

    # Rule 4 — highest-confidence tariff span, BUT only when the document
    # looks like a single-section sheet (<=5 spans). Compliance books (many
    # spans, none of which match the target family) are too risky to guess —
    # we'd be pinning the wrong section and silently locking in a bad span.
    tariff_spans = [s for s in spans if s["doc_type"] == "tariff"]
    if tariff_spans and len(spans) <= 5:
        tariff_spans.sort(key=lambda s: (-s["confidence"], -(s["end_page"] - s["start_page"])))
        return tariff_spans[0], "best_tariff_span"

    if tariff_spans:
        return None, "compliance_book_no_match"

    # Rule 5 — give up; ambiguous
    return None, "ambiguous_no_match"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument("--dry-run", action="store_true",
                        help="Default. Print and report what would change.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually write back to historical_documents.")
    parser.add_argument("--report-path", default="",
                        help="JSON report path. Defaults to "
                             "docs/reports/nc_document_gap_audit/span_linker_<ts>.json")
    args = parser.parse_args(argv)

    if args.execute and args.dry_run:
        print("ERROR: --dry-run and --execute are mutually exclusive", file=sys.stderr)
        return 2

    dry_run = not args.execute

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    orphans = _find_orphans(conn)
    print(f"Found {len(orphans)} NC docs with NULL start_page that have spans available")
    if not orphans:
        return 0

    decisions: list[dict] = []
    rule_counts: Counter[str] = Counter()
    updates_planned = 0
    for hd in orphans:
        spans = _load_latest_spans(conn, hd["local_path"])
        chosen, rule = _pick_best_span(hd, spans)
        rule_counts[rule] += 1
        decision = {
            "hd_id": hd["id"],
            "family_key": hd["family_key"],
            "leaf_no": hd["leaf_no"],
            "source_pdf": hd["local_path"],
            "span_count_available": len(spans),
            "selection_rule": rule,
            "chosen_start_page": chosen["start_page"] if chosen else None,
            "chosen_end_page": chosen["end_page"] if chosen else None,
            "chosen_doc_type": chosen["doc_type"] if chosen else None,
            "chosen_confidence": chosen["confidence"] if chosen else None,
            "chosen_leaves": chosen["leaves"] if chosen else None,
            "chosen_titles": chosen["titles_upper"] if chosen else None,
        }
        decisions.append(decision)
        if chosen:
            updates_planned += 1

    print(f"\nSelection rule distribution:")
    for rule, n in rule_counts.most_common():
        pct = round(100 * n / len(orphans), 1) if orphans else 0
        print(f"  {rule:<24} {n:>4}  ({pct}%)")
    print(f"\nWould update: {updates_planned} of {len(orphans)} orphans")
    skipped = len(orphans) - updates_planned
    if skipped:
        print(f"Skipped (ambiguous_no_match): {skipped}")

    if args.execute:
        print("\nExecuting updates...")
        cursor = conn.cursor()
        applied = 0
        for d in decisions:
            if d["chosen_start_page"] is None:
                continue
            cursor.execute(
                "UPDATE historical_documents SET start_page=?, end_page=? "
                "WHERE id=? AND start_page IS NULL",
                (d["chosen_start_page"], d["chosen_end_page"], d["hd_id"]),
            )
            applied += cursor.rowcount
        conn.commit()
        print(f"Updated {applied} rows in historical_documents.")
    else:
        print("\nDRY RUN — no changes made. Re-run with --execute to apply.")

    out_dir = REPO_ROOT / "docs" / "reports" / "nc_document_gap_audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(args.report_path) if args.report_path else (
        out_dir / f"span_linker_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "execute" if args.execute else "dry_run",
        "orphan_count": len(orphans),
        "updates_planned": updates_planned,
        "rule_distribution": dict(rule_counts),
        "decisions": decisions,
    }, indent=2, default=str))
    print(f"\nReport: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
