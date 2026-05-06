"""
Text slice extraction for embedding generation (Phase 4).

Extracts five standard text slices from a PDF for embedding:
  full_text, first_3_pages, title_block, rate_table_text, order_conclusion_section

Standalone — no database dependency. Uses fitz, pdfplumber for extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Rate table line filters — reused from HasRateTablesClassifier
# ---------------------------------------------------------------------------

_RATE_TABLE_FILTERS: list[re.Pattern] = [
    re.compile(r"¢/kWh|cents\s+per\s+k(?:ilo)?w(?:att)?[-\s]?h(?:our)?"),
    re.compile(r"(?i)\$\s*\d+\.?\d*\s*(?:per|/)\s*(?:month|kwh|kw|day)"),
    re.compile(r"(?i)(?:customer|basic\s+facility)\s+(?:charge|fee)"),
    re.compile(r"(?i)(?:energy|fuel|demand|distribution)\s+charge"),
    re.compile(r"(?i)rate\s+(?:schedule|class|code)\s*[:;]"),
    re.compile(r"(?i)(?:monthly|annual|daily)\s+rate"),
    re.compile(r"(?i)total\s+(?:monthly\s+)?bill"),
    re.compile(r"(?i)^\s*\d+\s+(?:kW|kWh|KW|KWH)"),
    re.compile(r"(?i)\$\s*\d+\.\d{2}"),
]

# ---------------------------------------------------------------------------
# Order conclusion line filters
# ---------------------------------------------------------------------------

_ORDER_CONCLUSION_FILTERS: list[re.Pattern] = [
    re.compile(r"(?i)it\s+is\s+(?:hereby\s+)?ordered"),
    re.compile(r"(?i)so\s+ordered"),
    re.compile(r"(?i)therefore\s+(?:it\s+is\s+)?ordered"),
    re.compile(r"(?i)(?:the\s+)?commission\s+(?:hereby\s+)?(?:orders|finds|concludes)"),
    re.compile(r"(?i)(?:this|the\s+)(?:order|decision)\s+(?:is|becomes?|shall\s+become)\s+(?:final|effective)"),
    re.compile(r"(?i)(?:issued|done)\s+(?:at|in)\s+(?:raleigh|the\s+city)"),
    re.compile(r"(?i)(?:by\s+order\s+of\s+the\s+commission|for\s+the\s+commission)"),
    re.compile(r"(?i)conclusion|concluding\s+paragraph"),
]


@dataclass
class TextSlices:
    """Five text slices extracted from a single PDF."""

    source_pdf: str
    full_text: str = ""
    first_3_pages: str = ""
    title_block: str = ""
    rate_table_text: str = ""
    order_conclusion_section: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def items(self):
        """Yield (embedding_kind, text) pairs for non-empty slices."""
        mapping = [
            ("full_text", self.full_text),
            ("first_3_pages", self.first_3_pages),
            ("title_block", self.title_block),
            ("rate_table_text", self.rate_table_text),
            ("order_conclusion_section", self.order_conclusion_section),
        ]
        for kind, text in mapping:
            if text and text.strip():
                yield kind, text


_MAX_EMBED_CHARS = 2000


def slice_pdf_text(path: Path, max_chars: int = _MAX_EMBED_CHARS) -> TextSlices:
    """Extract the five standard text slices from a PDF.

    Returns a ``TextSlices`` with as many slices populated as possible.
    Individual slices may be empty if the relevant text could not be
    extracted.

    Each slice is truncated to *max_chars* to stay within embedding model
    context windows (default 2000 characters).
    """
    source_pdf = str(path)
    full_text = ""
    first_3_pages = ""
    title_block = ""
    rate_table_text = ""
    order_conclusion_section = ""
    metadata: dict[str, Any] = {"max_chars": max_chars}

    # -- full_text via fitz / pdfplumber ---------------------------------------
    try:
        from duke_rates.parse.pdf_text import extract_pdf_text

        full_text = extract_pdf_text(path).strip()[:max_chars]
        metadata["full_text_chars"] = len(full_text)
    except Exception:
        full_text = ""
        metadata["full_text_error"] = "extract_pdf_text failed"

    # -- first_3_pages via pdfplumber ------------------------------------------
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            pages = pdf.pages[:3]
            first_3_pages = "\n".join(
                (p.extract_text() or "") for p in pages
            ).strip()
            metadata["first_3_pages_chars"] = len(first_3_pages)
    except Exception:
        metadata["first_3_pages_error"] = "pdfplumber failed"

    # If full_text failed but we got first_3_pages, use it as full_text proxy
    if not full_text and first_3_pages:
        full_text = first_3_pages

    # -- title_block: first 600 chars ------------------------------------------
    title_block = full_text[:600].strip() if full_text else ""

    # -- rate_table_text: lines matching rate patterns, max 5000 chars ---------
    if full_text:
        rate_lines: list[str] = []
        for line in full_text.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            for rx in _RATE_TABLE_FILTERS:
                if rx.search(line_stripped):
                    rate_lines.append(line_stripped)
                    break
            if sum(len(ln) for ln in rate_lines) >= 5000:
                break
        rate_table_text = "\n".join(rate_lines) if rate_lines else ""
        metadata["rate_table_lines"] = len(rate_lines) if rate_lines else 0

    # -- order_conclusion_section: last 2000 chars filtered, max 3000 chars ----
    if full_text:
        tail = full_text[-2000:] if len(full_text) > 2000 else full_text
        order_lines: list[str] = []
        for line in tail.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            for rx in _ORDER_CONCLUSION_FILTERS:
                if rx.search(line_stripped):
                    order_lines.append(line_stripped)
                    break
            if sum(len(ln) for ln in order_lines) >= 3000:
                break
        order_conclusion_section = "\n".join(order_lines) if order_lines else ""
        metadata["order_conclusion_lines"] = len(order_lines) if order_lines else 0

    return TextSlices(
        source_pdf=source_pdf,
        full_text=full_text,
        first_3_pages=first_3_pages,
        title_block=title_block,
        rate_table_text=rate_table_text,
        order_conclusion_section=order_conclusion_section,
        metadata=metadata,
    )
