"""
Phase 3 prep — Build a hand-labeling worksheet for the span-detection gold set.

Picks the documents from a Phase 1 boundary benchmark report where models
disagreed most (highest per-doc F1 variance across models). For each picked
doc, dumps a TSV worksheet with:

  page | first 200 chars of text | deterministic_boundary | leaf_no |
  schedule_codes | <one column per benchmarked model: predicted boundary>

The reviewer marks a `gold_boundary` column (Y/N) on each row to produce the
ground-truth set. A separate scoring step then re-runs the boundary metric
against the gold instead of the deterministic soft labels.

Usage:
    python scripts/benchmarks/build_span_gold_set.py \\
        --phase1-report docs/reports/embedding_benchmarks/<ts>_boundaries_soft.json \\
        --top-n 10 \\
        --out-dir docs/reports/embedding_benchmarks/gold_set_v1/
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH_DEFAULT = REPO_ROOT / "data" / "db" / "duke_rates.db"


def _per_doc_disagreement(report: dict) -> list[dict]:
    """Return list of {doc_index, source_pdf, page_count, span_count,
    f1_by_model, f1_variance, f1_range} sorted by f1_range descending."""
    docs = report["docs"]
    models = [r["model"] for r in report["results"]]
    per_model_f1 = [r["per_doc_f1"] for r in report["results"]]
    rows: list[dict] = []
    for i, d in enumerate(docs):
        f1_by_model = {m: per_model_f1[mi][i] for mi, m in enumerate(models)}
        vals = list(f1_by_model.values())
        rows.append({
            "doc_index": i,
            "source_pdf": d["source_pdf"],
            "page_artifact_version": d["page_artifact_version"],
            "span_artifact_version": d["span_artifact_version"],
            "page_count": d["page_count"],
            "span_count": d["span_count"],
            "f1_by_model": f1_by_model,
            "f1_min": min(vals),
            "f1_max": max(vals),
            "f1_range": max(vals) - min(vals),
            "f1_stdev": statistics.pstdev(vals),
        })
    rows.sort(key=lambda r: (r["f1_range"], r["f1_stdev"]), reverse=True)
    # Dedupe by source_pdf — Phase 1 picked the same PDF multiple times across
    # different (file_hash, artifact_version) re-imports. Keep the first
    # (highest-disagreement) row per source_pdf.
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        key = Path(r["source_pdf"]).name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def _load_pages(conn: sqlite3.Connection, doc: dict) -> list[dict]:
    rows = conn.execute(
        """
        SELECT page_number, text_content, metadata_json
        FROM ncuc_page_artifacts
        WHERE source_pdf = ? AND artifact_version = ?
        ORDER BY page_number ASC
        """,
        (doc["source_pdf"], doc["page_artifact_version"]),
    ).fetchall()
    out = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except Exception:
            pass
        text = (r["text_content"] or "").strip()
        out.append({
            "page_number": r["page_number"],
            "preview": (text[:200].replace("\n", " ").replace("\t", " ")) if text else "",
            "leaf_nos": meta.get("extracted_leaf_nos", []),
            "schedule_codes": meta.get("extracted_schedule_codes", []),
            "has_leaf_header": meta.get("has_leaf_header", False),
            "has_schedule_heading": meta.get("has_schedule_heading", False),
        })
    return out


def _load_span_starts(conn: sqlite3.Connection, doc: dict) -> set[int]:
    rows = conn.execute(
        """
        SELECT start_page
        FROM ncuc_span_artifacts
        WHERE source_pdf = ? AND artifact_version = ?
        ORDER BY start_page ASC
        """,
        (doc["source_pdf"], doc["span_artifact_version"]),
    ).fetchall()
    return {r["start_page"] for r in rows if r["start_page"] > 1}


def _dump_worksheet(
    out_path: Path,
    doc: dict,
    pages: list[dict],
    boundary_pages: set[int],
) -> None:
    """Write one TSV worksheet per doc. Reviewer fills `gold_boundary` column."""
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow([
            "page", "preview_200ch", "deterministic_boundary", "gold_boundary",
            "leaf_nos", "schedule_codes", "has_leaf_header", "has_schedule_heading",
        ])
        for p in pages:
            det = "Y" if p["page_number"] in boundary_pages else ""
            w.writerow([
                p["page_number"],
                p["preview"],
                det,
                "",  # gold_boundary — reviewer fills with Y or N
                ",".join(p["leaf_nos"]),
                ",".join(p["schedule_codes"]),
                "Y" if p["has_leaf_header"] else "",
                "Y" if p["has_schedule_heading"] else "",
            ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase1-report", required=True, help="JSON from boundary benchmark")
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "docs" / "reports" / "embedding_benchmarks" / "gold_set_v1"),
    )
    args = parser.parse_args(argv)

    report = json.loads(Path(args.phase1_report).read_text())
    ranked = _per_doc_disagreement(report)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Top {args.top_n} disagreement docs (sorted by F1 range across models):")
    print(f"{'rank':>4} {'pages':>6} {'spans':>6} {'F1 range':>9} {'F1 stdev':>9}  filename")
    summary_rows = []
    for rank, r in enumerate(ranked[: args.top_n], 1):
        name = Path(r["source_pdf"]).name
        print(f"{rank:>4} {r['page_count']:>6} {r['span_count']:>6} "
              f"{r['f1_range']:>9.3f} {r['f1_stdev']:>9.3f}  {name}")
        pages = _load_pages(conn, r)
        boundary_pages = _load_span_starts(conn, r)
        out_path = out_dir / f"doc_{rank:02d}_{name[:50]}.tsv"
        _dump_worksheet(out_path, r, pages, boundary_pages)
        summary_rows.append({
            "rank": rank,
            "source_pdf": r["source_pdf"],
            "page_artifact_version": r["page_artifact_version"],
            "span_artifact_version": r["span_artifact_version"],
            "page_count": r["page_count"],
            "deterministic_boundary_count": len(boundary_pages),
            "f1_by_model": r["f1_by_model"],
            "f1_range": r["f1_range"],
            "f1_stdev": r["f1_stdev"],
            "worksheet_path": str(out_path.relative_to(REPO_ROOT)),
        })

    summary_path = out_dir / "INDEX.json"
    summary_path.write_text(json.dumps(summary_rows, indent=2))
    readme_path = out_dir / "README.md"
    readme_path.write_text(
        "# Span Detection Gold Set v1\n\n"
        "One TSV per document, picked by highest F1-range across embedding\n"
        "models on the Phase 1 soft-label benchmark.\n\n"
        "## How to label\n\n"
        "For each TSV, fill the `gold_boundary` column on every page row:\n\n"
        "- `Y` — page N starts a new tariff section (rate schedule, rider,\n"
        "  terms-and-conditions, procedural block, etc.) distinct from page N-1.\n"
        "- `N` — page N continues the same section as page N-1.\n"
        "- Page 1 is never a boundary by convention (leave blank or N).\n\n"
        "Compare against the `deterministic_boundary` column to see what the\n"
        "current regex segmenter thinks. They will disagree; that's why this\n"
        "doc was picked.\n\n"
        "## Useful signals already extracted\n\n"
        "- `leaf_nos`, `schedule_codes` — what the page miner pulled from\n"
        "  the page text. Strong but not authoritative — OCR drops these.\n"
        "- `has_leaf_header`, `has_schedule_heading` — page-miner regex hits.\n\n"
        "## After labeling\n\n"
        "Run `score_span_gold_set.py` (next step) to recompute boundary F1\n"
        "against the human labels instead of the deterministic spans.\n"
    )

    print(f"\nWrote {len(summary_rows)} worksheets to: {out_dir}")
    print(f"Index: {summary_path}")
    print(f"README: {readme_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
