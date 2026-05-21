"""
Phase 1 — Embedding-model benchmark on page-boundary detection (soft labels).

Compares Ollama embedding models on how well per-page embedding cosine
similarity flags section boundaries that the deterministic segmenter
(`segment_document()`) currently produces. Treats deterministic span
boundaries from `ncuc_span_artifacts` as soft labels.

This is a pilot — small sample, no DB writes, no schema changes.

Usage:
    python scripts/benchmarks/benchmark_embedding_boundaries.py \
        --docs 20 \
        --models qwen3-embedding:0.6b,qwen3-embedding:4b,qwen3-embedding:8b,bge-m3:latest,snowflake-arctic-embed2:latest,nomic-embed-text:latest \
        --report docs/reports/embedding_benchmarks/<auto>.json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH_DEFAULT = REPO_ROOT / "data" / "db" / "duke_rates.db"
REPORT_DIR_DEFAULT = REPO_ROOT / "docs" / "reports" / "embedding_benchmarks"

DEFAULT_MODELS = [
    "qwen3-embedding:0.6b",
    "qwen3-embedding:4b",
    "qwen3-embedding:8b",
    "bge-m3:latest",
    "snowflake-arctic-embed2:latest",
    "nomic-embed-text:latest",
]


def _pick_docs(
    conn: sqlite3.Connection,
    n: int,
    min_pages: int = 4,
    max_pages: int = 50,
) -> list[dict]:
    """Stratified sample of PDFs with BOTH page artifacts AND >=2 span artifacts.

    Picks the LATEST artifact_version per (source_pdf, file_hash) on each side
    independently — page-miner and segmentation versions are not coupled.

    Filters: page_count in [min_pages, max_pages] and at least 2 spans. We cap
    page count to keep per-model wall time bounded; the 1000+ page compliance
    books are a different problem and would dominate the cost.

    Sampling: round-robin across small/medium/large buckets so the benchmark
    isn't dominated by either short single-leaf sheets or large compliance
    bundles.
    """
    rows = conn.execute(
        """
        WITH latest_pa AS (
            SELECT source_pdf, file_hash, MAX(artifact_version) AS artifact_version
            FROM ncuc_page_artifacts
            GROUP BY source_pdf, file_hash
        ),
        pa AS (
            SELECT a.source_pdf, a.file_hash, a.artifact_version,
                   COUNT(*) AS page_count
            FROM ncuc_page_artifacts a
            JOIN latest_pa lp
              ON lp.source_pdf = a.source_pdf
             AND lp.file_hash = a.file_hash
             AND lp.artifact_version = a.artifact_version
            WHERE a.text_content IS NOT NULL AND length(a.text_content) > 50
            GROUP BY a.source_pdf, a.file_hash, a.artifact_version
            HAVING COUNT(*) BETWEEN ? AND ?
        ),
        latest_sa AS (
            SELECT source_pdf, file_hash, MAX(artifact_version) AS artifact_version
            FROM ncuc_span_artifacts
            GROUP BY source_pdf, file_hash
        ),
        sa AS (
            SELECT a.source_pdf, a.file_hash, a.artifact_version,
                   COUNT(*) AS span_count
            FROM ncuc_span_artifacts a
            JOIN latest_sa ls
              ON ls.source_pdf = a.source_pdf
             AND ls.file_hash = a.file_hash
             AND ls.artifact_version = a.artifact_version
            GROUP BY a.source_pdf, a.file_hash, a.artifact_version
            HAVING COUNT(*) >= 2
        )
        SELECT pa.source_pdf, pa.file_hash,
               pa.artifact_version AS page_artifact_version,
               sa.artifact_version AS span_artifact_version,
               pa.page_count, sa.span_count,
               CASE
                 WHEN pa.page_count < 10 THEN 'S'
                 WHEN pa.page_count < 25 THEN 'M'
                 ELSE 'L'
               END AS bucket
        FROM pa
        JOIN sa
          ON sa.source_pdf = pa.source_pdf
         AND sa.file_hash  = pa.file_hash
        """,
        (min_pages, max_pages),
    ).fetchall()
    by_bucket: dict[str, list[dict]] = {"S": [], "M": [], "L": []}
    for r in rows:
        by_bucket[r["bucket"]].append(dict(r))
    # Deterministic order within a bucket: largest span_count first (most
    # boundaries per doc), then largest page_count.
    for b in by_bucket.values():
        b.sort(key=lambda d: (-d["span_count"], -d["page_count"]))
    # Round-robin across buckets until we hit n.
    out: list[dict] = []
    idx = {"S": 0, "M": 0, "L": 0}
    order = ["M", "L", "S"]  # prefer medium first (most signal/cost tradeoff)
    while len(out) < n:
        progressed = False
        for b in order:
            if idx[b] < len(by_bucket[b]):
                out.append(by_bucket[b][idx[b]])
                idx[b] += 1
                progressed = True
                if len(out) >= n:
                    break
        if not progressed:
            break
    return out


def _load_pages(conn: sqlite3.Connection, doc: dict) -> list[tuple[int, str]]:
    rows = conn.execute(
        """
        SELECT page_number, text_content
        FROM ncuc_page_artifacts
        WHERE source_pdf = ? AND file_hash = ? AND artifact_version = ?
        ORDER BY page_number ASC
        """,
        (doc["source_pdf"], doc["file_hash"], doc["page_artifact_version"]),
    ).fetchall()
    return [(r["page_number"], r["text_content"] or "") for r in rows]


def _load_span_boundaries(conn: sqlite3.Connection, doc: dict) -> set[int]:
    """Return the set of page numbers that START a new span (excluding page 1).

    A boundary between adjacent pages (p, p+1) exists iff p+1 is the start of
    some span. We never count page 1 as a boundary — there's no previous page.
    """
    rows = conn.execute(
        """
        SELECT start_page
        FROM ncuc_span_artifacts
        WHERE source_pdf = ? AND file_hash = ? AND artifact_version = ?
        ORDER BY start_page ASC
        """,
        (doc["source_pdf"], doc["file_hash"], doc["span_artifact_version"]),
    ).fetchall()
    return {r["start_page"] for r in rows if r["start_page"] > 1}


def _embed(host: str, model: str, text: str, timeout_s: float) -> list[float] | None:
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{host}/api/embeddings",
                json={"model": model, "prompt": text[:4000]},
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data.get("embedding")
            if vec and isinstance(vec, list):
                return [float(v) for v in vec]
    except Exception as exc:
        print(f"  embed error ({model}): {exc}", file=sys.stderr)
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _score_at_threshold(
    drops: list[tuple[int, float]],
    truth_boundary_pages: set[int],
    threshold: float,
) -> dict:
    """Predict a boundary at page p+1 when 1-cos(p, p+1) >= threshold."""
    tp = fp = fn = tn = 0
    for after_page, drop in drops:
        predicted = drop >= threshold
        actual = after_page in truth_boundary_pages
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "threshold": threshold,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
    }


def _best_threshold(
    drops: list[tuple[int, float]],
    truth_boundary_pages: set[int],
) -> dict:
    """Sweep thresholds to find best F1."""
    best = None
    candidates = sorted({round(d, 4) for _, d in drops}) or [0.5]
    for t in candidates:
        s = _score_at_threshold(drops, truth_boundary_pages, t)
        if best is None or s["f1"] > best["f1"]:
            best = s
    return best or {"threshold": 0.5, "f1": 0.0, "precision": 0.0, "recall": 0.0,
                    "tp": 0, "fp": 0, "fn": 0, "tn": 0}


def _bench_model(
    host: str,
    model: str,
    docs_with_pages: list[tuple[dict, list[tuple[int, str]], set[int]]],
    timeout_s: float,
) -> dict:
    """Run one model over the prepared doc/page/truth bundle. Compute drops + F1."""
    # Warm up: one untimed embed so model load time doesn't poison ms/page.
    warmup_ok = False
    t_warm = time.perf_counter()
    if _embed(host, model, "warmup", timeout_s) is not None:
        warmup_ok = True
    warmup_seconds = round(time.perf_counter() - t_warm, 1)
    if not warmup_ok:
        return {
            "model": model,
            "pages_embedded": 0,
            "embed_failures": 0,
            "ms_per_page": 0.0,
            "total_embed_seconds": 0.0,
            "warmup_seconds": warmup_seconds,
            "warmup_ok": False,
            "global_best": {"f1": 0.0, "precision": 0.0, "recall": 0.0,
                            "threshold": 0.0, "tp": 0, "fp": 0, "fn": 0, "tn": 0},
            "avg_per_doc_f1": 0.0,
            "per_doc_f1": [],
        }

    per_doc_drops: list[list[tuple[int, float]]] = []
    per_doc_truth: list[set[int]] = []
    pages_embedded = 0
    embed_failures = 0
    total_embed_time = 0.0

    for doc, pages, truth_boundaries in docs_with_pages:
        vecs: list[list[float] | None] = []
        for page_number, text in pages:
            if not text.strip():
                vecs.append(None)
                continue
            t0 = time.perf_counter()
            v = _embed(host, model, text, timeout_s)
            total_embed_time += time.perf_counter() - t0
            if v is None:
                embed_failures += 1
            else:
                pages_embedded += 1
            vecs.append(v)

        drops: list[tuple[int, float]] = []
        for i in range(len(pages) - 1):
            a = vecs[i]
            b = vecs[i + 1]
            after_page = pages[i + 1][0]
            if a is None or b is None:
                drops.append((after_page, 0.0))
            else:
                drops.append((after_page, 1.0 - _cosine(a, b)))
        per_doc_drops.append(drops)
        per_doc_truth.append(truth_boundaries)

    all_drops: list[tuple[int, float]] = []
    all_truth: set[int] = set()
    truth_pages_offset = 0
    for drops, truth in zip(per_doc_drops, per_doc_truth):
        offset_drops = [(after_page + truth_pages_offset * 10000, d) for after_page, d in drops]
        offset_truth = {p + truth_pages_offset * 10000 for p in truth}
        all_drops.extend(offset_drops)
        all_truth |= offset_truth
        truth_pages_offset += 1

    best_global = _best_threshold(all_drops, all_truth)

    per_doc_f1: list[float] = []
    for drops, truth in zip(per_doc_drops, per_doc_truth):
        s = _best_threshold(drops, truth)
        per_doc_f1.append(s["f1"])

    avg_doc_f1 = sum(per_doc_f1) / len(per_doc_f1) if per_doc_f1 else 0.0
    ms_per_page = (total_embed_time * 1000) / pages_embedded if pages_embedded else 0.0

    return {
        "model": model,
        "pages_embedded": pages_embedded,
        "embed_failures": embed_failures,
        "ms_per_page": round(ms_per_page, 1),
        "total_embed_seconds": round(total_embed_time, 1),
        "warmup_seconds": warmup_seconds,
        "warmup_ok": True,
        "global_best": best_global,
        "avg_per_doc_f1": round(avg_doc_f1, 3),
        "per_doc_f1": [round(f, 3) for f in per_doc_f1],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT), help="SQLite DB path")
    parser.add_argument("--docs", type=int, default=20, help="Number of docs to sample")
    parser.add_argument("--min-pages", type=int, default=4, help="Minimum pages per doc")
    parser.add_argument("--max-pages", type=int, default=50, help="Maximum pages per doc (skip giant compliance books)")
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated Ollama model names",
    )
    parser.add_argument("--host", default="http://localhost:11434", help="Ollama host")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument(
        "--report",
        default="",
        help="Output JSON path. Defaults to docs/reports/embedding_benchmarks/<ts>_boundaries_soft.json",
    )
    args = parser.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("ERROR: no models specified", file=sys.stderr)
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    docs = _pick_docs(conn, args.docs, min_pages=args.min_pages, max_pages=args.max_pages)
    if not docs:
        print("ERROR: no candidate docs found (need page_artifacts + span_artifacts)", file=sys.stderr)
        return 2

    print(f"Picked {len(docs)} docs:")
    for d in docs:
        print(f"  [{d['bucket']}] pages={d['page_count']:>3} spans={d['span_count']:>2}  {Path(d['source_pdf']).name}")

    docs_with_pages: list[tuple[dict, list[tuple[int, str]], set[int]]] = []
    total_pages = 0
    total_boundaries = 0
    for d in docs:
        pages = _load_pages(conn, d)
        truth = _load_span_boundaries(conn, d)
        docs_with_pages.append((d, pages, truth))
        total_pages += len(pages)
        total_boundaries += len(truth)

    print(f"\nTotal pages={total_pages}, soft-label boundaries={total_boundaries}\n")

    results: list[dict] = []
    for model in models:
        print(f"=== Benchmarking {model} ===")
        t0 = time.perf_counter()
        r = _bench_model(args.host, model, docs_with_pages, args.timeout_s)
        r["wall_seconds"] = round(time.perf_counter() - t0, 1)
        print(
            f"  pages={r['pages_embedded']} fails={r['embed_failures']} "
            f"ms/page={r['ms_per_page']} "
            f"global_F1={r['global_best']['f1']:.3f} "
            f"(P={r['global_best']['precision']:.3f} "
            f"R={r['global_best']['recall']:.3f} "
            f"thr={r['global_best']['threshold']:.3f}) "
            f"avg_doc_F1={r['avg_per_doc_f1']:.3f} "
            f"wall={r['wall_seconds']}s"
        )
        results.append(r)

    ranked = sorted(results, key=lambda r: r["global_best"]["f1"], reverse=True)

    print("\n=== Ranking (by global F1) ===")
    print(f"{'model':<40} {'F1':>6} {'P':>6} {'R':>6} {'thr':>6} {'ms/pg':>7} {'docF1':>6}")
    for r in ranked:
        gb = r["global_best"]
        print(
            f"{r['model']:<40} {gb['f1']:>6.3f} {gb['precision']:>6.3f} "
            f"{gb['recall']:>6.3f} {gb['threshold']:>6.3f} "
            f"{r['ms_per_page']:>7.1f} {r['avg_per_doc_f1']:>6.3f}"
        )

    report_path = Path(args.report) if args.report else (
        REPORT_DIR_DEFAULT
        / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_boundaries_soft.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": args.host,
        "doc_count": len(docs),
        "total_pages": total_pages,
        "total_soft_boundaries": total_boundaries,
        "docs": [
            {
                "source_pdf": d["source_pdf"],
                "file_hash": d["file_hash"],
                "page_artifact_version": d["page_artifact_version"],
                "span_artifact_version": d["span_artifact_version"],
                "page_count": d["page_count"],
                "span_count": d["span_count"],
            }
            for d in docs
        ],
        "results": results,
        "ranking": [r["model"] for r in ranked],
    }
    report_path.write_text(json.dumps(payload, indent=2))
    print(f"\nReport written: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
