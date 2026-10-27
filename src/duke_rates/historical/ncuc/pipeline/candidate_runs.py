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


def to_candidate_charge(
    charge: Any, *, source_pdf_path: str, source_pdf_sha256: str | None, profile: str | None
) -> CandidateCharge:
    """Map an ``ExtractedCharge`` without inventing provenance.

    Profiles carry no page number or bbox, so both stay unavailable. The
    snippet counts as a source quote only when it prints the value; most
    profiles store the label there, which is kept as ``extractor_snippet``.
    """
    snippet = (getattr(charge, "source_snippet", "") or "").strip() or None
    quote = snippet if _quote_contains_value(snippet, charge.rate_value) else None
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
        printed_unit=None,
        source_pdf_path=source_pdf_path,
        source_pdf_sha256=source_pdf_sha256,
        source_quote=quote,
        extractor_snippet=snippet,
        extraction_method=f"parser_profile:{profile}" if profile else "bulk_extractor",
        confidence_score=charge.confidence_score or 0.0,
    )


def extract_candidate(
    db_path: str | Path,
    historical_document_id: int,
    *,
    created_by: str,
    version_id: int | None = None,
) -> dict[str, Any]:
    """Extract one historical document into a sealed candidate run."""
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    from duke_rates.historical.ncuc.pipeline.stage_versions import HISTORICAL_BULK_PARSER_VERSION

    extractor = BulkExtractor(db_path=str(db_path))
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
        validation = seal_run(
            conn,
            run_id,
            status=run_status,
            metrics={"extractor_status": status, "profile": profile, "charge_count": len(charges)},
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
