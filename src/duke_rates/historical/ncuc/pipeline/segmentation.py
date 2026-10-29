import re
from typing import List, Optional

from duke_rates.models.pipeline import PageEvidence, TariffSpan


_GENERIC_SCHEDULE_TITLE_EXACT = {
    "CERTIFICATE OF SERVICE",
    "TYPE OF SERVICE",
    "RIDER APPLICATIONS",
    "SUPPLEMENTARY SERVICE",
    "STANDBY SERVICE",
    "NON-FIRM STANDBY SERVICE",
}
_GENERIC_SCHEDULE_TITLE_PREFIXES = (
    "EFFECTIVE FOR SERVICE",
    "SERVICE RENDERED UNDER THIS SCHEDULE",
    "TRANSMISSION SERVICE DISTRIBUTION SERVICE",
    "COMPANY HAS THE RIGHT TO SUSPEND SERVICE",
    "COMPANY RESERVES THE RIGHT TO PROVIDE SERVICE",
    "CUSTOMER ASSISTANCE RECOVERY RIDER",
    "PROGRAM CREDIT PROGRAM",
    "ADDITIONAL CHARGES",
    "PROVISION OF STANDBY SERVICE",
)


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).upper()


def _title_is_significant(title: str) -> bool:
    if not title:
        return False
    if title in _GENERIC_SCHEDULE_TITLE_EXACT:
        return False
    if any(title.startswith(prefix) for prefix in _GENERIC_SCHEDULE_TITLE_PREFIXES):
        return False
    if title.startswith(("SCHEDULE ", "RIDER ", "RATE ")):
        return True
    if "DUKE ENERGY" in title and any(
        token in title for token in ("SERVICE", "RIDER", "PROGRAM", "SCHEDULE")
    ):
        return True
    if title.isupper() and len(title.split()) <= 6 and any(
        token in title for token in ("SERVICE", "RIDER", "PROGRAM", "SCHEDULE")
    ):
        return True
    return False


def _filter_significant_schedule_titles(raw_titles: set[str] | list[str]) -> set[str]:
    significant: set[str] = set()
    for raw_title in raw_titles:
        title = _normalize_title(raw_title)
        if _title_is_significant(title):
            significant.add(title)
    return significant


def _extract_significant_schedule_titles(page: PageEvidence) -> set[str]:
    return _filter_significant_schedule_titles(page.extracted_schedule_codes)


def _span_schedule_titles(span: TariffSpan) -> set[str]:
    return {_normalize_title(title) for title in span.extracted_schedule_titles if title}


def _extract_explicit_schedule_codes(titles: set[str]) -> set[str]:
    return {title for title in titles if title.startswith(("SCHEDULE ", "RIDER ", "RATE "))}


def _span_significant_titles(span: TariffSpan) -> set[str]:
    return _filter_significant_schedule_titles(span.extracted_schedule_titles)


def _classify_page_doc_type(page: PageEvidence) -> str:
    """Formal sheet evidence outranks vocabulary; retain legacy feature-only inputs."""
    return "tariff" if _page_kind(page) in ("tariff_sheet", "tariff_appendix") else "procedural"


