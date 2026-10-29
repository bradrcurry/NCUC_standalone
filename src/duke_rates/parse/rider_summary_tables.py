"""Structured (table-aware) parser for "Summary of Rider Adjustments" sheets.

Reads the tables of a ``DocumentRepresentation`` (Docling or native geometry)
instead of flattened text, and returns the same ``RiderSummaryResult`` the
text and coordinate parsers return. Profiles can therefore arbitrate between
parses with ``checksum_score`` and emit charges with one code path.

What the table structure gives that text does not:

* the unit of every value comes from its column header ("cents /kWh",
  "dollars/kW", "Baseline cents/kWh"), not from a regex guess;
* effective dates come from the date column;
* every line item carries ``evidence``: page, table id, row, and the label,
  value and date cells with their printed text and bounding boxes.

Values in a column whose header names no unit are left unset and flagged
``unit_ambiguous``; nothing is assumed.
"""

from __future__ import annotations

import re
from decimal import Decimal

from duke_rates.document_intelligence.models import DocumentRepresentation
from duke_rates.document_intelligence.table_api import (
    CellRef,
    ColumnRole,
    TableView,
    classify_header_text,
    is_date_text,
    is_total_label,
    is_value_text,
    looks_like_rate_class,
    row_date,
    table_view,
)
from duke_rates.parse.canonical import Basis
from duke_rates.parse.rider_summary import (
    _BA_HEADER_RE,
    _REDLINE_DATE_RE,
    _REDLINE_TEXT_RE,
    _SKIP_LINE_RE,
    _SUBTOTAL_RE,
    _SUMMARY_TITLE_RE,
    _SUMMARY_TOTAL_RE,
    RiderLineItem,
    RiderRateClassBlock,
    RiderSummaryResult,
    _check_block_totals,
    _dedupe_rate_class_blocks,
    _extract_block_header,
    _extract_rider_code,
    _extract_summary_effective_date,
    _lookup_schedules,
)

PARSER_NAME = "rider_summary_tables"
PARSER_VERSION = "1"

_UNIT_IN_LABEL_RE = re.compile(r"\b(cents|dollars)\s*/\s*(kwh|kw)\b", re.I)


def _summary_pages(rep: DocumentRepresentation, warnings: list[str]) -> set[int]:
    """Pages that carry the summary; redline copies are dropped when a clean copy exists."""
    summary = {
        p.page_number
        for p in rep.pages
        if _SUMMARY_TITLE_RE.search(p.text)
        or _SUMMARY_TOTAL_RE.search(p.text)
        or any(is_total_label(r[0]) for t in p.tables for r in t.rows if r)
    }
    if not summary:
        return {p.page_number for p in rep.pages}
    redline = {
        p.page_number
        for p in rep.pages
        if p.page_number in summary
        and (_REDLINE_TEXT_RE.search(p.text) or len(_REDLINE_DATE_RE.findall(p.text)) >= 2)
    }
    keep = (summary - redline) or summary
    if redline and keep != summary:
        warnings.append(f"redline copy pages {sorted(redline)} excluded; clean copy kept")
    return keep


def _rate_class(header_label: str) -> tuple[str, list[str]]:
    label = " ".join(header_label.split())
    parsed = _extract_block_header(f"{label} cents/kWh")
    if parsed:
        return parsed
    name = label.rstrip("*").strip()
    return name, _lookup_schedules(name)


def _layout(columns: list[ColumnRole]) -> str:
    values = [c for c in columns if c.role == "value"]
    if any(c.qualifier == "baseline" for c in values) and any(
        c.qualifier == "incremental" for c in values
    ):
        return "baseline_incremental"
    bases = {c.unit.basis for c in values if c.unit is not None}
    if Basis.ENERGY in bases and Basis.DEMAND in bases:
        return "kwh_kw"
    if Basis.ENERGY in bases:
        return "kwh"
    if Basis.DEMAND in bases:
        return "kw"
    return "unknown"


def _cell_evidence(cell: CellRef, role: ColumnRole | None = None) -> dict:
    out = cell.as_evidence()
    if role is not None:
        out["role"] = role.role
        out["column_header"] = role.header
        out["printed_unit"] = role.header if role.unit else None
        out["qualifier"] = role.qualifier
    return out


