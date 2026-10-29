"""Read-only exposure audit: which documents depend on unverifiable Docling artifacts.

For every stored Docling artifact, :func:`audit_docling_page_integrity`
checks original-page integrity against the PDF on disk (see
``page_integrity``), then classifies each historical document bound to an
unsafe artifact by what the pre-M1 structured path would have done with its
span:

* ``wrong_pages`` -- the span overlaps the artifact's chunk-relative page
  numbers, so structure from other pages was returned under the span's
  numbers (possible false-positive charges and false page provenance);
* ``not_covered`` -- the span lies beyond them, so an empty structure was
  returned and hybrid mode skipped its native fallback (missing structure);
* ``unbounded`` -- no span; the whole chunk was read under its own numbers.

It also reports whether the legacy text slicer (``_slice_docling_text``),
which has its own coverage heuristic, would have accepted each unsafe
artifact, and how many Docling page-miner rows were derived from them.

The database is opened with ``mode=ro``; nothing is written. Exposure is
potential: production ``tariff_charges`` were not produced by the structured
path, so no charge is confirmed corrupted by this audit.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.page_integrity import (
    UNVERIFIABLE,
    VALID,
    assess_docling_page_integrity,
)


def open_read_only(db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _pdf_facts(path: str) -> tuple[int | None, str | None]:
    """(page count, sha256) of the PDF on disk, or (None, None) when unreadable."""
    try:
        import fitz

        with fitz.open(path) as pdf:
            pages = pdf.page_count
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        return pages, hasher.hexdigest()
    except Exception:
        return None, None


def _stored_pages(doc_json: dict) -> list[int]:
    pages = {int(k) for k in (doc_json.get("pages") or {}) if str(k).isdigit()}
    for item in doc_json.get("texts") or []:
        for prov in item.get("prov") or []:
            if isinstance(prov.get("page_no"), int):
                pages.add(prov["page_no"])
    return sorted(pages)


def _legacy_slicer_accepts(doc_json: dict, pdf_pages: int | None, start: int | None) -> bool:
    """Whether ``_slice_docling_text``'s heuristic would slice this artifact for ``start``."""
    if start is None:
        return False  # unbounded legacy reads plain_text, which the chunk merge keeps whole
    max_prov = max(
        (p["page_no"] for t in doc_json.get("texts") or [] for p in t.get("prov") or []
         if isinstance(p.get("page_no"), int)),
        default=0,
    )
    if max_prov < start:
        return False
    actual = pdf_pages or 0
    return not (actual > 10 and max_prov > 0 and max_prov < actual * 0.8)


def classify_span(stored: list[int], start: int | None, end: int | None) -> str:
    """wrong_pages / not_covered / unbounded for a span read through chunk-relative numbers."""
    if start is None:
        return "unbounded"
    last = end or start
    return "wrong_pages" if any(start <= p <= last for p in stored) else "not_covered"


