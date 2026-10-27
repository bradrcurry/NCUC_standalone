"""Run one code tree's extraction pipeline over the benchmark corpus.

The same script drives the baseline and the repaired pipeline: ``--src``
points at the ``src`` directory of the code tree under test (e.g. a git
worktree of the baseline commit).  For each corpus document it calls
``BulkExtractor.extract_charges_from_document`` -- the production text
selection (Docling artifact or pdfplumber), normalization, classification
short-circuits, profile selection and fallbacks -- and writes the charges,
text source and timing to JSON.  Nothing is persisted: the classification
recorders are disabled and no insert/process method is called.

``--db`` must be a scratch copy of the production DB (it supplies Docling
artifacts and page artifacts).  The script refuses ``data/db/duke_rates.db``.

Redline/fingerprint skip flags are forced off so every document is actually
extracted; whole-document redlines (blk13) are therefore scored as extracted.

``--mode`` (Phase 2B) selects the structured input the profiles see:
``legacy`` (text only, the Phase 0 pipeline), ``docling`` (structured Docling
tables from ``bench/docling/``, the committed cache built by
``build_docling_cache.py``), ``native`` (pdfplumber text layer with geometry
tables) or ``hybrid`` (Docling when cached, otherwise native, merged with the
legacy parse block by block). Code trees older than Phase 2B only support
``legacy``.

Usage::

    python bench/run_extraction.py --src <tree>/src --db <scratch.db> --label repaired \
        --out reports/phase0/bench_repaired.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import yaml

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
PRODUCTION_DB = (REPO / "data" / "db" / "duke_rates.db").resolve()

_RECORDERS = (
    "_record_document_type_classification",
    "_record_document_type_v2_classification",
    "_record_flag_classifications",
    "_record_embedding_document_type",
    "_record_llm_document_type",
)


def build_doc(entry: dict, conn: sqlite3.Connection) -> dict:
    """Document dict in the shape BulkExtractor's NC query produces."""
    doc: dict = {
        "id": entry.get("hd_id") or 0,
        "family_key": entry.get("family_key"),
        "title": entry.get("title") or "",
        "company": None,
        "state": "NC",
        "local_path": entry["source_path"],
        "content_hash": entry.get("sha256"),
        "effective_start": None,
        "revision_label": None,
        "supersedes_label": None,
        "leaf_no": None,
        "start_page": None,
        "end_page": None,
        "discovery_record_id": None,
        "docket_number": None,
        "acquisition_method": None,
        "discovery_doc_quality_tier": None,
        "is_redline_candidate": 0,
        "redline_confidence": 0.0,
        "classification_is_redline": None,
        "version_id": None,
    }
    if entry.get("hd_id"):
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id, family_key, title, company, state, local_path, content_hash,
                      effective_start, revision_label, supersedes_label, leaf_no,
                      start_page, end_page
               FROM historical_documents WHERE id = ?""",
            (entry["hd_id"],),
        ).fetchone()
        if row is not None:
            doc.update({k: row[k] for k in row.keys()})
    if "carolinas" in (doc["family_key"] or ""):
        doc["company"] = doc["company"] or "carolinas"
    elif "progress" in (doc["family_key"] or ""):
        doc["company"] = doc["company"] or "progress"
    return doc


def make_representation_provider(entries: dict[str, dict]):
    """Representations for benchmark docs: committed Docling cache, else native PDF."""
    from duke_rates.document_intelligence.adapters import (
        representation_from_docling,
        representation_from_native_pdf,
    )

    def provider(doc: dict, mode: str):
        entry = entries[doc["_bench_doc_id"]]
        start, end = doc.get("start_page"), doc.get("end_page")
        page_range = (int(start), int(end or start)) if start else None
        cache = BENCH / "docling" / f"{entry['doc_id']}.docling.json.gz"
        if mode in ("docling", "hybrid") and cache.exists():
            with gzip.open(cache, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
            return representation_from_docling(
                envelope["document"],
                source_pdf=doc["local_path"],
                file_sha256=envelope["file_sha256"],
                backend_version=envelope.get("backend_version"),
                docling_package_version=envelope.get("docling_package_version"),
                page_range=page_range,
                page_offset=int(envelope.get("page_offset") or 0),
            )
        if mode == "docling":
            return None
        return representation_from_native_pdf(doc["local_path"], page_range=page_range,
                                               file_sha256=entry["sha256"])

    return provider


def evidence_summary(evidence: dict | None, value_text: str | None = None) -> dict | None:
    if not evidence:
        return None
    printed = evidence.get("printed_value_text")
    if printed is None:
        cells = evidence.get("values") or evidence.get("cells") or []
        printed = cells[0].get("text") if cells else None
    text = evidence.get("text") or " ".join(
        str(c.get("text", "")) for c in (evidence.get("values") or evidence.get("cells") or [])
    )
    label = evidence.get("label") or {}
    if isinstance(label, dict) and label.get("text"):
        text = f"{label['text']} {text}"
    cells = evidence.get("values") or evidence.get("cells") or []
    bbox = evidence.get("bbox") or (cells[0].get("bbox") if cells else None)
    return {
        "method": evidence.get("method") or evidence.get("backend"),
        "page": evidence.get("page") or (cells[0].get("page") if cells else None),
        "table_id": evidence.get("table_id") or (cells[0].get("table_id") if cells else None),
        "row": evidence.get("row"),
        "has_bbox": bbox is not None,
        "printed_value_text": printed,
        "quote": text[:200] if text else None,
        "quote_contains_value": bool(printed and text and printed in text),
    }


def detect_effective_date(extractor, doc: dict) -> str | None:
    """Document effective date as the ingest heuristics read it (ISO), or None."""
    from dateutil import parser as dateparser

    from duke_rates.parse.heuristics import extract_effective_date

    try:
        text, _ = extractor.extract_text_from_pdf(
            doc["local_path"], start_page=doc.get("start_page"), end_page=doc.get("end_page")
        )
        raw = extract_effective_date(text or "")
        return dateparser.parse(raw).date().isoformat() if raw else None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="src/ directory of the code tree under test")
    ap.add_argument("--db", required=True, help="scratch DB copy (never production)")
    ap.add_argument("--corpus", default=str(BENCH / "corpus.yaml"))
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="legacy", choices=("legacy", "docling", "native", "hybrid"))
    args = ap.parse_args()

    if Path(args.db).resolve() == PRODUCTION_DB:
        raise SystemExit("refusing to run against the production database")
    sys.path.insert(0, str(Path(args.src).resolve()))
    logging.basicConfig(level=logging.WARNING)

    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    if args.mode == "legacy":
        extractor = BulkExtractor(db_path=args.db)
    else:
        extractor = BulkExtractor(db_path=args.db, structured_mode=args.mode)
    for name in _RECORDERS:
        if hasattr(extractor, name):
            setattr(extractor, name, lambda *a, **k: None)

    corpus = yaml.safe_load(Path(args.corpus).read_text(encoding="utf-8"))["docs"]
    if args.mode != "legacy":
        extractor.representation_provider = make_representation_provider(
            {entry["doc_id"]: entry for entry in corpus}
        )
    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    results = []
    for entry in corpus:
        doc = build_doc(entry, conn)
        doc["_bench_doc_id"] = entry["doc_id"]
        t0 = time.perf_counter()
        error = None
        try:
            charges, profile, _candidates, status, _signals, text_metrics, selection = (
                extractor.extract_charges_from_document(doc)
            )
        except Exception as exc:  # recorded, not fatal: one bad doc must not stop the run
            charges, profile, status, text_metrics, selection = [], None, "error", {}, {}
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - t0
        effective_date = detect_effective_date(extractor, doc)
        results.append({
            "doc_id": entry["doc_id"],
            "status": status,
            "profile": profile,
            "text_source": (text_metrics or {}).get("text_source") or (selection or {}).get("text_source"),
            "effective_date": effective_date,
            "seconds": round(elapsed, 3),
            "error": error,
            "charges": [
                {
                    "charge_type": c.charge_type,
                    "charge_label": c.charge_label,
                    "rate_value": c.rate_value,
                    "rate_unit": c.rate_unit,
                    "tou_period": c.tou_period,
                    "season": c.season,
                    "tier_min": c.tier_min,
                    "tier_max": c.tier_max,
                    "customer_class": getattr(c, "customer_class", None),
                    "source_snippet": c.source_snippet,
                    "evidence": evidence_summary(getattr(c, "evidence", None)),
                }
                for c in charges
            ],
        })
        print(f"{entry['doc_id']:32} {status:22} {str(profile):40} n={len(charges):4} {elapsed:6.2f}s")
    conn.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"label": args.label, "src": args.src, "mode": args.mode,
                                          "results": results}, indent=1),
                              encoding="utf-8")


if __name__ == "__main__":
    main()