def segment_document(
    pages: List[PageEvidence], parent_discovery_id: Optional[int] = None
) -> List[TariffSpan]:
    """
    Segment a list of PageEvidence blocks into grouped TariffSpans.
    A span groups contiguous pages belonging to the same tariff component.
    """
    if not pages:
        return []

    numbers = [p.page_number for p in pages]
    if any(p < 1 for p in numbers) or numbers != sorted(set(numbers)):
        raise ValueError("page evidence must have unique, increasing original page numbers")
    spans = []
    current_span = None

    for idx, page in enumerate(pages):
        kind = _page_kind(page)
        own_leaves = _own_leaf_numbers(page)
        doc_type = _classify_page_doc_type(page)
        continuation = bool(current_span and _is_table_continuation(page, current_span, kind))
        if continuation:
            kind, doc_type = "tariff_appendix", "tariff"

        # Determine if this page represents a hard boundary starting a new tariff section
        is_boundary = False

        # A leaf header appearing often signals a new sheet
        if page.has_leaf_header and page.has_schedule_heading:
            is_boundary = True

        # Leaf header alone (no schedule heading needed) is a sufficient boundary
        # when the page introduces a leaf number not seen in the current span
        elif page.has_leaf_header and own_leaves:
            if current_span is None:
                is_boundary = True
            else:
                # If this page's leaf nos are disjoint from the current span, start a new span
                new_leaves = set(own_leaves) - current_span.extracted_leaf_nos
                if new_leaves and not current_span.extracted_leaf_nos.intersection(own_leaves):
                    is_boundary = True

        # If we have a revised header but no explicit leaf, it's still probably a new page
        elif page.has_revised_header and len(page.extracted_schedule_codes) > 0:
            is_boundary = True

        # Sudden shift from procedural text to tariff text
        elif page.tariff_vocab_density > 0.02 and page.procedural_vocab_density == 0:
            if current_span and current_span.doc_type == "procedural":
                is_boundary = True

        # Book-style tariff PDFs often lack leaf headers but do repeat clear
        # schedule titles as each tariff section starts. Split on a new,
        # significant title rather than merging the whole tariff book into one
        # span.
        elif page.has_schedule_heading and current_span and current_span.doc_type == "tariff":
            current_titles = _span_schedule_titles(current_span)
            page_titles = _extract_significant_schedule_titles(page)
            current_explicit_codes = _extract_explicit_schedule_codes(current_titles)
            page_explicit_codes = _extract_explicit_schedule_codes(page_titles)
            if page_explicit_codes and page_explicit_codes.isdisjoint(current_explicit_codes):
                is_boundary = True
            elif page_titles and page_titles.isdisjoint(current_titles):
                is_boundary = True

        if continuation:
            is_boundary = False
        if current_span and (
            page.page_number != current_span.end_page + 1 or doc_type != current_span.doc_type
        ):
            is_boundary = True
        if (
            current_span
            and doc_type == "procedural"
            and kind in ("index", "commission_order", "application")
            and current_span.segmentation_evidence.get("kind") != kind
        ):
            is_boundary = True

        if (
            current_span
            and own_leaves
            and not current_span.extracted_leaf_nos.intersection(own_leaves)
        ):
            is_boundary = True
        reason = (
            "document_start"
            if current_span is None
            else "source_page_gap"
            if page.page_number != current_span.end_page + 1
            else "document_type_transition"
            if doc_type != current_span.doc_type
            else "own_leaf_header_change"
            if own_leaves and not current_span.extracted_leaf_nos.intersection(own_leaves)
            else "schedule_or_section_heading"
        )
        if is_boundary or current_span is None:
            # finalize old span
            if current_span:
                spans.append(current_span)

            current_span = TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=page.page_number,
                end_page=page.page_number,
                doc_type=doc_type,
                extracted_leaf_nos=set(own_leaves),
                extracted_schedule_titles=set(page.extracted_schedule_codes),
                header_footer_snippets=page.header_candidates + page.footer_candidates,
                segmentation_evidence={
                    "version": "segmentation@1.0.0",
                    "kind": kind,
                    "boundary_page": page.page_number,
                    "page_kinds": {str(page.page_number): kind},
                    "boundary_reason": reason,
                    "source_text_layer": "PageEvidence.text_content",
                    "authority_established": False,
                    "observations": [_page_observation(page, kind)],
                },
            )
        else:
            # Continue span
            current_span.end_page = page.page_number
            current_span.extracted_leaf_nos.update(own_leaves)
            current_span.extracted_schedule_titles.update(page.extracted_schedule_codes)
            current_span.header_footer_snippets.extend(page.header_candidates)

            current_span.segmentation_evidence["page_kinds"][str(page.page_number)] = kind
            current_span.segmentation_evidence["observations"].append(_page_observation(page, kind))

    # append the last span
    if current_span:
        spans.append(current_span)

    # Post-process: Merge small adjacent fragments if they share leaf nos or schedules
    merged_spans = []
    for span in spans:
        if not merged_spans:
            merged_spans.append(span)
            continue

        prev_span = merged_spans[-1]

        # Merge criteria: Both tariff, and they share a title or leaf no, OR one is just a continuation without explicit new headers
        if (
            prev_span.doc_type == "tariff"
            and span.doc_type == "tariff"
            and prev_span.end_page + 1 == span.start_page
        ):
            if (
                prev_span.extracted_leaf_nos
                and span.extracted_leaf_nos
                and prev_span.extracted_leaf_nos.isdisjoint(span.extracted_leaf_nos)
            ):
                merged_spans.append(span)
                continue
            shared_leaves = prev_span.extracted_leaf_nos.intersection(span.extracted_leaf_nos)
            prev_titles = _span_significant_titles(prev_span)
            span_titles = _span_significant_titles(span)
            prev_explicit_codes = _extract_explicit_schedule_codes(prev_titles)
            span_explicit_codes = _extract_explicit_schedule_codes(span_titles)
            shared_schedules = prev_titles.intersection(span_titles)

            if shared_leaves:
                # merge
                _merge_boundary_evidence(prev_span, span)
                prev_span.end_page = span.end_page
                prev_span.extracted_leaf_nos.update(span.extracted_leaf_nos)
                prev_span.extracted_schedule_titles.update(span.extracted_schedule_titles)
                continue

            if prev_explicit_codes or span_explicit_codes:
                if prev_explicit_codes.intersection(span_explicit_codes):
                    _merge_boundary_evidence(prev_span, span)
                    prev_span.end_page = span.end_page
                    prev_span.extracted_leaf_nos.update(span.extracted_leaf_nos)
                    prev_span.extracted_schedule_titles.update(span.extracted_schedule_titles)
                    continue
            elif shared_schedules and not span.extracted_leaf_nos:
                # merge
                _merge_boundary_evidence(prev_span, span)
                prev_span.end_page = span.end_page
                prev_span.extracted_leaf_nos.update(span.extracted_leaf_nos)
                prev_span.extracted_schedule_titles.update(span.extracted_schedule_titles)
                continue

        merged_spans.append(span)

    return merged_spans


