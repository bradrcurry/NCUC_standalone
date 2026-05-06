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

    page_count = artifact.get("page_count", 0)
    if page_count <= 0:
        return [], {}

    # Try to load structured content from doc_json_content
    page_text_by_number: dict[int, list[tuple[float, str]]] = {}
    page_table_count: dict[int, int] = {}

    doc_json_str = artifact.get("doc_json_content")
    if doc_json_str:
        try:
            doc_json = json.loads(doc_json_str) if isinstance(doc_json_str, str) else doc_json_str

            # Group text items by page, storing (bbox.t, text) for sorting
            texts = doc_json.get("texts", [])
            for text_item in texts:
                prov = text_item.get("prov", [])
                if not prov:
                    continue
                page_no = prov[0].get("page_no")
                bbox = prov[0].get("bbox", {})
                if page_no is None:
                    continue

                t_coord = bbox.get("t", 0.0)
                text_str = text_item.get("text", "")

                if page_no not in page_text_by_number:
                    page_text_by_number[page_no] = []
                page_text_by_number[page_no].append((t_coord, text_str))

            # Count tables per page
            tables = doc_json.get("tables", [])
            for table_item in tables:
                prov = table_item.get("prov", [])
                if not prov:
                    continue
                page_no = prov[0].get("page_no")
                if page_no is not None:
                    page_table_count[page_no] = page_table_count.get(page_no, 0) + 1

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse doc_json_content: {e}. Falling back to plain_text_content.")
            doc_json_str = None

    # Build final page texts
    pages: list[PageEvidence] = []

    for page_num in range(1, page_count + 1):
        # Reconstruct page text from sorted (t_coord, text) pairs
        if page_num in page_text_by_number:
            # Sort by bbox.t descending — higher t = higher on page = read first
            text_items = sorted(
                page_text_by_number[page_num],
                key=lambda x: x[0],
                reverse=True
            )
            page_text = "\n".join(text[1] for text in text_items if text[1])
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
