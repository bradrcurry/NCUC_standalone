from __future__ import annotations

import re
import unicodedata


_SMART_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)

_ALLOWED_PUNCTUATION = {'"', "(", ")", "*", "-", "'"}
_BOOLEAN_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
_STOPWORDS = {"a", "an", "and", "for", "or", "the"}


def sanitize_ncuc_query(query: str, safe_pattern_types: set[str] | None = None) -> str:
    """
    Rewrite a query into a conservative NCUC Zoom-safe form.

    Strategy:
    - normalize unicode punctuation and whitespace
    - drop punctuation that the Zoom docs say is ignored or that often causes errors
    - keep quotes/parentheses only if their pattern families are known-safe
    - degrade unsupported Boolean syntax to simple space-separated terms
    - strip wildcard use except suffix wildcards on plain tokens
    """
    safe_pattern_types = safe_pattern_types or {"single_term", "two_term"}

    text = unicodedata.normalize("NFKC", query).translate(_SMART_CHAR_MAP)
    text = "".join(ch if ch.isalnum() or ch.isspace() or ch in _ALLOWED_PUNCTUATION else " " for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    if "quoted_phrase" not in safe_pattern_types:
        text = text.replace('"', " ")
    if "complex" not in safe_pattern_types:
        text = text.replace("(", " ").replace(")", " ")

    if "boolean_and" not in safe_pattern_types:
        text = re.sub(r"\bAND\b", " ", text, flags=re.I)
    if "boolean_or" not in safe_pattern_types:
        text = re.sub(r"\bOR\b", " ", text, flags=re.I)
    if "near" not in safe_pattern_types:
        text = re.sub(r"\bNEAR\b", " ", text, flags=re.I)
    text = re.sub(r"\bAND\s+NOT\b", " ", text, flags=re.I)
    if "boolean_and" not in safe_pattern_types:
        text = re.sub(r"\bNOT\b", " ", text, flags=re.I)

    text = _normalize_wildcards(text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    if (
        " " in text
        and '"' not in text
        and not any(f" {op} " in f" {text.upper()} " for op in _BOOLEAN_OPERATORS)
        and "two_term" not in safe_pattern_types
        and "boolean_and" in safe_pattern_types
    ):
        parts = [part for part in text.split() if part.lower() not in _STOPWORDS]
        if len(parts) >= 2:
            text = " AND ".join(parts)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def classify_pattern_type(query: str) -> str:
    upper = query.upper()
    if "NEAR" in upper:
        return "near"
    if " AND " in upper and " NOT " in upper:
        return "boolean_and"
    if " AND " in upper:
        return "boolean_and"
    if " OR " in upper:
        return "boolean_or"
    if '"' in query:
        return "quoted_phrase"
    if "(" in query or ")" in query:
        return "complex"
    if "*" in query:
        return "suffix_wildcard"
    if len(query.split()) <= 1:
        return "single_term"
    return "two_term"


def _normalize_wildcards(text: str) -> str:
    parts = []
    for token in text.split():
        if "*" not in token:
            parts.append(token)
            continue
        if re.fullmatch(r"[A-Za-z0-9-]+\*", token):
            parts.append(token)
            continue
        parts.append(token.replace("*", " "))
    return " ".join(parts)
