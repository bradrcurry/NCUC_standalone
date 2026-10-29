"""
Page miner for Docling artifacts.

Reconstructs PageEvidence from stored Docling JSON without re-running Docling.
Uses the existing text-based feature extractors from page_miner.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from duke_rates.historical.ncuc.pipeline.docling_backend import (
    ACCELERATOR_CPU,
    DOCLING_BACKEND_VERSION,
)
from duke_rates.historical.ncuc.pipeline.page_miner import (
    _extract_page_features_from_text,
)
from duke_rates.db.artifact_cache import load_docling_artifact
from duke_rates.models.pipeline import PageEvidence

logger = logging.getLogger(__name__)


def _build_docling_page_metadata(
    artifact: dict,
    page_table_count: dict[int, int],
) -> dict:
    """Build metadata dict to attach to page artifacts when saving."""
    accelerator = artifact.get("accelerator", ACCELERATOR_CPU)
    pipeline = artifact.get("pipeline", "standard")

    return {
        "source_backend": "docling",
        "docling_pipeline": pipeline,
        "docling_accelerator": accelerator,
        "docling_table_count_per_page": page_table_count,
    }


def _ref_target(ref_obj: dict | None) -> tuple[str, int] | None:
    """Parse a Docling ``{"$ref": "#/<kind>/<idx>"}`` into ``(kind, idx)``."""
    ref = (ref_obj or {}).get("$ref", "")
    parts = ref.split("/")
    if len(parts) != 3 or parts[0] != "#":
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def _reading_order_refs(doc_json: dict) -> list[tuple[str, int]]:
    """Return ``(kind, idx)`` for texts/tables in ``body`` reading order.

    Descends into groups (lists, sections) so nested items keep their order.
    """
    groups = doc_json.get("groups") or []
    ordered: list[tuple[str, int]] = []
    seen_groups: set[int] = set()

    def walk(children: list) -> None:
        for child in children or []:
            target = _ref_target(child)
            if target is None:
                continue
            kind, idx = target
            if kind == "groups":
                if idx in seen_groups or not 0 <= idx < len(groups):
                    continue
                seen_groups.add(idx)
                walk(groups[idx].get("children") or [])
            elif kind in ("texts", "tables"):
                ordered.append(target)

    walk((doc_json.get("body") or {}).get("children") or [])
    return ordered


def _render_table_rows(table_item: dict) -> list[str]:
    """Render a Docling table grid as one line per row, cells space-separated.

    Spanned cells repeat in Docling's grid; each cell is emitted once, at its
    starting row and column, so values are not duplicated.  Single-space joins
    match how ``normalize_docling_markdown`` flattens tables for the profiles.
    """
    grid = (table_item.get("data") or {}).get("grid") or []
    lines: list[str] = []
    emitted: set[tuple] = set()
    for row_idx, row in enumerate(grid):
        cells: list[str] = []
        for col_idx, cell in enumerate(row):
            start_row = cell.get("start_row_offset_idx", row_idx)
            start_col = cell.get("start_col_offset_idx", col_idx)
            key = (start_row, start_col, cell.get("end_row_offset_idx"), cell.get("end_col_offset_idx"))
            if key in emitted:
                continue
            emitted.add(key)
            text = " ".join(str(cell.get("text") or "").split())
            if text:
                cells.append(text)
        if cells:
            lines.append(" ".join(cells))
    return lines


def _collect_page_blocks(doc_json: dict) -> dict[int, list[str]]:
    """Group the document's text and table content by page in reading order.

    Items reachable from ``body`` keep Docling's reading order (which already
    handles multi-column layouts).  Items outside it -- furniture such as page
    headers/footers, or orphans Docling left out of ``body.children`` -- are
    placed by position: above the page's first body item goes first, the rest
    after the body content.  Text items parented to a table are skipped because
    the table rows already carry their content.
    """
    texts = doc_json.get("texts") or []
    tables = doc_json.get("tables") or []

    def page_and_top(item: dict) -> tuple[int | None, float]:
        prov = item.get("prov") or []
        if not prov:
            return None, 0.0
        return prov[0].get("page_no"), float((prov[0].get("bbox") or {}).get("t", 0.0))

    def render(kind: str, idx: int) -> list[str]:
        if kind == "tables":
            return _render_table_rows(tables[idx])
        text = texts[idx].get("text") or ""
        return [text] if text else []

    def is_table_child(item: dict) -> bool:
        target = _ref_target(item.get("parent"))
        return target is not None and target[0] == "tables"

    body_order = [
        (kind, idx)
        for kind, idx in _reading_order_refs(doc_json)
        if 0 <= idx < len(texts if kind == "texts" else tables)
    ]
    in_body = set(body_order)

    body_by_page: dict[int, list[tuple[float, list[str]]]] = {}
    for kind, idx in body_order:
        item = texts[idx] if kind == "texts" else tables[idx]
        if kind == "texts" and is_table_child(item):
            continue
        page_no, top = page_and_top(item)
        if page_no is not None:
            body_by_page.setdefault(page_no, []).append((top, render(kind, idx)))

    extra_by_page: dict[int, list[tuple[float, list[str]]]] = {}
    for kind, items in (("texts", texts), ("tables", tables)):
        for idx, item in enumerate(items):
            if (kind, idx) in in_body or (kind == "texts" and is_table_child(item)):
                continue
            page_no, top = page_and_top(item)
            if page_no is not None:
                extra_by_page.setdefault(page_no, []).append((top, render(kind, idx)))

    blocks: dict[int, list[str]] = {}
    for page_no in set(body_by_page) | set(extra_by_page):
        body = body_by_page.get(page_no, [])
        # Higher bbox.t = higher on the page (BOTTOMLEFT origin).
        extras = sorted(extra_by_page.get(page_no, []), key=lambda x: x[0], reverse=True)
        body_top = max((top for top, _ in body), default=float("-inf"))
        above = [lines for top, lines in extras if top > body_top]
        below = [lines for top, lines in extras if top <= body_top]
        ordered = above + [lines for _, lines in body] + below
        blocks[page_no] = [line for lines in ordered for line in lines]
    return blocks


def _normalize_artifact_keys(artifact: dict) -> dict:
    """Accept both artifact shapes produced in this codebase.

    DB rows / ``load_docling_artifact`` use ``doc_json_content`` and
    ``plain_text_content``; ``convert_pdf_with_docling`` returns ``document``
    and ``plain_text``.  Prefer the DB names and fill them from the others.
    """
    if artifact.get("doc_json_content") or artifact.get("plain_text_content"):
        return artifact
    if "document" not in artifact and "plain_text" not in artifact:
        return artifact
    normalized = dict(artifact)
    normalized["doc_json_content"] = artifact.get("document")
    normalized["plain_text_content"] = artifact.get("plain_text") or ""
    return normalized


def mine_pages_from_docling_artifact(artifact: dict) -> tuple[list[PageEvidence], dict]:
    """
    Reconstruct page-level evidence from a stored Docling artifact dict.

    Takes a Docling artifact dict (as returned by load_docling_artifact or
    convert_pdf_with_docling) and reconstructs PageEvidence objects using
    the existing text-based feature extraction.

    Args:
        artifact: Dict with keys:
            - doc_json_content (str | None): Full Docling DoclingDocument JSON
            - plain_text_content (str | None): Fallback plain text
            - tables_json_content (str | None): Per-table metadata
            - page_count (int): Total page count
            - accelerator (str): "cpu" or "cuda"
            - pipeline (str): "standard" or "vlm"

    Returns:
        tuple of (list[PageEvidence], metadata_dict).
        PageEvidence list is ready to feed into segment_document().
        Each page has reconstructed text, all text-based features,
        and table_like_density boosted if Docling found tables.
        metadata_dict is passed to save_page_artifacts() as the metadata param.
    """
    if not artifact:
        return [], {}
    artifact = _normalize_artifact_keys(artifact)

    page_count = artifact.get("page_count", 0)
    if page_count <= 0:
        return [], {}

    # Try to load structured content from doc_json_content
    page_lines_by_number: dict[int, list[str]] = {}
    page_table_count: dict[int, int] = {}

    doc_json_str = artifact.get("doc_json_content")
    if doc_json_str:
        try:
            doc_json = json.loads(doc_json_str) if isinstance(doc_json_str, str) else doc_json_str

            # Texts and table rows per page, in reading order (D1: table cells
            # used to be dropped here, losing nearly every rate value).
            page_lines_by_number = _collect_page_blocks(doc_json)

            # Count tables per page
            tables = doc_json.get("tables", [])
            for table_item in tables:
                prov = table_item.get("prov", [])
                if not prov:
                    continue
                page_no = prov[0].get("page_no")
                if page_no is not None:
                    page_table_count[page_no] = page_table_count.get(page_no, 0) + 1

        except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as e:
            logger.warning(f"Failed to parse doc_json_content: {e}. Falling back to plain_text_content.")
            doc_json_str = None
            page_lines_by_number = {}
            page_table_count = {}

    # Build final page texts
    pages: list[PageEvidence] = []

    for page_num in range(1, page_count + 1):
        if page_num in page_lines_by_number:
            page_text = "\n".join(line for line in page_lines_by_number[page_num] if line)
        else:
            # Fallback: split plain_text_content evenly across pages
            plain_text = artifact.get("plain_text_content", "")
            if plain_text and doc_json_str is None:
                lines = plain_text.split("\n")
                lines_per_page = max(1, len(lines) // page_count)
                start_line = (page_num - 1) * lines_per_page
                end_line = start_line + lines_per_page if page_num < page_count else len(lines)
                page_text = "\n".join(lines[start_line:end_line])
            else:
                page_text = ""

        # Extract features from reconstructed text
        evidence = _extract_page_features_from_text(page_text, page_num)

        # Boost table_like_density if Docling found tables on this page
        table_count_on_page = page_table_count.get(page_num, 0)
        if table_count_on_page > 0:
            # Apply a modest boost: max with a baseline, then multiply by factor
            evidence.table_like_density = max(evidence.table_like_density, 0.15) * 1.5

        pages.append(evidence)

    metadata = _build_docling_page_metadata(artifact, page_table_count)
    return pages, metadata


def mine_pages_from_docling_db(
    conn: sqlite3.Connection,
    *,
    source_pdf: str,
    file_hash: Optional[str] = None,
    backend_version: str = DOCLING_BACKEND_VERSION,
    accelerator: str = ACCELERATOR_CPU,
) -> Optional[tuple[list[PageEvidence], dict]]:
    """
    Load a Docling artifact from DB and reconstruct PageEvidence.

    Args:
        conn: SQLite connection
        source_pdf: Path to source PDF
        file_hash: File hash (optional, for validation)
        backend_version: Docling backend version
        accelerator: "cpu" or "cuda"

    Returns:
        tuple of (list[PageEvidence], metadata_dict) if found and valid, else None
    """
    artifact = load_docling_artifact(
        conn,
        source_pdf=source_pdf,
        file_hash=file_hash,
        backend_version=backend_version,
        accelerator=accelerator,
    )

    if not artifact:
        return None

    try:
        return mine_pages_from_docling_artifact(artifact)
    except Exception as e:
        logger.error(f"Error mining pages from Docling artifact for {source_pdf}: {e}")
        return None
