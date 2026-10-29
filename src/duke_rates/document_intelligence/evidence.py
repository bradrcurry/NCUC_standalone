"""Locate the printed evidence for an extracted value in a ``DocumentRepresentation``.

Profiles that still parse text (or the coordinate parser) do not know where a
value sat on the page. :func:`locate_value_evidence` finds it: the table cell
or text block whose printed number equals the extracted value, preferring the
one whose surrounding text shares words with the charge label.

It never guesses. Evidence is returned only when one location is better than
every other; otherwise the result is ``None`` and the caller records the
provenance as unavailable. The value itself is never changed.
"""

from __future__ import annotations

import re
from decimal import Decimal

from duke_rates.document_intelligence.models import DocumentRepresentation
from duke_rates.parse.canonical import parse_decimal, parse_unit

_NUMBER_TOKEN_RE = re.compile(r"\(?-?\$?\d[\d,]*\.?\d*\)?|\(?-?\.\d+\)?")
_WORD_RE = re.compile(r"[a-z]{3,}")
_STOP = {
    "rate",
    "charge",
    "rider",
    "service",
    "schedule",
    "schedules",
    "adjustment",
    "the",
    "and",
    "per",
    "for",
    "kwh",
    "total",
}


def _printed_candidates(value: float, unit: str | None) -> set[Decimal]:
    """Values the page may print for an extracted value (as stored, or in cents)."""
    stored = parse_decimal(value)
    if stored is None:
        return set()
    out = {stored.normalize()}
    parsed = parse_unit(unit)
    if parsed.canonical is not None and parsed.canonical.value in ("$/kWh", "$/kW"):
        out.add((stored * 100).normalize())  # profiles store cents/kWh as $/kWh
    return out


def _tokens(text: str) -> list[tuple[str, Decimal]]:
    found = []
    for match in _NUMBER_TOKEN_RE.finditer(text or ""):
        number = parse_decimal(match.group(0))
        if number is not None:
            found.append((match.group(0), number.normalize()))
    return found


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


def locate_value_evidence(
    rep: DocumentRepresentation | None,
    *,
    value: float | None,
    unit: str | None,
    label: str | None,
) -> dict | None:
    if rep is None or value is None:
        return None
    candidates = _printed_candidates(value, unit)
    if not candidates:
        return None
    label_words = _words(label or "")
    hits: list[tuple[int, dict]] = []
    in_tables: set[tuple[int, str]] = set()
    for page in rep.pages:
        for table in page.tables:
            for cell in table.cells or []:
                for token, number in _tokens(cell.text):
                    if number in candidates:
                        row_text = " ".join(
                            c.text for c in table.cells if c.row_index == cell.row_index
                        )
                        hits.append(
                            (
                                len(label_words & _words(row_text)),
                                {
                                    "method": "value_locator",
                                    "page": page.page_number,
                                    "table_id": table.table_id,
                                    "row": cell.row_index,
                                    "col": cell.column_index,
                                    "text": row_text,
                                    "printed_value_text": token,
                                    "bbox": cell.bbox.model_dump() if cell.bbox else None,
                                },
                            )
                        )
                        in_tables.add((page.page_number, token))
        for block in page.blocks:
            for token, number in _tokens(block.text):
                if number in candidates and (page.page_number, token) not in in_tables:
                    hits.append(
                        (
                            len(label_words & _words(block.text)),
                            {
                                "method": "value_locator",
                                "page": page.page_number,
                                "text": block.text,
                                "printed_value_text": token,
                                "bbox": block.bbox.model_dump() if block.bbox else None,
                            },
                        )
                    )
    if not hits:
        return None
    hits.sort(key=lambda h: h[0], reverse=True)
    best_score = hits[0][0]
    best = [h for s, h in hits if s == best_score]
    if len(best) != 1:
        return None  # ambiguous: several equally good locations
    evidence = dict(best[0])
    evidence["label_overlap"] = best_score
    evidence["alternatives"] = len(hits) - 1
    evidence["backend"] = rep.normalizer_backend.value
    evidence["backend_version"] = rep.backend_version
    return evidence
