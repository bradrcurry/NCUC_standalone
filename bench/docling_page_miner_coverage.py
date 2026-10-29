"""D1 before/after: share of Docling table-cell decimals present in mined page text.

For the N most recent table-bearing ``docling_artifacts`` (read-only DB), runs
``mine_pages_from_docling_artifact`` from a baseline code tree and from the
working tree and counts, per artifact, the decimal values found in table
cells that appear in the mined text of the table's own page.

Usage::

    python bench/docling_page_miner_coverage.py --db <snapshot.db> --baseline-src <tree>/src --limit 40
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECIMAL = re.compile(r"\d*\.\d+")


def load_miner(src: Path):
    for name in [m for m in sys.modules if m.startswith("duke_rates")]:
        del sys.modules[name]
    sys.path.insert(0, str(src))
    try:
        module = importlib.import_module("duke_rates.historical.ncuc.pipeline.docling_page_miner")
        return module.mine_pages_from_docling_artifact
    finally:
        sys.path.remove(str(src))


def coverage(miner, artifact: dict, doc: dict) -> tuple[int, int, float]:
    t0 = time.perf_counter()
    pages, _ = miner(artifact)
    elapsed = time.perf_counter() - t0
    total = found = 0
    for table in doc.get("tables", []):
        prov = table.get("prov") or []
        if not prov:
            continue
        page_no = prov[0].get("page_no")
        if not page_no or page_no > len(pages):
            continue
        text = pages[page_no - 1].text_content
        for row in (table.get("data") or {}).get("grid") or []:
            for cell in row:
                for value in DECIMAL.findall(cell.get("text") or ""):
                    total += 1
                    found += value in text
    return total, found, elapsed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--baseline-src", required=True)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--out", default=str(REPO / "reports" / "phase0" / "d1_page_miner_coverage.json"))
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    rows = conn.execute(
        """SELECT id, source_pdf, page_count, doc_json_content, plain_text_content, accelerator, pipeline
           FROM docling_artifacts
           WHERE doc_json_content LIKE '%"tables": [{%' ORDER BY id DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()
    conn.close()

    miners = {"baseline": load_miner(Path(args.baseline_src)), "repaired": load_miner(REPO / "src")}
    results = {k: {"total": 0, "found": 0, "seconds": 0.0} for k in miners}
    per = []
    for aid, pdf, page_count, doc_json, plain, accel, pipeline in rows:
        doc = json.loads(doc_json)
        artifact = {"doc_json_content": doc_json, "plain_text_content": plain, "page_count": page_count,
                    "accelerator": accel, "pipeline": pipeline}
        entry = {"artifact_id": aid, "source_pdf": pdf}
        for name, miner in miners.items():
            total, found, elapsed = coverage(miner, artifact, doc)
            results[name]["total"] += total
            results[name]["found"] += found
            results[name]["seconds"] += elapsed
            entry[name] = {"total": total, "found": found}
        per.append(entry)
    for r in results.values():
        r["coverage"] = round(r["found"] / r["total"], 4) if r["total"] else None
        r["seconds"] = round(r["seconds"], 2)
    Path(args.out).write_text(json.dumps({"artifacts": len(rows), "results": results, "per_artifact": per}, indent=1),
                              encoding="utf-8")
    print(json.dumps({"artifacts": len(rows), "results": results}, indent=1))


if __name__ == "__main__":
    main()
