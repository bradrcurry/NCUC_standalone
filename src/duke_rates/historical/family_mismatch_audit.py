from __future__ import annotations

import re

from duke_rates.utils.duke_company import normalize_duke_company


_SCHEDULE_CODE_RE = re.compile(r"\bSCHEDULE\s+([A-Z][A-Z0-9-]{0,15})\b", re.I)
_RIDER_CODE_RE = re.compile(r"\bRIDER\s+([A-Z][A-Z0-9-]{0,15})\b", re.I)
_SUMMARY_HEADING_RE = re.compile(r"\bSUMMARY OF RIDER(?:\s+ADJUSTMENTS)?\b", re.I)
_LEAF_NO_RE = re.compile(r"\bLEAF\s+NO\.?\s*(\d{1,4})\b", re.I)


def expected_company_from_family_key(family_key: str | None) -> str | None:
    normalized = (family_key or "").lower()
    if normalized.startswith("nc-progress-"):
        return "progress"
    if normalized.startswith("nc-carolinas-"):
        return "carolinas"
    return None


def extract_schedule_code_hint(text: str) -> str | None:
    match = _SCHEDULE_CODE_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1).strip()
    if raw.upper() != raw:
        return None
    return _normalize_code(raw)


def extract_rider_code_hint(text: str) -> str | None:
    matches = list(_RIDER_CODE_RE.finditer(text or ""))
    for match in reversed(matches):
        raw = match.group(1).strip()
        if raw.upper() != raw:
            continue
        return _normalize_code(f"RIDER {raw}")
    return None


def _expected_code_present_in_text(
    text: str,
    expected_code: str | None,
    *,
    is_rider: bool,
) -> bool:
    """Return True if the expected schedule/rider code appears in the document text.

    Strategy:
      - For ANY length: word-boundary regex match for the bare code, "RIDER X",
        and "SCHEDULE X". This precisely matches short codes like "EE" or "PM"
        without false positives from substrings (e.g. "FEE" doesn't match "EE").
      - For codes ≥5 chars: also a normalized-substring fallback. This handles
        multi-word rider names like "BPM PROSPECTIVE RIDER" matching the
        expected="BPMPROSPECTIVERIDER" — the existing SCHEDULE/RIDER regexes
        only capture a single word after the keyword.
    """
    if not expected_code:
        return False
    normalized = _normalize_code(expected_code)
    if not normalized:
        return False
    upper_text = (text or "").upper()
    if not upper_text:
        return False
    # Build a separator-tolerant pattern: "OPTE" → "O[\s\-_]*P[\s\-_]*T[\s\-_]*E"
    # so the normalized code matches "OPT-E", "OPT E", and "OPTE" alike.
    sep_between = r"[\s\-_]*"
    sep_pattern = sep_between.join(re.escape(ch) for ch in normalized)
    word_boundary_patterns = (
        rf"\b{sep_pattern}\b",
        rf"\bRIDER\s+{sep_pattern}\b",
        rf"\bSCHEDULE\s+{sep_pattern}\b",
    )
    for pattern in word_boundary_patterns:
        if re.search(pattern, upper_text):
            return True
    # Fallback: normalized substring for longer codes (≥5 chars) — handles
    # multi-word rider names where regex word-boundary won't catch the whole code
    # (e.g. "BPM PROSPECTIVE RIDER" → "BPMPROSPECTIVERIDER").
    if len(normalized) >= 5:
        normalized_text = re.sub(r"[^A-Z0-9]+", "", upper_text)
        if normalized in normalized_text:
            return True
    base = _base_code(expected_code)
    if base and base != normalized and len(base) >= 3:
        base_sep = sep_between.join(re.escape(ch) for ch in base)
        if re.search(rf"\b{base_sep}\b", upper_text):
            return True
    return False