def _cents(number: Decimal, role: ColumnRole) -> float:
    """Value in cents/kWh (the unit RiderLineItem stores) from a $/kWh-basis column."""
    return float(number * role.unit.factor * 100)


def _column_positions(view: TableView) -> dict[int, float]:
    """Median right edge of each column's cells (only when bboxes exist)."""
    from statistics import median

    edges: dict[int, list[float]] = {}
    for row in view.grid:
        for cell in row:
            if cell.bbox is not None and cell.text:
                edges.setdefault(cell.col, []).append(cell.bbox.x1)
    return {col: median(xs) for col, xs in edges.items()}


def _map_continuation(
    view: TableView, previous: list[ColumnRole], previous_pos: dict[int, float]
) -> list[ColumnRole] | None:
    """Columns for a header-less continuation table, matched by x position."""
    positions = _column_positions(view)
    if not positions or not previous_pos:
        return previous if len(view.columns) == len(previous) else None
    mapped = [previous[0]]
    for col in view.columns[1:]:
        x = positions.get(col.index)
        if x is None:
            mapped.append(ColumnRole(col.index, "empty", ""))
            continue
        nearest = min(
            (c for c in previous[1:] if c.index in previous_pos),
            key=lambda c: abs(previous_pos[c.index] - x),
            default=None,
        )
        if nearest is None or abs(previous_pos[nearest.index] - x) > 25:
            return None
        mapped.append(
            ColumnRole(col.index, nearest.role, nearest.header, nearest.unit, nearest.qualifier)
        )
    return mapped


class _EndOfPage:
    metadata = {"reading_order": float("inf")}


_END_OF_PAGE = _EndOfPage()


_LOOSE_TITLE_RE = re.compile(r"\b(schedules?|service|lighting)\**\s*$", re.I)


def _is_loose_title(text: str) -> bool:
    """A rate-class title standing alone ("Sports Field Lighting Schedule"),
    not a footer ("Effective for service rendered on and after ...")."""
    text = " ".join((text or "").split())
    return (
        0 < len(text) < 120
        and not text.startswith("*")
        and not _SKIP_LINE_RE.match(text)
        and bool(_LOOSE_TITLE_RE.search(text))
        and looks_like_rate_class(text)
    )


def _loose_header_before(page, table, last_order: int):
    """A rate-class title Docling left as a text block between two tables."""
    order = table.metadata.get("reading_order")
    if order is None:
        return None
    candidates = [
        b
        for b in page.blocks
        if last_order < b.metadata.get("reading_order", -1) < order
        and not b.metadata.get("furniture")
        and _is_loose_title(b.text)
    ]
    return candidates[-1] if candidates else None


def _preceding_text(page, table) -> str:
    """Text of the block just before the table in reading order (a header left outside it)."""
    order = table.metadata.get("reading_order")
    if order is None:
        return ""
    before = [
        b
        for b in page.blocks
        if b.metadata.get("reading_order", -1) < order and not b.metadata.get("furniture")
    ]
    if not before:
        return ""
    block = max(before, key=lambda b: b.metadata["reading_order"])
    return block.text if order - block.metadata["reading_order"] == 1 else ""


class _State:
    def __init__(self, rep: DocumentRepresentation, result: RiderSummaryResult) -> None:
        self.rep = rep
        self.result = result
        self.blocks: list[RiderRateClassBlock] = []
        self.block: RiderRateClassBlock | None = None
        self.columns: list[ColumnRole] | None = None
        self.positions: dict[int, float] = {}
        self.in_ba = False
        self.pending_loose = None

    def start(
        self,
        header_label: str,
        columns: list[ColumnRole],
        evidence: dict,
        *,
        ambiguous: bool = False,
    ) -> None:
        name, schedules = (
            _rate_class(header_label) if header_label else ("(unlabelled section)", [])
        )
        self.block = RiderRateClassBlock(
            rate_class=name,
            applicable_schedules=schedules,
            column_layout=_layout(columns),
            ambiguous_rate_class=ambiguous,
            evidence={"header": evidence, "totals": []},
        )
        self.blocks.append(self.block)
        self.columns = columns
        self.in_ba = False