def audit_docling_page_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    pdf_cache: dict[str, tuple[int | None, str | None]] = {}
    artifacts: list[dict[str, Any]] = []
    status_counts: Counter = Counter()
    unsafe: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT id, source_pdf, file_hash, status, page_count, metadata_json "
        "FROM docling_artifacts WHERE doc_json_content IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        src = row["source_pdf"]
        if src not in pdf_cache:
            pdf_cache[src] = _pdf_facts(src)
        pdf_pages, sha = pdf_cache[src]
        doc_json = json.loads(
            conn.execute(
                "SELECT doc_json_content FROM docling_artifacts WHERE id = ?", (row["id"],)
            ).fetchone()[0]
            or "null"
        )
        verdict = assess_docling_page_integrity(
            doc_json,
            pdf_page_count=pdf_pages,
            requested=None,
            metadata=row["metadata_json"],
            artifact_page_count=row["page_count"],
        )
        status_counts[(verdict.status, verdict.reason)] += 1
        if verdict.status == VALID or not isinstance(doc_json, dict):
            continue
        entry = {
            "artifact_id": row["id"],
            "source_pdf": src,
            "status": row["status"],
            "verdict": verdict.status,
            "reason": verdict.reason,
            "pdf_page_count": pdf_pages,
            "stored_pages": verdict.details.get("stored_pages")
            or [[p, p] for p in _stored_pages(doc_json)][:1],
            "stored_table_count": len(doc_json.get("tables") or []),
            "file_hash_matches_disk": row["file_hash"] == sha,
            "_stored": _stored_pages(doc_json),
            "_doc_json": doc_json,
        }
        artifacts.append(entry)
        if verdict.status == UNVERIFIABLE:
            unsafe.setdefault(src, []).append(entry)

    documents: list[dict[str, Any]] = []
    for src, entries in unsafe.items():
        entry = entries[-1]  # newest unsafe artifact for the path
        bound = conn.execute(
            """
            SELECT hd.id, hd.family_key, hd.state, hd.start_page, hd.end_page,
                   GROUP_CONCAT(tv.id) AS version_ids,
                   (SELECT COUNT(*) FROM tariff_charges tc
                     JOIN tariff_versions v ON v.id = tc.version_id
                    WHERE v.historical_document_id = hd.id) AS charges
            FROM historical_documents hd
            LEFT JOIN tariff_versions tv ON tv.historical_document_id = hd.id
            WHERE hd.local_path = ?
            GROUP BY hd.id
            ORDER BY hd.id
            """,
            (src,),
        ).fetchall()
        for doc in bound:
            span_class = classify_span(entry["_stored"], doc["start_page"], doc["end_page"])
            version_ids = sorted(int(v) for v in (doc["version_ids"] or "").split(",") if v)
            documents.append(
                {
                    "historical_document_id": doc["id"],
                    "family_key": doc["family_key"],
                    "state": doc["state"],
                    "start_page": doc["start_page"],
                    "end_page": doc["end_page"],
                    "version_ids": version_ids,
                    "production_charges": doc["charges"],
                    "artifact_id": entry["artifact_id"],
                    "span_class": span_class,
                    # What M1 classifies each source as.
                    "classification": (
                        "no_structured_extraction_required"
                        if not version_ids
                        else "unsafe_page_mapping" if span_class != "not_covered"
                        else "incomplete_structured_coverage"
                    ),
                    "requires_regeneration": bool(version_ids),
                    "legacy_slicer_would_accept": _legacy_slicer_accepts(
                        entry["_doc_json"], entry["pdf_page_count"], doc["start_page"]
                    ),
                }
            )

    unsafe_paths = sorted(unsafe)
    page_miner_rows = 0
    if unsafe_paths:
        placeholders = ",".join("?" * len(unsafe_paths))
        page_miner_rows = conn.execute(
            f"SELECT COUNT(*) FROM ncuc_page_artifacts WHERE artifact_version = 'docling_page_miner_v1' "
            f"AND source_pdf IN ({placeholders})",
            unsafe_paths,
        ).fetchone()[0]

    for entry in artifacts:
        entry.pop("_stored", None)
        entry.pop("_doc_json", None)

    def count(pred) -> int:
        return sum(1 for d in documents if pred(d))

    with_version = [d for d in documents if d["version_ids"]]
    summary = {
        "artifacts_total": len(rows),
        "artifact_verdicts": {f"{s}:{r}": n for (s, r), n in sorted(status_counts.items(), key=str)},
        "unsafe_artifacts": sum(len(v) for v in unsafe.values()),
        "unsafe_pdfs": len(unsafe),
        "bound_documents": len(documents),
        "bound_nc_documents": count(lambda d: d["state"] == "NC"),
        "bound_documents_with_version": len(with_version),
        "affected_tariff_versions": len({v for d in with_version for v in d["version_ids"]}),
        "span_class": dict(Counter(d["span_class"] for d in documents)),
        "span_class_with_version": dict(Counter(d["span_class"] for d in with_version)),
        "classification": dict(Counter(d["classification"] for d in documents)),
        "possible_false_positive_exposures": count(
            lambda d: d["version_ids"] and d["span_class"] != "not_covered"
        ),
        "missing_structure_exposures": count(
            lambda d: d["version_ids"] and d["span_class"] == "not_covered"
        ),
        "requires_regeneration_documents": len(with_version),
        "production_charges_on_exposed_versions": sum(d["production_charges"] for d in with_version),
        "legacy_slicer_would_accept": count(lambda d: d["legacy_slicer_would_accept"]),
        "legacy_slicer_would_accept_with_version": count(
            lambda d: d["legacy_slicer_would_accept"] and d["version_ids"]
        ),
        "docling_page_miner_rows_from_unsafe_pdfs": page_miner_rows,
        "confirmed_corrupted_charges": None,
        "note": (
            "Exposure is potential. Production tariff_charges were not written by the "
            "structured path (no extraction_runs schema in this DB); confirmation needs a "
            "per-version comparison against the source pages."
        ),
    }
    return {"summary": summary, "artifacts": artifacts, "documents": documents}
