"""Build the benchmark's Docling cache: one gzip'd Docling document per corpus doc.

Structured extraction runs read Docling output from here, so benchmark runs
are reproducible without the production DB. For each corpus document the
builder:

1. reuses a cached ``docling_artifacts`` row from ``--db`` whose ``file_hash``
   equals the corpus SHA-256 (bytes identical to the benchmark PDF), or
2. converts the PDF with the existing backend
   (``docling_backend.convert_pdf_with_docling``, standard pipeline) in a
   temporary directory, so nothing is written next to the source PDF or to
   any database.

It never writes to ``--db``. Output: ``bench/docling/<doc_id>.docling.json.gz``.

Usage::

    python bench/build_docling_cache.py --db <scratch copy> [--accelerator cuda]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import yaml

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
OUT = BENCH / "docling"
PRODUCTION_DB = (ROOT / "data" / "db" / "duke_rates.db").resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pdf_for(entry: dict) -> Path | None:
    for candidate in (entry.get("bench_pdf"), entry.get("source_path")):
        if candidate and (ROOT / candidate).exists():
            path = ROOT / candidate
            if _sha256(path) == entry["sha256"]:
                return path
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="scratch DB to read cached artifacts from")
    ap.add_argument("--accelerator", default="cuda")
    ap.add_argument("--only", nargs="*", help="doc_ids to (re)build")
    args = ap.parse_args()
    if Path(args.db).resolve() == PRODUCTION_DB:
        raise SystemExit("refusing to read the production database; use a scratch copy")
    sys.path.insert(0, str(ROOT / "src"))
    from duke_rates.historical.ncuc.pipeline.docling_backend import convert_pdf_with_docling

    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    corpus = yaml.safe_load((BENCH / "corpus.yaml").read_text(encoding="utf-8"))["docs"]
    docling_version = importlib.metadata.version("docling")
    for entry in corpus:
        doc_id = entry["doc_id"]
        target = OUT / f"{doc_id}.docling.json.gz"
        if args.only and doc_id not in args.only:
            continue
        if target.exists() and not args.only:
            print(f"{doc_id:32s} cached")
            continue
        row = conn.execute(
            "SELECT backend_version, accelerator, status, page_count, doc_json_content "
            "FROM docling_artifacts WHERE file_hash = ? AND doc_json_content IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (entry["sha256"],),
        ).fetchone()
        if row:
            envelope = {
                "origin": "docling_artifacts_cache",
                "backend_version": row[0],
                "accelerator": row[1],
                "status": row[2],
                "page_count": row[3],
                "docling_package_version": None,  # not recorded by the original conversion
                "document": json.loads(row[4]),
            }
        else:
            pdf = _pdf_for(entry)
            if pdf is None:
                print(f"{doc_id:32s} SKIP: no PDF with the corpus SHA-256")
                continue
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / pdf.name
                shutil.copy2(pdf, local)
                result = convert_pdf_with_docling(str(local), accelerator=args.accelerator)
            if not result or not result.get("document"):
                print(f"{doc_id:32s} FAILED conversion")
                continue
            envelope = {
                "origin": "converted_for_benchmark",
                "backend_version": result.get("backend_version"),
                "accelerator": result.get("accelerator"),
                "status": str(result.get("conversion_status")),
                "page_count": result.get("page_count"),
                "docling_package_version": docling_version,
                "document": result["document"],
            }
        envelope.update({"doc_id": doc_id, "file_sha256": entry["sha256"]})
        with gzip.open(target, "wt", encoding="utf-8") as handle:
            json.dump(envelope, handle, sort_keys=True, separators=(",", ":"))
        print(f"{doc_id:32s} {envelope['origin']}")


if __name__ == "__main__":
    main()
