"""Phase 2E M1: hybrid extraction on documents bound to unverifiable Docling artifacts.

Extract only; nothing is persisted (classification recorders disabled, no
insert/process call). Run once per code tree (``--src``) on a scratch DB and
compare the outputs. For each document in the audit report it records the
structured backend used, the fallback reason, the charge count, charges whose
evidence came from Docling (every such artifact here is unverifiable, so all
of them are contamination), and evidence pages outside the document's span.

    python bench/phase2e/contamination_probe.py --src src --db <scratch.db> \
        --audit reports/phase2e_m1/docling_page_integrity_audit.json --out <out.json>
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRODUCTION_DB = (REPO / "data" / "db" / "duke_rates.db").resolve()
sys.path.insert(0, str(REPO / "bench"))


def _pages(evidence) -> set[int]:
    pages, stack = set(), [evidence]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("page"), int):
                pages.add(item["page"])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return pages


def _from_docling(evidence) -> bool:
    text = json.dumps(evidence or {}, default=str)
    return '"backend": "docling"' in text or "docling:#/" in text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="hybrid")
    args = ap.parse_args()
    if Path(args.db).resolve() == PRODUCTION_DB:
        raise SystemExit("refusing to run against the production database")
    sys.path.insert(0, str(Path(args.src).resolve()))
    logging.basicConfig(level=logging.ERROR)
    from run_extraction import _RECORDERS, build_doc

    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    extractor = BulkExtractor(db_path=args.db, structured_mode=args.mode)
    for name in _RECORDERS:
        if hasattr(extractor, name):
            setattr(extractor, name, lambda *a, **k: None)
    audit = json.loads(Path(args.audit).read_text(encoding="utf-8"))
    targets = [d for d in audit["documents"] if d["version_ids"]]
    conn = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
    rows = []
    for target in targets:
        doc = build_doc({"hd_id": target["historical_document_id"], "source_path": ""}, conn)
        doc["version_id"] = target["version_ids"][0]
        start, end = doc.get("start_page"), doc.get("end_page")
        t0 = time.perf_counter()
        try:
            charges, profile, _c, status, _s, _t, selection = extractor.extract_charges_from_document(doc)
            error = None
        except Exception as exc:
            charges, profile, status, selection, error = [], None, "error", {}, f"{type(exc).__name__}: {exc}"
        structured = (selection or {}).get("structured_input") or {}
        outside = [
            {"label": c.charge_label, "value": c.rate_value, "pages": sorted(_pages(c.evidence))}
            for c in charges
            if start and _pages(c.evidence) - set(range(int(start), int(end or start) + 1))
        ]
        rows.append({
            "historical_document_id": target["historical_document_id"],
            "family_key": target["family_key"],
            "span": [start, end],
            "span_class": target["span_class"],
            "status": status,
            "profile": profile,
            "error": error,
            "backend": structured.get("backend"),
            "docling_fallback_reason": structured.get("docling_fallback_reason"),
            "charges": len(charges),
            "charges_with_evidence": sum(1 for c in charges if c.evidence),
            "docling_evidence_charges": sum(1 for c in charges if _from_docling(c.evidence)),
            "evidence_outside_span": outside,
            "charge_keys": sorted((
                [c.charge_type, c.charge_label, c.rate_value, c.rate_unit, c.tou_period, c.season]
                for c in charges
            ), key=str),
            "seconds": round(time.perf_counter() - t0, 2),
        })
        r = rows[-1]
        print(f"hd {r['historical_document_id']:6} {r['span_class']:12} {str(r['backend']):10} "
              f"n={r['charges']:3} docling_ev={r['docling_evidence_charges']:3} "
              f"outside={len(outside):3} {r['seconds']:6.1f}s", flush=True)
    conn.close()
    summary = {
        "documents": len(rows),
        "errors": sum(1 for r in rows if r["error"]),
        "backend": {},
        "charges": sum(r["charges"] for r in rows),
        "docling_evidence_charges": sum(r["docling_evidence_charges"] for r in rows),
        "docs_with_docling_evidence": sum(1 for r in rows if r["docling_evidence_charges"]),
        "evidence_outside_span": sum(len(r["evidence_outside_span"]) for r in rows),
    }
    for r in rows:
        key = f"{r['span_class']}:{r['backend']}"
        summary["backend"][key] = summary["backend"].get(key, 0) + 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"src": args.src, "mode": args.mode, "summary": summary,
                                          "documents": rows}, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
