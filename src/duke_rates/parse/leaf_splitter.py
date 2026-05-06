"""
Leaf/schedule splitter for NCUC compliance tariff PDFs.

NCUC compliance tariff filings bundle many individual rate schedule leaves
into a single PDF.  This module splits them into per-leaf segments so the
existing heuristic and LLM parsers can operate on clean, focused text.

Splitting strategies (tried in order, first match wins):

  1. LEAF_HEADER — "NC ... Revised Leaf No. NNN" at top of page (modern era)
  2. SCHED_REVISION — "RES-31" / "SGS-TOUE-50" style header (2010s compliance packs)
  3. SCHED_KEYWORD — "SCHEDULE RES-86" / "Schedule No. 5" (1990s CP&L style)
  4. COVER_SKIP — pages that are clearly cover letters / certification pages

Each detected boundary starts a new LeafSegment.  Pages before any boundary
are collected into a "cover" segment and discarded by default.

Usage:
    segments = split_pdf_into_leaves(path)
    for seg in segments:
        text = seg.full_text()
        result = parse_schedule_text(
            document_id=1, title=seg.title,
            state="NC", company="DEP", text=text
        )
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Boundary detection regexes
# ---------------------------------------------------------------------------

# Modern (post-2010): "NC First Revised Leaf No. 500" or "Leaf No. 711"
_LEAF_NO_RE = re.compile(
    r"""
    (?:
        (?:NC\s+)?                                  # optional "NC"
        (?:[A-Za-z]+\s+){0,4}                       # 0-4 revision words: First/Second/Original/Third...
        (?:Revised\s+)?Leaf\s+No\.?\s*(\d{2,4})     # "Leaf No. 500"
    )
    """,
    re.I | re.X,
)

# 2010s compliance packs: page header is exactly "RES-31" or "SGS-TOUE-50"
# (schedule code dash revision number, alone on the first non-blank line)
_SCHED_REV_RE = re.compile(
    r"""
    ^([A-Z][A-Z0-9-]{1,20})-(\d{1,3})$
    """,
    re.X,
)

# 1990s CP&L style: "SCHEDULE RES-86" or "SCHEDULE SGS-86"
# Must be at start of line, code must look like an abbreviation (2+ uppercase chars,
# optionally followed by digits/hyphens), NOT a common English word.
_SCHED_KEYWORD_RE = re.compile(
    r"""
    ^SCHEDULE\s+
    ([A-Z]{2,}[A-Z0-9-]{0,18})  # schedule code: 2+ uppercase letters + optional suffix
    (?:\s*[-–]\s*\d+)?           # optional revision "-86"
    (?:\s|$)                     # followed by whitespace or end of line
    """,
    re.X | re.M,
)

# Common English words that should NOT be treated as schedule codes
_COMMON_WORDS = frozenset({
    "NO", "OR", "AND", "TO", "FOR", "THE", "OF", "IN", "IS", "AS",
    "BE", "AT", "BY", "ON", "UP", "IF", "AN", "IT", "DO", "SO",
    "GENERAL", "SERVICE", "RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL",
    "APPLICABLE", "UNLESS", "PURSUANT", "SCHEDULE", "RATES", "RATE",
    "CONFORMING", "ESTABLISHED", "SHALL",
})

# Pages to discard as cover material
_COVER_SIGNALS = re.compile(
    r"""
    (?:
        \bhereby\s+(?:submit|certif|request)\b |
        \benclosed?\s+(?:are|is|herewith)\b |
        \bChief\s+Clerk\b |
        \bNorth\s+Carolina\s+Utilities\s+Commission\b |
        \bVIA\s+ELECTRONIC\s+FILING\b |
        \bCertificate\s+of\s+Service\b |
        \bRespectfully\s+submitted\b
    )
    """,
    re.I | re.X,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class PageText(NamedTuple):
    page_num: int          # 1-based
    text: str


@dataclass
class LeafSegment:
    """One rate schedule / leaf extracted from a compliance tariff PDF."""
    leaf_no: str | None           # e.g. "500", "711"; None if split by sched header
    schedule_code: str | None     # e.g. "RES", "SGS-TOUE"
    revision: str | None          # revision number from sched header, e.g. "31"
    title: str                    # human-readable label
    pages: list[PageText] = field(default_factory=list)
    source_pdf: Path | None = None

    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    def page_range(self) -> tuple[int, int]:
        if not self.pages:
            return (0, 0)
        return (self.pages[0].page_num, self.pages[-1].page_num)

    def __repr__(self) -> str:
        start, end = self.page_range()
        return (
            f"LeafSegment(leaf={self.leaf_no!r}, code={self.schedule_code!r}, "
            f"rev={self.revision!r}, pages={start}-{end})"
        )


# ---------------------------------------------------------------------------
# Main splitter
# ---------------------------------------------------------------------------

def split_pdf_into_leaves(
    path: Path,
    *,
    skip_cover: bool = True,
    min_text_chars: int = 50,
) -> list[LeafSegment]:
    """
    Split a PDF into per-leaf / per-schedule segments.

    Args:
        path: Path to the PDF file.
        skip_cover: If True, discard cover letter pages that precede the first
                    leaf/schedule boundary.
        min_text_chars: Pages with fewer characters than this are ignored.

    Returns:
        List of LeafSegment objects in page order.  Empty list if no boundaries
        detected (caller should fall back to treating the whole PDF as one segment).
    """
    pages = _extract_pages(path, min_text_chars=min_text_chars)
    if not pages:
        return []

    strategy = _detect_strategy(pages)

    if strategy == "leaf_no":
        return _split_by_leaf_no(pages, path)
    elif strategy == "sched_rev":
        return _split_by_sched_rev(pages, path, skip_cover=skip_cover)
    elif strategy == "sched_keyword":
        return _split_by_sched_keyword(pages, path, skip_cover=skip_cover)
    else:
        # No internal boundaries — whole PDF is one segment
        seg = LeafSegment(
            leaf_no=None,
            schedule_code=None,
            revision=None,
            title=path.stem,
            pages=pages,
            source_pdf=path,
        )
        return [seg]


def detect_split_strategy(path: Path) -> str:
    """Return the splitting strategy name for a PDF without doing the split."""
    pages = _extract_pages(path, min_text_chars=50)
    return _detect_strategy(pages)


# ---------------------------------------------------------------------------
# Strategy detection
# ---------------------------------------------------------------------------

def _detect_strategy(pages: list[PageText]) -> str:
    leaf_hits = sum(1 for p in pages if _LEAF_NO_RE.search(p.text))
    rev_hits = sum(1 for p in pages if _first_meaningful_line_matches_sched_rev(p.text))
    kw_hits = sum(
        1 for p in pages
        if (m := _SCHED_KEYWORD_RE.search(p.text)) and m.group(1).upper() not in _COMMON_WORDS
    )

    if leaf_hits >= 2:
        return "leaf_no"
    if rev_hits >= 2:
        return "sched_rev"
    if kw_hits >= 2:
        return "sched_keyword"
    # Single leaf (modern single-leaf filing like DSM exhibits)
    if leaf_hits == 1:
        return "leaf_no"
    return "none"


def _first_meaningful_line_matches_sched_rev(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return bool(_SCHED_REV_RE.match(line))
    return False


# ---------------------------------------------------------------------------
# Split implementations
# ---------------------------------------------------------------------------

def _split_by_leaf_no(pages: list[PageText], source: Path) -> list[LeafSegment]:
    """
    Group pages by the leaf number found on each page.
    Pages that share the same leaf number stay together.
    Pages before the first leaf (cover pages) are discarded.
    """
    segments: list[LeafSegment] = []
    current_leaf: str | None = None
    current_pages: list[PageText] = []

    for page in pages:
        leaf_m = _LEAF_NO_RE.search(page.text)
        leaf_no = leaf_m.group(1) if leaf_m else None

        if leaf_no and leaf_no != current_leaf:
            # Flush previous segment
            if current_leaf is not None and current_pages:
                segments.append(_make_leaf_segment(current_leaf, current_pages, source))
            current_leaf = leaf_no
            current_pages = [page]
        elif current_leaf is not None:
            # Continuation of current leaf
            current_pages.append(page)
        # else: pre-first-leaf page — skip

    # Flush last
    if current_leaf is not None and current_pages:
        segments.append(_make_leaf_segment(current_leaf, current_pages, source))

    return segments


def _split_by_sched_rev(
    pages: list[PageText], source: Path, *, skip_cover: bool
) -> list[LeafSegment]:
    """
    Split when the first meaningful line of a page is a "CODE-NN" header.
    """
    segments: list[LeafSegment] = []
    current_code: str | None = None
    current_rev: str | None = None
    current_pages: list[PageText] = []

    for page in pages:
        code, rev = _extract_sched_rev_header(page.text)

        if code and (code != current_code or rev != current_rev):
            if current_code is not None and current_pages:
                segments.append(
                    _make_sched_rev_segment(current_code, current_rev, current_pages, source)
                )
            current_code = code
            current_rev = rev
            current_pages = [page]
        elif current_code is not None:
            current_pages.append(page)
        else:
            # Cover page
            if not skip_cover:
                current_pages.append(page)

    if current_code is not None and current_pages:
        segments.append(
            _make_sched_rev_segment(current_code, current_rev, current_pages, source)
        )

    return segments


def _split_by_sched_keyword(
    pages: list[PageText], source: Path, *, skip_cover: bool
) -> list[LeafSegment]:
    """
    Split on "SCHEDULE XXX" keyword matches (1990s CP&L format).
    The boundary page itself starts the new segment.
    """
    segments: list[LeafSegment] = []
    current_code: str | None = None
    current_pages: list[PageText] = []

    for page in pages:
        m = _SCHED_KEYWORD_RE.search(page.text)
        if m and m.group(1).upper() not in _COMMON_WORDS:
            new_code = m.group(1).upper()
            if current_code is not None and current_pages:
                segments.append(
                    _make_sched_keyword_segment(current_code, current_pages, source)
                )
            current_code = new_code
            current_pages = [page]
        elif current_code is not None:
            current_pages.append(page)
        else:
            if not skip_cover:
                current_pages.append(page)

    if current_code is not None and current_pages:
        segments.append(
            _make_sched_keyword_segment(current_code, current_pages, source)
        )

    return segments


# ---------------------------------------------------------------------------
# Segment factory helpers
# ---------------------------------------------------------------------------

def _make_leaf_segment(
    leaf_no: str, pages: list[PageText], source: Path
) -> LeafSegment:
    # Try to extract schedule code from the text
    sched_code = _infer_schedule_code_from_text("\n".join(p.text for p in pages))
    return LeafSegment(
        leaf_no=leaf_no,
        schedule_code=sched_code,
        revision=None,
        title=f"Leaf No. {leaf_no}" + (f" ({sched_code})" if sched_code else ""),
        pages=pages,
        source_pdf=source,
    )


def _make_sched_rev_segment(
    code: str, rev: str | None, pages: list[PageText], source: Path
) -> LeafSegment:
    leaf_no = _infer_leaf_no_from_text("\n".join(p.text for p in pages))
    return LeafSegment(
        leaf_no=leaf_no,
        schedule_code=code,
        revision=rev,
        title=f"Schedule {code}" + (f" rev{rev}" if rev else "") + (f" Leaf {leaf_no}" if leaf_no else ""),
        pages=pages,
        source_pdf=source,
    )


def _make_sched_keyword_segment(
    code: str, pages: list[PageText], source: Path
) -> LeafSegment:
    leaf_no = _infer_leaf_no_from_text("\n".join(p.text for p in pages))
    return LeafSegment(
        leaf_no=leaf_no,
        schedule_code=code,
        revision=None,
        title=f"Schedule {code}" + (f" Leaf {leaf_no}" if leaf_no else ""),
        pages=pages,
        source_pdf=source,
    )


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _extract_pages(path: Path, min_text_chars: int) -> list[PageText]:
    try:
        import fitz  # type: ignore
        doc = fitz.open(str(path))
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text("text")
            if len(text.strip()) >= min_text_chars:
                pages.append(PageText(page_num=i + 1, text=text))
        doc.close()
        return pages
    except Exception:
        pass

    try:
        import pdfplumber  # type: ignore
        pages = []
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if len(text.strip()) >= min_text_chars:
                    pages.append(PageText(page_num=i + 1, text=text))
        return pages
    except Exception:
        return []


def _extract_sched_rev_header(text: str) -> tuple[str | None, str | None]:
    """Return (code, revision) if the first meaningful line is a CODE-NN header."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SCHED_REV_RE.match(line)
        if m:
            return m.group(1), m.group(2)
        return None, None
    return None, None