_LEAF_LINE = re.compile(r"\bLeaf\s*No\.?\s*(?:[A-Z]{1,4}\s*)?\d{1,4}[A-Z]?\b", re.I)
_EXPLICIT_CODE = re.compile(r"^(?:SCHEDULE|RIDER|RATE)\s+[A-Z][A-Z0-9-]{0,20}\b")


def _own_sheet_lines(page: PageEvidence) -> list[str]:
    lines = [line.strip() for line in (page.text_content or "").splitlines() if line.strip()]
    found = []
    for line in lines[:10] + lines[-10:]:
        match = _LEAF_LINE.search(line)
        if not match or len(line) > 240 or "supersed" in line[: match.start()].lower():
            continue
        if re.search(r"\b(?:Original|Revised|Substitute)\b", line, re.I) or re.match(
            r"^(?:NC\s+)?Leaf", line, re.I
        ):
            found.append(line)
    return list(dict.fromkeys(found))


def _page_kind(page: PageEvidence) -> str:
    text = page.text_content
    if text is None:  # Existing callers can supply feature-only PageEvidence.
        if (
            page.has_leaf_header
            or page.has_revised_header
            or page.has_schedule_heading
            or page.extracted_leaf_nos
        ):
            return "tariff_sheet"
        return (
            "tariff_sheet"
            if page.tariff_vocab_density > page.procedural_vocab_density
            else "procedural"
        )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    head = " ".join(lines[:12]).upper()
    if re.search(
        r"INDEX OF TARIFFS|TABLE OF CONTENTS|LISTING OF EXHIBITS|SUMMARY OF EXHIBITS", head
    ):
        return "index"
    if _own_sheet_lines(page):
        return "tariff_sheet"
    if re.search(r"EXHIBIT|CALCULATION OF|GENERATION ASSET PURCHASE", head):
        return "supporting_exhibit"
    if re.search(r"EVALUATION|EVALUATOR|TESTIMONY|SUPREME COURT|BRIEF FOR|APPEARANCE SLIP", head):
        return "procedural"
    if re.search(r"\bORDER (?:APPROVING|DENYING|GRANTING|ACCEPTING|ESTABLISHING)\b", head):
        return "commission_order"
    if re.search(r"\bAPPLICATION\b", head + " " + " ".join(lines[-6:]).upper()):
        return "application"
    explicit = [line for line in lines[:12] if _EXPLICIT_CODE.match(line)]
    if explicit and (
        page.has_effective_date_phrase
        or page.numeric_density > 0.05
        or re.search(r"AVAILABILITY|APPLICABILITY|CUSTOMER CHARGE|ENERGY CHARGE", text, re.I)
    ):
        return "tariff_sheet"
    return "procedural"


def _is_table_continuation(page: PageEvidence, span: TariffSpan, kind: str) -> bool:
    if (
        span.doc_type != "tariff"
        or page.page_number != span.end_page + 1
        or not span.extracted_leaf_nos
    ):
        return False
    if kind in ("index", "commission_order", "application", "supporting_exhibit", "unknown"):
        return False
    if _own_sheet_lines(page) or page.text_content is None:
        return False
    text = page.text_content or ""
    # A dense credit/rate appendix may omit the repeated leaf header. Never
    # infer a tariff continuation from prose or a named supporting exhibit.
    return len(re.findall(r"\d+\.\d+", text)) >= 8 and bool(
        re.search(r"ENERGY\s+CREDIT|CAPACITY\s+CREDIT|PER\s+KWH|PER\s+KW", text, re.I)
    )


def _page_observation(page: PageEvidence, kind: str) -> dict:
    return {
        "page": page.page_number,
        "kind": kind,
        "sheet_header_quotes": _own_sheet_lines(page),
        "text_available": bool((page.text_content or "").strip()),
        "proposal_or_redline_text_marker": page.has_redline_markers,
        "label_source": "machine:source_text_structure",
        "authority_established": False,
    }


def _merge_boundary_evidence(target: TariffSpan, source: TariffSpan) -> None:
    target.segmentation_evidence.setdefault("page_kinds", {}).update(
        source.segmentation_evidence.get("page_kinds", {})
    )
    target.segmentation_evidence.setdefault("observations", []).extend(
        source.segmentation_evidence.get("observations", [])
    )


def _own_leaf_numbers(page: PageEvidence) -> set[str]:
    if page.text_content is None:
        return set(page.extracted_leaf_nos)
    result = set()
    for line in _own_sheet_lines(page):
        match = _LEAF_LINE.search(line)
        if match:
            number = re.search(r"\d{1,4}[A-Z]?\b", match.group(), re.I)
            if number:
                result.add(number.group().upper())
    return result