_DECIMALS_RE = re.compile(r"\.(\d+)")


def _decimals(text: str) -> int | None:
    match = _DECIMALS_RE.search(text or "")
    return len(match.group(1)) if match else None


def column_precision(view: TableView, columns: list[ColumnRole]) -> dict[int, int]:
    """Dominant printed decimal places per value column (needs 3+ values to decide)."""
    from collections import Counter

    out: dict[int, int] = {}
    for role in columns:
        if role.role != "value":
            continue
        places = [d for row in view.body() if (d := _decimals(row[role.index].text)) is not None]
        if len(places) >= 3:
            ((common, count),) = Counter(places).most_common(1)
            if count * 2 > len(places):
                out[role.index] = common
    return out


def _assign(
    item: RiderLineItem,
    row: list[CellRef],
    columns: list[ColumnRole],
    block: RiderRateClassBlock,
    precision: dict[int, int] | None = None,
) -> list[dict]:
    cells: list[dict] = []
    for role in columns:
        if role.role != "value":
            continue
        cell = row[role.index]
        number = _number(cell.text)
        if number is None:
            continue
        cells.append(_cell_evidence(cell, role))
        expected_places = (precision or {}).get(role.index)
        places = _decimals(cell.text.split()[0] if cell.text else "")
        if expected_places is not None and places is not None and places > expected_places:
            # Redline copies fuse struck and inserted digits ("0.000" + "2" ->
            # "0.0002"); a value printed with more decimals than its column is
            # left unset for review instead of being trusted.
            item.unit_ambiguous = True
            block.warnings.append(
                f"value {cell.text!r} for {item.label!r} has {places} decimals; column prints "
                f"{expected_places} (possible fused redline digits); needs review"
            )
            continue
        if role.unit is None:
            item.unit_ambiguous = True
            block.warnings.append(
                f"unit ambiguous for {item.label!r}: column {role.index} header {role.header!r} names no unit"
            )
            continue
        if role.unit.basis == Basis.ENERGY:
            if role.qualifier == "incremental":
                item.incremental_cents_per_kwh = _cents(number, role)
            else:
                item.cents_per_kwh = _cents(number, role)
        elif role.unit.basis == Basis.DEMAND:
            item.dollars_per_kw = float(number * role.unit.factor)
        else:
            item.unit_ambiguous = True
            block.warnings.append(f"unexpected unit {role.unit.printed!r} for {item.label!r}")
    known = {c.index for c in columns if c.role in ("label", "value", "effective_date")}
    for cell in row:
        if cell.col not in known and is_value_text(cell.text):
            item.unit_ambiguous = True
            block.warnings.append(
                f"value {cell.text!r} for {item.label!r} is in an unclassified column"
            )
    return cells


_FUSED_VALUE_DATE_RE = re.compile(r"^\s*(\S+)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s*$")


def _number(text: str) -> Decimal | None:
    from duke_rates.parse.canonical import parse_decimal

    value = parse_decimal(text)
    if value is None:
        # Docling sometimes fuses a value with the date column: "0.92 12/1/25".
        fused = _FUSED_VALUE_DATE_RE.match(text or "")
        if fused:
            value = parse_decimal(fused.group(1))
    return value


def _fused_date(text: str) -> str | None:
    fused = _FUSED_VALUE_DATE_RE.match(text or "")
    return fused.group(2) if fused else None


def _header_columns(
    view: TableView,
    row_index: int,
    label_rows: list[list[CellRef]],
    current: list[ColumnRole] | None = None,
    warnings: list[str] | None = None,
) -> list[ColumnRole]:
    """Column roles from a header row found inside a table body.

    A header cell OCR left unreadable ("ccnts/k Vh") takes the role of the
    same column in ``current``, the header this table printed above it, and
    the substitution is recorded in ``warnings``.
    """
    row = view.grid[row_index]
    roles = [ColumnRole(0, "label", " ".join(r[0].text for r in label_rows + [row]))]
    above = {c.index: c for c in current or []}
    for cell in row[1:]:
        role, unit, qualifier = classify_header_text(cell.text)
        prior = above.get(cell.col)
        if role == "other" and prior is not None and prior.role in ("value", "effective_date"):
            role, unit, qualifier = prior.role, prior.unit, prior.qualifier
            if warnings is not None:
                warnings.append(
                    f"column header {cell.text!r} unreadable; role taken from {prior.header!r} "
                    "printed above it in the same table"
                )
        roles.append(ColumnRole(cell.col, role, cell.text, unit, qualifier))
    return roles