def _infer_leaf_no_from_text(text: str) -> str | None:
    m = _LEAF_NO_RE.search(text)
    return m.group(1) if m else None


def _infer_schedule_code_from_text(text: str) -> str | None:
    # Try sched_rev header first
    code, _ = _extract_sched_rev_header(text)
    if code:
        return code
    # Try keyword
    m = _SCHED_KEYWORD_RE.search(text)
    if m:
        return m.group(1).upper()
    # Fallback: heuristics
    try:
        from duke_rates.parse.heuristics import extract_schedule_code
        return extract_schedule_code("", text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Batch processing helper
# ---------------------------------------------------------------------------

def split_docket_directory(
    docket_dir: Path,
    *,
    skip_cover: bool = True,
    min_text_chars: int = 50,
) -> dict[str, list[LeafSegment]]:
    """
    Split all PDFs in a docket directory into leaf segments.

    Returns a dict of {pdf_filename: [segments]}.
    Files with no internal boundaries return a single-segment list.
    """
    results: dict[str, list[LeafSegment]] = {}
    for pdf in sorted(docket_dir.glob("*.pdf")):
        segs = split_pdf_into_leaves(pdf, skip_cover=skip_cover, min_text_chars=min_text_chars)
        results[pdf.name] = segs
    return results


def summarize_split_results(results: dict[str, list[LeafSegment]]) -> None:
    """Print a summary table of split results."""
    total_segs = sum(len(v) for v in results.values())
    print(f"\n{'File':<45} {'Strategy':<14} {'Segments':>8}")
    print("-" * 70)
    for fname, segs in sorted(results.items()):
        strategy = "none"
        if segs:
            has_leaf = any(s.leaf_no for s in segs)
            has_rev  = any(s.revision for s in segs)
            if has_leaf and not has_rev:
                strategy = "leaf_no"
            elif has_rev:
                strategy = "sched_rev"
            elif segs[0].schedule_code:
                strategy = "sched_keyword"
        seg_labels = [s.schedule_code or s.leaf_no or "?" for s in segs[:4]]
        extra = f"+{len(segs)-4}" if len(segs) > 4 else ""
        print(f"  {fname[:43]:<43} {strategy:<14} {len(segs):>3}  {', '.join(seg_labels)}{extra}")
    print(f"\n  Total segments: {total_segs} across {len(results)} files")
