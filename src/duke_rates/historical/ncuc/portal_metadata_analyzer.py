"""
NCUC Portal Metadata Analyzer - Extract quality signals from search results.

Before downloading documents, analyze the portal metadata (titles, dates, descriptions)
to identify high-quality tariff documents vs. procedural filings.

Focuses on signals that correlate with tariff quality:
- Document type keywords (Compliance Tariffs, Exhibits, Rider)
- Date patterns (recent > old for compliance, but need historical versions too)
- Filing description content
- Associated docket/exhibit structure
"""
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional
import re


@dataclass
class DocumentQualitySignals:
    """Quality indicators extracted from portal metadata."""
    is_compliance_filing: bool  # "Compliance Tariff", "Compliance Book", "Exhibit"
    is_rider_document: bool     # "Rider", "Adjustment", "Schedule"
    is_order_or_approval: bool  # Final order vs. testimony/brief
    has_effective_date: bool    # Explicit effective date mentioned
    filing_type: Optional[str]  # Type inferred from title
    confidence: float           # 0.0-1.0, quality confidence
    quality_tier: str           # "high" | "medium" | "low"
    reason: str                 # Why this tier was assigned


# Keywords that indicate high-quality tariff documents
COMPLIANCE_KEYWORDS = [
    "compliance tariff",
    "compliance book",
    "compliance filing",
    "exhibit",
    "schedule",
    "rider",
    "rate schedule",
    "tariff sheet",
    "tariff",
]

QUALITY_KEYWORDS = [
    "effective",
    "approval",
    "approved",
    "order",
    "final",
]

PROCEDURAL_KEYWORDS = [
    "petition",
    "intervene",
    "motion",
    "brief",
    "testimony",
    "interrogatory",
    "discovery",
    "settlement agreement",
    "stipulation",
    "hearing",
]

REDLINE_KEYWORDS = [
    "proposed",
    "marked up",
    "redline",
    "revised",
    "draft",
    "strike",
]

ORDER_APPROVAL_KEYWORDS = [
    "order approving",
    "order approving application",
    "order approving fuel",
    "order approving rider",
    "approval of tariff",
    "order authorizing",
    "approving compliance",
]

RATE_CHANGE_KEYWORDS = [
    "revised rate tariffs",
    "revised tariff",
    "tariff compliance",
    "compliance filing",
    "compliance tariffs",
    "fuel charge",
    "fuel adjustment",
    "change in rates",
    "change in rates based solely on fuel",
]


def assess_document_quality(title: str, description: Optional[str] = None) -> DocumentQualitySignals:
    """
    Assess document quality from portal metadata.

    Returns quality signals and confidence scoring.
    """
    title_lower = (title or "").lower()
    desc_lower = (description or "").lower()
    combined = title_lower + " " + desc_lower

    # Check for compliance/tariff document
    is_compliance = any(kw in combined for kw in COMPLIANCE_KEYWORDS)
    is_rider = any(kw in combined for kw in ["rider", "adjustment", "schedule"])
    is_order = any(kw in combined for kw in ["order", "approval", "approved"])
    has_effective = any(kw in combined for kw in ["effective", "effect"])

    # Detect redline indicators (quality concern)
    has_redline_indicators = any(kw in combined for kw in REDLINE_KEYWORDS)

    # Detect procedural (lower quality)
    is_procedural = any(kw in combined for kw in PROCEDURAL_KEYWORDS)

    # Scoring logic
    confidence = 0.0
    quality_tier = "low"
    reason = ""

    if is_procedural and not is_compliance:
        quality_tier = "low"
        confidence = 0.1
        reason = "Procedural document (motion/brief/testimony), not a tariff"
    elif is_compliance and is_order:
        quality_tier = "high"
        confidence = 0.95
        reason = "Compliance tariff + Final order (most reliable)"
    elif is_compliance and has_effective:
        quality_tier = "high"
        confidence = 0.90
        reason = "Compliance tariff with effective date"
    elif is_compliance:
        quality_tier = "medium"
        confidence = 0.75
        reason = "Compliance document but missing effective date"
    elif is_rider and is_order:
        quality_tier = "high"
        confidence = 0.85
        reason = "Rider document + Order"
    elif is_rider:
        quality_tier = "medium"
        confidence = 0.65
        reason = "Rider-related document"
    else:
        quality_tier = "low"
        confidence = 0.3
        reason = "Generic or non-tariff content"

    # Adjust for redline indicators
    if has_redline_indicators:
        confidence *= 0.7  # Reduce confidence if redline indicators present
        quality_tier = "medium" if quality_tier == "high" else "low"
        reason += " [redline_candidate]"

    filing_type = None
    if is_order:
        filing_type = "order"
    elif is_compliance:
        filing_type = "compliance_tariff"
    elif is_rider:
        filing_type = "rider_adjustment"
    else:
        filing_type = "other"

    return DocumentQualitySignals(
        is_compliance_filing=is_compliance,
        is_rider_document=is_rider,
        is_order_or_approval=is_order,
        has_effective_date=has_effective,
        filing_type=filing_type,
        confidence=confidence,
        quality_tier=quality_tier,
        reason=reason,
    )


