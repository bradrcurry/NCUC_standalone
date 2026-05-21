"""
Audit silent classification conflicts in historical_documents.

Pattern surfaced from the REPS-vs-CPRE / REPS-vs-EDIT-4 fix on 2026-05-16:
the same PDF (same content_hash + same span) was ingested by two paths and
classified into two different family_keys, with the canonical-path title
contradicting the orphan-path title. The NCUC family-classifier picked
the wrong rider name in both cases.

This script finds all such pairs in the NC corpus so we can spot-check the
classifier's accuracy and produce a review queue.

Approach:
  1. Group historical_documents by (content_hash, start_page, end_page)
     where multiple rows exist
  2. For each group, compare family_key and title across rows
  3. Flag groups where title differs (case-insensitive, normalized) — those
     are silent classification disagreements worth human review
  4. Bucket: same-title (safe dedupes), different-title (real conflicts)

Pure read-only. Output: JSON + Markdown summary.

Usage:
    python scripts/benchmarks/audit_classification_conflicts.py
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


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
# Parenthesized provenance tags often appended by the NCUC ingest path.
# Strip them so the same doc with/without `(Span N-M)` or
# `(E-2 Sub NNNN Proposed Order, ...)` normalizes the same way.
_PAREN_PROVENANCE_RE = re.compile(r"\([^)]*\)")
_SPAN_TAG_RE = re.compile(r"span\s+\d+-\d+")
# `$aver` (Duke marketing) <-> `saver` — they describe the same program.
_SAVER_RE = re.compile(r"\$aver", re.IGNORECASE)


def _normalize_title(t: str) -> str:
    """Aggressive normalization: lowercase, strip parenthesized provenance,
    strip 'span N-M', collapse non-alphanum, equate `$aver` and `saver`.

    Examples:
      "REPS Rider (E-2 Sub 1109 Proposed Order, Span 1-6)" -> "repsrider"
      "REPS Rider"                                          -> "repsrider"
      "Residential Smart Saver Energy Efficiency..."        -> "residentialsmartsaver..."
      "Residential - Smart $aver Energy Efficiency..."      -> "residentialsmartsaver..."
    """
    if not t:
        return ""
    t = t.lower()
    t = _SAVER_RE.sub("saver", t)
    t = _PAREN_PROVENANCE_RE.sub("", t)
    t = _SPAN_TAG_RE.sub("", t)
    return _NORMALIZE_RE.sub("", t)


def _find_conflict_groups(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT content_hash, start_page, end_page,
               COUNT(*) AS n,
               GROUP_CONCAT(id) AS ids
        FROM historical_documents
        WHERE state = 'NC'
          AND content_hash IS NOT NULL
          AND start_page IS NOT NULL
          AND end_page IS NOT NULL
        GROUP BY content_hash, start_page, end_page
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        """
    ).fetchall()
    groups: list[dict] = []
    for r in rows:
        ids = [int(x) for x in r["ids"].split(",")]
        groups.append({
            "content_hash": r["content_hash"],
            "start_page": r["start_page"],
            "end_page": r["end_page"],
            "n": r["n"],
            "ids": ids,
        })
    return groups


