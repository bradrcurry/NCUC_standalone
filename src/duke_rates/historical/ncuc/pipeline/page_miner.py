import re
import pdfplumber
from typing import List, Optional
import logging
from duke_rates.historical.ncuc.metadata import extract_rider_codes, extract_schedule_codes
from duke_rates.historical.ncuc.pipeline.ocr_normalization import normalize_ocr_text
from duke_rates.models.pipeline import PageEvidence

TARIFF_VOCAB_REGEX = re.compile(r'(?i)(AVAILABILITY|RATE|RIDER|SERVICE RENDERED|per kWh|per kW|Customer Charge|Energy Charge)')
PROCEDURAL_VOCAB_REGEX = re.compile(r'(?i)(motion|testimony|affidavit|brief|commission|certificate of service|order granting|docket no\.?)')
LEAF_HEADER_REGEX = re.compile(r'(?i)(Leaf\s+No\.?\s+\d{1,4}|NCUC\s+No\.?\s+\d{1,4})')
REVISED_HEADER_REGEX = re.compile(r'(?i)(Original|Revised|Substitute)\s+Leaf')
SCHEDULE_HEADING_REGEX = re.compile(
    r'(?i)^\s*('
    r'RIDER\s+[A-Z0-9\-]+(?:\s+\([A-Z0-9/\-\s]+\))?|'
    r'SCHEDULE\s+[A-Z0-9\-]+(?:\s+\([A-Z0-9/\-\s]+\))?|'
    r'RATE\s+[A-Z0-9\-]+(?:\s+\([A-Z0-9/\-\s]+\))?|'
    r'[A-Z][A-Z0-9&(),/\- ]{6,}(?:RIDER|SERVICE|SCHEDULE|PROGRAM)(?:\s+\([A-Z0-9/\-\s]+\))?'
    r')\s*$'
)
INLINE_DESCRIPTIVE_HEADING_REGEX = re.compile(
    r'(?i)\b([A-Z][A-Z0-9&(),/\-\s]{6,}?(?:RIDER|SERVICE|SCHEDULE|PROGRAM)(?:\s+\([A-Z0-9/\-\s]+\))?)\b'
)
EFFECTIVE_DATE_REGEX = re.compile(r'(?i)(effective\s+[A-Z]+[a-z]*\s+\d{1,2},?\s+\d{4}|applicable\s+beginning.*|service\s+rendered\s+on\s+or\s+after)')
NUMBER_REGEX = re.compile(r'\d+\.\d+|\$\d+')
DOCKET_REGEX = re.compile(r'(?i)Docket\s+No\.?\s*([E|G]\-\d+\s+Sub\s+\d+)')

# ---------------------------------------------------------------------------
# Redline / tracked-changes detection
# ---------------------------------------------------------------------------

# Explicit textual markers that indicate a document contains tracked changes,
# proposed language, or "new vs. old" comparison structure.
REDLINE_MARKER_REGEX = re.compile(
    r'\b(NEW|OLD|PROPOSED|SUPERSEDED|REDLINED?|MARK(?:ED[\s\-]?)?UP|DRAFT)\b'
)

# Dual-rate slash patterns — the most reliable text-layer redline signal.
# Matches "0.0464/0.0512" or "12.34 / 56.78" style side-by-side rate pairs.
DUAL_RATE_REGEX = re.compile(
    r'\b\d{1,4}\.\d{3,6}\s*/\s*\d{1,4}\.\d{3,6}\b'
)

# "Was X, now Y" and similar comparative phrasing used in narrative redlines.
COMPARATIVE_RATE_REGEX = re.compile(
    r'(?i)\b(?:was|previously|prior|changed\s+from|from\s+\$?\d[\d,.]+\s+to)\b'
)

# Table-of-contents / index page marker — used for compliance book detection.
TOC_PAGE_REGEX = re.compile(
    r'(?i)(TABLE\s+OF\s+CONTENTS|INDEX\s+OF\s+TARIFF\s+(?:LEAVES|SHEETS)|'
    r'CONTENTS\s+OF\s+(?:THIS\s+)?FILING|TARIFF\s+BOOK\s+INDEX|'
    r'SCHEDULE\s+OF\s+CONTENTS|INDEX\s+OF\s+SCHEDULES)'
)
EXPLICIT_HEADING_CODE_REGEX = re.compile(
    r'(?i)^\s*(?:SCHEDULE|RIDER|RATE)\s+([A-Z0-9\-]+)'
)
_GENERIC_HEADING_EXACT = {
    "CERTIFICATE OF SERVICE",
    "TYPE OF SERVICE",
    "RIDER APPLICATIONS",
    "SUPPLEMENTARY SERVICE",
    "STANDBY SERVICE",
    "NON-FIRM STANDBY SERVICE",
}
_GENERIC_HEADING_PREFIXES = (
    "EFFECTIVE FOR SERVICE",
    "EFFECTIVE NOVEMBER",
    "SERVICE RENDERED UNDER THIS SCHEDULE",
    "TRANSMISSION SERVICE DISTRIBUTION SERVICE",
    "COMPANY HAS THE RIGHT TO SUSPEND SERVICE",
    "COMPANY RESERVES THE RIGHT TO PROVIDE SERVICE",
    "PROGRAM CREDIT PROGRAM",
    "ADDITIONAL CHARGES",
    "PROVISION OF STANDBY SERVICE",
)


