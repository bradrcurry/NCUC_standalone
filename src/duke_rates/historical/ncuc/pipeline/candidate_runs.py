"""Run the bulk extractor as a non-destructive *candidate* extraction.

``BulkExtractor.process_document`` replaces a version's ``tariff_charges``
in place. :func:`extract_candidate` runs the same extraction code
(``extract_charges_from_document``) but stores the output as a sealed
``extraction_runs`` candidate. ``tariff_charges`` and the classification
tables are not written; only an explicit ``provenance.promote`` changes what
billing reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from duke_rates.db.provenance import (
    CandidateCharge,
    code_fingerprint,
    git_producer_version,
    record_charges,
    seal_run,
    start_run,
)
from duke_rates.db.sqlite import connect

_PIPELINE_DIR = Path(__file__).resolve().parent
PRODUCER_MODULES = tuple(
    _PIPELINE_DIR / name
    for name in (
        "bulk_extractor.py",
        "parser_profiles.py",
        "rate_extractor.py",
        "document_prep.py",
        "ocr_normalization.py",
        "page_miner.py",
        "docling_page_miner.py",
        "profile_dependencies.py",
        "stage_versions.py",
    )
) + tuple(
    _PIPELINE_DIR.parents[2] / rel
    for rel in (
        "parse/rider_summary.py",
        "parse/rider_summary_tables.py",
        "parse/class_rate_tables.py",
        "parse/canonical.py",
        "document_intelligence/adapters.py",
        "document_intelligence/table_api.py",
        "document_intelligence/evidence.py",
    )
)

# Side-effect recorders that write classification tables; candidate runs
# must not touch them (same list as bench/run_extraction.py).
_RECORDERS = (
    "_record_document_type_classification",
    "_record_document_type_v2_classification",
    "_record_flag_classifications",
    "_record_embedding_document_type",
    "_record_llm_document_type",
)

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")


def _quote_contains_value(snippet: str | None, value: float | None) -> bool:
    """True when the snippet prints the value (as written, or in cents)."""
    if not snippet or value is None:
        return False
    for token in _NUMBER_RE.findall(snippet):
        try:
            number = float(token.replace(",", ""))
        except ValueError:
            continue
        for candidate in (value, value * 100.0, value / 100.0):
            if abs(number - candidate) <= 1e-9 + 1e-6 * abs(candidate):
                return True
    return False


def _evidence_fields(evidence: dict | None) -> dict[str, Any]:
    """Page, bbox, quote and table reference from structured evidence, as recorded.

    Nothing is filled in that the evidence does not contain.
    """
    if not evidence:
        return {}
    values = evidence.get("values") or evidence.get("cells") or []
    first = values[0] if values else {}
    label = evidence.get("label") if isinstance(evidence.get("label"), dict) else {}
    printed = evidence.get("printed_value_text") or first.get("text")
    text = evidence.get("text")
    if text is None and values:
        text = " ".join(
            x
            for x in [
                label.get("text"),
                *[v.get("text") for v in values],
                (evidence.get("date") or {}).get("text"),
            ]
            if x
        )
    bbox = evidence.get("bbox") or first.get("bbox")
    page = evidence.get("page") or first.get("page")
    table_id = evidence.get("table_id") or first.get("table_id")
    return {
        "page_number": int(page) if page is not None else None,
        "bbox": bbox,
        # A quote must print the value; otherwise it is not evidence.
        "source_quote": text if (text and printed and printed in text) else None,
        "printed_value_text": printed,
        "printed_unit": first.get("printed_unit") or evidence.get("printed_unit"),
        "effective_start": (evidence.get("date") or {}).get("text"),
        "table_ref": {
            "table_id": table_id,
            "row": evidence.get("row", first.get("row")),
            "col": evidence.get("col", first.get("col")),
            "method": evidence.get("method"),
            "backend": evidence.get("backend"),
            "backend_version": evidence.get("backend_version"),
            "selected_from": evidence.get("selected_from"),
        }
        if (table_id or evidence.get("method"))
        else None,
    }


def to_candidate_charge(
    charge: Any, *, source_pdf_path: str, source_pdf_sha256: str | None, profile: str | None
) -> CandidateCharge:
    """Map an ``ExtractedCharge`` without inventing provenance.

    Page, bbox, quote and table reference come from ``charge.evidence`` when
    a structured parse or the value locator recorded them; otherwise they
    stay unavailable. A text snippet counts as a quote only when it prints
    the value; most profiles store the label there, kept as
    ``extractor_snippet``.
    """
    snippet = (getattr(charge, "source_snippet", "") or "").strip() or None
    fields = _evidence_fields(getattr(charge, "evidence", None))
    quote = fields.get("source_quote") or (
        snippet if _quote_contains_value(snippet, charge.rate_value) else None
    )
    return CandidateCharge(
        charge_type=charge.charge_type,
        charge_label=charge.charge_label,
        rate_value=charge.rate_value,
        rate_unit=charge.rate_unit,
        tier_min=charge.tier_min,
        tier_max=charge.tier_max,
        tou_period=charge.tou_period,
        season=charge.season,
        customer_class=getattr(charge, "customer_class", None),
        printed_value_text=fields.get("printed_value_text"),
        printed_unit=fields.get("printed_unit"),
        effective_start=_iso_date(fields.get("effective_start")),
        source_pdf_path=source_pdf_path,
        source_pdf_sha256=source_pdf_sha256,
        page_number=fields.get("page_number"),
        bbox=fields.get("bbox"),
        source_quote=quote,
        extractor_snippet=snippet,
        table_ref=fields.get("table_ref"),
        extraction_method=f"parser_profile:{profile}" if profile else "bulk_extractor",
        confidence_score=charge.confidence_score or 0.0,
    )


def _iso_date(printed: str | None) -> str | None:
    """'12/1/25' -> '2025-12-01'; None when absent or unreadable."""
    if not printed:
        return None
    from datetime import datetime

    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(printed.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _validation_context(db_path: str | Path, version_id: int, family_key: str) -> dict[str, Any]:
    """Family type and the previous revision's live charges, for the validators."""
    import sqlite3

    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        family = conn.execute(
            "SELECT family_type FROM tariff_families WHERE family_key = ?", (family_key,)
        ).fetchone()
        current = conn.execute(
            "SELECT effective_start FROM tariff_versions WHERE id = ?", (version_id,)
        ).fetchone()
        previous: list[dict] = []
        if current and current["effective_start"]:
            prev = conn.execute(
                "SELECT id FROM tariff_versions WHERE family_key = ? AND id <> ? "
                "AND effective_start < ? AND COALESCE(status, 'approved') = 'approved' "
                "ORDER BY effective_start DESC LIMIT 1",
                (family_key, version_id, current["effective_start"]),
            ).fetchone()
            if prev:
                previous = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT charge_type, charge_label, rate_value, rate_unit, customer_class, "
                        "tou_period, season, tier_min, tier_max FROM tariff_charges WHERE version_id = ?",
                        (prev["id"],),
                    )
                ]
        return {"family_type": family["family_type"] if family else None, "previous": previous}
    finally:
        conn.close()