def _is_inline_header(row: list[CellRef]) -> bool:
    rest = [c.text for c in row[1:] if c.text]
    if not rest or any(is_value_text(t) or is_date_text(t) for t in rest):
        return False
    roles = [classify_header_text(t)[0] for t in rest]
    if all(r in ("value", "effective_date") for r in roles):
        return True
    # OCR-damaged header ("EfTeclivc", "ccnts/k\Vh"): a rate-class title with
    # at least one readable column header and no digits in the other cells.
    return (
        looks_like_rate_class(row[0].text)
        and any(r in ("value", "effective_date") for r in roles)
        and not any(ch.isdigit() for t in rest for ch in t)
    )


def _total(
    block: RiderRateClassBlock,
    label: str,
    values: list[tuple[ColumnRole, Decimal, CellRef]],
    evidence: list[dict],
) -> None:
    label_unit = _UNIT_IN_LABEL_RE.search(label)
    for role, number, cell in values:
        basis = role.unit.basis if role.unit else None
        if basis is None and label_unit:
            # "TOTAL cents/kWh" names its own unit; use it only for a lone value.
            basis = Basis.ENERGY if label_unit.group(2).lower() == "kwh" else Basis.DEMAND
            if len(values) != 1:
                block.warnings.append(f"TOTAL {label!r}: values in columns without units ignored")
                continue
            factor = Decimal("0.01") if label_unit.group(1).lower() == "cents" else Decimal(1)
        elif basis is None:
            block.warnings.append(f"TOTAL {label!r}: value {cell.text!r} has no unit; ignored")
            continue
        else:
            factor = role.unit.factor
        if basis == Basis.ENERGY:
            cents = float(number * factor * 100)
            if role.qualifier == "incremental":
                block.total_incremental_cents_per_kwh = cents
            else:
                block.total_cents_per_kwh = cents
        elif basis == Basis.DEMAND:
            block.total_dollars_per_kw = float(number * factor)
    block.evidence["totals"].append({"label": label, "cells": evidence})


