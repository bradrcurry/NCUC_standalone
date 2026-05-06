import re
from typing import List, Optional
from datetime import datetime
from dateutil import parser as date_parser

from duke_rates.models.pipeline import TariffSpan, DateCandidate

# Layered date pattern matchers
_DATE_FRAGMENT = r'([A-Z]+[a-z]*\s+\d{1,2}(?:,|\.|\s)+\d{4})'

PATTERNS = {
    "effective": re.compile(rf'(?i)effective(?:\s+for\s+service\s+rendered\s+on\s+and\s+after|\s+for\s+service\s+on\s+and\s+after|\s+on\s+and\s+after|\s+on\s+or\s+after|\s+on)?\s+{_DATE_FRAGMENT}'),
    "service_rendered": re.compile(rf'(?i)service rendered\s+on\s+or\s+after\s+{_DATE_FRAGMENT}'),
    "applicable": re.compile(rf'(?i)applicable\s+beginning\s+{_DATE_FRAGMENT}'),
    "issued": re.compile(rf'(?i)issued\s+(?:on\s+)?{_DATE_FRAGMENT}'),
    "superseding": re.compile(rf'(?i)superseding.*(?:effective\s+)?{_DATE_FRAGMENT}')
}

def _parse_date_str(date_str: str) -> Optional[str]:
    """Normalize extracted date string to ISO YYYY-MM-DD."""
    try:
        normalized = re.sub(r'(\d{1,2})\.(\d{4})', r'\1 \2', date_str)
        dt = date_parser.parse(normalized, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None

def extract_dates_from_span(span: TariffSpan, full_text_pages: dict[int, str]) -> List[DateCandidate]:
    """
    Extract structured date candidates from a TariffSpan using layered regex strategies.
    `full_text_pages` maps page_number -> text_content.
    """
    candidates = []
    
    # 1. Search primarily in Headers and Footers
    for snip in span.header_footer_snippets:
        for date_type, pattern in PATTERNS.items():
            for match in pattern.finditer(snip):
                raw_date = match.group(1)
                iso_date = _parse_date_str(raw_date)
                if iso_date:
                    candidates.append(DateCandidate(
                        date_value=iso_date,
                        date_type=date_type,
                        evidence_text=match.group(0),
                        page_number=span.start_page, # approximate
                        confidence=0.9 # High confidence in headers/footers
                    ))
                    
    # 2. Search body text across the full span if no strong effective date found.
    # Many tariffs place the effective date on the final page of the leaf rather
    # than the opening page.
    effective_dates = [c for c in candidates if c.date_type in ("effective", "service_rendered", "applicable")]
    
    if not effective_dates:
        for page_number in range(span.start_page, span.end_page + 1):
            page_text = full_text_pages.get(page_number)
            if not page_text:
                continue
            for date_type, pattern in PATTERNS.items():
                for match in pattern.finditer(page_text):
                    raw_date = match.group(1)
                    iso_date = _parse_date_str(raw_date)
                    if iso_date:
                        # Check if we already have this exact date to avoid duplicates
                        if not any(c.date_value == iso_date and c.date_type == date_type for c in candidates):
                            candidates.append(DateCandidate(
                                date_value=iso_date,
                                date_type=date_type,
                                evidence_text=match.group(0),
                                page_number=page_number,
                                confidence=0.7 # Medium confidence in body text
                            ))
                    
    # Update span in-place
    span.dates = sorted(candidates, key=lambda x: x.confidence, reverse=True)
    return span.dates
