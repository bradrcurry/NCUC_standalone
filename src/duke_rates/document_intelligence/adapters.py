"""Adapters that turn existing backend output into a structured ``DocumentRepresentation``.

Three sources, one shape:

* :func:`representation_from_docling` -- a cached Docling document
  (``docling_artifacts.doc_json_content``). Keeps reading order, page numbers,
  text blocks, tables with cell spans, column-header flags and bounding boxes.
* :func:`representation_from_native_pdf` -- the PDF's text layer via
  pdfplumber. Text lines become blocks with real coordinates. Tables are
  rebuilt from word geometry, because Duke tariff tables have no ruling
  lines for pdfplumber's own detector to find.
* :func:`representation_from_ocr_text` -- an OCR text sidecar. Pages and
  paragraphs only; no coordinates exist, so none are invented
  (``coordinates_available=False``).

All coordinates are PDF points in a top-left origin
(``coordinate_space="pdf_top_left"``). Representations serialize to canonical
JSON; :func:`representation_fingerprint` hashes that JSON, and
:class:`RepresentationCache` stores it keyed by source hash, backend, backend
version and options, so a cached representation is reused only for the same
bytes and settings.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from duke_rates.document_intelligence.models import (
    BoundingBox,
    DocumentRepresentation,
    ExtractedTable,
    LayoutBlock,
    NormalizationBackend,
    NormalizationMetrics,
    PageRepresentation,
    TableCell,
)
from duke_rates.parse.canonical import parse_decimal

REPRESENTATION_VERSION = "2b.1"
TOP_LEFT = "pdf_top_left"


def _sha256_file(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _in_range(page: int, page_range: tuple[int, int] | None) -> bool:
    return page_range is None or page_range[0] <= page <= page_range[1]


# ---------------------------------------------------------------------------
# Docling
# ---------------------------------------------------------------------------


def _docling_bbox(raw: dict | None, page_height: float | None) -> BoundingBox | None:
    if not raw:
        return None
    left, right = float(raw["l"]), float(raw["r"])
    top, bottom = float(raw["t"]), float(raw["b"])
    if str(raw.get("coord_origin", "TOPLEFT")).upper().endswith("BOTTOMLEFT"):
        if page_height is None:
            return None  # cannot flip without the page height; do not guess
        top, bottom = page_height - top, page_height - bottom
    return BoundingBox(
        x0=left, y0=min(top, bottom), x1=right, y1=max(top, bottom), coordinate_space=TOP_LEFT
    )


def _resolve(doc: dict, ref: str) -> dict | None:
    parts = ref.lstrip("#/").split("/")
    if len(parts) != 2:
        return None
    collection, index = parts
    items = doc.get(collection)
    if not isinstance(items, list):
        return None
    try:
        return items[int(index)]
    except (ValueError, IndexError):
        return None


def _walk(doc: dict, node: dict, out: list[dict], seen: set[str]) -> None:
    for child in node.get("children") or []:
        ref = child.get("$ref") if isinstance(child, dict) else None
        if not ref or ref in seen:
            continue
        seen.add(ref)
        item = _resolve(doc, ref)
        if item is None:
            continue
        if ref.startswith("#/texts") or ref.startswith("#/tables"):
            out.append(item)
        _walk(doc, item, out, seen)


def _docling_table(item: dict, page_no: int, page_height: float | None) -> ExtractedTable:
    data = item.get("data") or {}
    cells: list[TableCell] = []
    for cell in data.get("table_cells") or []:
        cells.append(
            TableCell(
                text=str(cell.get("text") or "").strip(),
                row_index=int(cell.get("start_row_offset_idx", 0)),
                column_index=int(cell.get("start_col_offset_idx", 0)),
                row_span=int(cell.get("row_span") or 1),
                col_span=int(cell.get("col_span") or 1),
                is_column_header=bool(cell.get("column_header")),
                bbox=_docling_bbox(cell.get("bbox"), page_height),
                confidence=1.0,
            )
        )
    grid = data.get("grid") or []
    rows = [[str(c.get("text") or "").strip() for c in row] for row in grid]
    header_rows = 0
    for row in grid:
        if row and all(c.get("column_header") for c in row if (c.get("text") or "").strip()):
            header_rows += 1
        else:
            break
    prov = (item.get("prov") or [{}])[0]
    return ExtractedTable(
        page_number=page_no,
        table_id=f"docling:{item.get('self_ref')}",
        row_count=int(data.get("num_rows") or len(rows)),
        column_count=int(data.get("num_cols") or max((len(r) for r in rows), default=0)),
        headers=rows[0] if rows else [],
        rows=rows,
        cells=cells,
        bbox=_docling_bbox(prov.get("bbox"), page_height),
        confidence=1.0,
        metadata={"header_rows": header_rows, "source_ref": item.get("self_ref")},
    )


def representation_from_docling(
    doc_json: dict,
    *,
    source_pdf: str,
    file_sha256: str | None,
    backend_version: str | None,
    docling_package_version: str | None = None,
    page_range: tuple[int, int] | None = None,
    page_offset: int = 0,
) -> DocumentRepresentation:
    """Structured representation of a Docling document.

    ``page_offset`` is added to Docling's page numbers when the document was
    converted from an excerpt of the source PDF.
    """
    page_sizes = {
        int(k) + page_offset: (v.get("size") or {})
        for k, v in (doc_json.get("pages") or {}).items()
    }
    ordered: list[dict] = []
    seen: set[str] = set()
    _walk(doc_json, doc_json.get("body") or {}, ordered, seen)
    furniture: list[dict] = []
    _walk(doc_json, doc_json.get("furniture") or {}, furniture, seen)

    pages: dict[int, PageRepresentation] = {}
    order = 0
    for item, is_furniture in [(i, False) for i in ordered] + [(i, True) for i in furniture]:
        prov = (item.get("prov") or [{}])[0]
        if "page_no" not in prov:
            continue
        page_no = int(prov["page_no"]) + page_offset
        if not _in_range(page_no, page_range):
            continue
        size = page_sizes.get(page_no) or {}
        height = size.get("height")
        page = pages.setdefault(
            page_no,
            PageRepresentation(
                page_number=page_no,
                width=size.get("width"),
                height=height,
                source="docling",
                backend=NormalizationBackend.DOCLING,
            ),
        )
        ref = str(item.get("self_ref") or "")
        if ref.startswith("#/tables"):
            table = _docling_table(item, page_no, height)
            table.metadata["reading_order"] = order
            page.tables.append(table)
        else:
            page.blocks.append(
                LayoutBlock(
                    block_type=str(item.get("label") or "text"),
                    text=str(item.get("text") or ""),
                    page_number=page_no,
                    confidence=1.0,
                    bbox=_docling_bbox(prov.get("bbox"), height),
                    metadata={
                        "reading_order": order,
                        "source_ref": ref,
                        # Page headers/footers: Docling marks them by layer even
                        # when the body references them.
                        "furniture": is_furniture or item.get("content_layer") == "furniture",
                    },
                )
            )
        order += 1
    for page in pages.values():
        parts: list[tuple[int, str]] = [
            (b.metadata["reading_order"], b.text)
            for b in page.blocks
            if not b.metadata["furniture"]
        ]
        parts += [
            (t.metadata["reading_order"], "\n".join("\t".join(r) for r in t.rows))
            for t in page.tables
        ]
        parts += [
            (b.metadata["reading_order"], b.text) for b in page.blocks if b.metadata["furniture"]
        ]
        page.text = "\n".join(text for _, text in sorted(parts, key=lambda x: x[0]) if text)
    ordered_pages = [pages[k] for k in sorted(pages)]
    raw_text = "\f".join(p.text for p in ordered_pages)
    return DocumentRepresentation(
        source_pdf=source_pdf,
        file_hash=file_sha256,
        raw_text=raw_text,
        normalizer_backend=NormalizationBackend.DOCLING,
        pages=ordered_pages,
        normalization_metrics=NormalizationMetrics(
            backend=NormalizationBackend.DOCLING,
            page_count=len(ordered_pages),
            text_char_count=len(raw_text),
            table_page_count=sum(1 for p in ordered_pages if p.tables),
        ),
        document_metadata={
            "docling_package_version": docling_package_version,
            "page_range": list(page_range) if page_range else None,
            "page_offset": page_offset,
        },
        backend_version=backend_version,
        representation_version=REPRESENTATION_VERSION,
        coordinates_available=True,
    )


# ---------------------------------------------------------------------------
# Native PDF text layer
# ---------------------------------------------------------------------------

_DATE_CELL_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
_HEADER_TOKEN_RE = re.compile(
    r"\b(cents|dollars|kwh|/kw|kw\b|effective|date|baseline|incremental)", re.I
)
_LINE_TOLERANCE = 2.5
_CELL_GAP = 9.0  # points between words that separate cells
_COLUMN_TOLERANCE = 14.0


@dataclass
class _Cell:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    words: list | None = None

    @property
    def is_value(self) -> bool:
        return parse_decimal(self.text) is not None or bool(_DATE_CELL_RE.match(self.text))


def _lines_from_words(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - word["top"]) <= _LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _cells_from_line(line: list[dict]) -> list[_Cell]:
    cells: list[_Cell] = []
    for word in line:
        if cells and word["x0"] - cells[-1].x1 <= _CELL_GAP:
            prev = cells[-1]
            prev.text = f"{prev.text} {word['text']}"
            prev.x1 = word["x1"]
            prev.top = min(prev.top, word["top"])
            prev.bottom = max(prev.bottom, word["bottom"])
            prev.words.append(word)
        else:
            cells.append(
                _Cell(word["text"], word["x0"], word["x1"], word["top"], word["bottom"], [word])
            )
    return cells


def _is_short_label(cells: list[_Cell]) -> bool:
    return len(cells) == 1 and len(cells[0].text) < 90


def _is_wrapped_row_label(prev: list[_Cell], line: list[_Cell]) -> bool:
    """``line`` finishes the first-column label of the value-less multi-cell ``prev``.

    Both lines start at the same x, sit closer together than rows do, and fill
    different columns: ``prev`` carries text cells (e.g. schedules) left of the
    values, ``line`` carries the values.
    """
    if len(prev) < 2 or len(line) < 2 or any(c.is_value for c in prev) or line[0].is_value:
        return False
    values = [c for c in line[1:] if c.is_value]
    if not values or line[0].top - prev[0].bottom >= 4 or abs(line[0].x0 - prev[0].x0) > 3:
        return False
    if any(c.x1 > values[0].x0 for c in prev[1:]):
        return False
    return not any(a.x0 < b.x1 and b.x0 < a.x1 for a in prev[1:] for b in line[1:])


def _merge_wrapped_labels(lines: list[list[_Cell]]) -> list[list[_Cell]]:
    """Join a label-only line with the next line when that line continues it.

    "Fuel ... Experience Modification" + "Factor (EMF) 0.518 12/1/25" becomes
    one row. A continuation starts with "(" or a lowercase letter, or is a
    short label fragment directly under the previous line.
    """
    merged: list[list[_Cell]] = []
    for line in lines:
        prev = merged[-1] if merged else None
        if prev and _is_wrapped_row_label(prev, line):
            # "Seasonal and Intermittent | SI" + "Service | 0.00651": the class
            # name wraps inside its row while the schedule column sits on the
            # first line and the value on the second.
            head, first = prev[0], line[0]
            joined = _Cell(
                f"{head.text} {first.text}",
                min(head.x0, first.x0),
                max(head.x1, first.x1),
                head.top,
                first.bottom,
                [*(head.words or []), *(first.words or [])],
            )
            merged[-1] = [joined, *sorted([*prev[1:], *line[1:]], key=lambda c: c.x0)]
            continue
        if (
            prev
            and _is_short_label(prev)
            and line
            and not line[0].is_value
            and line[0].top - prev[0].bottom < 4
            and (
                line[0].text[:1] == "("
                or line[0].text[:1].islower()
                or (line[0].text.split()[0] in {"Factor", "Rate"})
            )
        ):
            head = prev[0]
            first = line[0]
            joined = _Cell(
                f"{head.text} {first.text}",
                min(head.x0, first.x0),
                max(head.x1, first.x1),
                head.top,
                first.bottom,
            )
            merged[-1] = [joined, *line[1:]]
        else:
            merged.append(line)
    return merged


def _geometry_tables(lines: list[list[_Cell]], page_number: int) -> list[ExtractedTable]:
    """Group aligned rows into tables.

    A row belongs to a table when it carries at least one value cell (number
    or date) right of its first cell, or is a header/label row directly above
    or between such rows. Columns are clustered on value-cell right edges
    (numbers are right-aligned); everything left of the first value column is
    the label column.
    """

    def is_row(cells: list[_Cell]) -> bool:
        return len(cells) >= 2 and any(c.is_value for c in cells[1:])

    def is_header(cells: list[_Cell]) -> bool:
        return bool(_HEADER_TOKEN_RE.search(" ".join(c.text for c in cells)))

    tables: list[ExtractedTable] = []
    i = 0
    while i < len(lines):
        if not is_row(lines[i]):
            i += 1
            continue
        start = i
        # Header lines and short label lines (e.g. a rate-class title, or a
        # section header such as "Annual Billing Adjustments Rider BA") sit
        # directly above the first value row.
        while (
            start > 0
            and i - start < 4
            and not is_row(lines[start - 1])
            and (is_header(lines[start - 1]) or _is_short_label(lines[start - 1]))
            and lines[start][0].top - lines[start - 1][-1].bottom < 8
        ):
            start -= 1
        end = i
        j = i + 1
        while j < len(lines):
            if is_row(lines[j]):
                end = j
            elif not (len(lines[j]) == 1 and j + 1 < len(lines) and is_row(lines[j + 1])):
                if not (is_header(lines[j]) and j + 1 < len(lines) and is_row(lines[j + 1])):
                    break
            j += 1
        block = lines[start : end + 1]
        value_cells = [c for row in block for c in row[1:] if c.is_value]
        if len(value_cells) >= 2:
            tables.append(_table_from_rows(block, page_number, len(tables) + 1))
        i = end + 1
    return tables


_HEADER_WORD_RE = re.compile(
    r"^(cents|dollars|/kwh|/kw|baseline|incremental|effective|date)$", re.I
)


def _table_from_rows(rows: list[list[_Cell]], page_number: int, ordinal: int) -> ExtractedTable:
    edges: list[float] = sorted(c.x1 for row in rows for c in row[1:] if c.is_value)
    # Column-header words also mark columns, so a column with no values on
    # this page (e.g. "dollars /kW" above a section that continues overleaf)
    # still gets its own position. Headers sit slightly left of right-aligned
    # numbers, hence the offset.
    first_value_x0 = min((c.x0 for row in rows for c in row[1:] if c.is_value), default=None)
    for row in rows:
        if any(c.is_value for c in row[1:]):
            continue
        for cell in row:
            for word in cell.words or []:
                if _HEADER_WORD_RE.match(word["text"]) and (
                    first_value_x0 is None or word["x0"] >= first_value_x0 - 30
                ):
                    edges.append(word["x1"] + 8)
    edges.sort()
    clusters: list[list[float]] = []
    for edge in edges:
        if clusters and edge - clusters[-1][-1] <= _COLUMN_TOLERANCE:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])
    centers = [median(c) for c in clusters]
    label_limit = min((min(c) for c in clusters), default=float("inf")) - 40
    ncols = 1 + len(centers)
    grid: list[list[str]] = []
    cells: list[TableCell] = []
    for r, row in enumerate(rows):
        out = [""] * ncols
        if not any(c.is_value for c in row[1:]) and centers:
            # Header rows: column headers ("cents", "dollars", "/kWh", "/kW")
            # sit close together, so place each word on its own column.
            row = (
                [
                    _Cell(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], [w])
                    for c in row
                    for w in (c.words or [])
                ]
                if row and row[0].x1 > label_limit
                else [row[0]]
                + [
                    _Cell(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], [w])
                    for c in row[1:]
                    for w in (c.words or [])
                ]
            )
        for cell in row:
            if cell.x1 <= label_limit or not centers:
                col = 0
            elif cell.is_value:
                col = 1 + min(range(len(centers)), key=lambda k: abs(centers[k] - cell.x1))
            else:
                # Header text is centred or left-aligned over right-aligned numbers.
                mid = (cell.x0 + cell.x1) / 2
                col = 1 + min(range(len(centers)), key=lambda k: abs(centers[k] - 12 - mid))
            out[col] = f"{out[col]} {cell.text}".strip()
            cells.append(
                TableCell(
                    text=cell.text,
                    row_index=r,
                    column_index=col,
                    bbox=BoundingBox(
                        x0=cell.x0,
                        y0=cell.top,
                        x1=cell.x1,
                        y1=cell.bottom,
                        coordinate_space=TOP_LEFT,
                    ),
                    confidence=0.8,
                    is_column_header=not any(c.is_value for c in row[1:])
                    and bool(_HEADER_TOKEN_RE.search(" ".join(c.text for c in row))),
                )
            )
        grid.append(out)
    header_rows = 0
    for row in rows:
        if any(c.is_value for c in row[1:]):
            break
        header_rows += 1
    x0 = min(c.x0 for row in rows for c in row)
    x1 = max(c.x1 for row in rows for c in row)
    return ExtractedTable(
        page_number=page_number,
        table_id=f"native:p{page_number}-t{ordinal}",
        row_count=len(grid),
        column_count=ncols,
        headers=grid[0] if grid else [],
        rows=grid,
        cells=cells,
        bbox=BoundingBox(
            x0=x0, y0=rows[0][0].top, x1=x1, y1=rows[-1][-1].bottom, coordinate_space=TOP_LEFT
        ),
        confidence=0.7,
        metadata={
            "header_rows": header_rows,
            "column_right_edges": centers,
            "table_backend": "native_geometry",
        },
    )


def representation_from_native_pdf(
    pdf_path: str | Path,
    *,
    page_range: tuple[int, int] | None = None,
    file_sha256: str | None = None,
) -> DocumentRepresentation:
    import importlib.metadata

    import pdfplumber

    pages: list[PageRepresentation] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        # Only the requested pages are parsed, and each page's layout cache is
        # released after use, so a span of a large filing stays bounded.
        first, last = page_range or (1, len(pdf.pages))
        for index in range(max(first, 1), min(last, len(pdf.pages)) + 1):
            page = pdf.pages[index - 1]
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            line_words = _lines_from_words(words)
            line_cells = _merge_wrapped_labels([_cells_from_line(line) for line in line_words])
            blocks = [
                LayoutBlock(
                    block_type="line",
                    text=" ".join(w["text"] for w in line),
                    page_number=index,
                    confidence=0.9,
                    bbox=BoundingBox(
                        x0=min(w["x0"] for w in line),
                        y0=min(w["top"] for w in line),
                        x1=max(w["x1"] for w in line),
                        y1=max(w["bottom"] for w in line),
                        coordinate_space=TOP_LEFT,
                    ),
                    metadata={"reading_order": n},
                )
                for n, line in enumerate(line_words)
            ]
            tables = _geometry_tables(line_cells, index)
            pages.append(
                PageRepresentation(
                    page_number=index,
                    text="\n".join(b.text for b in blocks),
                    width=float(page.width),
                    height=float(page.height),
                    source="pdfplumber",
                    backend=NormalizationBackend.NATIVE_PDF,
                    blocks=blocks,
                    tables=tables,
                )
            )
            page.close()
    raw_text = "\f".join(p.text for p in pages)
    return DocumentRepresentation(
        source_pdf=str(pdf_path),
        file_hash=file_sha256 or _sha256_file(pdf_path),
        raw_text=raw_text,
        normalizer_backend=NormalizationBackend.NATIVE_PDF,
        pages=pages,
        normalization_metrics=NormalizationMetrics(
            backend=NormalizationBackend.NATIVE_PDF,
            page_count=len(pages),
            text_char_count=len(raw_text),
            table_page_count=sum(1 for p in pages if p.tables),
        ),
        document_metadata={"page_range": list(page_range) if page_range else None},
        backend_version=f"pdfplumber-{importlib.metadata.version('pdfplumber')}+geometry-tables-1",
        representation_version=REPRESENTATION_VERSION,
        coordinates_available=True,
    )


# ---------------------------------------------------------------------------
# OCR text
# ---------------------------------------------------------------------------


def representation_from_ocr_text(
    text: str,
    *,
    source_pdf: str,
    file_sha256: str | None,
    ocr_backend: str,
    first_page: int = 1,
) -> DocumentRepresentation:
    """Pages split on form feeds; paragraphs as blocks. No coordinates."""
    pages: list[PageRepresentation] = []
    for offset, page_text in enumerate(text.split("\f")):
        page_number = first_page + offset
        blocks = [
            LayoutBlock(
                block_type="paragraph",
                text=chunk.strip(),
                page_number=page_number,
                confidence=0.5,
                metadata={"reading_order": n},
            )
            for n, chunk in enumerate(c for c in page_text.split("\n\n") if c.strip())
        ]
        pages.append(
            PageRepresentation(
                page_number=page_number,
                text=page_text,
                source=ocr_backend,
                backend=NormalizationBackend.UNKNOWN,
                blocks=blocks,
            )
        )
    return DocumentRepresentation(
        source_pdf=source_pdf,
        file_hash=file_sha256,
        raw_text=text,
        normalizer_backend=NormalizationBackend.UNKNOWN,
        pages=pages,
        normalization_metrics=NormalizationMetrics(
            page_count=len(pages), text_char_count=len(text)
        ),
        document_metadata={"ocr_backend": ocr_backend},
        backend_version=ocr_backend,
        representation_version=REPRESENTATION_VERSION,
        coordinates_available=False,
    )


# ---------------------------------------------------------------------------
# Serialization and cache
# ---------------------------------------------------------------------------

_VOLATILE_METRICS = ("elapsed_ms",)


def representation_to_json(rep: DocumentRepresentation) -> str:
    payload = rep.model_dump(mode="json")
    for key in _VOLATILE_METRICS:
        payload.get("normalization_metrics", {}).pop(key, None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def representation_from_json(text: str) -> DocumentRepresentation:
    return DocumentRepresentation.model_validate_json(text)


def representation_fingerprint(rep: DocumentRepresentation) -> str:
    return hashlib.sha256(representation_to_json(rep).encode("utf-8")).hexdigest()


def options_fingerprint(options: dict[str, Any] | None) -> str:
    return hashlib.sha256(
        json.dumps(options or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


class RepresentationCache:
    """Gzip JSON files keyed by (source SHA-256, backend, backend version, options)."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(
        self, file_sha256: str, backend: str, backend_version: str, options: dict | None
    ) -> Path:
        safe_version = re.sub(r"[^A-Za-z0-9._+-]", "_", backend_version)
        name = f"{file_sha256}.{backend}.{safe_version}.{options_fingerprint(options)}.json.gz"
        return self.directory / name

    def get(
        self, file_sha256: str, backend: str, backend_version: str, options: dict | None = None
    ) -> DocumentRepresentation | None:
        path = self._path(file_sha256, backend, backend_version, options)
        if not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            rep = representation_from_json(handle.read())
        return rep if rep.file_hash == file_sha256 else None

    def put(self, rep: DocumentRepresentation, backend: str, options: dict | None = None) -> Path:
        if not rep.file_hash or not rep.backend_version:
            raise ValueError(
                "only representations with a source hash and backend version are cached"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(rep.file_hash, backend, rep.backend_version, options)
        # mtime=0 keeps the gzip bytes reproducible.
        with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
            handle.write(representation_to_json(rep).encode("utf-8"))
        return path


def iter_tables(rep: DocumentRepresentation) -> Iterable[ExtractedTable]:
    for page in rep.pages:
        yield from sorted(page.tables, key=lambda t: t.metadata.get("reading_order", 0))
