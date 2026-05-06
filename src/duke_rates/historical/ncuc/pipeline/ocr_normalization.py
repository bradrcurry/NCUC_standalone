from __future__ import annotations

import re

_WHOLE_WORD_REPLACEMENTS = {
    "Julv": "July",
    "Januarv": "January",
    "Februarv": "February",
    "Mav": "May",
    "Jnne": "June",
    "Angust": "August",
    "Septemher": "September",
    "Octoher": "October",
    "Novemher": "November",
    "Decemher": "December",
    "senice": "service",
    "streel": "street",
    "Floodlighl": "Floodlight",
    # Additional OCR corruption patterns
    "Febmary": "February",
    "Februay": "February",
    "Novenber": "November",
    "Deceinber": "December",
    "Aprll": "April",
    "Augnst": "August",
    "Septembcr": "September",
    "Octobcr": "October",
    "Jaiuary": "January",
}

# Garbage characters from decorative fonts, table borders, and OCR artifacts.
# These are single characters that appear as noise in OCR output.
_GARBAGE_CHARS_RE = re.compile(r"[═-╬─-┼▀-▟■-◿★☆✦-✧¦¬±¶¨]")

# Repeated garbage patterns (3+ decorative/drawing chars in a row)
_GARBAGE_LINE_RE = re.compile(r"^[\s═-╬─-┼▀-▟■-◿¦¬±¶¨\-_=]{3,}\s*$", re.MULTILINE)

# Digit↔letter OCR corruption in numeric contexts (dollar/cent amounts, decimals)
# These only apply when adjacent to digits — avoids corrupting regular words
_DIGIT_LETTER_FIXES = [
    # "5" OCR'd as "S" before digits: S123.45 → $123.45, S0.50 → $0.50
    (re.compile(r"(?<!\w)[5S](?=\d+\.\d{2})"), "$"),
    # "l" (lowercase L) OCR'd as "1" in rate labels: "1arge" → "Large"
    (re.compile(r"\b1(?=arge|ight|ighting)\b", re.I), "l"),
    # "0" OCR'd as "O" in numeric positions: "O.50" → "0.50" (but not "TO" → "T0")
    (re.compile(r"(?<=[\s(])([Oo])(?=\.\d{2})"), "0"),
    # "l" OCR'd as "1" between digits: "1.50" is correct, but "l.50" → "1.50"
    (re.compile(r"(?<=[\s(])l(?=\.\d{2})"), "1"),
]


def _apply_whole_word_replacements(text: str) -> str:
    normalized = text
    for raw, fixed in _WHOLE_WORD_REPLACEMENTS.items():
        normalized = re.sub(rf"\b{re.escape(raw)}\b", fixed, normalized)
    return normalized


def _apply_digit_letter_fixes(text: str) -> str:
    """Fix OCR digit↔letter corruption in numeric contexts only."""
    normalized = text
    for pattern, replacement in _DIGIT_LETTER_FIXES:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def _remove_garbage_characters(text: str) -> str:
    """Strip decorative font chars, box-drawing, and OCR noise artifacts."""
    text = _GARBAGE_CHARS_RE.sub(" ", text)
    text = _GARBAGE_LINE_RE.sub("", text)
    return text


def _fix_whitespace_fragmentation(text: str) -> str:
    """Repair OCR whitespace fragmentation in numeric values and text flow.

    Handles:
    - $14.\\n00 → $14.00 (line-break in the middle of a decimal amount)
    - 1,234.\\n56 → 1,234.56
    - Sentence frag\\nments → Sentence fragments (single line-break join)
    """
    # Line-break in the middle of a decimal amount: "14.\n00" → "14.00"
    text = re.sub(r"(\d+\.)\s*\n\s*(\d{2})(?!\d)", r"\1\2", text)
    # Line-break after comma in thousands: "1,\n234.56" → "1,234.56"
    text = re.sub(r"(\d+),?\s*\n\s*(\d{3}\.\d{2})", r"\1,\2", text)
    return text


def _fix_column_merge(text: str) -> str:
    """Prevent OCR column merging by normalizing inter-column whitespace.

    When OCR merges text from adjacent columns, the result often has
    excessive spaces between words that should be in separate columns.
    We normalize to single spaces and let the parser regexes handle
    multi-column layouts.

    Also collapses 3+ consecutive spaces (likely column gaps) to a single space.
    """
    text = re.sub(r" {3,}", "  ", text)
    return text


