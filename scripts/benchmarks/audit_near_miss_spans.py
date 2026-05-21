"""
Move 2 audit — Classify span/boundary errors on near-miss profile docs.

Samples ~20 documents stratified across the top near-miss profiles surfaced by
``show-near-miss-profiles-nc``. For each, dumps:

  - the document's assigned span (start_page, end_page) if any
  - the latest ncuc_span_artifacts segmentation for the doc's source_pdf
  - the latest ncuc_page_artifacts page text (first 300 chars per page)
  - the parser profile that "almost worked" and the candidate score
  - flags suggesting which failure mode this is

Failure modes the script labels heuristically (and the human can re-classify):

  NO_SPAN              — historical_documents.start_page/end_page are NULL
  SPAN_WHOLE_DOC       — span covers entire doc; profile likely sees procedural+tariff mixed
  SPAN_OVER_SPLIT      — many small spans (>= ceil(page_count/3)), profile sees a fragment
  SPAN_LIKELY_OK       — span looks reasonable on size + leaf/schedule evidence
  OCR_BROKEN           — concatenated text is mostly garbage / very short
  UNCLEAR              — none of the above fired confidently

Standalone script, no DB writes. JSON + Markdown output under
docs/reports/nc_document_gap_audit/near_miss_span_audit/.

Usage:
    python scripts/benchmarks/audit_near_miss_spans.py \\
        --per-profile 4 --max-profiles 5 \\
        --out-dir docs/reports/nc_document_gap_audit/near_miss_span_audit/
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH_DEFAULT = REPO_ROOT / "data" / "db" / "duke_rates.db"


def _latest_problem_runs(conn: sqlite3.Connection) -> list[dict]:
    """Latest run per historical doc, filtered to empty/weak/missing on NC."""
    rows = conn.execute(
        """
        WITH latest_runs AS (
            SELECT r.*
            FROM historical_processing_runs r
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                GROUP BY historical_document_id
            ) latest ON latest.max_id = r.id
        )
        SELECT
            lr.id AS run_id,
            lr.historical_document_id AS hd_id,
            lr.parser_profile AS selected_profile,
            lr.outcome_quality,
            lr.charge_count,
            hd.local_path AS source_pdf,
            hd.family_key,
            hd.title,
            hd.company,
            hd.start_page,
            hd.end_page,
            hd.raw_text_path,
            json_extract(lr.metadata_json, '$.selection.top_candidates') AS top_candidates_json
        FROM latest_runs lr
        JOIN historical_documents hd ON hd.id = lr.historical_document_id
        WHERE hd.state = 'NC'
          AND lr.outcome_quality IN ('empty', 'weak', 'missing')
          AND json_extract(lr.metadata_json, '$.selection.top_candidates') IS NOT NULL
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _stratified_sample(
    runs: list[dict],
    per_profile: int,
    max_profiles: int,
) -> list[dict]:
    """For each top near-miss profile, take up to `per_profile` docs.

    Profiles are ranked by (empty+missing count) descending, matching
    show-near-miss-profiles-nc ordering. We label each run with its
    top-candidate profile to bucket it.
    """
    by_profile: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        try:
            cands = json.loads(r["top_candidates_json"]) if r["top_candidates_json"] else []
        except Exception:
            cands = []
        if not cands:
            continue
        top = cands[0]
        score = float(top.get("score") or 0.0)
        if score <= 0:
            continue  # no-near-miss family — different bucket entirely
        name = str(top.get("name") or "unknown")
        outcome = r["outcome_quality"]
        # Match ranking metric: weight empty/missing over weak
        r["_top_score"] = score
        r["_top_profile"] = name
        r["_outcome_weight"] = 2 if outcome in ("empty", "missing") else 1
        by_profile[name].append(r)

    # Rank profiles by empty+missing dominance, like show-near-miss-profiles-nc
    profile_rank = sorted(
        by_profile.keys(),
        key=lambda p: -sum(
            1 for r in by_profile[p] if r["outcome_quality"] in ("empty", "missing")
        ),
    )[:max_profiles]

    sample: list[dict] = []
    for profile in profile_rank:
        bucket = sorted(
            by_profile[profile],
            key=lambda r: (-r["_outcome_weight"], -r["_top_score"]),
        )
        for r in bucket[:per_profile]:
            sample.append(r)
    return sample


