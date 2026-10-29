"""Table-aware helpers that parser profiles share.

Works on any ``ExtractedTable`` (Docling or native geometry) and answers the
questions every tariff table parser asks:

* which leading rows are column headers, and what the row label header says
  (often the rate class: "Residential Service Schedules"),
* what each column means: the label column, value columns with their printed
  unit ("cents /kWh", "dollars/kW", "Baseline cents/kWh"), effective-date
  columns, or unknown,
* which body rows are section headers, subtotals or published TOTAL rows,
* where every value came from (:class:`CellRef`: table, page, row, column,
  text and bbox).

Units are interpreted by ``duke_rates.parse.canonical``. A value column whose
header names no unit gets ``unit=None``; callers must treat its values as
ambiguous instead of assuming one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal

from duke_rates.document_intelligence.models import (
    BoundingBox,
    DocumentRepresentation,
    ExtractedTable,
)
from duke_rates.parse.canonical import UnitParse, parse_decimal, parse_unit

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
_HEADER_TOKEN_RE = re.compile(
    r"\b(cents|dollars|kwh|kw|effective|date|baseline|incremental)\b|/\s*kwh|/\s*kw\b|¢", re.I
)
_RATE_CLASS_RE = re.compile(
    r"\b(schedules?|service|lighting|residential|industrial|general|traffic|outdoor)\b", re.I
)
_TOTAL_RE = re.compile(r"^\s*total\b", re.I)


@dataclass(frozen=True)
class CellRef:
    table_id: str | None
    page_number: int
    row: int
    col: int
    text: str
    bbox: BoundingBox | None = None

    def as_evidence(self) -> dict:
        return {
            "table_id": self.table_id,
            "page": self.page_number,
            "row": self.row,
            "col": self.col,
            "text": self.text,
            "bbox": self.bbox.model_dump() if self.bbox else None,
        }


@dataclass(frozen=True)
class ColumnRole:
    index: int
    role: str  # label | value | effective_date | empty | other
    header: str
    unit: UnitParse | None = None
    qualifier: str | None = None  # e.g. "baseline" / "incremental"


@dataclass
class TableView:
    table: ExtractedTable
    grid: list[list[CellRef]]
    header_rows: int
    columns: list[ColumnRole]
    header_label: str
    warnings: list[str] = field(default_factory=list)

    @property
    def value_columns(self) -> list[ColumnRole]:
        return [c for c in self.columns if c.role == "value"]

    def body(self) -> Iterator[list[CellRef]]:
        yield from self.grid[self.header_rows :]


def is_value_text(text: str) -> bool:
    return parse_decimal(text) is not None


def is_date_text(text: str) -> bool:
    return bool(DATE_RE.match(text.strip()))


def is_total_label(text: str) -> bool:
    return bool(_TOTAL_RE.match(text or ""))


def looks_like_rate_class(text: str) -> bool:
    return bool(_RATE_CLASS_RE.search(text or "")) and not is_total_label(text)


def _grid(table: ExtractedTable) -> list[list[CellRef]]:
    nrows = max(table.row_count, len(table.rows))
    ncols = max(table.column_count, max((len(r) for r in table.rows), default=0))
    grid = [
        [CellRef(table.table_id, table.page_number, r, c, "") for c in range(ncols)]
        for r in range(nrows)
    ]
    if table.cells:
        for cell in table.cells:
            for dr in range(cell.row_span):
                for dc in range(cell.col_span):
                    r, c = cell.row_index + dr, cell.column_index + dc
                    if r < nrows and c < ncols:
                        existing = grid[r][c].text
                        text = cell.text if not existing else f"{existing} {cell.text}".strip()
                        grid[r][c] = CellRef(
                            table.table_id, table.page_number, r, c, text, cell.bbox
                        )
    else:
        for r, row in enumerate(table.rows):
            for c, text in enumerate(row):
                grid[r][c] = CellRef(table.table_id, table.page_number, r, c, text)
    return grid


def _row_has_values(row: list[CellRef]) -> bool:
    return any(is_value_text(c.text) or is_date_text(c.text) for c in row[1:])


def _is_header_row(row: list[CellRef], index: int) -> bool:
    if _row_has_values(row):
        return False
    rest = " ".join(c.text for c in row[1:])
    if rest.strip():
        return bool(_HEADER_TOKEN_RE.search(rest))
    return index == 0 and looks_like_rate_class(row[0].text) if row else False


def classify_header_text(header: str) -> tuple[str, UnitParse | None, str | None]:
    text = " ".join(header.split())
    lowered = text.lower()
    if not text:
        return "empty", None, None
    compact = lowered.replace(" ", "")
    if "effective" in compact or compact == "date":  # Docling may split words: "Effectiv e"
        return "effective_date", None, None
    qualifier = None
    for q in ("baseline", "incremental"):
        # OCR/layout can split a word ("Incrementa l"); match letters ignoring spaces.
        spaced = r"\s*".join(q)
        if re.search(spaced, lowered):
            qualifier = q
            lowered = re.sub(spaced, "", lowered)
    lowered = re.sub(r"\(.*?\)", "", lowered).strip()
    parsed = parse_unit(lowered.replace(" /", "/").replace("/ ", "/"))
    if parsed.recognized:
        return "value", parsed, qualifier
    if qualifier:
        return "value", None, qualifier
    return "other", None, None


def table_view(table: ExtractedTable, *, header_rows: int | None = None) -> TableView:
    grid = _grid(table)
    if header_rows is None:
        header_rows = 0
        while header_rows < len(grid) and _is_header_row(grid[header_rows], header_rows):
            header_rows += 1
    ncols = len(grid[0]) if grid else 0
    columns: list[ColumnRole] = []
    warnings: list[str] = []
    for c in range(ncols):
        header = " ".join(grid[r][c].text for r in range(header_rows) if grid[r][c].text)
        if c == 0:
            columns.append(ColumnRole(0, "label", header))
            continue
        role, unit, qualifier = classify_header_text(header)
        body = [row[c].text for row in grid[header_rows:] if row[c].text]
        if role in ("empty", "other") and body:
            if all(is_date_text(t) for t in body):
                role = "effective_date"
            elif all(is_value_text(t) for t in body):
                role = "value"
                warnings.append(f"column {c} holds numbers but its header names no unit")
        columns.append(ColumnRole(c, role, header, unit, qualifier))
    header_label = " ".join(grid[r][0].text for r in range(header_rows) if grid[r][0].text)
    return TableView(table, grid, header_rows, columns, header_label.strip(), warnings)


def row_label(row: list[CellRef]) -> CellRef:
    return row[0]


def row_values(view: TableView, row: list[CellRef]) -> list[tuple[ColumnRole, Decimal, CellRef]]:
    out = []
    for role in view.value_columns:
        cell = row[role.index]
        number = parse_decimal(cell.text)
        if number is not None:
            out.append((role, number, cell))
    return out


def row_date(view: TableView, row: list[CellRef]) -> CellRef | None:
    for role in view.columns:
        if role.role == "effective_date" and is_date_text(row[role.index].text):
            return row[role.index]
    return None


def stray_values(view: TableView, row: list[CellRef]) -> list[CellRef]:
    """Numbers in columns that are neither value nor date columns."""
    value_cols = {c.index for c in view.columns if c.role in ("value", "effective_date", "label")}
    return [c for c in row if c.col not in value_cols and is_value_text(c.text)]


def iter_table_views(rep: DocumentRepresentation) -> Iterable[TableView]:
    for page in rep.pages:
        for table in sorted(page.tables, key=lambda t: t.metadata.get("reading_order", 0)):
            yield table_view(table)


def find_rate_tables(rep: DocumentRepresentation, *, basis: str | None = None) -> list[TableView]:
    """Tables with at least one unit-bearing value column (optionally of one basis)."""
    found = []
    for view in iter_table_views(rep):
        units = [c.unit for c in view.value_columns if c.unit is not None]
        if not units:
            continue
        if basis and not any(u.basis and u.basis.value == basis for u in units):
            continue
        found.append(view)
    return found
