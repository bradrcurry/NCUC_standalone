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
    """
    Prefer explicit tariff-sheet structure over weak text-density ties.

    Older NCUC attachments often include a procedural cover letter followed by a
    single revised tariff sheet. Those leaf pages can have only a few tariff
    keywords, so a density-only comparison misclassifies them as procedural.
    """
    if (
        page.has_leaf_header
        or page.has_revised_header
        or page.has_schedule_heading
        or bool(page.extracted_leaf_nos)
    ):
        return "tariff"
    return "tariff" if page.tariff_vocab_density > page.procedural_vocab_density else "procedural"

def segment_document(pages: List[PageEvidence], parent_discovery_id: Optional[int] = None) -> List[TariffSpan]:
    """
    Segment a list of PageEvidence blocks into grouped TariffSpans.
    A span groups contiguous pages belonging to the same tariff component.
    """
    if not pages:
        return []

    spans = []
    current_span = None

    for idx, page in enumerate(pages):

        # Determine if this page represents a hard boundary starting a new tariff section
        is_boundary = False

        # A leaf header appearing often signals a new sheet
        if page.has_leaf_header and page.has_schedule_heading:
            is_boundary = True

        # Leaf header alone (no schedule heading needed) is a sufficient boundary
        # when the page introduces a leaf number not seen in the current span
        elif page.has_leaf_header and page.extracted_leaf_nos:
            if current_span is None:
                is_boundary = True
            else:
                # If this page's leaf nos are disjoint from the current span, start a new span
                new_leaves = set(page.extracted_leaf_nos) - current_span.extracted_leaf_nos
                if new_leaves and not current_span.extracted_leaf_nos.intersection(page.extracted_leaf_nos):
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

        if is_boundary or current_span is None:
            # finalize old span
            if current_span:
                spans.append(current_span)
                
            doc_type = _classify_page_doc_type(page)
            
            current_span = TariffSpan(
                parent_discovery_id=parent_discovery_id,
                start_page=page.page_number,
                end_page=page.page_number,
                doc_type=doc_type,
                extracted_leaf_nos=set(page.extracted_leaf_nos),
                extracted_schedule_titles=set(page.extracted_schedule_codes),
                header_footer_snippets=page.header_candidates + page.footer_candidates
            )
        else:
            # Continue span
            current_span.end_page = page.page_number
            current_span.extracted_leaf_nos.update(page.extracted_leaf_nos)
            current_span.extracted_schedule_titles.update(page.extracted_schedule_codes)
            current_span.header_footer_snippets.extend(page.header_candidates)
            
            # update doc_type if it flips significantly
            if _classify_page_doc_type(page) == "tariff" and current_span.doc_type == "procedural":
                current_span.doc_type = "tariff"

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
        if prev_span.doc_type == "tariff" and span.doc_type == "tariff":
            shared_leaves = prev_span.extracted_leaf_nos.intersection(span.extracted_leaf_nos)
            prev_titles = _span_significant_titles(prev_span)
            span_titles = _span_significant_titles(span)
            prev_explicit_codes = _extract_explicit_schedule_codes(prev_titles)
            span_explicit_codes = _extract_explicit_schedule_codes(span_titles)
            shared_schedules = prev_titles.intersection(span_titles)
            
            if shared_leaves:
                # merge
                prev_span.end_page = span.end_page
                prev_span.extracted_leaf_nos.update(span.extracted_leaf_nos)
                prev_span.extracted_schedule_titles.update(span.extracted_schedule_titles)
                continue

            if prev_explicit_codes or span_explicit_codes:
                if prev_explicit_codes.intersection(span_explicit_codes):
                    prev_span.end_page = span.end_page
                    prev_span.extracted_leaf_nos.update(span.extracted_leaf_nos)
                    prev_span.extracted_schedule_titles.update(span.extracted_schedule_titles)
                    continue
            elif shared_schedules and not span.extracted_leaf_nos:
                # merge
                prev_span.end_page = span.end_page
                prev_span.extracted_leaf_nos.update(span.extracted_leaf_nos)
                prev_span.extracted_schedule_titles.update(span.extracted_schedule_titles)
                continue
                
        merged_spans.append(span)
        
    return merged_spans
