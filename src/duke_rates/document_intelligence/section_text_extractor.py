"""Extract text for a section span from ncuc_page_artifacts.

A section is identified by ``(source_pdf, start_page, end_page)``. The
extractor concatenates per-page text rows for pages within that range,
joined by form-feed separators so downstream consumers can split back to
page boundaries if needed.

This module deliberately does not slice by ``section_index`` alone — sections
in ``document_sections`` are stored with both ``start_page`` and ``end_page``
and we honor those rather than re-deriving page ranges.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


PAGE_SEPARATOR = "\n\f\n"  # form feed between pages, easy to split back


@dataclass(frozen=True)
class SectionText:
    """Result of a section-text fetch.

    ``text`` is the concatenated page text (may be empty). ``page_count`` is
    how many pages contributed text. ``missing_pages`` are page numbers in
    the requested range that had no row in ``ncuc_page_artifacts``.
    """

    source_pdf: str
    start_page: int
    end_page: int
    text: str
    page_count: int
    missing_pages: tuple[int, ...]


def fetch_section_text(
    conn: sqlite3.Connection,
    source_pdf: str,
    start_page: int,
    end_page: int,
    *,
    max_chars: int | None = None,
) -> SectionText:
    """Fetch concatenated page text for ``[start_page, end_page]`` inclusive.

    Returns a :class:`SectionText`. If ``max_chars`` is provided the returned
    ``text`` is truncated to that length (counted in characters of the
    concatenated string).

    Pages are joined by ``PAGE_SEPARATOR``. The query reads from
    ``ncuc_page_artifacts``, using the most recent ``artifact_version`` per
    page when multiple exist.
    """
    if end_page < start_page:
        raise ValueError(
            f"end_page ({end_page}) must be >= start_page ({start_page})"
        )

    # Prefer the latest artifact_version per page in case multiple
    # extractions exist. We pick the row with the max created_at.
    cur = conn.execute(
        """
        SELECT page_number, text_content
        FROM (
            SELECT page_number, text_content,
                   ROW_NUMBER() OVER (
                       PARTITION BY page_number
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM ncuc_page_artifacts
            WHERE source_pdf = ?
              AND page_number >= ?
              AND page_number <= ?
        ) WHERE rn = 1
        ORDER BY page_number
        """,
        (source_pdf, start_page, end_page),
    )
    rows = cur.fetchall()
    cur.close()

    found_pages = {int(r[0]) for r in rows}
    requested = set(range(start_page, end_page + 1))
    missing = tuple(sorted(requested - found_pages))

    chunks: list[str] = []
    for page_num, text in rows:
        if text:
            chunks.append(text)

    full_text = PAGE_SEPARATOR.join(chunks)
    if max_chars is not None and len(full_text) > max_chars:
        full_text = full_text[:max_chars]

    return SectionText(
        source_pdf=source_pdf,
        start_page=start_page,
        end_page=end_page,
        text=full_text,
        page_count=len(rows),
        missing_pages=missing,
    )