def _normalize_heading(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).upper()


def _is_generic_heading(text: str) -> bool:
    heading = _normalize_heading(text)
    if not heading:
        return True
    if heading in _GENERIC_HEADING_EXACT:
        return True
    return any(heading.startswith(prefix) for prefix in _GENERIC_HEADING_PREFIXES)


# Prefixes that signal a heading is a rate schedule / rider identifier
# rather than a descriptive phrase. Stripped before code-likeness check.
_CODE_HEADING_PREFIX_RE = re.compile(
    r'^(?:SCHEDULE|RIDER|RATE)\s+', re.IGNORECASE,
)

# A code-like token has a digit or a dash, OR is a short all-caps acronym
# (2-6 letters). Examples that should pass: "RES-28", "SGS", "EDPR", "B-13",
# "RA-1". Examples that should fail: "RESIDENTIAL", "GENERAL", "APPLICABLE",
# "SERVICE" — these are descriptive English words.
_CODE_TOKEN_RE = re.compile(
    r"""
    ^(?:
        [A-Z0-9]{1,6}(?:-[A-Z0-9]+){1,3}     # RES-28, B-13-A, SGS-TOU-E
      | \d+[A-Za-z]?                          # 28, 31a
      | [A-Z]{2,6}                            # SGS, EDPR, REPS  (short acronym)
      | [A-Z][A-Za-z]*\d+                     # Sub3, RES28
    )$
    """,
    re.VERBOSE,
)


def _is_likely_code(text: str) -> bool:
    """Return True if *text* looks like a schedule/rider code, not a phrase.

    A heading is "code-like" when, after stripping any SCHEDULE/RIDER/RATE
    prefix, the remaining content is either (a) a single token matching the
    code-token pattern (digits, dashes, or short all-caps), or (b) ALL of
    its tokens are individually code-like.

    This is the inverse of a deny-list: instead of enumerating every English
    word that could appear in a descriptive heading, we require positive
    evidence that each token looks like an identifier. This prevents
    descriptive phrases like "APPLICABLE TO ELECTRIC UTILITY SERVICE" from
    being stored as schedule codes, while still admitting legitimate codes
    that contain words like "RES" (Residential) or "SGS" (Small General
    Service) — those abbreviations are already short all-caps acronyms.
    """
    if not text or len(text) > 25:
        return False
    check_text = _CODE_HEADING_PREFIX_RE.sub("", text).strip()
    if not check_text:
        return False
    raw_tokens = [
        t.strip(".,;:()[]{}!?\"'")
        for t in check_text.replace("/", " ").split()
    ]
    tokens = [t for t in raw_tokens if t]
    if not tokens:
        return False
    return all(bool(_CODE_TOKEN_RE.match(t)) for t in tokens)