def _enrich_group(conn: sqlite3.Connection, group: dict) -> dict:
    placeholders = ",".join("?" * len(group["ids"]))
    docs = conn.execute(
        f"""
        SELECT id, family_key, title, leaf_no, effective_start, archived_url, local_path
        FROM historical_documents WHERE id IN ({placeholders})
        ORDER BY id
        """,
        group["ids"],
    ).fetchall()
    titles_norm = {_normalize_title(d["title"] or "") for d in docs}
    families = {d["family_key"] for d in docs}
    # Run outcomes — what did each side land on
    docs_with_runs = []
    for d in docs:
        run = conn.execute(
            """
            SELECT parser_profile, outcome_quality, charge_count
            FROM historical_processing_runs WHERE historical_document_id=?
            ORDER BY id DESC LIMIT 1
            """, (d["id"],),
        ).fetchone()
        docs_with_runs.append({
            "id": d["id"],
            "family_key": d["family_key"],
            "title": d["title"],
            "leaf_no": d["leaf_no"],
            "effective_start": d["effective_start"],
            "archived_url": d["archived_url"],
            "latest_profile": run["parser_profile"] if run else None,
            "latest_outcome": run["outcome_quality"] if run else None,
            "latest_charges": run["charge_count"] if run else 0,
        })
    return {
        **group,
        "unique_families": sorted(families),
        "unique_normalized_titles": sorted(t for t in titles_norm if t),
        "titles_disagree": len({t for t in titles_norm if t}) > 1,
        "families_disagree": len(families) > 1,
        "docs": docs_with_runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "docs" / "reports" / "nc_document_gap_audit"
                    / "classification_conflicts"),
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    raw_groups = _find_conflict_groups(conn)
    print(f"Found {len(raw_groups)} groups with multiple historical_documents "
          f"sharing (content_hash, start_page, end_page)")

    enriched = [_enrich_group(conn, g) for g in raw_groups]

    title_conflicts = [g for g in enriched if g["titles_disagree"]]
    family_only_conflicts = [g for g in enriched
                              if g["families_disagree"] and not g["titles_disagree"]]
    safe_dupes = [g for g in enriched
                   if not g["families_disagree"] and not g["titles_disagree"]]

    print(f"\n  titles disagree (real conflicts): {len(title_conflicts)}")
    print(f"  family-only differences (same title, different family_key): "
          f"{len(family_only_conflicts)}")
    print(f"  identical title + family (true dupes): {len(safe_dupes)}")

    print(f"\nTitle-conflict groups (top 20 by group size):")
    for g in sorted(title_conflicts, key=lambda x: -x["n"])[:20]:
        print(f"  n={g['n']} hash={g['content_hash'][:12]} "
              f"p{g['start_page']}-{g['end_page']}")
        for d in g["docs"]:
            print(f"    hd_id={d['id']:>5} family={d['family_key']:<40} "
                  f"title={(d['title'] or '')[:60]}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{ts}_conflicts.json"
    md_path = out_dir / f"{ts}_conflicts.md"

    json_path.write_text(json.dumps({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "group_count": len(enriched),
        "title_conflict_count": len(title_conflicts),
        "family_only_conflict_count": len(family_only_conflicts),
        "safe_dupe_count": len(safe_dupes),
        "title_conflicts": title_conflicts,
        "family_only_conflicts": family_only_conflicts,
        "safe_dupes": safe_dupes,
    }, indent=2, default=str))

    lines: list[str] = []
    lines.append("# Classification Conflict Audit\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append(f"- Groups with shared (content_hash, span): {len(enriched)}")
    lines.append(f"- **Title-disagreement conflicts (real bugs)**: {len(title_conflicts)}")
    lines.append(f"- Family-only differences (same title, different family_key): {len(family_only_conflicts)}")
    lines.append(f"- Safe duplicates (identical title + family): {len(safe_dupes)}")
    lines.append("")
    lines.append("## Title conflicts\n")
    lines.append("These groups have the same PDF content + same span but their")
    lines.append("`historical_documents.title` describes different riders/schedules.")
    lines.append("Each represents a silent classification disagreement — one side is")
    lines.append("the manual-import path (filename-derived title), the other is the")
    lines.append("NCUC canonical-path title (family-classifier-derived). The")
    lines.append("classifier may have picked the wrong family.")
    lines.append("")
    for g in sorted(title_conflicts, key=lambda x: -x["n"]):
        lines.append(f"### {g['content_hash'][:12]} (p{g['start_page']}-{g['end_page']}, "
                     f"{g['n']} rows)\n")
        lines.append("| hd_id | family_key | title | latest_outcome | charges |")
        lines.append("|---:|---|---|---|---:|")
        for d in g["docs"]:
            lines.append(
                f"| {d['id']} | `{d['family_key']}` | {(d['title'] or '')[:80]} | "
                f"`{d.get('latest_profile') or 'NULL'}/{d.get('latest_outcome') or 'no_run'}` | "
                f"{d.get('latest_charges', 0)} |"
            )
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nReport JSON: {json_path}")
    print(f"Report MD:   {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