def parse_rider_summary_from_representation(
    rep: DocumentRepresentation, *, leaf_no: str | None = None
) -> RiderSummaryResult:
    from duke_rates.parse.heuristics import extract_docket_footer, extract_supersedes

    text = rep.raw_text or "\f".join(p.text for p in rep.pages)
    docket_number, order_date = extract_docket_footer(text)
    result = RiderSummaryResult(
        source_pdf=rep.source_pdf,
        leaf_no=leaf_no,
        effective_date=_extract_summary_effective_date(text),
        docket_number=docket_number,
        order_date=order_date,
        supersedes=extract_supersedes(text),
    )
    pages = _summary_pages(rep, result.parse_warnings)
    state = _State(rep, result)
    backend = rep.normalizer_backend.value

    for page in rep.pages:
        if page.page_number not in pages:
            continue
        last_order = -1
        for table in sorted(page.tables, key=lambda t: t.metadata.get("reading_order", 0)):
            view = table_view(table)
            if not view.value_columns and not any(c.role == "effective_date" for c in view.columns):
                continue
            header_evidence = {
                "backend": backend,
                "page": page.page_number,
                "table_id": table.table_id,
                "cells": [
                    _cell_evidence(c) for r in view.grid[: view.header_rows] for c in r if c.text
                ],
            }
            loose = _loose_header_before(page, table, last_order)
            if loose is None and last_order == -1 and state.pending_loose is not None:
                # Title at the foot of the previous page, table at the top of this one.
                loose = state.pending_loose
            state.pending_loose = None
            last_order = table.metadata.get("reading_order", last_order)
            if not view.header_rows and loose and state.columns is not None:
                mapped = _map_continuation(view, state.columns, state.positions)
                if mapped is not None:
                    view.columns = mapped
                    state.start(
                        loose.text,
                        mapped,
                        {
                            "backend": backend,
                            "page": page.page_number,
                            "table_id": table.table_id,
                            "outside_header_text": loose.text,
                            "cells": [
                                {
                                    "text": loose.text,
                                    "page": loose.page_number,
                                    "bbox": loose.bbox.model_dump() if loose.bbox else None,
                                }
                            ],
                        },
                    )
                    state.block.warnings.append(
                        f"rate-class header {loose.text!r} is outside the table; columns taken from "
                        "the previous section's header"
                    )
            has_unit_header = bool(view.header_rows) and any(c.unit for c in view.value_columns)
            header_label = view.header_label
            if has_unit_header and not looks_like_rate_class(header_label):
                outside = _preceding_text(page, table)
                if looks_like_rate_class(outside):
                    header_label = f"{outside} {header_label}".strip()
                    header_evidence["outside_header_text"] = outside
            if view.header_rows and looks_like_rate_class(header_label):
                state.start(header_label, view.columns, header_evidence)
                state.positions = _column_positions(view)
            elif has_unit_header:
                # A fresh unit header always opens a new section. Without a
                # recognizable rate class it is kept apart, never merged into
                # the previous class.
                state.start(header_label, view.columns, header_evidence, ambiguous=True)
                state.positions = _column_positions(view)
                state.block.warnings.append(
                    f"table {table.table_id} has column headers but no rate class ({header_label!r})"
                )
            elif state.block is None:
                result.parse_warnings.append(
                    f"table {table.table_id} on page {page.page_number} has no rate-class header; skipped"
                )
                continue
            elif view.header_rows == 0 and state.columns is not None:
                # Page-spanning section: columns of the header it continues.
                mapped = _map_continuation(view, state.columns, state.positions)
                if mapped is not None:
                    view.columns = mapped
                else:
                    state.block.warnings.append(
                        f"continuation table {table.table_id} has {len(view.columns)} columns that do "
                        f"not line up with the {len(state.columns)} header columns; values ambiguous"
                    )
                    view.columns = [view.columns[0]] + [
                        ColumnRole(c.index, c.role, c.header, None, c.qualifier)
                        for c in view.columns[1:]
                    ]
            columns = view.columns
            precision = column_precision(view, columns)
            pending: list[list[CellRef]] = []
            for offset, row in enumerate(view.body()):
                row_index = view.header_rows + offset
                block = state.block
                label_cell = row[0]
                label = " ".join(label_cell.text.split())
                if _is_inline_header(row):
                    header_label = " ".join(r[0].text for r in pending + [row] if r[0].text)
                    substituted: list[str] = []
                    columns = _header_columns(view, row_index, pending, columns, substituted)
                    if looks_like_rate_class(header_label):
                        state.start(
                            header_label,
                            columns,
                            {
                                "backend": backend,
                                "page": page.page_number,
                                "table_id": table.table_id,
                                "cells": [
                                    _cell_evidence(c) for r in pending + [row] for c in r if c.text
                                ],
                            },
                        )
                        state.block.warnings.extend(substituted)
                    pending = []
                    continue
                values = [
                    (role, _number(row[role.index].text), row[role.index])
                    for role in columns
                    if role.role == "value" and _number(row[role.index].text) is not None
                ]
                if not values and not is_total_label(label):
                    if label:
                        pending.append(row)
                    continue
                for pending_row in pending:
                    plabel = " ".join(pending_row[0].text.split())
                    header_item = RiderLineItem(
                        label=plabel,
                        rider_code=_extract_rider_code(plabel),
                        is_section_header=True,
                        evidence={
                            "backend": backend,
                            "page": page.page_number,
                            "table_id": table.table_id,
                            "row": pending_row[0].row,
                            "label": _cell_evidence(pending_row[0]),
                        },
                    )
                    block.line_items.append(header_item)
                    if _BA_HEADER_RE.match(plabel):
                        state.in_ba = True
                pending = []
                evidence = {
                    "method": PARSER_NAME,
                    "backend": backend,
                    "backend_version": rep.backend_version,
                    "page": page.page_number,
                    "table_id": table.table_id,
                    "row": row_index,
                    "label": _cell_evidence(label_cell),
                }
                if is_total_label(label):
                    cells = [_cell_evidence(c, r) for r, _, c in values]
                    _total(block, label, values, cells)
                    block.line_items.append(
                        RiderLineItem(
                            label=label, is_total=True, evidence={**evidence, "values": cells}
                        )
                    )
                    state.in_ba = False
                    continue
                item = RiderLineItem(label=label, rider_code=_extract_rider_code(label))
                if not label:
                    item.unit_ambiguous = True
                    block.warnings.append(
                        f"values without a label in {table.table_id} row {row_index}"
                    )
                if _SUBTOTAL_RE.search(label):
                    item.is_subtotal = True
                    item.rider_code = item.rider_code or "BA"
                    state.in_ba = False
                elif state.in_ba:
                    item.indented = True
                cells = _assign(item, row, columns, block, precision)
                date_cell = (
                    row_date(view, row) if columns is view.columns else _date_in(row, columns)
                )
                if date_cell is not None:
                    item.effective_date = date_cell.text
                    evidence["date"] = _cell_evidence(date_cell)
                else:
                    for role in columns:
                        if role.role == "value" and _fused_date(row[role.index].text):
                            item.effective_date = _fused_date(row[role.index].text)
                            evidence["date"] = {
                                **_cell_evidence(row[role.index]),
                                "fused_with_value": True,
                            }
                            break
                evidence["values"] = cells
                item.evidence = evidence
                block.line_items.append(item)
            state.columns = columns
        # A rate-class title after the page's last table belongs to the next page's table.
        state.pending_loose = _loose_header_before(page, _END_OF_PAGE, last_order)

    for block in state.blocks:
        _check_block_totals(block)
    result.rate_classes = _dedupe_rate_class_blocks(state.blocks, result.parse_warnings)
    for block in result.rate_classes:
        result.parse_warnings.extend(f"{block.rate_class}: {w}" for w in block.warnings)
    return result


