"""Corpus-wide before/after for the text rider-summary parser.

Re-parses every source PDF behind ``rider_summary_blocks`` (plus extra PDFs
given with ``--pdf``) with two versions of ``duke_rates.parse.rider_summary``:
the module at a git ref (``--baseline-ref``) and the working tree.  Both run
on pdfplumber and PyMuPDF text (pages joined by form feeds).  Nothing is
written to any database; the DB (a snapshot, opened read-only) is only used
to list source PDFs.

Metrics use one scoring routine for both versions, independent of the
parser's own flags:

* merged blocks: a block with more than one TOTAL row for the same unit
* footer leakage: a row whose label contains "Leaf No"
* implausible cents: a non-total row with |cents/kWh| > 5
* JAA unit swaps: a JAA row stored as cents/kWh > 1 (JAA cents are < 1;
  $/kW values are > 1)
* checksum pass rate: per block and unit, sum of top-level rows (BA
  sub-items excluded) equals the printed TOTAL within 0.002

Usage::

    python bench/rider_summary_reparse.py --db <snapshot.db> --out reports/phase0/rider_reparse
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
MODULE_PATH = "src/duke_rates/parse/rider_summary.py"


def load_module(name: str, source: str):
    tmp = Path(tempfile.mkdtemp()) / f"{name}.py"
    tmp.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, tmp)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolves annotations via sys.modules
    spec.loader.exec_module(module)
    return module


def page_texts(pdf: str) -> dict[str, str]:
    import fitz
    import pdfplumber

    with pdfplumber.open(pdf) as doc:
        plumber = "\f".join(p.extract_text() or "" for p in doc.pages)
    with fitz.open(pdf) as doc:
        mupdf = "\f".join(p.get_text() for p in doc)
    return {"pdfplumber": plumber, "pymupdf": mupdf}


def score(result) -> dict:
    stats: Counter = Counter()
    residential: list[dict] = []
    for block in result.rate_classes:
        stats["blocks"] += 1
        if getattr(block, "ambiguous_rate_class", False):
            stats["ambiguous_blocks"] += 1
        totals = [li for li in block.line_items if li.is_total]
        for attr in ("cents_per_kwh", "dollars_per_kw"):
            if len([t for t in totals if getattr(t, attr) is not None]) > 1:
                stats["merged_blocks"] += 1
                break
        in_ba = False
        top = []
        for li in block.line_items:
            if li.is_section_header:
                in_ba = li.rider_code == "BA"
                continue
            if li.is_total:
                in_ba = False
                continue
            if "Leaf No" in li.label:
                stats["footer_rows"] += 1
            if li.cents_per_kwh is not None and abs(li.cents_per_kwh) > 5:
                stats["implausible_cents_rows"] += 1
            if (li.rider_code == "JAA" or "JAA" in li.label) and (li.cents_per_kwh or 0) > 1:
                stats["jaa_cents_gt_1"] += 1
            if getattr(li, "unit_ambiguous", False):
                stats["unit_ambiguous_rows"] += 1
            stats["rows"] += 1
            if in_ba and not li.is_subtotal:
                continue
            if li.is_subtotal:
                in_ba = False
            top.append(li)
        for total, attr in ((block.total_cents_per_kwh, "cents_per_kwh"), (block.total_dollars_per_kw, "dollars_per_kw")):
            if total is None:
                continue
            vals = [getattr(li, attr) for li in top if getattr(li, attr) is not None]
            stats["checksums"] += 1
            if vals and abs(sum(vals) - total) <= 0.002:
                stats["checksums_ok"] += 1
        if block.rate_class.lower().startswith("residential"):
            residential.append({"rate_class": block.rate_class, "total": block.total_cents_per_kwh,
                                "rows": len(top)})
    return {"stats": dict(stats), "residential": residential}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="snapshot DB (opened read-only)")
    ap.add_argument("--baseline-ref", default="8c092b5")
    ap.add_argument("--pdf", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    baseline_src = subprocess.run(
        ["git", "show", f"{args.baseline_ref}:{MODULE_PATH}"], cwd=REPO, check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    versions = {
        "baseline": load_module("rider_summary_baseline", baseline_src),
        "repaired": load_module("rider_summary_repaired", (REPO / MODULE_PATH).read_text(encoding="utf-8")),
    }

    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    pdfs = sorted({r[0] for r in conn.execute("SELECT DISTINCT source_pdf FROM rider_summary_blocks")})
    pdfs += [p for p in args.pdf if p not in pdfs]
    conn.close()

    per_doc = []
    totals = {v: {e: Counter() for e in ("pdfplumber", "pymupdf")} for v in versions}
    timing = {v: 0.0 for v in versions}
    for pdf in pdfs:
        if not Path(pdf).exists():
            per_doc.append({"pdf": pdf, "missing": True})
            continue
        texts = page_texts(pdf)
        row = {"pdf": pdf}
        for vname, module in versions.items():
            for ename, text in texts.items():
                t0 = time.perf_counter()
                result = module.parse_rider_summary(text, source_pdf=pdf)
                timing[vname] += time.perf_counter() - t0
                s = score(result)
                totals[vname][ename].update(s["stats"])
                row[f"{vname}.{ename}"] = s
        per_doc.append(row)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "baseline_ref": args.baseline_ref,
        "documents": len(pdfs),
        "missing": sum(1 for d in per_doc if d.get("missing")),
        "totals": {v: {e: dict(c) for e, c in es.items()} for v, es in totals.items()},
        "parse_seconds": timing,
    }
    out.with_suffix(".json").write_text(json.dumps({"summary": summary, "documents": per_doc}, indent=1), encoding="utf-8")

    keys = ["blocks", "merged_blocks", "ambiguous_blocks", "footer_rows", "implausible_cents_rows",
            "jaa_cents_gt_1", "unit_ambiguous_rows", "checksums", "checksums_ok"]
    lines = [f"# Rider-summary re-parse: baseline `{args.baseline_ref}` vs working tree", "",
             f"Documents: {len(pdfs)} (missing on disk: {summary['missing']})", "",
             "| metric | " + " | ".join(f"{v} / {e}" for v in versions for e in ("pdfplumber", "pymupdf")) + " |",
             "|---|" + "---:|" * 4]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(totals[v][e].get(k, 0)) for v in versions for e in ("pdfplumber", "pymupdf")) + " |")
    for v in versions:
        for e in ("pdfplumber", "pymupdf"):
            c = totals[v][e]
            if c.get("checksums"):
                lines.append(f"\nChecksum pass rate {v}/{e}: {c['checksums_ok']}/{c['checksums']} = {c['checksums_ok'] / c['checksums']:.1%}")
    lines.append(f"\nParse time (s, all docs x 2 extractors): " + ", ".join(f"{v} {t:.2f}" for v, t in timing.items()))
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