def normalize_ocr_text(text: str) -> str:
    if not text:
        return text

    normalized = text.replace("�", "")
    normalized = _remove_garbage_characters(normalized)
    normalized = _apply_whole_word_replacements(normalized)
    normalized = _fix_whitespace_fragmentation(normalized)
    normalized = _apply_digit_letter_fixes(normalized)
    # lO → 10 (lowercase-L uppercase-O looks like "ten" in OCR)
    normalized = re.sub(r"\blO(?=\.\d)", "10", normalized)
    # I → 1 before decimal (I.50 → 1.50)
    normalized = re.sub(r"\bI(?=\.\d)", "1", normalized)
    # S/5 before digit sequence → $
    normalized = re.sub(r"\b[5S](?=\d+\.\d+)", "$", normalized)
    # ^, ?, ! after digit and before /kwh → cent sign
    normalized = re.sub(r"(?<=\d)(?:[\^?!]{1,3})(?=\s*(?:per\s+kwh|/kwh|kwh|$))", "¢", normalized, flags=re.I)
    # £ after digit → cent sign
    normalized = re.sub(r"(?<=\d)\s*£(?=\s*(?:\d|per\s+kwh|/kwh|$|\n))", "¢", normalized, flags=re.I)
    # Trailing " 0" at end of line after a decimal → cent sign
    normalized = re.sub(r"(\d+\.\d+)\s+0\s*(\n|$)", r"\1¢\2", normalized)
    # "fi" ligature after decimal → cent sign
    normalized = re.sub(r"(\d+\.\d+)\s+fi\s*(\n|$)", r"\1¢\2", normalized)
    # Dollar sign spacing: "$ 14.00" → "$14.00"
    normalized = re.sub(r"\$\s+(\d+\.\d+)", r"$\1", normalized)
    # Column merge prevention
    normalized = _fix_column_merge(normalized)
    # Whitespace normalization
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    return normalized.strip()


# Markdown patterns produced by Docling's export_to_markdown(). Parsers were
# tuned against pdfplumber's flat output, so we flatten Docling's markdown
# back to a comparable shape before parser regexes run.
_MD_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_MD_TABLE_SEPARATOR_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$", re.MULTILINE)
_MD_TABLE_ROW_RE = re.compile(r"^[ \t]*\|(.*)\|[ \t]*$", re.MULTILINE)


def _flatten_markdown_table_row(match: "re.Match[str]") -> str:
    """Convert ``| a | b | c |`` to ``a  b  c`` (two spaces between cells).

    Two-space gaps preserve column distinguishability for downstream regexes
    that look for column-aligned values (rates, kWh figures).
    """
    inner = match.group(1)
    cells = [cell.strip() for cell in inner.split("|")]
    return "  ".join(cell for cell in cells if cell)


def normalize_docling_markdown(text: str) -> str:
    """Flatten Docling's markdown export so existing parsers can read it.

    Docling's ``document.export_to_markdown()`` uses ``## Header`` lines,
    GitHub-flavored ``| col | col |`` tables, and ``<!-- image -->`` HTML
    comments. Parser profiles were calibrated against pdfplumber's flat
    text and don't recognize these markers. Apply this BEFORE the universal
    ``normalize_ocr_text`` step so the OCR fixups see plain text.

    Idempotent — safe to call on already-flat pdfplumber text.
    """
    if not text:
        return text
    normalized = _MD_HTML_COMMENT_RE.sub("", text)
    # Drop the |---|---| separator rows entirely (they carry no data).
    normalized = _MD_TABLE_SEPARATOR_RE.sub("", normalized)
    # Convert |a|b|c| rows to whitespace-delimited.
    normalized = _MD_TABLE_ROW_RE.sub(_flatten_markdown_table_row, normalized)
    # Strip ## header markers but keep the heading text as a regular line.
    normalized = _MD_HEADER_RE.sub("", normalized)
    return normalized


def normalize_ocr_money_line(line: str) -> str:
    normalized = normalize_ocr_text(line)
    normalized = normalized.replace("SI ", "$1").replace("S1 ", "$1").replace("5I ", "$1")
    normalized = re.sub(r"\b[5S](?=\d+\.\d+)", "$", normalized)
    normalized = re.sub(r"\$\s+(\d+\.\d+)", r"$\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def normalize_ocr_label(label: str) -> str:
    normalized = normalize_ocr_text(label)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
