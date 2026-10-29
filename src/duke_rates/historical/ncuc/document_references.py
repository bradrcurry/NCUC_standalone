"""Own-page document references; portal filing identity is kept separate.

A printed docket is evidence of a reference, never evidence of approval scope.
Native cached text must match the original file hash. Only the first two pages
are read as a fallback; unscanned body pages are not negative evidence.
"""

from __future__ import annotations

import re
from collections import defaultdict

from duke_rates.historical.ncuc.clue_ledger import (
    BboxLocator,
    PageText,
    file_identity,
    load_pdf_pages,
    write_page_clues,
)
from duke_rates.historical.ncuc.regulatory_clues import PageClue, extract_page_clues

PRODUCER = "document_references"
VERSION = "1.0.0"
COVERAGE_VERSION = "order_reference_coverage@1.1.0"
_DOCKET = re.compile(r"\bDOCKET\s+NO\.?\s*([A-Z]+-\d+)\s*,?\s*SUB\s+(\d+)\b", re.I)


def extract_document_references(text: str, page: int) -> list[PageClue]:
    """Literal docket spans with conservative caption/body/footer roles."""
    clues = []
    seen = set()
    for match in _DOCKET.finditer(text):
        prefix = text[: match.start()]
        line_prefix = prefix.rsplit("\n", 1)[-1]
        role = (
            "caption"
            if page <= 2 and not line_prefix.strip() and prefix.count("\n") < 40
            else "body_reference"
        )
        value = f"{match.group(1).upper()} Sub {match.group(2)}"
        clues.append(
            PageClue(
                "docket",
                match.start(),
                match.end(),
                match.group(),
                value,
                "normalized",
                role,
                {
                    "docket": value,
                    "relationship": "references",
                    "authority_scope_established": False,
                },
            )
        )
        seen.add((match.start(), match.end(), value))
    for clue in extract_page_clues(text):
        if (
            clue.kind == "docket"
            and (clue.char_start, clue.char_end, clue.normalized_value) not in seen
        ):
            clue.normalized.update(
                {"relationship": "references", "authority_scope_established": False}
            )
            clues.append(clue)
    for match in re.finditer(r"(?im)^[^\n]{0,120}\b(implements|approved\s+by)\b[^\n]{0,240}", text):
        if not re.search(r"\b(order|docket)\b", match.group(), re.I):
            continue
        kind = "implements" if match.group(1).lower() == "implements" else "approved_by"
        clues.append(
            PageClue(
                "approval_reference",
                match.start(),
                match.end(),
                match.group(),
                None,
                "unparsed",
                "source_relationship_claim",
                {"relationship": kind, "authority_scope_established": False},
            )
        )
    return clues


def order_reference_index(conn, *, pages: int = 2, persist: bool = False) -> dict:
    """Index all verified cached order pages plus a bounded native caption fallback.

    Result includes explicit coverage and failures. It does not alter the portal's
    primary docket. Persisting writes only the existing clue ledger.
    """
    import fitz

    by_path = defaultdict(list)
    for row in conn.execute(
        "SELECT id, local_path FROM ncuc_discovery_records "
        "WHERE filing_classification = 'order' AND local_path IS NOT NULL ORDER BY id"
    ):
        by_path[row[1]].append(row[0])
    references = defaultdict(list)
    failures = []
    coverage = []
    locator = BboxLocator()
    try:
        for path, record_ids in by_path.items():
            identity = file_identity(path)
            if identity.status != "computed" or identity.page_count is None:
                failures.append({"source_pdf": path, "reason": "unavailable_or_unreadable"})
                continue
            cached = load_pdf_pages(conn, path, identity)
            if cached.integrity_error:
                failures.append({"source_pdf": path, "reason": cached.integrity_error})
                continue
            page_texts = {p: t for p, t in cached.pages.items() if t.text_layer == "native_text"}
            try:
                with fitz.open(path) as document:
                    for p in range(1, min(pages, len(document)) + 1):
                        if p not in page_texts:
                            text = document[p - 1].get_text("text")
                            page_texts[p] = PageText(
                                p, text, None, "direct_native_caption@1.0.0", "native_text"
                            )
            except Exception as exc:
                failures.append({"source_pdf": path, "reason": type(exc).__name__})
                continue
            coverage.append(
                {
                    "source_pdf": path,
                    "file_sha256": identity.sha256,
                    "pages_scanned": sorted(page_texts),
                    "page_count": identity.page_count,
                    "complete": len(page_texts) == identity.page_count
                    and all(t.text.strip() for t in page_texts.values()),
                    "pages_without_native_text": sorted(
                        p for p, t in page_texts.items() if not t.text.strip()
                    ),
                }
            )
            for p, page_text in sorted(page_texts.items()):
                clues = extract_document_references(page_text.text, p)
                ids = (
                    write_page_clues(
                        conn,
                        source_pdf=path,
                        identity=identity,
                        page=page_text,
                        scan_reason="order_reference_index",
                        locator=locator,
                        page_clues=clues,
                        producer=PRODUCER,
                        version=VERSION,
                    )
                    if persist
                    else []
                )
                for i, clue in enumerate(clues):
                    if not clue.normalized_value:
                        continue
                    bbox, bbox_status = locator.locate(path, p, page_text.text, clue, "native_text")
                    references[clue.normalized_value].append(
                        {
                            "source_pdf": path,
                            "file_sha256": identity.sha256,
                            "page": p,
                            "printed_text": clue.printed_text,
                            "char_start": clue.char_start,
                            "char_end": clue.char_end,
                            "role": clue.role,
                            "bbox_json": bbox,
                            "bbox_status": bbox_status,
                            "discovery_record_ids": record_ids,
                            "clue_id": ids[i] if ids else None,
                            "producer": f"{PRODUCER}@{VERSION}",
                            "authority_scope_established": False,
                        }
                    )
            locator.close()  # bound open PDF handles to one source at a time
    finally:
        locator.close()
    return {
        "references": dict(references),
        "coverage": coverage,
        "failures": failures,
        "scope": "verified_cached_pages_and_bounded_caption_fallback",
    }