def _load_pages(
    conn: sqlite3.Connection, source_pdf: str
) -> list[tuple[int, str, dict]]:
    """Return (page_number, text_content[:300], metadata_dict) using latest version."""
    row = conn.execute(
        "SELECT MAX(artifact_version) AS v FROM ncuc_page_artifacts WHERE source_pdf=?",
        (source_pdf,),
    ).fetchone()
    if not row or not row[0]:
        return []
    version = row[0]
    pages = conn.execute(
        """
        SELECT page_number, text_content, metadata_json
        FROM ncuc_page_artifacts
        WHERE source_pdf=? AND artifact_version=?
        ORDER BY page_number ASC
        """,
        (source_pdf, version),
    ).fetchall()
    out = []
    for p in pages:
        try:
            meta = json.loads(p["metadata_json"] or "{}")
        except Exception:
            meta = {}
        text = (p["text_content"] or "")
        out.append((p["page_number"], text, meta))
    return out


def _load_spans(
    conn: sqlite3.Connection, source_pdf: str
) -> list[dict]:
    row = conn.execute(
        "SELECT MAX(artifact_version) AS v FROM ncuc_span_artifacts WHERE source_pdf=?",
        (source_pdf,),
    ).fetchone()
    if not row or not row[0]:
        return []
    version = row[0]
    spans = conn.execute(
        """
        SELECT span_index, start_page, end_page, doc_type, confidence,
               extracted_leaf_nos_json, extracted_schedule_titles_json
        FROM ncuc_span_artifacts
        WHERE source_pdf=? AND artifact_version=?
        ORDER BY span_index ASC
        """,
        (source_pdf, version),
    ).fetchall()
    return [dict(s) for s in spans]


_GARBAGE_RATIO_RE = re.compile(r"[A-Za-z0-9 \t\n\r.,;:()/\-]")


def _ocr_health(pages: list[tuple[int, str, dict]]) -> dict:
    """Coarse heuristic: fraction of 'sensible' characters across all page text."""
    total_chars = 0
    sensible_chars = 0
    nonempty_pages = 0
    total_pages = len(pages)
    for _, text, _ in pages:
        if not text:
            continue
        nonempty_pages += 1
        total_chars += len(text)
        sensible_chars += len(_GARBAGE_RATIO_RE.findall(text))
    sensible_ratio = (sensible_chars / total_chars) if total_chars else 0.0
    avg_chars_per_page = (total_chars / nonempty_pages) if nonempty_pages else 0.0
    return {
        "total_pages": total_pages,
        "nonempty_pages": nonempty_pages,
        "total_chars": total_chars,
        "avg_chars_per_page": round(avg_chars_per_page, 1),
        "sensible_char_ratio": round(sensible_ratio, 3),
    }


