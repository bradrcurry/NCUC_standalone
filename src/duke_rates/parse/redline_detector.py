"""
Redline document detector for Duke Energy tariff PDFs.

Redline filings show proposed changes to tariff sheets using:
  1. Red-colored text (most common in modern filings) — RGB values with
     high red channel and low green/blue, typically #c00000 or #ff0000.
  2. Strikethrough text — PDF span flag bit 3 (value 8), or horizontal
     line drawings overlapping text.
  3. Tracked-changes annotations — PDF annotation types like "StrikeOut",
     "Highlight", or "Underline" on regions of text.
  4. "Redline" keyword in document text or filename.
  5. Two versions of the same value on adjacent lines (old value struck,
     new value alongside).

Important nuance: Duke Energy tariff *index* pages legitimately use
dark red (#c00000) for newly-added leaf entries in the table of contents.
These are NOT redline documents — the red marks the index entry, not
changed tariff text.  We distinguish these by checking whether the red
text appears in the *body* of rate schedules (numeric rates, rider names
in a rates section) vs. only in an index table.

PyMuPDF (fitz) provides all signals we need:
  - ``span['color']`` — integer RGB for text color
  - ``span['flags']`` — bitmask including strikethrough (bit 3)
  - ``page.annots()`` — PDF annotation objects with type names
  - ``page.get_drawings()`` — vector paths (thin horizontal lines = strikethroughs)

This module returns a ``RedlineSignals`` dataclass with confidence score
and the signals that triggered it.  The caller decides what to do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RedlineSignals:
    """Detected redline signals for one document."""
    is_redline: bool
    confidence: float                       # 0.0 – 1.0
    signals: list[str] = field(default_factory=list)
    red_text_samples: list[str] = field(default_factory=list)
    strikethrough_samples: list[str] = field(default_factory=list)
    annotation_types: list[str] = field(default_factory=list)
    # Set True when red text is only in index/TOC context, not body rates
    red_is_index_only: bool = False


# ---------------------------------------------------------------------------
# Color thresholds
# ---------------------------------------------------------------------------

def _is_red(color_int: int) -> bool:
    """True if color looks like a redline red (high R, low G+B)."""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return r > 150 and g < 100 and b < 100


def _is_dark_red_index(color_int: int) -> bool:
    """True if color matches the Duke index dark red #c00000 (192,0,0)."""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return 180 <= r <= 200 and g == 0 and b == 0


# Patterns that suggest index/TOC context rather than rate-body context
_INDEX_CONTEXT_RE = re.compile(
    r"\*\*Leaf\s+\d+|Table\s+of\s+Contents|Index\s+of|Leaf\s+Index",
    re.I,
)

