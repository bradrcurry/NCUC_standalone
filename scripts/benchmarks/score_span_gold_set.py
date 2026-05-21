"""
Phase 3 scoring — Re-score each embedding model against hand-labeled gold
boundaries instead of the deterministic soft labels.

Reads the labeled TSV worksheets produced by build_span_gold_set.py (the
reviewer fills the `gold_boundary` column with Y/N) and recomputes per-model
F1 on the new labels. Compares to the deterministic-soft F1 from Phase 1.

This is the validation step: if a model that lost on soft labels gains on
gold labels, it's because the deterministic spans were the problem, not the
model.

Usage:
    python scripts/benchmarks/score_span_gold_set.py \\
        --gold-dir docs/reports/embedding_benchmarks/gold_set_v1/ \\
        --models qwen3-embedding:0.6b,qwen3-embedding:4b,qwen3-embedding:8b,bge-m3:latest,snowflake-arctic-embed2:latest,nomic-embed-text:latest \\
        --report docs/reports/embedding_benchmarks/<auto>.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH_DEFAULT = REPO_ROOT / "data" / "db" / "duke_rates.db"

DEFAULT_MODELS = [
    "qwen3-embedding:0.6b",
    "qwen3-embedding:4b",
    "qwen3-embedding:8b",
    "bge-m3:latest",
    "snowflake-arctic-embed2:latest",
    "nomic-embed-text:latest",
]


def _load_index(gold_dir: Path) -> list[dict]:
    path = gold_dir / "INDEX.json"
    if not path.exists():
        raise FileNotFoundError(f"INDEX.json not found at {path}")
    return json.loads(path.read_text())


def _load_worksheet(tsv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(tsv_path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for r in reader:
            rows.append(r)
    return rows


def _gold_boundary_pages(worksheet_rows: list[dict]) -> tuple[set[int], int]:
    """Return (set of page numbers labeled Y as gold boundary, count of labeled rows)."""
    boundary_pages: set[int] = set()
    labeled = 0
    for r in worksheet_rows:
        label = (r.get("gold_boundary") or "").strip().upper()
        if label in ("Y", "N"):
            labeled += 1
            if label == "Y":
                try:
                    p = int(r["page"])
                    if p > 1:  # by convention page 1 is never a boundary
                        boundary_pages.add(p)
                except ValueError:
                    continue
    return boundary_pages, labeled


def _load_pages_text(conn: sqlite3.Connection, source_pdf: str, version: str) -> list[tuple[int, str]]:
    rows = conn.execute(
        """
        SELECT page_number, text_content
        FROM ncuc_page_artifacts
        WHERE source_pdf = ? AND artifact_version = ?
        ORDER BY page_number ASC
        """,
        (source_pdf, version),
    ).fetchall()
    return [(r["page_number"], r["text_content"] or "") for r in rows]


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
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _score_drops(
    drops: list[tuple[int, float]],
    truth_pages: set[int],
    threshold: float,
) -> dict:
    tp = fp = fn = tn = 0
    for after_page, drop in drops:
        predicted = drop >= threshold
        actual = after_page in truth_pages
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


def _best_threshold(drops: list[tuple[int, float]], truth_pages: set[int]) -> dict:
    if not drops:
        return {"threshold": 0.0, "f1": 0.0, "precision": 0.0, "recall": 0.0,
                "tp": 0, "fp": 0, "fn": 0, "tn": 0}
    best = None
    for t in sorted({round(d, 4) for _, d in drops}):
        s = _score_drops(drops, truth_pages, t)
        if best is None or s["f1"] > best["f1"]:
            best = s
    return best or {"threshold": 0.0, "f1": 0.0, "precision": 0.0, "recall": 0.0,
                    "tp": 0, "fp": 0, "fn": 0, "tn": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-dir", required=True)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    gold_dir = Path(args.gold_dir)
    index = _load_index(gold_dir)

    # Load gold labels
    gold_per_doc: list[dict] = []
    skipped_unlabeled = 0
    for entry in index:
        tsv_path = REPO_ROOT / entry["worksheet_path"]
        if not tsv_path.exists():
            print(f"  skip (no worksheet): {tsv_path}", file=sys.stderr)
            continue
        rows = _load_worksheet(tsv_path)
        gold_pages, labeled = _gold_boundary_pages(rows)
        if labeled == 0:
            skipped_unlabeled += 1
            continue
        gold_per_doc.append({
            "source_pdf": entry["source_pdf"],
            "page_artifact_version": entry["page_artifact_version"],
            "page_count": entry["page_count"],
            "deterministic_boundary_count": entry["deterministic_boundary_count"],
            "gold_boundary_pages": sorted(gold_pages),
            "gold_boundary_count": len(gold_pages),
            "rows_labeled": labeled,
            "f1_by_model_soft": entry["f1_by_model"],
        })

    if not gold_per_doc:
        print("ERROR: no labeled worksheets found. Fill `gold_boundary` columns first.",
              file=sys.stderr)
        return 2

    print(f"Loaded {len(gold_per_doc)} labeled docs ({skipped_unlabeled} skipped unlabeled)")
    total_gold = sum(d["gold_boundary_count"] for d in gold_per_doc)
    total_det = sum(d["deterministic_boundary_count"] for d in gold_per_doc)
    print(f"  gold boundaries: {total_gold}   deterministic boundaries: {total_det}")
    print()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Load text for all gold docs once
    docs_text: list[tuple[dict, list[tuple[int, str]]]] = []
    for d in gold_per_doc:
        pages = _load_pages_text(conn, d["source_pdf"], d["page_artifact_version"])
        docs_text.append((d, pages))

    results: list[dict] = []
    for model in models:
        print(f"=== Re-scoring {model} on gold labels ===", flush=True)
        # warmup
        _embed(args.host, model, "warmup", args.timeout_s)
        per_doc_drops: list[list[tuple[int, float]]] = []
        per_doc_truth: list[set[int]] = []
        pages_embedded = 0
        embed_failures = 0
        total_embed_time = 0.0
        for d, pages in docs_text:
            vecs: list[list[float] | None] = []
            for page_number, text in pages:
                if not text.strip():
                    vecs.append(None)
                    continue
                t0 = time.perf_counter()
                v = _embed(args.host, model, text, args.timeout_s)
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
            per_doc_truth.append(set(d["gold_boundary_pages"]))

        all_drops: list[tuple[int, float]] = []
        all_truth: set[int] = set()
        for i, (drops, truth) in enumerate(zip(per_doc_drops, per_doc_truth)):
            offset = i * 100000
            all_drops.extend([(p + offset, d) for p, d in drops])
            all_truth |= {p + offset for p in truth}
        gold_best = _best_threshold(all_drops, all_truth)

        per_doc_f1: list[float] = []
        for drops, truth in zip(per_doc_drops, per_doc_truth):
            per_doc_f1.append(_best_threshold(drops, truth)["f1"])
        avg_doc_f1 = sum(per_doc_f1) / len(per_doc_f1) if per_doc_f1 else 0.0

        ms_per_page = (total_embed_time * 1000) / pages_embedded if pages_embedded else 0.0

        r = {
            "model": model,
            "pages_embedded": pages_embedded,
            "embed_failures": embed_failures,
            "ms_per_page": round(ms_per_page, 1),
            "gold_global_best": gold_best,
            "avg_per_doc_f1_gold": round(avg_doc_f1, 3),
            "per_doc_f1_gold": [round(f, 3) for f in per_doc_f1],
        }
        print(
            f"  pages={r['pages_embedded']} fails={r['embed_failures']} "
            f"ms/page={r['ms_per_page']} "
            f"gold_F1={r['gold_global_best']['f1']:.3f} "
            f"(P={r['gold_global_best']['precision']:.3f} "
            f"R={r['gold_global_best']['recall']:.3f} "
            f"thr={r['gold_global_best']['threshold']:.3f}) "
            f"avg_doc_F1={r['avg_per_doc_f1_gold']:.3f}",
            flush=True,
        )
        results.append(r)

    ranked = sorted(results, key=lambda r: r["gold_global_best"]["f1"], reverse=True)

    print("\n=== Ranking on GOLD labels (by global F1) ===")
    print(f"{'model':<40} {'gold_F1':>8} {'P':>6} {'R':>6} {'thr':>6} {'docF1':>6} {'ms/pg':>7}")
    for r in ranked:
        gb = r["gold_global_best"]
        print(
            f"{r['model']:<40} {gb['f1']:>8.3f} {gb['precision']:>6.3f} "
            f"{gb['recall']:>6.3f} {gb['threshold']:>6.3f} "
            f"{r['avg_per_doc_f1_gold']:>6.3f} {r['ms_per_page']:>7.1f}"
        )

    report_path = Path(args.report) if args.report else (
        REPO_ROOT / "docs" / "reports" / "embedding_benchmarks"
        / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_boundaries_gold.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": args.host,
        "gold_dir": str(Path(args.gold_dir).resolve()),
        "doc_count": len(gold_per_doc),
        "total_gold_boundaries": total_gold,
        "total_deterministic_boundaries": total_det,
        "docs": gold_per_doc,
        "results": results,
        "ranking_gold": [r["model"] for r in ranked],
    }
    report_path.write_text(json.dumps(payload, indent=2))
    print(f"\nReport written: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