def detect_historical_family_mismatch(
    *,
    family_key: str,
    family_schedule_code: str | None,
    text: str,
    state: str = "NC",
) -> list[str]:
    reasons: list[str] = []

    expected_company = expected_company_from_family_key(family_key)
    inferred_company = normalize_duke_company(text, fallback=None, state=state)
    if expected_company and inferred_company and inferred_company != expected_company:
        reasons.append("company_text_mismatch")

    expected_code = _normalize_code(family_schedule_code)
    is_rider = "-rider-" in (family_key or "").lower()

    if expected_code and not expected_code.startswith("PROGRAM"):
        # If the expected code is mentioned anywhere in the text, the doc is
        # presumed correctly classified (multi-schedule bundles and riders-list
        # references commonly contain other codes alongside the right one).
        if not _expected_code_present_in_text(text, expected_code, is_rider=is_rider):
            # Expected code missing — look for any other code that IS present.
            # For rider families: a misbinding usually drops onto SCHEDULE text,
            # so we check both rider AND schedule extractors and flag whichever
            # produces a non-equivalent code.
            schedule_found = extract_schedule_code_hint(text)
            rider_found = extract_rider_code_hint(text)
            mismatch_codes: list[str] = []
            for code in (rider_found, schedule_found):
                if code and not _codes_equivalent(expected_code, code):
                    mismatch_codes.append(code)
            if mismatch_codes:
                reasons.append("schedule_code_mismatch")

    summary_leaf = _summary_leaf_for_family_key(family_key)
    assigned_leaf = _assigned_leaf_from_family_key(family_key)
    if summary_leaf and assigned_leaf:
        found_leafs = extract_leaf_no_hints(text)
        if (
            _looks_like_rider_summary(text)
            and assigned_leaf != summary_leaf
            and found_leafs.intersection(_known_summary_leafs_for_company(family_key))
        ):
            reasons.append("summary_sheet_family_mismatch")

    return reasons


def _normalize_code(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "", value.upper())
    return normalized or None


def _base_code(value: str | None) -> str | None:
    normalized = _normalize_code(value)
    if not normalized:
        return None
    normalized = re.sub(r"RY\d+$", "", normalized)
    normalized = re.sub(r"\d+$", "", normalized)
    return normalized or None


def _codes_equivalent(expected: str | None, found: str | None) -> bool:
    if not expected or not found:
        return False
    if expected == found:
        return True
    # Strip "RIDER" / "SCHEDULE" prefixes from both sides — `tariff_families`
    # stores rider schedule_code as the bare suffix (e.g. "EE") while the
    # extractor returns "RIDEREE" form. Without this, every rider family with
    # a short code would be falsely flagged as a mismatch.
    def _strip_keyword_prefix(code: str) -> str:
        for prefix in ("RIDER", "SCHEDULE"):
            if code.startswith(prefix) and len(code) > len(prefix):
                return code[len(prefix):]
        return code
    e_stripped = _strip_keyword_prefix(expected)
    f_stripped = _strip_keyword_prefix(found)
    if e_stripped == f_stripped:
        return True
    return _base_code(e_stripped) == _base_code(f_stripped)


def extract_leaf_no_hints(text: str) -> set[str]:
    return {match.group(1) for match in _LEAF_NO_RE.finditer(text or "")}


def _looks_like_rider_summary(text: str) -> bool:
    normalized = (text or "").upper()
    if _SUMMARY_HEADING_RE.search(normalized):
        return True
    return (
        "THE FOLLOWING IS A SUMMARY OF RIDER" in normalized
        and "EFFECTIVE FOR SERVICE" in normalized
    )


def _summary_leaf_for_family_key(family_key: str | None) -> str | None:
    normalized = (family_key or "").lower()
    if normalized.startswith("nc-progress-"):
        return "600"
    if normalized.startswith("nc-carolinas-"):
        return "99"
    return None


def _known_summary_leafs_for_company(family_key: str | None) -> set[str]:
    summary_leaf = _summary_leaf_for_family_key(family_key)
    return {summary_leaf} if summary_leaf else set()


def _assigned_leaf_from_family_key(family_key: str | None) -> str | None:
    match = re.search(r"leaf-(\d{1,4})$", (family_key or "").lower())
    if not match:
        return None
    return match.group(1)