def _expand_heading_codes(headings: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()

    for heading in headings:
        candidate = heading.strip()
        if not candidate:
            continue

        # Extract explicit code from SCHEDULE/RIDER/RATE prefix first —
        # this is the cleanest signal and works even when the rest of
        # the heading is descriptive.
        explicit_match = EXPLICIT_HEADING_CODE_REGEX.match(candidate)
        if explicit_match:
            explicit_code = explicit_match.group(1).upper()
            if explicit_code and explicit_code not in seen:
                expanded.append(explicit_code)
                seen.add(explicit_code)

        # Extract numeric schedule codes via metadata module patterns
        for code in extract_schedule_codes(candidate) + extract_rider_codes(candidate):
            code_norm = _normalize_heading(code)
            if code_norm and code_norm not in seen:
                expanded.append(code)
                seen.add(code_norm)

        # Only add the full heading text as a fallback code when it
        # actually looks like a code (short, no common English words).
        # Descriptive phrases like "APPLICABLE TO ELECTRIC UTILITY
        # SERVICE" are NOT schedule codes and must not be stored.
        if _is_likely_code(candidate):
            normalized = _normalize_heading(candidate)
            if normalized not in seen:
                expanded.append(candidate)
                seen.add(normalized)

    return expanded

def _extract_page_features_from_text(text: str, page_num: int) -> PageEvidence:
    """Extract text-derived page features from already-available page text."""
    normalized_text = normalize_ocr_text(text)
    evidence = PageEvidence(
        page_number=page_num,
        text_length=len(normalized_text),
        text_content=normalized_text,
    )
    
    if not normalized_text.strip():
        return evidence
        
    # Headers and Footers
    lines = normalized_text.split('\n')
    header_lines = lines[:5]
    footer_lines = lines[-5:] if len(lines) > 5 else []
    
    evidence.header_candidates = header_lines
    evidence.footer_candidates = footer_lines
    
    full_text = normalized_text.upper()
    
    # regex hits
    evidence.has_leaf_header = bool(LEAF_HEADER_REGEX.search(normalized_text))
    evidence.has_revised_header = bool(REVISED_HEADER_REGEX.search(normalized_text))
    evidence.has_schedule_heading = any(
        bool(SCHEDULE_HEADING_REGEX.match(line.strip()))
        for line in lines
    )
    evidence.has_effective_date_phrase = bool(EFFECTIVE_DATE_REGEX.search(normalized_text))
    evidence.has_docket_phrase = bool(DOCKET_REGEX.search(normalized_text))
    
    # Extractions
    leaf_matches = LEAF_HEADER_REGEX.findall(normalized_text)
    if leaf_matches:
        # Extract just the digits from sentences like 'Leaf No. 604'
        leaves = []
        for m in leaf_matches:
            num = re.search(r'\d{1,4}', m)
            if num:
                leaves.append(num.group(0))
        if leaves:
            evidence.extracted_leaf_nos = list(set(leaves))
        
    schedule_matches = []
    for line in lines:
        match = SCHEDULE_HEADING_REGEX.match(line.strip())
        if match:
            schedule_matches.append(match.group(1).strip())
    if not schedule_matches:
        inline_matches = []
        for line in lines:
            match = INLINE_DESCRIPTIVE_HEADING_REGEX.search(line)
            if match:
                inline_matches.append(match.group(1).strip())
        schedule_matches = inline_matches
    if schedule_matches:
        filtered_matches = [
            match.strip()
            for match in schedule_matches
            if match and not _is_generic_heading(match)
        ]
        evidence.extracted_schedule_codes = _expand_heading_codes(filtered_matches)
        
    # densities
    word_count = max(1, len(normalized_text.split()))

    tariff_hits = len(TARIFF_VOCAB_REGEX.findall(normalized_text))
    proc_hits = len(PROCEDURAL_VOCAB_REGEX.findall(normalized_text))
    num_hits = len(NUMBER_REGEX.findall(normalized_text))

    evidence.tariff_vocab_density = tariff_hits / word_count
    evidence.procedural_vocab_density = proc_hits / word_count
    evidence.numeric_density = num_hits / word_count

    # table heuristic: lots of numbers and short lines
    avg_line_len = len(normalized_text) / max(1, len(lines))
    if evidence.numeric_density > 0.05 and avg_line_len < 60:
        evidence.table_like_density = evidence.numeric_density * 2.0

    # Redline / tracked-changes signals
    redline_hits = REDLINE_MARKER_REGEX.findall(normalized_text)
    evidence.has_redline_markers = len(redline_hits) > 0
    evidence.redline_marker_count = len(redline_hits)
    evidence.has_dual_rate_pair = bool(DUAL_RATE_REGEX.search(normalized_text))
    evidence.has_toc_page = bool(TOC_PAGE_REGEX.search(normalized_text))

    return evidence


def classify_compliance_book(evidence_list: List[PageEvidence]) -> dict:
    """
    Classify whether a multi-page document is a "compliance tariff book" —
    an index/bundle containing multiple tariff leaves — or a standalone sheet.

    Returns a dict with:
        is_compliance_book  — True if TOC page found or ≥2 distinct leaf_nos
        has_toc_page        — True if any page has a table-of-contents marker
        unique_leaf_nos     — sorted list of distinct leaf numbers found
        leaf_span_count     — count of distinct leaf numbers
        confidence          — 0.0–1.0
    """
    all_leaves: set[str] = set()
    has_toc = False
    for ev in evidence_list:
        all_leaves.update(ev.extracted_leaf_nos)
        if ev.has_toc_page:
            has_toc = True
    leaf_count = len(all_leaves)
    is_book = has_toc or leaf_count >= 2
    if has_toc:
        confidence = 0.95
    elif leaf_count >= 3:
        confidence = 0.75
    elif leaf_count == 2:
        confidence = 0.55
    else:
        confidence = 0.1
    return {
        "is_compliance_book": is_book,
        "has_toc_page": has_toc,
        "unique_leaf_nos": sorted(all_leaves),
        "leaf_span_count": leaf_count,
        "confidence": confidence,
    }


def _extract_page_features(page: pdfplumber.page.Page, page_num: int) -> PageEvidence:
    """Extract text and features from a single pdfplumber page."""
    text = page.extract_text() or ""
    return _extract_page_features_from_text(text, page_num)


def mine_document_pages(file_path: str, max_pages: Optional[int] = None) -> List[PageEvidence]:
    """
    Mine page-level features for a given PDF using pdfplumber.
    """
    evidence_list = []
    
    with pdfplumber.open(file_path) as pdf:
        pages_to_process = pdf.pages[:max_pages] if max_pages else pdf.pages
        for i, page in enumerate(pages_to_process, 1):
            evidence = _extract_page_features(page, i)
            evidence_list.append(evidence)
            
    return evidence_list