# Patterns suggesting rate body content (numeric rates, section headers)
_RATE_BODY_RE = re.compile(
    r"\d+\.\d+\s*(?:cents?|¢|\$|/kWh|/kW)|"
    r"MONTHLY\s+RATE|SCHEDULE\s+[A-Z]|Rider\s+[A-Z]|"
    r"Basic\s+Customer\s+Charge|Energy\s+Charge",
    re.I,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_redline(
    pdf_path: str,
    max_pages: int = 5,
    *,
    start_page: int | None = None,
    end_page: int | None = None,
) -> RedlineSignals:
    """Analyze a PDF for redline/tracked-changes signals.

    Args:
        pdf_path:   Path to the PDF file.
        max_pages:  Maximum number of pages to scan (first N pages).

    Returns:
        ``RedlineSignals`` with confidence score and evidence.
    """
    try:
        import fitz
    except ImportError:
        raise ImportError("PyMuPDF (fitz) is required for redline detection")

    signals: list[str] = []
    red_text: list[str] = []
    sout_text: list[str] = []
    annot_types: list[str] = []
    red_in_index = 0
    red_in_body = 0

    doc = fitz.open(pdf_path)
    try:
        if start_page is not None or end_page is not None:
            start_idx = max(0, (start_page or 1) - 1)
            end_idx = min(len(doc) - 1, (end_page or len(doc)) - 1)
            page_indexes = list(range(start_idx, end_idx + 1))[:max_pages]
        else:
            page_indexes = list(range(min(max_pages, len(doc))))

        for pg in page_indexes:
            page = doc[pg]
            page_text = page.get_text("text")

            # --- Check annotations ---
            for annot in page.annots():
                atype = annot.type[1] if annot.type else "unknown"
                if atype in ("StrikeOut", "Highlight", "Underline", "Squiggly"):
                    annot_types.append(atype)

            # --- Check span colors and flags ---
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if not text:
                            continue
                        color = span.get("color", 0)
                        flags = span.get("flags", 0)

                        # Strikeout lives in char_flags bit 0, not span font flags.
                        char_flags = span.get("char_flags")
                        has_strike = bool(int(char_flags or 0) & 1)
                        if has_strike and text:
                            sout_text.append(text[:60])

                        # Red text
                        if color != 0 and _is_red(color):
                            # Classify context: index entry vs. body rate text
                            nearby = page_text[max(0, page_text.find(text) - 200):
                                               page_text.find(text) + 200]
                            if _INDEX_CONTEXT_RE.search(nearby):
                                red_in_index += 1
                            else:
                                red_in_body += 1
                                red_text.append(text[:60])

            # --- Check for thin horizontal line drawings (manual strikethroughs) ---
            drawings = page.get_drawings()
            thin_hlines = [
                d for d in drawings
                if abs(d["rect"].height) < 2.0 and d["rect"].width > 20
            ]
            if len(thin_hlines) > 5:
                signals.append(f"p{pg+1}:horizontal_lines={len(thin_hlines)}")

        # --- Check filename ---
        fname = Path(pdf_path).name.lower()
        if "redline" in fname or "red-line" in fname or "markup" in fname:
            signals.append("filename_contains_redline")

    finally:
        doc.close()

    # --- Score ---
    confidence = 0.0
    is_redline = False

    if sout_text:
        signals.append(f"strikethrough_spans={len(sout_text)}")
        confidence = max(confidence, 0.80 if len(sout_text) > 3 else 0.55)

    if annot_types:
        signals.append(f"pdf_annotations={annot_types}")
        confidence = max(confidence, 0.90)

    if red_in_body > 0:
        signals.append(f"red_text_in_body={red_in_body}")
        # If only 1-2 red body spans and all samples are purely numeric/punctuation
        # (e.g. exhibit reference numbers like "620493"), treat as a weak signal
        # and require corroboration from another signal before declaring redline.
        _all_numeric = all(
            re.fullmatch(r"[\d\s\.\,\-\(\)\/\$\¢]+", t) for t in red_text
        )
        if red_in_body <= 2 and _all_numeric and not sout_text and not annot_types:
            confidence = max(confidence, 0.40)
            signals.append("red_numeric_only_weak")
        else:
            confidence = max(confidence, 0.85 if red_in_body > 3 else 0.65)
    elif red_in_index > 0:
        signals.append(f"red_text_index_only={red_in_index}")
        # Index red is not a redline — do not raise confidence

    if "filename_contains_redline" in signals:
        confidence = max(confidence, 0.95)

    # Penalty: if red is index-only and no other signals, it's not a redline
    red_is_index_only = red_in_index > 0 and red_in_body == 0 and not sout_text and not annot_types

    if confidence >= 0.50:
        is_redline = True

    return RedlineSignals(
        is_redline=is_redline,
        confidence=confidence,
        signals=signals,
        red_text_samples=red_text[:5],
        strikethrough_samples=sout_text[:5],
        annotation_types=list(set(annot_types)),
        red_is_index_only=red_is_index_only,
    )


def scan_documents_for_redlines(
    db_path: str,
    family_key_pattern: str = "%",
    max_pages: int = 3,
) -> list[dict]:
    """Scan all historical_documents matching a pattern for redline signals.

    Returns a list of dicts with keys:
        hd_id, family_key, local_path, signals
    suitable for updating document_fingerprints.
    """
    import sqlite3, os

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT DISTINCT hd.id, hd.family_key, hd.local_path
        FROM historical_documents hd
        WHERE hd.local_path IS NOT NULL
          AND hd.family_key LIKE ?
        ORDER BY hd.family_key
    """, (family_key_pattern,)).fetchall()
    conn.close()

    results = []
    for hd_id, fk, local_path in rows:
        if not local_path or not os.path.exists(local_path):
            continue
        try:
            sig = detect_redline(local_path, max_pages=max_pages)
            results.append({
                "hd_id": hd_id,
                "family_key": fk,
                "local_path": local_path,
                "is_redline": sig.is_redline,
                "confidence": sig.confidence,
                "signals": sig.signals,
                "red_is_index_only": sig.red_is_index_only,
            })
        except Exception as e:
            results.append({
                "hd_id": hd_id,
                "family_key": fk,
                "local_path": local_path,
                "is_redline": False,
                "confidence": 0.0,
                "signals": [f"error:{e}"],
                "red_is_index_only": False,
            })
    return results
