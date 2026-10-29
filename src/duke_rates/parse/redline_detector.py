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


# ---------------------------------------------------------------------------
# Page-level evidence (Phase 2E D2)
# ---------------------------------------------------------------------------
#
# ``detect_redline`` scores whatever slice it is given, and its verdict is
# stored per bound span. A page run found anywhere else (a local candidate, a
# rebind target) has no stored verdict. 1b72e8de pp.100-105 (bound slice: the
# index page) and 7f394a37 pp.221-226 are such runs. Their markup is coloured
# text with coloured *filled bars* drawn through (deleted) or under (inserted)
# it: no strikeout flag and no annotation, which ``detect_redline`` reads.
# This evidence is computed from the pages themselves, every time it is needed.

PAGE_EVIDENCE_VERSION = "page_redline_evidence@1.0.0"
_BAR_MAX_HEIGHT = 2.6     # pt; strike/underline bars are hairline rectangles
_STRIKE_BAND = (0.30, 0.78)  # bar centre, as a fraction of the span height from its top
_UNDERLINE_BAND = (0.78, 1.20)


def _rgb(color) -> tuple[int, int, int] | None:
    if color is None:
        return None
    if isinstance(color, int):
        return (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    return tuple(int(round(c * 255)) for c in color[:3])  # type: ignore[return-value]


def _is_colored(rgb: tuple[int, int, int] | None) -> bool:
    """Chromatic (not black, grey or white): redline markup colours."""
    return rgb is not None and max(rgb) - min(rgb) > 60


@dataclass
class PageRedlineEvidence:
    """Redline markup observed on one page. ``verdict`` is a machine reading."""

    page: int
    verdict: str                      # 'redline' | 'suspect' | 'clean' | 'no_text_layer'
    colored_spans: int = 0
    struck_spans: int = 0             # coloured bar through the middle of the span
    inserted_spans: int = 0           # coloured bar under the span
    char_flag_strikes: int = 0
    strike_annotations: int = 0
    glued_text: list[str] = field(default_factory=list)
    struck_samples: list[str] = field(default_factory=list)
    inserted_samples: list[str] = field(default_factory=list)
    strike_regions: list[tuple[float, float, float, float]] = field(default_factory=list)

    def signals(self) -> dict:
        return {
            "page": self.page, "verdict": self.verdict, "colored_spans": self.colored_spans,
            "struck_spans": self.struck_spans, "inserted_spans": self.inserted_spans,
            "char_flag_strikes": self.char_flag_strikes, "strike_annotations": self.strike_annotations,
            "glued_text": self.glued_text[:5], "struck_samples": self.struck_samples[:5],
            "inserted_samples": self.inserted_samples[:5],
            "label_source": f"machine:{PAGE_EVIDENCE_VERSION}",
        }

    def region_struck(self, rect: tuple[float, float, float, float], min_fraction: float = 0.5) -> bool:
        """True when struck regions on the same line cover ``min_fraction`` of ``rect``'s width."""
        x0, y0, x1, y1 = rect
        covered = sum(
            max(0.0, min(x1, sx1) - max(x0, sx0))
            for sx0, sy0, sx1, sy1 in self.strike_regions
            if min(y1, sy1) > max(y0, sy0)
        )
        return covered >= min_fraction * max(x1 - x0, 1e-6)


def evaluate_redline_page(page, page_number: int) -> PageRedlineEvidence:
    """Read redline markup from one PyMuPDF page."""
    from duke_rates.parse.rider_summary import _REDLINE_TEXT_RE

    spans = [
        s for block in page.get_text("dict")["blocks"] if block.get("type") == 0
        for line in block["lines"] for s in line["spans"] if s["text"].strip()
    ]
    if not spans:
        return PageRedlineEvidence(page=page_number, verdict="no_text_layer")
    ev = PageRedlineEvidence(page=page_number, verdict="clean")
    bars = []
    for drawing in page.get_drawings():
        r = drawing["rect"]
        color = _rgb(drawing.get("fill") or drawing.get("color"))
        if abs(r.height) < _BAR_MAX_HEIGHT and r.width > 2 and _is_colored(color):
            bars.append(r)
    for span in spans:
        x0, y0, x1, y1 = span["bbox"]
        height = max(y1 - y0, 1.0)
        text = span["text"].strip()
        if _is_colored(_rgb(span.get("color", 0))):
            ev.colored_spans += 1
        if int(span.get("char_flags") or 0) & 1:
            ev.char_flag_strikes += 1
            ev.strike_regions.append((x0, y0, x1, y1))
        struck = inserted = False
        for r in bars:
            overlap = min(x1, r.x1) - max(x0, r.x0)
            if overlap <= 0 or overlap < 0.5 * min(r.width, x1 - x0):
                continue
            rel = ((r.y0 + r.y1) / 2 - y0) / height
            if _STRIKE_BAND[0] <= rel <= _STRIKE_BAND[1]:
                struck = True
                ev.strike_regions.append((max(x0, r.x0), y0, min(x1, r.x1), y1))
            elif _UNDERLINE_BAND[0] < rel <= _UNDERLINE_BAND[1]:
                inserted = True
        if struck:
            ev.struck_spans += 1
            ev.struck_samples.append(text[:60])
        if inserted:
            ev.inserted_spans += 1
            ev.inserted_samples.append(text[:60])
    for annot in page.annots() or ():
        if annot.type and annot.type[1] == "StrikeOut":
            ev.strike_annotations += 1
            ev.strike_regions.append(tuple(annot.rect))
    ev.glued_text = [m.group(0) for m in _REDLINE_TEXT_RE.finditer(page.get_text("text"))]
    # An underline-band bar under black text alone is a table or box border as often as an insertion
    # (D3 source audit: a DSM workpaper page, one blue box edge under a heading). It needs coloured text.
    if ev.struck_spans or ev.char_flag_strikes or ev.strike_annotations or (ev.inserted_spans and ev.colored_spans):
        ev.verdict = "redline"
    elif ev.colored_spans or ev.glued_text or ev.inserted_spans:
        ev.verdict = "suspect"
    return ev


class PageRedlineReader:
    """Page evidence for many (pdf, page) pairs; each PDF opened once, each page read once."""

    def __init__(self) -> None:
        self._docs: dict[str, object] = {}
        self._cache: dict[tuple[str, int], PageRedlineEvidence | None] = {}

    def _doc(self, pdf_path: str):
        import fitz

        if pdf_path not in self._docs:
            try:
                self._docs[pdf_path] = fitz.open(pdf_path)
            except Exception:
                self._docs[pdf_path] = None
        return self._docs[pdf_path]

    def page(self, pdf_path: str, page_number: int) -> PageRedlineEvidence | None:
        key = (pdf_path, page_number)
        if key not in self._cache:
            doc = self._doc(pdf_path)
            self._cache[key] = (
                evaluate_redline_page(doc[page_number - 1], page_number)
                if doc is not None and 1 <= page_number <= len(doc) else None
            )
        return self._cache[key]

    def pages(self, pdf_path: str, start: int, end: int) -> list[PageRedlineEvidence]:
        return [ev for p in range(start, end + 1) if (ev := self.page(pdf_path, p)) is not None]

    def text_struck(self, pdf_path: str, page_number: int, text: str) -> bool | None:
        """Is every printed occurrence of ``text`` on the page struck through?

        ``None`` when the text cannot be located (no text layer, or the
        occurrence spans lines). All occurrences are checked, so a clue whose
        bbox was ambiguous (printed twice) is decided too. A quarter of the
        occurrence struck is enough: a struck suffix ("MROP-~~16A~~") changes
        the identity the text names.
        """
        ev = self.page(pdf_path, page_number)
        doc = self._doc(pdf_path)
        if ev is None or doc is None or not text.strip():
            return None
        needle = " ".join(text.splitlines()[0].split())
        page = doc[page_number - 1]
        # search_for ignores case; keep only occurrences printed exactly as the clue.
        rects = [r for r in page.search_for(needle)
                 if needle in " ".join(page.get_textbox(r + (-1, -1, 1, 1)).split())]
        if not rects:
            return None
        return all(ev.region_struck((r.x0, r.y0, r.x1, r.y1), 0.25) for r in rects)

    def close(self) -> None:
        for doc in self._docs.values():
            if doc is not None:
                doc.close()
        self._docs.clear()


def summarize_page_evidence(evidence: list[PageRedlineEvidence]) -> dict:
    """Span-level summary: the worst verdict, with the pages that carry each verdict."""
    order = ("redline", "suspect", "no_text_layer", "clean")
    verdict = next((v for v in order if any(e.verdict == v for e in evidence)), "unknown")
    return {
        "verdict": verdict,
        "redline_pages": [e.page for e in evidence if e.verdict == "redline"],
        "suspect_pages": [e.page for e in evidence if e.verdict == "suspect"],
        "pages": [e.signals() for e in evidence if e.verdict != "clean"],
        "label_source": f"machine:{PAGE_EVIDENCE_VERSION}",
    }
