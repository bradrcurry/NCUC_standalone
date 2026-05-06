from __future__ import annotations

import re

REVISION_LINE_RE = re.compile(
    r"^\s*((?:NC|SC|FL|OH|IN|KY)\s+.+?Leaf No\. ?[A-Z0-9 -]+)\s*$",
    re.I | re.M,
)
SUPERSEDES_LINE_RE = re.compile(
    r"^\s*Superseding\s+((?:NC|SC|FL|OH|IN|KY)\s+.+?Leaf No\. ?[A-Z0-9 -]+)\s*$",
    re.I | re.M,
)
LEAF_NO_RE = re.compile(r"Leaf No\. ?([A-Z]*\s*\d+)", re.I)
EFFECTIVE_RANGE_RE = re.compile(
    (
        r"Effective(?:\s+for service rendered)?\s+from\s+"
        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})\s+through\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})"
    ),
    re.I,
)
EFFECTIVE_ON_AFTER_RE = re.compile(
    (
        r"Effective(?:\s+for service rendered)?\s+"
        r"(?:on\s+and\s+after|for service rendered on and after)\s+"
        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})"
    ),
    re.I,
)


def extract_historical_metadata(text: str) -> dict[str, str | None]:
    revision_label = _first_unique_match(REVISION_LINE_RE, text)
    supersedes_label = _first_unique_match(SUPERSEDES_LINE_RE, text)
    leaf_no = _extract_leaf_no(revision_label) or _extract_leaf_no(supersedes_label)

    effective_start = None
    effective_end = None
    range_match = EFFECTIVE_RANGE_RE.search(text)
    if range_match:
        effective_start = range_match.group(1)
        effective_end = range_match.group(2)
    else:
        on_after_match = EFFECTIVE_ON_AFTER_RE.search(text)
        if on_after_match:
            effective_start = on_after_match.group(1)

    return {
        "revision_label": revision_label,
        "supersedes_label": supersedes_label,
        "leaf_no": leaf_no,
        "effective_start": effective_start,
        "effective_end": effective_end,
    }


def _first_unique_match(pattern: re.Pattern[str], text: str) -> str | None:
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        value = " ".join(match.group(1).split())
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values[0] if values else None


def _extract_leaf_no(label: str | None) -> str | None:
    if not label:
        return None
    match = LEAF_NO_RE.search(label)
    return " ".join(match.group(1).split()) if match else None