def _classify_failure(
    run: dict,
    pages: list[tuple[int, str, dict]],
    spans: list[dict],
    ocr: dict,
) -> tuple[str, str]:
    """Return (failure_mode_label, rationale_string)."""
    page_count = len(pages)

    # OCR_BROKEN first — if text is too sparse or garbled, the other signals are unreliable
    if ocr["nonempty_pages"] == 0:
        return "OCR_BROKEN", "no nonempty page artifacts (text extraction failed entirely)"
    if ocr["avg_chars_per_page"] < 80:
        return "OCR_BROKEN", f"avg_chars_per_page={ocr['avg_chars_per_page']} (very thin text)"
    if ocr["sensible_char_ratio"] < 0.85 and ocr["total_chars"] > 200:
        return "OCR_BROKEN", f"sensible_char_ratio={ocr['sensible_char_ratio']} (garbled text)"

    # NO_SPAN — historical_documents has no assigned span
    if run["start_page"] is None or run["end_page"] is None:
        if not spans:
            return "NO_SPAN", "historical_documents has no start/end_page AND no ncuc_span_artifacts rows"
        return "NO_SPAN", (
            f"historical_documents start/end_page is NULL but "
            f"{len(spans)} ncuc_span_artifacts spans exist (not linked through)"
        )

    span_pages = run["end_page"] - run["start_page"] + 1
    coverage = span_pages / page_count if page_count else 0.0

    # SPAN_WHOLE_DOC — span is essentially the whole document
    if coverage >= 0.9 and page_count >= 5:
        return "SPAN_WHOLE_DOC", (
            f"assigned span {run['start_page']}-{run['end_page']} covers "
            f"{round(coverage*100)}% of {page_count} pages — profile sees procedural mix"
        )

    # SPAN_OVER_SPLIT — too many small spans relative to doc size
    if spans and len(spans) >= max(3, math.ceil(page_count / 3)):
        small_count = sum(1 for s in spans if (s["end_page"] - s["start_page"]) <= 1)
        if small_count >= max(2, len(spans) // 2):
            return "SPAN_OVER_SPLIT", (
                f"{len(spans)} spans on {page_count} pages, "
                f"{small_count} are 1-2 pages each — segmenter is fragmenting"
            )

    # Otherwise — span size looks reasonable
    if 0.05 <= coverage <= 0.7:
        return "SPAN_LIKELY_OK", (
            f"assigned span {run['start_page']}-{run['end_page']} ({span_pages} pages of {page_count}, "
            f"{round(coverage*100)}%) — boundary looks reasonable; failure is profile-side"
        )

    return "UNCLEAR", (
        f"span_pages={span_pages} page_count={page_count} spans={len(spans)} "
        f"coverage={round(coverage,2)}"
    )


def _audit_one(
    conn: sqlite3.Connection, run: dict
) -> dict:
    pages = _load_pages(conn, run["source_pdf"])
    spans = _load_spans(conn, run["source_pdf"])
    ocr = _ocr_health(pages)
    label, rationale = _classify_failure(run, pages, spans, ocr)

    # First 200 chars from each page (capped to 5 pages for the summary table)
    page_previews = []
    for pn, text, meta in pages[:8]:
        page_previews.append({
            "page": pn,
            "preview": (text or "")[:200].replace("\n", " ").replace("\t", " "),
            "extracted_leaf_nos": meta.get("extracted_leaf_nos", []),
            "extracted_schedule_codes": meta.get("extracted_schedule_codes", []),
        })

    return {
        "hd_id": run["hd_id"],
        "source_pdf": run["source_pdf"],
        "family_key": run["family_key"],
        "title": run["title"],
        "company": run["company"],
        "outcome": run["outcome_quality"],
        "charge_count": run["charge_count"],
        "selected_profile": run["selected_profile"],
        "near_miss_profile": run["_top_profile"],
        "near_miss_score": round(run["_top_score"], 3),
        "assigned_span": {"start_page": run["start_page"], "end_page": run["end_page"]},
        "page_count": len(pages),
        "ncuc_span_count": len(spans),
        "ncuc_spans_summary": [
            {
                "i": s["span_index"],
                "p": f"{s['start_page']}-{s['end_page']}",
                "type": s["doc_type"],
                "conf": round(s["confidence"] or 0.0, 2),
            }
            for s in spans
        ],
        "ocr_health": ocr,
        "failure_mode": label,
        "rationale": rationale,
        "page_previews": page_previews,
    }


def _write_markdown_summary(audits: list[dict], path: Path) -> None:
    from collections import Counter

    by_mode = Counter(a["failure_mode"] for a in audits)
    by_profile = Counter(a["near_miss_profile"] for a in audits)

    lines: list[str] = []
    lines.append("# Near-Miss Span Audit\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append(f"Sample size: {len(audits)} documents across {len(by_profile)} near-miss profiles.\n")
    lines.append("\n## Failure mode distribution\n")
    lines.append("| Mode | Count | Pct |")
    lines.append("|---|---:|---:|")
    for mode, n in by_mode.most_common():
        pct = round(100 * n / len(audits), 1) if audits else 0
        lines.append(f"| {mode} | {n} | {pct}% |")
    lines.append("\n## Sample distribution by profile\n")
    lines.append("| Profile | Sampled |")
    lines.append("|---|---:|")
    for p, n in by_profile.most_common():
        lines.append(f"| {p} | {n} |")

    lines.append("\n## Per-document audit\n")
    for a in audits:
        lines.append(f"### hd_id={a['hd_id']}  {Path(a['source_pdf']).name}\n")
        lines.append(f"- **near_miss_profile**: `{a['near_miss_profile']}` (score {a['near_miss_score']})")
        lines.append(f"- **outcome**: `{a['outcome']}` ({a['charge_count']} charges), selected profile `{a['selected_profile']}`")
        lines.append(f"- **failure_mode**: `{a['failure_mode']}` — {a['rationale']}")
        lines.append(f"- **assigned_span**: pages {a['assigned_span']['start_page']}–{a['assigned_span']['end_page']}; "
                     f"doc has {a['page_count']} pages and {a['ncuc_span_count']} ncuc_span_artifacts rows")
        if a["ncuc_spans_summary"]:
            spans_str = ", ".join(
                f"#{s['i']}({s['p']}, {s['type']}, conf={s['conf']})" for s in a["ncuc_spans_summary"][:8]
            )
            lines.append(f"- **spans**: {spans_str}")
        oh = a["ocr_health"]
        lines.append(f"- **ocr**: {oh['nonempty_pages']}/{oh['total_pages']} pages with text; "
                     f"avg {oh['avg_chars_per_page']} chars/page; sensible ratio {oh['sensible_char_ratio']}")
        if a["page_previews"]:
            lines.append("- **page previews** (first 200 chars per page):")
            for pp in a["page_previews"][:5]:
                lines.append(f"  - p{pp['page']}: `{pp['preview']}`")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument("--per-profile", type=int, default=4,
                        help="Max docs per near-miss profile")
    parser.add_argument("--max-profiles", type=int, default=5,
                        help="Number of top near-miss profiles to sample")
    parser.add_argument("--out-dir",
                        default=str(REPO_ROOT / "docs" / "reports" / "nc_document_gap_audit" / "near_miss_span_audit"))
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    runs = _latest_problem_runs(conn)
    print(f"Found {len(runs)} problem runs with top_candidates")

    sample = _stratified_sample(runs, args.per_profile, args.max_profiles)
    print(f"Sampling {len(sample)} docs across top {args.max_profiles} near-miss profiles, "
          f"up to {args.per_profile} per profile")
    print()

    audits: list[dict] = []
    for i, run in enumerate(sample, 1):
        print(f"[{i}/{len(sample)}] hd_id={run['hd_id']} profile={run['_top_profile']} "
              f"score={run['_top_score']:.2f}  {Path(run['source_pdf']).name[:60]}",
              flush=True)
        try:
            a = _audit_one(conn, run)
            audits.append(a)
            print(f"    -> {a['failure_mode']}: {a['rationale']}")
        except Exception as exc:
            print(f"    ERROR: {exc}", file=sys.stderr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{ts}_audit.json"
    md_path = out_dir / f"{ts}_audit.md"

    json_path.write_text(json.dumps({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "per_profile": args.per_profile,
        "max_profiles": args.max_profiles,
        "total_problem_runs": len(runs),
        "sample_size": len(audits),
        "audits": audits,
    }, indent=2, default=str))
    _write_markdown_summary(audits, md_path)

    print(f"\nJSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
