"""Derive whole-document type labels by aggregating section_type_gold rows.

The section_type_gold corpus labels sections (rate_schedule, rider, T&C,
procedural, cover_letter) rather than documents (TARIFF_SHEET, RIDER,
ORDER_FINAL). For KNN voting in EmbeddingKNNClassifier we need a single
doc-level label per neighbor PDF, so this module collapses the per-section
labels into a single doc_type using a priority rule and reports evidence
(section counts) so callers can inspect.

Why this exists: rule_document_type_v1 mislabels documents whose first page
is a cover letter or order but whose body contains tariff/rider content.
Empirically (2026-05-25, 273 PDFs), section-derived labels disagree with
rule_v1 on 56% of PDFs, mostly recovering hidden TARIFF_SHEET/RIDER content.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable


# Section types we recognize. Anything else is ignored.
_KNOWN_SECTION_TYPES = {
    "rate_schedule",
    "rider",
    "terms_conditions",
    "cover_letter",
    "procedural",
}


def derive_doc_type_from_sections(
    section_types: Iterable[str],
) -> str | None:
    """Collapse a set of section_type labels into a single doc_type.

    The priority rule is:
      1. rate_schedule + procedural (or other non-rate types) → COMPLIANCE_FILING
         (multi-purpose bundle).
      2. rate_schedule + rider → TARIFF_SHEET (rate doc with rider sections).
      3. rate_schedule + T&C (only) → TARIFF_SHEET.
      4. rate_schedule alone → TARIFF_SHEET.
      5. rider (with or without T&C) → RIDER.
      6. terms_conditions alone → TARIFF_SHEET (T&C is a tariff component).
      7. procedural + cover_letter → ORDER_FINAL.
      8. procedural alone → ORDER_PROCEDURAL.
      9. cover_letter alone → COVER_LETTER.

    Returns None if no recognized section types were supplied (caller should
    fall back to rule_v1 or treat as UNKNOWN).
    """
    types = {t for t in section_types if t in _KNOWN_SECTION_TYPES}
    if not types:
        return None

    has_rate = "rate_schedule" in types
    has_rider = "rider" in types
    has_tc = "terms_conditions" in types
    has_proc = "procedural" in types
    has_cover = "cover_letter" in types

    # Bundles: rate_schedule co-located with procedural is a compliance filing.
    if has_rate and has_proc:
        return "COMPLIANCE_FILING"

    if has_rate:
        # rate + rider, rate + T&C, rate alone → all TARIFF_SHEET.
        return "TARIFF_SHEET"

    if has_rider:
        return "RIDER"

    if has_tc:
        # T&C without rate or rider is rare but still tariff content.
        return "TARIFF_SHEET"

    if has_proc and has_cover:
        return "ORDER_FINAL"

    if has_proc:
        return "ORDER_PROCEDURAL"

    if has_cover:
        return "COVER_LETTER"

    return None


def fetch_section_derived_labels(
    conn: sqlite3.Connection,
) -> dict[str, dict]:
    """Build {source_pdf → {label, confidence, n_sections, section_types}}.

    Only includes PDFs that have at least one active section_type_gold row.
    Confidence is the mean of per-section confidences, capped at 0.95 (we
    don't want section-derived labels to dominate the vote against
    independently-confirmed signals).
    """
    cur = conn.execute(
        """
        SELECT source_pdf,
               GROUP_CONCAT(DISTINCT section_type) AS section_types,
               AVG(confidence) AS avg_conf,
               COUNT(*) AS n_sections
        FROM section_type_gold
        WHERE superseded_by IS NULL
        GROUP BY source_pdf
        """
    )
    out: dict[str, dict] = {}
    for row in cur.fetchall():
        source_pdf, types_str, avg_conf, n_sections = row
        types = set(types_str.split(","))
        derived = derive_doc_type_from_sections(types)
        if derived is None:
            continue
        out[source_pdf] = {
            "label": derived,
            "confidence": min(0.95, float(avg_conf or 0.5)),
            "n_sections": int(n_sections),
            "section_types": sorted(types),
            "source": "section_gold",
        }
    cur.close()
    return out