def extract_candidate(
    db_path: str | Path,
    historical_document_id: int,
    *,
    created_by: str,
    version_id: int | None = None,
    structured_mode: str = "hybrid",
) -> dict[str, Any]:
    """Extract one historical document into a sealed candidate run.

    ``structured_mode`` defaults to ``hybrid``: structured tables where the
    document has them, merged with the text parse section by section, with
    page/table evidence attached. Pass ``legacy`` for the Phase 0 behaviour.
    """
    from duke_rates.document_intelligence.charge_validators import run_charge_validators
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    from duke_rates.historical.ncuc.pipeline.stage_versions import HISTORICAL_BULK_PARSER_VERSION

    extractor = BulkExtractor(db_path=str(db_path), structured_mode=structured_mode)
    for name in _RECORDERS:
        if hasattr(extractor, name):
            setattr(extractor, name, lambda *a, **k: None)
    doc = extractor.get_document_for_extraction(historical_document_id)
    if doc is None:
        raise ValueError(f"historical document {historical_document_id} does not exist")
    version_id = version_id or extractor.get_tariff_version_for_document(historical_document_id)
    if not version_id:
        raise ValueError(f"historical document {historical_document_id} has no tariff version")

    charges, profile, _candidates, status, _signals, text_metrics, selection = (
        extractor.extract_charges_from_document(doc)
    )
    text_source = (text_metrics or {}).get("text_source") or (selection or {}).get("text_source")
    conn = connect(Path(db_path))
    try:
        run_id = start_run(
            conn,
            target_kind="tariff_version",
            target_id=version_id,
            parser_name=f"bulk_extractor:{profile or 'none'}",
            parser_version=git_producer_version(),
            extraction_method="deterministic_profile",
            created_by=created_by,
            family_key=doc["family_key"],
            version_id=version_id,
            source_document_kind="historical_document",
            source_document_id=historical_document_id,
            source_path=doc["local_path"],
            code_fingerprint=code_fingerprint(PRODUCER_MODULES),
            config={
                "pipeline_stage_version": HISTORICAL_BULK_PARSER_VERSION,
                "start_page": doc.get("start_page"),
                "end_page": doc.get("end_page"),
                "recorders_disabled": list(_RECORDERS),
                "structured_mode": structured_mode,
            },
            text_source=text_source,
            interpreted_effective_start=doc.get("effective_start"),
        )
        sha = conn.execute(
            "SELECT document_sha256 FROM extraction_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        record_charges(
            conn,
            run_id,
            [
                to_candidate_charge(
                    c, source_pdf_path=doc["local_path"], source_pdf_sha256=sha, profile=profile
                )
                for c in charges
            ],
        )
        run_status = "failed" if status in ("missing_file", "error") else None
        context = _validation_context(db_path, version_id, doc["family_key"])
        findings = run_charge_validators(
            charges,
            family_type=context["family_type"],
            document_effective=doc.get("effective_start"),
            previous=context["previous"],
        )
        validation = seal_run(
            conn,
            run_id,
            status=run_status,
            metrics={
                "extractor_status": status,
                "profile": profile,
                "charge_count": len(charges),
                "structured_input": (selection or {}).get("structured_input"),
                "charges_with_page": sum(
                    1
                    for c in charges
                    if (c.evidence or {}).get("page") or (c.evidence or {}).get("values")
                ),
            },
            extra_issues=[f.as_dict() for f in findings],
        )
    finally:
        conn.close()
    return {
        "run_id": run_id,
        "version_id": version_id,
        "extractor_status": status,
        "profile": profile,
        **validation,
    }
