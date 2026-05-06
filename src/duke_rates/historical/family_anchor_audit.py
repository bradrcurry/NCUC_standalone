from __future__ import annotations

import re


_SCHEDULE_CODE_RE = re.compile(r"\bSCHEDULE\s+([A-Z][A-Z0-9-]{0,20})\b", re.I)
_LEAF_NUMBER_RE = re.compile(r"\b(?:LEAF(?:\s+NO\.?)?|LEAF-?)\s*([0-9]{3,4})\b", re.I)
_FAMILY_LEAF_RE = re.compile(r"leaf-(\d{3,4})$", re.I)


def extract_leaf_number(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None

    family_match = _FAMILY_LEAF_RE.search(text)
    if family_match:
        return family_match.group(1)

    leaf_match = _LEAF_NUMBER_RE.search(text)
    if leaf_match:
        return leaf_match.group(1)

    bare_match = re.search(r"(\d{3,4})", text)
    if bare_match and text.lower().startswith("leaf-"):
        return bare_match.group(1)
    return None


def extract_schedule_code_hint(value: str | None) -> str | None:
    match = _SCHEDULE_CODE_RE.search(value or "")
    if not match:
        return None
    raw = match.group(1).strip()
    if raw.upper() != raw:
        return None
    return _normalize_code(raw)


def detect_current_family_anchor_mismatch(
    *,
    family_key: str,
    family_schedule_code: str | None,
    document_tariff_identifier: str | None,
    document_schedule_code: str | None,
    document_title: str | None,
    page_headings: list[str] | None = None,
    page_leaf_nos: list[str] | None = None,
) -> list[str]:
    reasons: list[str] = []

    family_leaf = extract_leaf_number(family_key)
    document_leaf = extract_leaf_number(document_tariff_identifier)
    if family_leaf and document_leaf and family_leaf != document_leaf:
        reasons.append("tariff_identifier_leaf_mismatch")

    mined_leafs = {leaf for leaf in (page_leaf_nos or []) if leaf}
    if (
        family_leaf
        and mined_leafs
        and family_leaf not in mined_leafs
        and (not document_leaf or document_leaf != family_leaf)
    ):
        reasons.append("mined_leaf_mismatch")

    expected_code = _normalize_code(family_schedule_code)
    document_code = _normalize_code(document_schedule_code)
    if expected_code and document_code and expected_code != document_code:
        reasons.append("document_schedule_code_mismatch")

    mined_code = None
    for heading in page_headings or []:
        mined_code = extract_schedule_code_hint(heading)
        if mined_code:
            break
    if (
        expected_code
        and mined_code
        and expected_code != mined_code
        and (not document_code or document_code != expected_code)
    ):
        reasons.append("mined_schedule_code_mismatch")

    if (
        expected_code
        and not document_code
        and not mined_code
        and document_title
        and expected_code not in _normalize_code(document_title or "")
    ):
        reasons.append("schedule_code_not_supported_by_title")

    return reasons


def _normalize_code(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "", value.upper())
    return normalized or None
