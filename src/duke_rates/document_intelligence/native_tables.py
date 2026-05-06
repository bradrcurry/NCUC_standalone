from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.models import ExtractedTable


@dataclass(slots=True)
class NativeTableExtractionResult:
    tables: list[ExtractedTable]
    backend: str | None = None
    metadata: dict[str, Any] | None = None


def extract_native_tables_for_page(
    *,
    source_pdf: str | Path,
    pdfplumber_page: Any,
    page_number: int,
) -> NativeTableExtractionResult:
    tables = _extract_with_pdfplumber(pdfplumber_page, page_number)
    if tables:
        return NativeTableExtractionResult(
            tables=tables,
            backend="pdfplumber",
            metadata={"table_count": len(tables)},
        )

    camelot_tables = _extract_with_camelot(source_pdf, page_number)
    if camelot_tables:
        return NativeTableExtractionResult(
            tables=camelot_tables,
            backend="camelot",
            metadata={"table_count": len(camelot_tables)},
        )

    return NativeTableExtractionResult(
        tables=[],
        backend=None,
        metadata={"table_count": 0},
    )


def _extract_with_pdfplumber(pdfplumber_page: Any, page_number: int) -> list[ExtractedTable]:
    extract_tables = getattr(pdfplumber_page, "extract_tables", None)
    if extract_tables is None:
        return []
    raw_tables = extract_tables() or []
    extracted: list[ExtractedTable] = []
    for raw_table in raw_tables:
        rows = [
            [str(cell).strip() if cell is not None else "" for cell in row]
            for row in (raw_table or [])
        ]
        rows = [row for row in rows if any(cell for cell in row)]
        if len(rows) < 2:
            continue
        headers = rows[0]
        body = rows[1:]
        column_count = max((len(row) for row in rows), default=0)
        extracted.append(
            ExtractedTable(
                page_number=page_number,
                row_count=len(body),
                column_count=column_count,
                headers=headers,
                rows=body,
                markdown=_rows_to_markdown(headers, body),
                confidence=0.75,
                metadata={"table_backend": "pdfplumber"},
            )
        )
    return extracted


def _extract_with_camelot(source_pdf: str | Path, page_number: int) -> list[ExtractedTable]:
    try:
        import camelot  # type: ignore
    except ImportError:
        return []

    extracted: list[ExtractedTable] = []
    try:
        tables = camelot.read_pdf(str(source_pdf), pages=str(page_number))
    except Exception:
        return []
    for table in tables:
        df = getattr(table, "df", None)
        if df is None or getattr(df, "empty", True):
            continue
        values = df.fillna("").values.tolist()
        rows = [[str(cell).strip() for cell in row] for row in values]
        rows = [row for row in rows if any(cell for cell in row)]
        if len(rows) < 2:
            continue
        headers = rows[0]
        body = rows[1:]
        extracted.append(
            ExtractedTable(
                page_number=page_number,
                row_count=len(body),
                column_count=max((len(row) for row in rows), default=0),
                headers=headers,
                rows=body,
                markdown=_rows_to_markdown(headers, body),
                confidence=0.82,
                metadata={"table_backend": "camelot"},
            )
        )
    return extracted


def _rows_to_markdown(headers: list[str], rows: list[list[str]]) -> str | None:
    if not headers:
        return None
    header_row = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body_rows = [
        "| " + " | ".join(row[: len(headers)] + [""] * max(0, len(headers) - len(row))) + " |"
        for row in rows
    ]
    return "\n".join([header_row, separator, *body_rows])
