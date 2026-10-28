"""Phase 2D: compare Docling conversion runs structurally, before any extraction.

For each run directory written by ``convert_corpus.py`` and each corpus doc:

* tables detected, table cells, numeric cells, pages;
* table-cell recovery: share of the hand-transcribed expected values
  (``bench/expected/<doc_id>.json``, non-TOTAL rows) whose printed digits occur
  in some Docling table cell, and in any Docling text item or cell;
* page provenance: share of table cells / texts whose ``prov.page_no`` (+ the
  envelope ``page_offset``) falls inside the document span;
* resources from ``metrics.jsonl``.

Usage::

    python bench/phase2d/compare_conversions.py --runs <dir> [<dir> ...] --excerpts <dir> --out <prefix>
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
NUM = re.compile(r"\(?-?\$?\d[\d,]*\.?\d*\)?")


def _digits(text: str) -> set[str]:
    out = set()
    for m in NUM.findall(text or ""):
        t = m.strip("()$").replace(",", "").lstrip("$")
        if t and any(ch.isdigit() for ch in t):
            out.add(t.lstrip("-"))
    return out


def _value_key(value: str) -> str:
    return str(value).replace(",", "").lstrip("-").lstrip("$")


def doc_stats(envelope: dict, expected: dict, span: tuple[int, int]) -> dict:
    doc = envelope["document"]
    offset = int(envelope.get("page_offset") or 0)
    cell_nums, all_nums = set(), set()
    cells = numeric = 0
    in_span = with_page = 0
    for table in doc.get("tables", []):
        for cell in table.get("data", {}).get("table_cells", []):
            cells += 1
            nums = _digits(cell.get("text", ""))
            numeric += bool(nums)
            cell_nums |= nums
        for prov in table.get("prov", []):
            with_page += 1
            in_span += span[0] <= prov.get("page_no", 0) + offset <= span[1]
    for text in doc.get("texts", []):
        all_nums |= _digits(text.get("text", ""))
        for prov in text.get("prov", []):
            with_page += 1
            in_span += span[0] <= prov.get("page_no", 0) + offset <= span[1]
    all_nums |= cell_nums
    exp = [c for c in expected.get("charges", []) if not c.get("is_total")]
    keys = [_value_key(c["value"]) for c in exp]
    return {
        "pages": len(doc.get("pages", {})), "tables": len(doc.get("tables", [])), "cells": cells,
        "numeric_cells": numeric, "expected_values": len(keys),
        "in_table_cells": sum(k in cell_nums for k in keys), "in_any_text": sum(k in all_nums for k in keys),
        "prov_items": with_page, "prov_in_span": in_span,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--excerpts", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    manifest = {m["doc_id"]: m for m in json.loads((Path(args.excerpts) / "manifest.json").read_text())}
    result: dict = {}
    for run in map(Path, args.runs):
        metrics = {}
        if (run / "metrics.jsonl").exists():
            for line in (run / "metrics.jsonl").read_text().splitlines():
                rec = json.loads(line)
                metrics[rec["doc_id"]] = rec
        per = {}
        for doc_id, m in manifest.items():
            path = run / f"{doc_id}.docling.json.gz"
            row = {"status": metrics.get(doc_id, {}).get("status", "ok" if path.exists() else "missing")}
            row.update({k: metrics.get(doc_id, {}).get(k) for k in
                        ("seconds", "peak_rss_mb", "gpu_mem_used_peak_mib", "torch_peak_alloc_mb", "gpu_util_mean",
                         "cpu_mean_pct", "min_avail_gb")})
            if path.exists():
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    env = json.load(fh)
                expected = json.loads((BENCH / "expected" / f"{doc_id}.json").read_text(encoding="utf-8"))
                row.update(doc_stats(env, expected, tuple(m["span"])))
            per[doc_id] = row
        tot = {k: sum((r.get(k) or 0) for r in per.values()) for k in
               ("tables", "cells", "numeric_cells", "expected_values", "in_table_cells", "in_any_text",
                "prov_items", "prov_in_span", "seconds")}
        tot["ok_docs"] = sum(r["status"] == "ok" for r in per.values())
        tot["peak_rss_mb_max"] = max((r.get("peak_rss_mb") or 0) for r in per.values())
        tot["gpu_mem_peak_mib_max"] = max((r.get("gpu_mem_used_peak_mib") or 0) for r in per.values())
        tot["table_cell_recovery"] = round(tot["in_table_cells"] / tot["expected_values"], 4) if tot["expected_values"] else None
        tot["any_text_recovery"] = round(tot["in_any_text"] / tot["expected_values"], 4) if tot["expected_values"] else None
        tot["prov_in_span_rate"] = round(tot["prov_in_span"] / tot["prov_items"], 4) if tot["prov_items"] else None
        run_json = run / "run.json"
        result[run.name] = {"run": json.loads(run_json.read_text()) if run_json.exists() else None,
                            "totals": tot, "docs": per}
    Path(args.out + ".json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    names = list(result)
    lines = ["| metric | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    for k in ("ok_docs", "tables", "cells", "numeric_cells", "expected_values", "in_table_cells",
              "table_cell_recovery", "any_text_recovery", "prov_in_span_rate", "seconds", "peak_rss_mb_max",
              "gpu_mem_peak_mib_max"):
        lines.append(f"| {k} | " + " | ".join(str(result[n]["totals"].get(k)) for n in names) + " |")
    lines += ["", "| doc | " + " | ".join(f"{n} tables/cells/in-cell/exp | s" for n in names) + " |",
              "|---|" + "---|---:|" * len(names)]
    for doc_id in manifest:
        cells = []
        for n in names:
            r = result[n]["docs"][doc_id]
            cells.append(f"{r.get('tables')}/{r.get('cells')}/{r.get('in_table_cells')}/{r.get('expected_values')} | "
                         f"{r.get('seconds')}" if r["status"] == "ok" else f"{r['status']} | ")
        lines.append(f"| {doc_id} | " + " | ".join(cells) + " |")
    Path(args.out + ".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