def _date_in(row: list[CellRef], columns: list[ColumnRole]) -> CellRef | None:
    for role in columns:
        if (
            role.role == "effective_date"
            and role.index < len(row)
            and is_date_text(row[role.index].text)
        ):
            return row[role.index]
    return None


def _class_key(block: RiderRateClassBlock) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", block.rate_class.lower().replace("*", "")).strip()
    return f"{name}|{','.join(block.applicable_schedules)}"


def _item_keys(items: list[RiderLineItem]) -> list[str]:
    """Match keys: rider code (plus occurrence) when known, else the normalized label.

    The two parsers wrap long labels differently ("... Modification" vs
    "... Modification Factor (EMF)"), so the code is the stable identity.
    """
    keys, seen = [], {}
    for item in items:
        if item.rider_code:
            base = f"code:{item.rider_code}|{'sub' if item.is_subtotal else 'row'}"
        else:
            base = "label:" + " ".join(re.sub(r"[^a-z0-9]+", " ", item.label.lower()).split())
        seen[base] = seen.get(base, 0) + 1
        keys.append(f"{base}#{seen[base]}")
    return keys


_EXACT_RESIDUAL = 1e-6


def block_residual(block: RiderRateClassBlock) -> float | None:
    """Largest |component sum - printed TOTAL| over the block's TOTALs, or None."""
    components = [
        li for li in block.line_items if not li.is_total and not li.is_section_header and not li.indented
    ]
    residuals = []
    for total, attr in (
        (block.total_cents_per_kwh, "cents_per_kwh"),
        (block.total_incremental_cents_per_kwh, "incremental_cents_per_kwh"),
        (block.total_dollars_per_kw, "dollars_per_kw"),
    ):
        values = [getattr(li, attr) for li in components if getattr(li, attr) is not None]
        if total is not None and values:
            residuals.append(abs(sum(values) - total))
    return max(residuals) if residuals else None