def extract_date_from_title(title: str) -> Optional[str]:
    """Extract date from document title if present."""
    # Look for patterns like "12/01/2025" or "December 1, 2025" or "12.01.2025"
    date_patterns = [
        r'\d{1,2}/\d{1,2}/\d{4}',      # MM/DD/YYYY
        r'\d{1,2}\.\d{1,2}\.\d{4}',    # MM.DD.YYYY
        r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, \d{4}',  # Month DD, YYYY
    ]

    for pattern in date_patterns:
        match = re.search(pattern, title, re.I)
        if match:
            return match.group(0)
    return None


def extract_rider_codes(title: str) -> list[str]:
    """Extract rider codes from title (JAA, STS, RES, etc.)."""
    # Common Duke rider codes
    rider_patterns = [
        r'\b(JAA|STS|RES|PPM|RDM|DSM|EDIT|REPS)\b',  # Explicit codes
        r'Rider\s+([A-Z0-9-]+)',                       # "Rider ABC"
        r'(Joint Agency Asset Rider)',                  # Full names
        r'(Storm Securitization)',
        r'(Revenue Decoupling)',
        r'(Renewable Energy)',
        r'(Purchased Power)',
        r'(Demand.{0,5}Side)',
        r'(Deferred Income Tax)',
    ]

    codes = []
    for pattern in rider_patterns:
        matches = re.findall(pattern, title, re.I)
        codes.extend(matches)

    return list(set(codes))  # Deduplicate


def score_portal_result(title: str, date_filed: Optional[str] = None,
                       description: Optional[str] = None) -> dict:
    """
    Score a portal search result for likelihood of being high-quality tariff.

    Returns dict with scoring breakdown.
    """
    quality_signals = assess_document_quality(title, description)
    extracted_date = extract_date_from_title(title) or date_filed
    rider_codes = extract_rider_codes(title)

    score_breakdown = {
        "title": title,
        "quality_tier": quality_signals.quality_tier,
        "confidence": quality_signals.confidence,
        "reason": quality_signals.reason,
        "filing_type": quality_signals.filing_type,
        "extracted_date": extracted_date,
        "rider_codes": rider_codes,
        "signals": {
            "is_compliance": quality_signals.is_compliance_filing,
            "is_rider": quality_signals.is_rider_document,
            "is_order": quality_signals.is_order_or_approval,
            "has_effective_date": quality_signals.has_effective_date,
        }
    }

    return score_breakdown


def filter_high_quality_results(results: list[dict], min_confidence: float = 0.75) -> list[dict]:
    """
    Filter portal search results to high-quality candidates.

    Each result dict should have: title, date_filed (optional), description (optional)
    """
    scored = []
    for result in results:
        score = score_portal_result(
            result.get("title", ""),
            result.get("date_filed"),
            result.get("description"),
        )
        if score["confidence"] >= min_confidence:
            scored.append({**result, **score})

    return sorted(scored, key=lambda x: x["confidence"], reverse=True)


def _normalize_blob(parts: Iterable[str | None]) -> str:
    return " ".join((part or "").strip().lower() for part in parts if (part or "").strip())


def has_order_approval_signal(*parts: str | None) -> bool:
    blob = _normalize_blob(parts)
    return any(token in blob for token in ORDER_APPROVAL_KEYWORDS)


def has_rate_change_signal(*parts: str | None) -> bool:
    blob = _normalize_blob(parts)
    return any(token in blob for token in RATE_CHANGE_KEYWORDS)


def has_structural_rate_case_pair(entries: Iterable[tuple[str | None, str | None]]) -> bool:
    """
    Return True when a docket appears to contain both an approving order and a
    tariff/compliance filing.

    This catches annual fuel and rider proceedings whose individual document
    titles are generic, but whose docket composition is highly specific.
    """
    has_order = False
    has_rate_doc = False
    for title, description in entries:
        if has_order_approval_signal(title, description):
            has_order = True
        if has_rate_change_signal(title, description):
            has_rate_doc = True
        if has_order and has_rate_doc:
            return True
    return False
