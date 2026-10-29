"""Phase 2E M1: can a stored Docling artifact serve original-PDF pages?"""

from __future__ import annotations

import pytest

from duke_rates.document_intelligence.page_integrity import (
    MISSING,
    PARTIAL,
    UNVERIFIABLE,
    VALID,
    assess_docling_page_integrity,
)

CHUNKED = {"degraded_modes": ["chunked"], "skipped_pages": [], "used_chunking": True}


def _doc(pages, *, items=None, page_no_override=None):
    """A minimal Docling doc_json with page entries and one text item per page."""
    items = pages if items is None else items
    return {
        "pages": {
            str(p): {"page_no": (page_no_override or {}).get(p, p), "size": {"width": 612, "height": 792}}
            for p in pages
        },
        "texts": [{"self_ref": f"#/texts/{i}", "text": f"page {p}", "prov": [{"page_no": p}]}
                  for i, p in enumerate(items)],
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(len(items))]},
    }


def _assess(doc, pdf_pages, requested=None, metadata=None, artifact_pages=None):
    return assess_docling_page_integrity(
        doc, pdf_page_count=pdf_pages, requested=requested, metadata=metadata,
        artifact_page_count=artifact_pages,
    )


def test_whole_document_artifact_is_valid_for_any_range():
    doc = _doc(range(1, 11))
    whole = _assess(doc, 10, artifact_pages=10)
    assert whole.status == VALID and whole.numbering == "absolute" and whole.usable
    assert whole.as_dict()["covered_pages"] == [[1, 10]]
    assert _assess(doc, 10, (4, 6)).status == VALID


def test_bounded_range_inside_a_complete_artifact():
    r = _assess(_doc(range(1, 31)), 30, (12, 14), artifact_pages=30)
    assert r.usable and r.missing_pages == () and r.page_offset == 0


def test_chunked_artifact_with_relative_numbers_is_unverifiable():
    # 106-page filing, only the last chunk (6 pages) stored, numbered 1..6.
    r = _assess(_doc(range(1, 7)), 106, (1, 3), metadata=CHUNKED, artifact_pages=106)
    assert r.status == UNVERIFIABLE and r.reason == "chunk_relative_numbering"
    assert r.numbering == "chunk_relative" and not r.usable
    # Nothing is claimed as covered: original numbers are not inferred.
    assert r.covered_pages == () and r.missing_pages == ()


def test_chunked_metadata_as_json_string_and_span_beyond_the_chunk():
    import json

    r = _assess(_doc(range(1, 7)), 106, (50, 55), metadata=json.dumps(CHUNKED), artifact_pages=106)
    assert r.status == UNVERIFIABLE and r.reason == "chunk_relative_numbering"


def test_chunked_but_complete_artifact_is_valid():
    # A merge that kept every page with original numbers would be correct.
    r = _assess(_doc(range(1, 21)), 20, (5, 8), metadata=CHUNKED, artifact_pages=20)
    assert r.status == VALID and r.chunked is True


def test_truncated_artifact_without_metadata_is_ambiguous():
    # Pages 1..6 of a 106-page PDF and no record of chunking: could be either.
    r = _assess(_doc(range(1, 7)), 106, (1, 3), metadata={})
    assert r.status == UNVERIFIABLE and r.reason == "ambiguous_truncated_numbering"


def test_requested_range_outside_the_pdf():
    r = _assess(_doc(range(1, 11)), 10, (9, 12))
    assert r.status == MISSING and r.reason == "range_outside_document"
    assert _assess(_doc(range(1, 11)), 10, (0, 2)).reason == "range_outside_document"


def test_span_not_covered_and_partially_covered_with_absolute_gaps():
    # Whole-document conversion lost pages 4-6; the rest keep their numbers.
    doc = _doc([1, 2, 3, 7, 8, 9, 10])
    assert _assess(doc, 10, (4, 6)).reason == "span_not_covered"
    partial = _assess(doc, 10, (2, 5))
    assert partial.status == PARTIAL and partial.missing_pages == (4, 5)
    assert partial.numbering_verified and not partial.usable


def test_gaps_short_of_the_last_page_need_an_explicit_whole_document_record():
    doc = _doc([1, 2, 4, 5])
    assert _assess(doc, 10, (1, 2)).reason == "ambiguous_truncated_numbering"
    explicit = _assess(doc, 10, (1, 2), metadata={"used_chunking": False})
    assert explicit.status == VALID


def test_duplicate_page_identifiers_are_unverifiable():
    doc = _doc([1, 2])
    doc["pages"]["01"] = {"page_no": 1}
    assert _assess(doc, 2).reason == "duplicate_page_identifiers"


def test_conflicting_page_identifiers_are_unverifiable():
    doc = _doc([1, 2, 3], page_no_override={2: 7})
    assert _assess(doc, 3).reason == "conflicting_page_identifiers"


def test_non_integer_page_identifiers_are_unverifiable():
    doc = _doc([1, 2])
    doc["texts"][0]["prov"][0]["page_no"] = "1"
    assert _assess(doc, 2).reason == "non_integer_page_id"


def test_item_on_a_page_the_artifact_does_not_list():
    doc = _doc([1, 2], items=[1, 2, 3])
    assert _assess(doc, 3).reason == "prov_page_not_in_pages"


def test_missing_original_page_metadata_is_unverifiable():
    assert _assess(_doc([1, 2]), None).reason == "pdf_page_count_unknown"
    assert _assess(_doc([1, 2]), 0).reason == "pdf_page_count_unknown"


def test_artifact_page_count_must_match_the_pdf():
    r = _assess(_doc(range(1, 11)), 10, artifact_pages=12)
    assert r.status == UNVERIFIABLE and r.reason == "page_count_mismatch"


def test_empty_or_invalid_documents_are_missing():
    assert _assess({"pages": {}, "texts": []}, 5).reason == "empty_document"
    assert _assess(None, 5).reason == "invalid_doc_json"


def test_recorded_offset_is_applied_never_derived():
    # An excerpt converted from pages 11-15 that recorded its offset.
    r = _assess(_doc(range(1, 6)), 20, (12, 13), metadata={"page_offset": 10})
    assert r.status == VALID and r.numbering == "offset" and r.page_offset == 10
    assert r.covered_pages == (11, 12, 13, 14, 15)
    beyond = _assess(_doc(range(1, 6)), 12, (11, 12), metadata={"page_offset": 10})
    assert beyond.reason == "page_out_of_bounds"


@pytest.mark.parametrize("bad", [-1, "10", True, 1.5])
def test_invalid_offsets_are_ignored(bad):
    r = _assess(_doc(range(1, 7)), 106, (1, 3), metadata={**CHUNKED, "page_offset": bad})
    assert r.reason == "chunk_relative_numbering"
