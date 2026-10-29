"""Bounded structured representations for large filings (Phase 2C).

Converting a whole compliance filing with Docling (e.g. the 110-page JAA
docket, hd 5324) exhausts memory, and ``docling_backend`` keeps only the last
chunk's Docling document when it falls back to chunking, so tables and page
numbers of earlier chunks are lost. This module converts only a document's
tariff span (``historical_documents.start_page``/``end_page``, or a span from
``ncuc_span_artifacts``), a few pages at a time:

* each chunk is cut from the source PDF with PyMuPDF (no page images), then
  converted with the existing Docling backend;
* each chunk's Docling document becomes a ``DocumentRepresentation`` with
  ``page_offset`` so pages keep their numbers in the original filing, and
  tables stay on the page they were found on (they are never stitched across
  chunk boundaries);
* a chunk Docling cannot convert falls back to the native text layer for its
  pages, and the result is marked degraded with the failed ranges, so a
  partial structured parse is never reported as complete.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.models import DocumentRepresentation

logger = logging.getLogger(__name__)

#: Converter: path of a (chunk) PDF -> Docling result dict with "document"
#: (and optionally "backend_version"), or None on failure.
Converter = Callable[[str], "dict | None"]


@dataclass
class BoundedResult:
    representation: DocumentRepresentation | None
    page_range: tuple[int, int]
    chunks: list[tuple[int, int]] = field(default_factory=list)
    degraded_chunks: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    peak_rss_mb: float | None = None

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_chunks)

    def as_metadata(self) -> dict[str, Any]:
        return {
            "page_range": list(self.page_range),
            "chunks": [list(c) for c in self.chunks],
            "degraded": self.degraded,
            "degraded_chunks": self.degraded_chunks,
            "seconds": round(self.seconds, 2),
            "peak_rss_mb": self.peak_rss_mb,
        }


def peak_rss_mb() -> float | None:
    """Peak resident memory of this process so far (MB), where the OS reports it."""
    try:
        import psutil

        info = psutil.Process().memory_info()
        peak = getattr(info, "peak_wset", None) or getattr(info, "rss", None)
        return round(peak / 2**20, 1) if peak else None
    except Exception:
        try:
            import resource

            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        except Exception:
            return None


def page_count(pdf_path: str | Path) -> int:
    import fitz

    with fitz.open(str(pdf_path)) as doc:
        return doc.page_count


def chunk_ranges(page_range: tuple[int, int], chunk_pages: int) -> list[tuple[int, int]]:
    start, end = page_range
    return [(s, min(s + chunk_pages - 1, end)) for s in range(start, end + 1, chunk_pages)]


def _excerpt(pdf_path: str | Path, start: int, end: int) -> str:
    """Write pages start..end (1-based, inclusive) to a temporary PDF; return its path."""
    import fitz

    handle = tempfile.NamedTemporaryFile(suffix=f"_p{start}-{end}.pdf", delete=False)
    handle.close()
    with fitz.open(str(pdf_path)) as src, fitz.open() as out:
        out.insert_pdf(src, from_page=start - 1, to_page=end - 1)
        out.save(handle.name)
    return handle.name


def default_converter(accelerator: str = "cpu") -> Converter:
    """The existing Docling backend (standard pipeline, no page images)."""

    def convert(path: str) -> dict | None:
        from duke_rates.historical.ncuc.pipeline.docling_backend import convert_pdf_with_docling

        return convert_pdf_with_docling(path, accelerator=accelerator, force=True)

    return convert


def merge_representations(
    parts: list[DocumentRepresentation], *, source_pdf: str, file_sha256: str | None
) -> DocumentRepresentation | None:
    """Concatenate chunk representations in page order (pages are disjoint)."""
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    pages = sorted((pg for p in parts for pg in p.pages), key=lambda pg: pg.page_number)
    first = parts[0]
    backends = {p.normalizer_backend for p in parts}
    merged = first.model_copy(
        update={
            "source_pdf": source_pdf,
            "file_hash": file_sha256 or first.file_hash,
            "pages": pages,
            "page_start": pages[0].page_number if pages else None,
            "page_end": pages[-1].page_number if pages else None,
            "raw_text": "\f".join(pg.text for pg in pages),
            "warnings": [w for p in parts for w in p.warnings],
            "coordinates_available": all(p.coordinates_available for p in parts),
            "document_metadata": {
                **first.document_metadata,
                "bounded_parts": [
                    {"backend": p.normalizer_backend.value, "pages": [p.page_start, p.page_end]}
                    for p in parts
                ],
                "mixed_backends": len(backends) > 1,
            },
        }
    )
    return merged


def bounded_representation(
    pdf_path: str | Path,
    page_range: tuple[int, int] | None = None,
    *,
    chunk_pages: int = 8,
    converter: Converter | None = None,
    native_fallback: bool = True,
    file_sha256: str | None = None,
) -> BoundedResult:
    """Structured representation of ``page_range`` only, converted chunk by chunk.

    ``page_range`` defaults to the whole document (still chunked). Pages keep
    their numbering in the original PDF.
    """
    from duke_rates.document_intelligence.adapters import (
        representation_from_docling,
        representation_from_native_pdf,
    )

    started = time.perf_counter()
    total = page_count(pdf_path)
    start, end = page_range or (1, total)
    start, end = max(1, start), min(end, total)
    if start > end:
        raise ValueError(f"page range {page_range} is outside 1-{total}")
    convert = converter or default_converter()
    result = BoundedResult(representation=None, page_range=(start, end))
    parts: list[DocumentRepresentation] = []
    for c_start, c_end in chunk_ranges((start, end), chunk_pages):
        result.chunks.append((c_start, c_end))
        excerpt = _excerpt(pdf_path, c_start, c_end)
        reason = None
        try:
            converted = convert(excerpt)
            document = (converted or {}).get("document")
            if not document:
                reason = "docling returned no document"
            else:
                parts.append(
                    representation_from_docling(
                        document,
                        source_pdf=str(pdf_path),
                        file_sha256=file_sha256,
                        backend_version=(converted or {}).get("backend_version"),
                        page_offset=c_start - 1,
                    )
                )
        except MemoryError:
            reason = "out of memory"
        except Exception as exc:  # conversion failure is recorded, not raised
            reason = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            try:
                os.unlink(excerpt)
            except OSError:
                pass
        if reason is not None:
            entry: dict[str, Any] = {"pages": [c_start, c_end], "reason": reason, "fallback": None}
            if native_fallback:
                try:
                    parts.append(
                        representation_from_native_pdf(
                            pdf_path, page_range=(c_start, c_end), file_sha256=file_sha256
                        )
                    )
                    entry["fallback"] = "native"
                except Exception as exc:
                    entry["fallback_error"] = str(exc)[:200]
            logger.warning("bounded conversion of %s pages %s-%s degraded: %s", pdf_path, c_start, c_end, reason)
            result.degraded_chunks.append(entry)
    result.representation = merge_representations(
        parts, source_pdf=str(pdf_path), file_sha256=file_sha256
    )
    result.seconds = time.perf_counter() - started
    result.peak_rss_mb = peak_rss_mb()
    if result.representation is not None:
        result.representation.document_metadata["bounded"] = result.as_metadata()
    return result
