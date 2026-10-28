"""Can a stored structured artifact serve a page range of the original PDF?

Structured extraction reads tables and blocks by page number and records that
number as provenance. Both are only correct when the artifact numbers its
pages the way the source PDF does. Some stored Docling artifacts do not:
``convert_pdf_safe``'s chunk fallback keeps only the last chunk's document,
numbered 1..N relative to that chunk, and records no offset. Such an artifact
looks like pages 1..N of the filing but holds pages from its end.

:func:`assess_docling_page_integrity` decides, from the artifact alone plus the
page count of the PDF on disk, whether the requested original-PDF pages can be
read from it. It never infers original numbers from chunk-relative ones: when
the mapping cannot be verified the artifact is ``unverifiable`` and must not
be used for structure or page provenance.

Pure functions; no database or file access.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

#: The artifact covers every requested page with verified original numbering.
VALID = "valid"
#: Verified numbering, but some requested pages are absent from the artifact.
PARTIAL = "partial"
#: Original page numbers cannot be established; never use for structure.
UNVERIFIABLE = "unverifiable"
#: No artifact, no pages, or none of the requested pages.
MISSING = "missing"


@dataclass(frozen=True)
class PageIntegrity:
    status: str
    reason: str | None
    requested: tuple[int, int] | None
    pdf_page_count: int | None
    artifact_page_count: int | None
    #: Pages the artifact holds, in original-PDF numbering (empty when unverifiable).
    covered_pages: tuple[int, ...] = ()
    #: Requested pages the artifact does not hold (only when numbering is verified).
    missing_pages: tuple[int, ...] = ()
    #: Offset added to the artifact's page numbers to get original numbers.
    page_offset: int = 0
    chunked: bool = False
    numbering: str = "unknown"  # absolute | offset | chunk_relative | ambiguous | unknown
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """True only when every requested page can be read with its original number."""
        return self.status == VALID

    @property
    def numbering_verified(self) -> bool:
        return self.numbering in ("absolute", "offset")

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["requested"] = list(self.requested) if self.requested else None
        out["covered_pages"] = _compact(self.covered_pages)
        out["missing_pages"] = _compact(self.missing_pages)
        return out


def _compact(pages: tuple[int, ...]) -> list[list[int]]:
    """[1, 2, 3, 7] -> [[1, 3], [7, 7]] so diagnostics stay small."""
    runs: list[list[int]] = []
    for page in pages:
        if runs and page == runs[-1][1] + 1:
            runs[-1][1] = page
        else:
            runs.append([page, page])
    return runs


def _metadata_dict(metadata: dict | str | None) -> dict:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _is_chunked(metadata: dict) -> bool:
    modes = metadata.get("degraded_modes") or metadata.get("_degraded_modes") or []
    return bool(metadata.get("used_chunking")) or "chunked" in modes


def _explicit_offset(metadata: dict) -> int | None:
    """A page offset the producer recorded, or None. Never derived."""
    value = metadata.get("page_offset")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _page_numbers(doc_json: dict) -> tuple[list[int], list[int], str | None]:
    """(page keys, prov page numbers, problem) as stored, before any offset."""
    keys: list[int] = []
    for key, entry in (doc_json.get("pages") or {}).items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            return [], [], "non_integer_page_id"
        inner = (entry or {}).get("page_no") if isinstance(entry, dict) else None
        if inner is not None and inner != number:
            return [], [], "conflicting_page_identifiers"
        keys.append(number)
    if len(keys) != len(set(keys)):
        return [], [], "duplicate_page_identifiers"
    prov: set[int] = set()
    for collection in ("texts", "tables", "pictures", "key_value_items", "form_items"):
        for item in doc_json.get(collection) or []:
            for p in item.get("prov") or []:
                page_no = p.get("page_no")
                if page_no is None:
                    continue
                if isinstance(page_no, bool) or not isinstance(page_no, int):
                    return [], [], "non_integer_page_id"
                prov.add(page_no)
    return sorted(keys), sorted(prov), None


def assess_docling_page_integrity(
    doc_json: dict | None,
    *,
    pdf_page_count: int | None,
    requested: tuple[int, int] | None,
    metadata: dict | str | None = None,
    artifact_page_count: int | None = None,
) -> PageIntegrity:
    """Whether ``doc_json`` can serve original-PDF pages ``requested``.

    ``pdf_page_count`` is the page count of the source PDF on disk (the caller
    has already matched the artifact to those bytes). ``requested`` is an
    inclusive 1-based range, or None for the whole document.
    ``artifact_page_count`` is the page count recorded with the artifact row.
    """

    def result(status, reason, **kw) -> PageIntegrity:
        return PageIntegrity(
            status=status,
            reason=reason,
            requested=requested,
            pdf_page_count=pdf_page_count,
            artifact_page_count=artifact_page_count,
            **kw,
        )

    meta = _metadata_dict(metadata)
    chunked = _is_chunked(meta)
    if not isinstance(doc_json, dict):
        return result(MISSING, "invalid_doc_json", chunked=chunked)
    if not pdf_page_count or pdf_page_count < 1:
        return result(UNVERIFIABLE, "pdf_page_count_unknown", chunked=chunked)
    if requested is not None:
        first, last = requested
        if first < 1 or last < first or last > pdf_page_count:
            return result(MISSING, "range_outside_document", chunked=chunked)
    if artifact_page_count and artifact_page_count != pdf_page_count:
        # Recorded for different bytes or a different page set than the PDF on disk.
        return result(UNVERIFIABLE, "page_count_mismatch", chunked=chunked)

    keys, prov, problem = _page_numbers(doc_json)
    if problem:
        return result(UNVERIFIABLE, problem, chunked=chunked)
    stored = sorted(set(keys) | set(prov))
    if not stored:
        return result(MISSING, "empty_document", chunked=chunked)
    if keys and not set(prov) <= set(keys):
        return result(UNVERIFIABLE, "prov_page_not_in_pages", chunked=chunked)

    offset = _explicit_offset(meta)
    full = list(range(1, pdf_page_count + 1))
    if offset is not None:
        numbering = "offset" if offset else "absolute"
    elif stored == full:
        # A complete, correctly numbered document, whether or not it was chunked.
        offset, numbering = 0, "absolute"
    elif chunked:
        # Chunk merge kept one chunk numbered from 1 and recorded no offset.
        return result(
            UNVERIFIABLE,
            "chunk_relative_numbering",
            chunked=True,
            numbering="chunk_relative",
            details={"stored_pages": _compact(tuple(stored))},
        )
    elif stored == list(range(1, len(stored) + 1)):
        # Pages 1..N of a longer PDF and no record of how it was produced: either
        # the first N pages or an unlabeled chunk. Not decidable from the artifact.
        return result(
            UNVERIFIABLE,
            "ambiguous_truncated_numbering",
            numbering="ambiguous",
            details={"stored_pages": _compact(tuple(stored))},
        )
    elif stored[-1] == pdf_page_count or meta.get("used_chunking") is False:
        # Gaps in a whole-document conversion: pages that failed are absent,
        # the rest keep their own numbers.
        offset, numbering = 0, "absolute"
    else:
        # Gaps, short of the last page, and no record that this was one
        # whole-document conversion.
        return result(
            UNVERIFIABLE,
            "ambiguous_truncated_numbering",
            numbering="ambiguous",
            details={"stored_pages": _compact(tuple(stored))},
        )

    covered = tuple(p + offset for p in stored)
    if covered[0] < 1 or covered[-1] > pdf_page_count:
        return result(UNVERIFIABLE, "page_out_of_bounds", chunked=chunked, numbering=numbering)
    first, last = requested or (1, pdf_page_count)
    wanted = range(first, last + 1)
    have = set(covered)
    missing = tuple(p for p in wanted if p not in have)
    common = dict(
        covered_pages=covered,
        missing_pages=missing,
        page_offset=offset,
        chunked=chunked,
        numbering=numbering,
    )
    if not missing:
        return result(VALID, None, **common)
    if len(missing) == len(wanted):
        return result(MISSING, "span_not_covered", **common)
    return result(PARTIAL, "span_partially_covered", **common)
