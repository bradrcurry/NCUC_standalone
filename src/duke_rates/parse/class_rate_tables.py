"""Structured parser for "rate by customer class" tables.

Rider schedules such as JAA (DEP Leaf 602) and the single-value riders that
publish one rate per class (Storm Recovery, CAR, RDM ...) print a table:

    Rate Class                | Applicable Schedule(s)        | Rate
    Non-Demand Rate Class (dollars per kilowatt-hour)
    Residential               | RES, R-TOUD, R-TOU, R-TOU-CPP | 0.00464
    ...
    Demand Rate Classes (dollars per kilowatt)
    Large General Service     | LGS, LGS-TOU, LGS-HLF         | 3.03

The unit of a row is taken from the nearest section row or column header that
names one ("(dollars per kilowatt-hour)", "cents per kWh", "Rate ($/kW)").
Rows whose unit cannot be determined are returned with ``unit=None`` and a
warning, never with an assumed unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from duke_rates.document_intelligence.models import DocumentRepresentation
from duke_rates.document_intelligence.table_api import CellRef, table_view
from duke_rates.parse.canonical import UnitParse, parse_decimal, parse_unit

PARSER_NAME = "class_rate_tables"
PARSER_VERSION = "1"

_UNIT_PHRASE_RE = re.compile(
    r"(dollars?|cents?)\s+per\s+kilowatt[- ]?hours?|(dollars?|cents?)\s+per\s+kilowatts?\b"
    r"|(cents?|¢|\$)\s*/\s*kwh|(cents?|¢|\$)\s*/\s*kw\b|(dollars?|cents?)\s+per\s+kwh",
    re.I,
)
_CLASS_HEADER_RE = re.compile(r"\b(rate\s+class(es)?|customer\s+class|class)\b", re.I)


@dataclass
class ClassRateRow:
    class_label: str
    schedules: str | None
    value: Decimal
    printed_value_text: str
    unit: UnitParse | None
    section: str | None
    evidence: dict
    warnings: list[str] = field(default_factory=list)


def unit_from_text(text: str) -> UnitParse | None:
    match = _UNIT_PHRASE_RE.search(text or "")
    if not match:
        return None
    phrase = match.group(0).lower()
    phrase = re.sub(r"kilowatts?\b", "kilowatt", phrase).replace("kilowatt hours", "kilowatt-hour")
    parsed = parse_unit(phrase)
    return parsed if parsed.recognized else None


def parse_class_rate_tables(rep: DocumentRepresentation) -> list[ClassRateRow]:
    rows: list[ClassRateRow] = []
    for page in rep.pages:
        for table in sorted(page.tables, key=lambda t: t.metadata.get("reading_order", 0)):
            view = table_view(table, header_rows=0)
            grid = view.grid
            if not grid or not any(_CLASS_HEADER_RE.search(c.text) for r in grid[:3] for c in r):
                continue
            header_unit = None
            section_unit: UnitParse | None = None
            section: str | None = None
            for row in grid:
                texts = [c.text for c in row]
                joined = " ".join(t for t in texts if t)
                numbers = [
                    (c, parse_decimal(c.text)) for c in row[1:] if parse_decimal(c.text) is not None
                ]
                if not numbers:
                    unit = unit_from_text(joined)
                    if (
                        unit
                        and _CLASS_HEADER_RE.search(joined)
                        and len(set(t for t in texts if t)) == 1
                    ):
                        section_unit, section = unit, texts[0]  # spanning section row
                    elif unit:
                        header_unit = unit
                    continue
                label = row[0].text.strip()
                if not label or _CLASS_HEADER_RE.fullmatch(label):
                    continue
                cell, value = numbers[-1]
                unit = section_unit or header_unit
                warnings = [] if unit else ["no unit in a section row or column header"]
                rows.append(
                    ClassRateRow(
                        class_label=label,
                        schedules=row[1].text
                        if len(row) > 2 and parse_decimal(row[1].text) is None
                        else None,
                        value=value,
                        printed_value_text=cell.text,
                        unit=unit,
                        section=section,
                        evidence=_evidence(
                            rep, page.page_number, table.table_id, row, cell, unit, section
                        ),
                        warnings=warnings,
                    )
                )
    return rows


def _evidence(
    rep,
    page_number: int,
    table_id: str | None,
    row: list[CellRef],
    cell: CellRef,
    unit: UnitParse | None,
    section: str | None,
) -> dict:
    return {
        "method": PARSER_NAME,
        "backend": rep.normalizer_backend.value,
        "backend_version": rep.backend_version,
        "page": page_number,
        "table_id": table_id,
        "row": cell.row,
        "col": cell.col,
        "text": " | ".join(c.text for c in row if c.text),
        "printed_value_text": cell.text,
        "printed_unit": unit.printed if unit else None,
        "section": section,
        "bbox": cell.bbox.model_dump() if cell.bbox else None,
        "label": row[0].as_evidence(),
    }