def _is_zero_row(item: RiderLineItem) -> bool:
    values = [v for v in _item_values(item) if v is not None]
    return bool(values) and all(v == 0 for v in values)


def _with_rows(block: RiderRateClassBlock, rows: list[RiderLineItem]) -> RiderRateClassBlock:
    """A copy of ``block`` with ``rows`` inserted before its TOTAL rows, re-checked."""
    from copy import deepcopy

    candidate = deepcopy(block)
    totals_at = next(
        (n for n, li in enumerate(candidate.line_items) if li.is_total),
        len(candidate.line_items),
    )
    candidate.line_items[totals_at:totals_at] = deepcopy(rows)
    _check_block_totals(candidate)
    return candidate


def _item_values(item: RiderLineItem) -> tuple:
    return tuple(
        None if v is None else round(v, 6)
        for v in (item.cents_per_kwh, item.dollars_per_kw, item.incremental_cents_per_kwh)
    )


def _merge_items(
    legacy: RiderRateClassBlock, structured: RiderRateClassBlock
) -> RiderRateClassBlock:
    """Legacy block enriched by the structured one, item by item.

    * same item, same values: keep it and attach the structured evidence;
    * same item, different values: keep the legacy values, flag for review and
      record both readings (structure never silently overrides);
    * items only the structured parse found are added, but only if the block
      still reconciles with them (otherwise they are reported, not added).
    """
    from copy import deepcopy

    merged = deepcopy(legacy)
    s_items = dict(zip(_item_keys(structured.line_items), structured.line_items, strict=True))
    seen = set()
    for key, item in zip(_item_keys(merged.line_items), merged.line_items, strict=True):
        seen.add(key)
        other = s_items.get(key)
        if other is None:
            continue
        if _item_values(item) == _item_values(other):
            item.evidence = other.evidence
        else:
            item.evidence = {
                "disagreement": {"legacy": _item_values(item), "structured": _item_values(other)},
                "structured_evidence": other.evidence,
            }
            merged.warnings.append(
                f"{item.label!r}: text parse {_item_values(item)} vs table parse "
                f"{_item_values(other)}; kept the text value, needs review"
            )
    # Only top-level rows can be vouched for by the printed TOTAL; sub-items
    # and section headers the text parse lacks are reported, not added.
    extra = [
        i
        for k, i in s_items.items()
        if k not in seen
        and not i.is_total
        and not i.is_section_header
        and not i.indented
        and not i.is_subtotal
        and (i.cents_per_kwh is not None or i.dollars_per_kw is not None)
    ]
    if extra:
        before = merged.checksum_ok
        candidate = _with_rows(merged, extra)
        after_residual = block_residual(candidate)
        # The printed TOTAL vouches for added rows only if it reconciles
        # because of them, or reconciles exactly with them. A block that
        # already reconciled within tolerance cannot vouch for a non-zero row
        # smaller than the tolerance (redline "0.0002" for a printed 0.002,
        # blk13). Rows printed as exactly zero leave the component sum, and
        # so every billed amount, unchanged; they are still added.
        if candidate.checksum_ok is True and (
            before is not True
            or (after_residual is not None and after_residual <= _EXACT_RESIDUAL)
        ):
            merged, unvouched = candidate, []
        else:
            zeros = [i for i in extra if _is_zero_row(i)] if before is True else []
            unvouched = [i for i in extra if i not in zeros]
            if zeros:
                merged = _with_rows(merged, zeros)
        if unvouched:
            reason = (
                "block would not reconcile"
                if candidate.checksum_ok is False
                else "printed TOTAL cannot vouch for them"
            )
            merged.warnings.append(
                f"table parse rows {[i.label for i in unvouched]} not added: {reason}; needs review"
            )
            merged.evidence = {
                **(merged.evidence or {}),
                "unvouched_table_rows": [
                    {"label": i.label, "values": _item_values(i), "evidence": i.evidence}
                    for i in unvouched
                ],
            }
    for attr in ("total_cents_per_kwh", "total_dollars_per_kw", "total_incremental_cents_per_kwh"):
        if getattr(merged, attr) is not None and getattr(merged, attr) == getattr(structured, attr):
            merged.evidence = {
                **(merged.evidence or {}),
                "totals": (structured.evidence or {}).get("totals", []),
            }
    if merged.evidence is None:
        merged.evidence = {"totals": [], "header": (structured.evidence or {}).get("header")}
    else:
        merged.evidence.setdefault("header", (structured.evidence or {}).get("header"))
        merged.evidence.setdefault("totals", [])
    return merged


