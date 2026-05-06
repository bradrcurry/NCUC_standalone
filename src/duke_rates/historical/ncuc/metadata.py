"""NCUC metadata extraction and normalization from HTML pages and document URLs."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from duke_rates.models.ncuc import NcucFilingClassification
from duke_rates.utils.duke_company import is_duke_company_related

# ---------------------------------------------------------------------------
# Known Duke Progress NC rate/rider codes for relevance scoring
# ---------------------------------------------------------------------------

PRIORITY_SCHEDULE_CODES = {
    "501", "503", "504", "571", "572",
    "602", "604", "605", "607", "609",
    "610", "611", "613", "640", "662",
    "670", "672",
}

SCHEDULE_CODE_PAT = re.compile(
    r"\b(?:schedule|rate schedule|RS|tariff)\s*(?:no\.?\s*)?([A-Z]?\d{2,4}(?:-[A-Z0-9]+)?)\b",
    re.IGNORECASE,
)

RIDER_CODE_PAT = re.compile(
    r"\b(?:rider|clause|adjustment)\s+([A-Z]{1,3}\d*|[A-Z]+(?:-\d+)?)\b",
    re.IGNORECASE,
)

LEAF_NO_PAT = re.compile(
    r"\bleaf\s*(?:no\.?\s*)?(\d+[A-Z]?)\b",
    re.IGNORECASE,
)

DOCKET_PAT = re.compile(
    r"\bE-(\d+)\s*,?\s*(?:Sub\s*(\d+))?\b",
    re.IGNORECASE,
)

DATE_PAT = re.compile(
    r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b"
    r"|\b(\d{4})-(\d{2})-(\d{2})\b"
)

CLASSIFICATION_KEYWORDS: dict[NcucFilingClassification, list[str]] = {
    NcucFilingClassification.ORDER: ["order", "final order", "interim order", "commission order"],
    NcucFilingClassification.NOTICE: ["notice", "public notice", "notice of hearing"],
    NcucFilingClassification.COMPLIANCE_FILING: [
        "compliance filing", "compliance report", "annual report", "semi-annual"
    ],
    NcucFilingClassification.TARIFF_SHEETS: [
        "tariff sheet", "tariff pages", "rate schedule", "tariff filing", "revised tariff",
        "tariff revision",
    ],
    NcucFilingClassification.EXHIBIT: ["exhibit", "exh.", "exh "],
    NcucFilingClassification.TESTIMONY: ["testimony", "direct testimony", "rebuttal testimony"],
    NcucFilingClassification.ATTACHMENT: ["attachment", "appendix", "enclosure"],
    NcucFilingClassification.APPLICATION: ["application", "petition", "motion"],
    NcucFilingClassification.SETTLEMENT: ["settlement", "stipulation", "agreement"],
}

def extract_docket_from_text(text: str) -> tuple[str | None, str | None]:
    """Return (docket_number, sub_number) from free text."""
    m = DOCKET_PAT.search(text)
    if m:
        base = m.group(1)
        sub = m.group(2)
        docket = f"E-{base}"
        return docket, sub
    return None, None


def extract_docket_from_url(url: str) -> tuple[str | None, str | None]:
    """Parse docket/sub from NCUC viewer URL query parameters."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    # NCUC eDocket uses CaseSub, CaseYear, Utility, Sub params
    # e.g. ?CaseSub=10&CaseYear=2014&Utility=E
    docket = None
    sub = None
    utility = qs.get("Utility", [None])[0]
    case_sub = qs.get("CaseSub", [None])[0]
    case_year = qs.get("CaseYear", [None])[0]
    # Also handles /ViewDocket/E-7 style paths
    path_m = re.search(r"/([A-Z]-\d+)", parsed.path)
    if path_m:
        docket = path_m.group(1)
    elif utility and case_sub:
        docket = f"{utility}-{case_sub}"
    sub_param = qs.get("Sub", [None])[0]
    if sub_param:
        sub = sub_param
    return docket, sub


def extract_schedule_codes(text: str) -> list[str]:
    codes = []
    for m in SCHEDULE_CODE_PAT.finditer(text):
        code = m.group(1).lstrip("0") or m.group(1)
        # Normalize: strip leading zeros but keep 3+ digit forms
        if code and len(code) >= 2:
            codes.append(code.upper())
    return list(dict.fromkeys(codes))


def extract_rider_codes(text: str) -> list[str]:
    codes = []
    for m in RIDER_CODE_PAT.finditer(text):
        codes.append(m.group(1).upper())
    return list(dict.fromkeys(codes))


def extract_leaf_nos(text: str) -> list[str]:
    nos = []
    for m in LEAF_NO_PAT.finditer(text):
        nos.append(m.group(1))
    return list(dict.fromkeys(nos))


def classify_filing(text: str) -> NcucFilingClassification:
    text_lower = text.lower()
    for classification, keywords in CLASSIFICATION_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return classification
    return NcucFilingClassification.OTHER


def is_duke_progress_related(text: str) -> bool:
    return is_duke_company_related(text, "progress")


def score_relevance(
    title: str | None,
    docket: str | None,
    schedule_codes: list[str],
    rider_codes: list[str],
) -> float:
    """Return 0.0–1.0 relevance score for this NCUC document lead."""
    score = 0.0
    text = (title or "").lower()

    # Utility match
    if is_duke_progress_related(text):
        score += 0.3

    # Docket match (E-2 is Duke Energy Progress NC)
    if docket and docket.startswith("E-2"):
        score += 0.3
    elif docket and docket.startswith("E-"):
        score += 0.1

    # Priority schedule/rider codes
    for code in schedule_codes:
        if code in PRIORITY_SCHEDULE_CODES:
            score += 0.2
            break

    # Any schedule or rider
    if schedule_codes or rider_codes:
        score += 0.1

    # Rate/tariff/rider content keywords
    rate_kws = ["rate", "tariff", "rider", "surcharge", "adjustment", "schedule"]
    if any(kw in text for kw in rate_kws):
        score += 0.1

    return min(score, 1.0)


def normalize_filing_date(raw: str | None) -> str | None:
    """Attempt to normalize various date formats to YYYY-MM-DD."""
    if not raw:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2})$", raw)
    if m:
        year = int(m.group(3))
        year = 2000 + year if year < 50 else 1900 + year
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return raw  # return as-is if can't parse
