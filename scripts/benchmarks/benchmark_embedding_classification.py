"""
Phase 2 — Embedding-model benchmark on document-type KNN classification.

Compares Ollama embedding models on leave-one-out KNN accuracy against
rule-based labels stored in ``document_classifications``.

For each model:
  1. Embed every PDF that has a rule_utility_v1 label (full-text, capped)
  2. For each doc, find k nearest neighbors among the rest (cosine similarity)
  3. Predict label by weighted vote; compare to the rule label
  4. Report top-1 accuracy on rule_utility_v1 (5-class) and
     rule_tariff_family_v1 (long tail, restricted to families with >=3 docs)

This is a pilot — no DB writes. Standalone script. Promote later if useful.

Usage:
    python scripts/benchmarks/benchmark_embedding_classification.py \\
        --limit 250 \\
        --models qwen3-embedding:0.6b,qwen3-embedding:4b,qwen3-embedding:8b,bge-m3:latest,snowflake-arctic-embed2:latest,nomic-embed-text:latest
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

import httpx
from collections import Counter

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


def _load_labeled_docs(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Pick docs that have a rule_utility_v1 label and concatenated page text.

    Joins:
      - document_classifications (rule_utility_v1, rule_tariff_family_v1)
      - ncuc_page_artifacts (concatenated text, latest version per file)
    """
    rows = conn.execute(
        """
        WITH latest_pa AS (
            SELECT source_pdf, file_hash, MAX(artifact_version) AS artifact_version
            FROM ncuc_page_artifacts
            GROUP BY source_pdf, file_hash
        ),
        doc_text AS (
            SELECT a.source_pdf,
                   GROUP_CONCAT(a.text_content, ' ') AS full_text,
                   COUNT(*) AS page_count
            FROM ncuc_page_artifacts a
            JOIN latest_pa lp
              ON lp.source_pdf = a.source_pdf
             AND lp.file_hash  = a.file_hash
             AND lp.artifact_version = a.artifact_version
            WHERE a.text_content IS NOT NULL AND length(a.text_content) > 50
            GROUP BY a.source_pdf
        )
        SELECT hd.local_path AS source_pdf,
               dc_u.label AS utility,
               COALESCE(dc_f.label, '') AS family,
               dt.full_text,
               dt.page_count
        FROM document_classifications dc_u
        JOIN historical_documents hd
          ON hd.id = CAST(dc_u.subject_id AS INTEGER)
         AND dc_u.subject_kind = 'historical_document'
        JOIN doc_text dt ON dt.source_pdf = hd.local_path
        LEFT JOIN document_classifications dc_f
          ON dc_f.subject_kind = 'historical_document'
         AND dc_f.subject_id = dc_u.subject_id
         AND dc_f.classifier = 'rule_tariff_family_v1'
        WHERE dc_u.classifier = 'rule_utility_v1'
        ORDER BY hd.local_path
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _embed(host: str, model: str, text: str, timeout_s: float, max_chars: int) -> list[float] | None:
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{host}/api/embeddings",
                json={"model": model, "prompt": text[:max_chars]},
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data.get("embedding")
            if vec and isinstance(vec, list):
                return [float(v) for v in vec]
    except Exception as exc:
        print(f"  embed error ({model}): {exc}", file=sys.stderr)
    return None


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n == 0:
        return v
    return [x / n for x in v]


def _knn_accuracy(
    vectors: list[list[float] | None],
    labels: list[str],
    k: int,
    skip_label: str | None = "unknown",
) -> dict:
    """Leave-one-out KNN top-1 + top-3 accuracy on labels.

    If skip_label is given, docs with that label are still used as neighbors
    but don't count toward accuracy (they're not labeled gold).
    """
    valid_indices = [i for i, v in enumerate(vectors) if v is not None]
    if len(valid_indices) < k + 1:
        return {"top1": 0.0, "top3": 0.0, "n_scored": 0, "n_total": len(vectors), "k": k}

    # Pre-normalize for cosine = dot product
    normed: list[list[float] | None] = [None] * len(vectors)
    for i in valid_indices:
        normed[i] = _normalize(vectors[i])

    correct_top1 = 0
    correct_top3 = 0
    scored = 0

    for i in valid_indices:
        true_label = labels[i]
        if skip_label is not None and true_label == skip_label:
            continue
        qv = normed[i]
        sims: list[tuple[float, str]] = []
        for j in valid_indices:
            if j == i:
                continue
            ov = normed[j]
            if ov is None:
                continue
            dot = sum(a * b for a, b in zip(qv, ov))
            sims.append((dot, labels[j]))
        sims.sort(key=lambda s: s[0], reverse=True)
        top_k = sims[:k]
        # Weighted vote by similarity
        vote: dict[str, float] = {}
        for sim, lab in top_k:
            vote[lab] = vote.get(lab, 0.0) + sim
        ranked = sorted(vote.items(), key=lambda kv: kv[1], reverse=True)
        scored += 1
        if ranked and ranked[0][0] == true_label:
            correct_top1 += 1
        if any(lab == true_label for lab, _ in ranked[:3]):
            correct_top3 += 1

    return {
        "top1": correct_top1 / scored if scored else 0.0,
        "top3": correct_top3 / scored if scored else 0.0,
        "n_scored": scored,
        "n_total": len(valid_indices),
        "k": k,
    }


def _bench_model(
    host: str,
    model: str,
    docs: list[dict],
    timeout_s: float,
    max_chars: int,
) -> dict:
    # Warm up
    t_warm = time.perf_counter()
    warmup_ok = _embed(host, model, "warmup", timeout_s, max_chars) is not None
    warmup_seconds = round(time.perf_counter() - t_warm, 1)
    if not warmup_ok:
        return {"model": model, "warmup_ok": False, "warmup_seconds": warmup_seconds}

    vectors: list[list[float] | None] = []
    embed_failures = 0
    total_embed_time = 0.0
    for i, d in enumerate(docs, 1):
        text = (d.get("full_text") or "")
        if not text.strip():
            vectors.append(None)
            continue
        t0 = time.perf_counter()
        v = _embed(host, model, text, timeout_s, max_chars)
        total_embed_time += time.perf_counter() - t0
        if v is None:
            embed_failures += 1
        vectors.append(v)
        if i % 50 == 0:
            print(f"    embedded {i}/{len(docs)} docs (fails={embed_failures})", flush=True)

    ms_per_doc = (total_embed_time * 1000) / max(1, len(docs) - embed_failures)

    utility_labels = [d["utility"] for d in docs]
    family_labels = [d.get("family") or "" for d in docs]

    utility_acc = _knn_accuracy(vectors, utility_labels, k=11, skip_label="unknown")

    family_counts = Counter([f for f in family_labels if f])
    valid_family_docs = {i for i, f in enumerate(family_labels) if f and family_counts[f] >= 3}
    masked_family_labels = [
        family_labels[i] if i in valid_family_docs else "" for i in range(len(family_labels))
    ]
    family_acc = _knn_accuracy(vectors, masked_family_labels, k=5, skip_label="")

    return {
        "model": model,
        "warmup_ok": True,
        "warmup_seconds": warmup_seconds,
        "docs_embedded": sum(1 for v in vectors if v is not None),
        "embed_failures": embed_failures,
        "ms_per_doc": round(ms_per_doc, 1),
        "total_embed_seconds": round(total_embed_time, 1),
        "utility_knn": utility_acc,
        "family_knn_min3": family_acc,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT))
    parser.add_argument("--limit", type=int, default=250, help="Number of labeled docs to embed")
    parser.add_argument("--max-chars", type=int, default=2000)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--report", default="")
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
    docs = _load_labeled_docs(conn, args.limit)
    if not docs:
        print("ERROR: no labeled docs found", file=sys.stderr)
        return 2

    utility_dist = Counter(d["utility"] for d in docs)
    family_dist = Counter(d["family"] for d in docs if d["family"])
    print(f"Loaded {len(docs)} labeled docs")
    print(f"  utility distribution: {dict(utility_dist)}")
    print(f"  family classes (>=3 docs): "
          f"{sum(1 for c in family_dist.values() if c >= 3)} of {len(family_dist)}")
    print()

    results: list[dict] = []
    for model in models:
        print(f"=== Benchmarking {model} ===", flush=True)
        t0 = time.perf_counter()
        r = _bench_model(args.host, model, docs, args.timeout_s, args.max_chars)
        r["wall_seconds"] = round(time.perf_counter() - t0, 1)
        if r.get("warmup_ok") is False:
            print(f"  WARMUP FAILED for {model}")
            results.append(r)
            continue
        u = r["utility_knn"]
        f = r["family_knn_min3"]
        print(
            f"  docs={r['docs_embedded']} fails={r['embed_failures']} "
            f"ms/doc={r['ms_per_doc']} "
            f"util_top1={u['top1']:.3f} util_top3={u['top3']:.3f} (n={u['n_scored']}) "
            f"fam_top1={f['top1']:.3f} fam_top3={f['top3']:.3f} (n={f['n_scored']}) "
            f"wall={r['wall_seconds']}s",
            flush=True,
        )
        results.append(r)

    valid = [r for r in results if r.get("warmup_ok") is not False]
    ranked = sorted(valid, key=lambda r: r["utility_knn"]["top1"], reverse=True)

    print("\n=== Ranking (by utility KNN top-1) ===")
    print(f"{'model':<40} {'util_t1':>8} {'util_t3':>8} {'fam_t1':>7} {'fam_t3':>7} {'ms/doc':>8}")
    for r in ranked:
        u = r["utility_knn"]
        f = r["family_knn_min3"]
        print(
            f"{r['model']:<40} {u['top1']:>8.3f} {u['top3']:>8.3f} "
            f"{f['top1']:>7.3f} {f['top3']:>7.3f} {r['ms_per_doc']:>8.1f}"
        )

    report_path = Path(args.report) if args.report else (
        REPORT_DIR_DEFAULT
        / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_classification.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": args.host,
        "limit": args.limit,
        "max_chars": args.max_chars,
        "doc_count": len(docs),
        "utility_distribution": dict(utility_dist),
        "family_classes_min3": sum(1 for c in family_dist.values() if c >= 3),
        "results": results,
        "ranking": [r["model"] for r in ranked],
    }
    report_path.write_text(json.dumps(payload, indent=2))
    print(f"\nReport written: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