def merge_with_legacy(
    structured: RiderSummaryResult, legacy: RiderSummaryResult
) -> RiderSummaryResult:
    """Combine a structured and a legacy parse, section by section.

    * Only one parse reconciles a section's printed TOTALs: use that parse.
    * Both reconcile, or neither does: merge item by item (``_merge_items``),
      starting from the legacy block, so a value the parses disagree on keeps
      its legacy reading and is flagged for review.
    * Only one parse found the section: keep it, flagged if it does not
      reconcile.

    Every block records the decision in ``evidence['selected_from']``.
    """
    merged = RiderSummaryResult(
        source_pdf=structured.source_pdf or legacy.source_pdf,
        leaf_no=structured.leaf_no or legacy.leaf_no,
        effective_date=structured.effective_date or legacy.effective_date,
        docket_number=structured.docket_number or legacy.docket_number,
        order_date=structured.order_date or legacy.order_date,
        supersedes=structured.supersedes or legacy.supersedes,
        parse_warnings=list(structured.parse_warnings),
    )
    s_blocks = {_class_key(b): b for b in structured.rate_classes if not b.ambiguous_rate_class}
    l_blocks = {_class_key(b): b for b in legacy.rate_classes if not b.ambiguous_rate_class}
    order = [_class_key(b) for b in legacy.rate_classes if not b.ambiguous_rate_class]
    order += [k for k in s_blocks if k not in l_blocks]
    for key in order:
        s_block, l_block = s_blocks.get(key), l_blocks.get(key)
        if s_block and l_block:
            s_ok, l_ok = s_block.checksum_ok is True, l_block.checksum_ok is True
            if s_ok and not l_ok:
                chosen, source = s_block, "structured"
                _fill_missing_totals(chosen, l_block)
            elif l_ok and not s_ok:
                chosen, source = _merge_items(l_block, s_block), "legacy_with_table_evidence"
            else:
                chosen = _merge_items(l_block, s_block)
                source = "item_merge" if l_ok else "item_merge_unreconciled"
                if not l_ok:
                    chosen.warnings.append("neither parse reconciles this section; needs review")
        else:
            chosen = s_block or l_block
            source = "structured_only" if s_block else "legacy_only"
            if chosen.checksum_ok is False:
                source += "_unreconciled"
        chosen.evidence = {**(chosen.evidence or {}), "selected_from": source}
        merged.rate_classes.append(chosen)
    for block in structured.rate_classes + legacy.rate_classes:
        if block.ambiguous_rate_class:
            merged.parse_warnings.append(f"ambiguous section {block.rate_class!r} not merged")
    for block in merged.rate_classes:
        merged.parse_warnings.extend(
            f"{block.rate_class}: {w}"
            for w in block.warnings
            if "needs review" in w or "not added" in w
        )
    return merged


def _fill_missing_totals(block: RiderRateClassBlock, legacy: RiderRateClassBlock) -> None:
    components = [
        li
        for li in block.line_items
        if not li.is_total and not li.is_section_header and not li.indented
    ]
    for attr, item_attr in (
        ("total_dollars_per_kw", "dollars_per_kw"),
        ("total_cents_per_kwh", "cents_per_kwh"),
    ):
        if getattr(block, attr) is not None or getattr(legacy, attr) is None:
            continue
        values = [getattr(li, item_attr) for li in components if getattr(li, item_attr) is not None]
        if values and abs(sum(values) - getattr(legacy, attr)) <= 0.002:
            setattr(block, attr, getattr(legacy, attr))
            block.evidence.setdefault("totals", []).append(
                {"label": attr, "from": "legacy_parse", "verified_by_component_sum": True}
            )
