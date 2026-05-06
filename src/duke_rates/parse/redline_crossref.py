"""
Extract docket cross-references and supersession metadata from redline PDFs.

Redline compliance filings submitted to NCUC contain rich metadata on their
cover pages:

  - All docket numbers involved (E-2, Sub 931; E-7, Sub 1032; etc.)
  - The leaf/schedule being revised ("NC First Revised Leaf No. 701")
  - What it supersedes ("Superseding NC Original Leaf No. 701")
  - Old and new effective dates (often printed as "old_dateNew_date" in red)
  - Filing date (letter date)

This module parses that cover-page metadata and returns structured records
suitable for:
  1. Enriching historical_documents with supersession chains
  2. Identifying discovery targets (dockets we haven't yet downloaded from)
  3. Confirming that clean approved filings match the redline's "after" values

Two formats appear in the corpus:
  a) "DOCKET NO. E-2, SUB 931" (all-caps, one per line) — used in DSM/EE bundles
  b) "Docket Nos. E-2, Sub 1361 and E-2, Sub 1300" — prose in cover letter
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RedlineCrossRef:
    """Metadata extracted from one redline PDF."""
    source_pdf: str
    docket_numbers: list[str] = field(default_factory=list)   # ["E-2, Sub 931", ...]
    filing_date: Optional[str] = None                          # "March 20, 2025"
    leaf_nos: list[str] = field(default_factory=list)          # ["701", "725"]
    supersedes_leaf_nos: list[str] = field(default_factory=list)
    old_effective_date: Optional[str] = None                   # "November 28, 2023"
    new_effective_date: Optional[str] = None                   # "January 1, 2025"
    utility: Optional[str] = None                              # "DEP" | "DEC" | "both"
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# All-caps table format: "DOCKET NO. E-2, SUB 931"
_DOCKET_ALLCAPS_RE = re.compile(
    r"DOCKET\s+NO\.?\s+(E-\d+,\s+SUB\s+\d+)",
    re.I,
)

# Prose format: "Docket Nos. E-2, Sub 1361 and E-2, Sub 1300"
# Also catches: "Docket No. E-2, Sub 1361"
_DOCKET_PROSE_RE = re.compile(
    r"Docket\s+No[s]?\.\s+((?:[A-Z]-\d+,\s+Sub\s+\d+)(?:\s+and\s+[A-Z]-\d+,\s+Sub\s+\d+)*)",
    re.I,
)

# "Re: ... Docket Nos. E-2, Sub 931 and E-7, Sub 1032" (Re: line in letter)
_RE_LINE_RE = re.compile(
    r"Re:.*?Docket\s+No[s]?\.\s+((?:[A-Z]-\d+,\s+Sub\s+\d+)(?:[\s,]+and\s+[A-Z]-\d+,\s+Sub\s+\d+)*)",
    re.I | re.DOTALL,
)

# Individual docket from a compound string
_DOCKET_ITEM_RE = re.compile(r"[A-Z]-\d+,\s+Sub\s+\d+", re.I)

# Leaf revision header: "NC First Revised Leaf No. 701"
_LEAF_HEADER_RE = re.compile(
    r"NC\s+(?:\w+\s+)*(?:Revised|Original)\s+Leaf\s+No\.?\s*(\d+)",
    re.I,
)

# "Superseding NC ... Leaf No. 725"
_SUPERSEDES_RE = re.compile(
    r"Superseding\s+NC\s+(?:\w+\s+)*Leaf\s+No\.?\s*(\d+)",
    re.I,
)

# Date patterns: "January 1, 2025"  or "March 20, 2025"
_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}\b",
)

# "Effective for service rendered on and after <date>"
_EFFECTIVE_RE = re.compile(
    r"Effective\s+for\s+service\s+rendered\s+on\s+and\s+after\s+"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4})",
    re.I,
)

# Utility detection
_DEP_RE = re.compile(r"Duke Energy Progress", re.I)
_DEC_RE = re.compile(r"Duke Energy Carolinas", re.I)

# Filing date: letter date appears before "VIA ELECTRONIC FILING" or at top of letter
_FILING_DATE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_crossref(
    pdf_path: str,
    max_pages: int = 3,
    *,
    start_page: int | None = None,
    end_page: int | None = None,
) -> RedlineCrossRef:
    """Extract docket cross-references and metadata from a redline PDF's cover pages.

    Args:
        pdf_path:   Path to the PDF file.
        max_pages:  Number of pages to scan.
        start_page: Optional 1-based page number to start scanning from.
        end_page:   Optional 1-based page number to stop scanning at.

    Returns:
        ``RedlineCrossRef`` with dockets, dates, leaf numbers.
    """
    try:
        import fitz
    except ImportError:
        raise ImportError("PyMuPDF (fitz) is required")

    result = RedlineCrossRef(source_pdf=pdf_path)
    dockets_seen: set[str] = set()
    leaves_seen: set[str] = set()
    supersedes_seen: set[str] = set()
    all_dates: list[str] = []

    doc = fitz.open(pdf_path)
    try:
        start_index = max(0, int(start_page or 1) - 1)
        stop_index = len(doc)
        if end_page is not None:
            stop_index = min(stop_index, int(end_page))
        if start_index >= stop_index:
            start_index = 0
            stop_index = min(max_pages, len(doc))
        pages_to_check = min(stop_index, start_index + max_pages)
        full_text = ""
        for pg in range(start_index, pages_to_check):
            full_text += doc[pg].get_text("text") + "\n"
    finally:
        doc.close()

    # --- Docket numbers ---
    # Try all-caps format first (DSM/EE bundle cover pages)
    for m in _DOCKET_ALLCAPS_RE.finditer(full_text):
        d = _normalise_docket(m.group(1))
        if d not in dockets_seen:
            dockets_seen.add(d)
            result.docket_numbers.append(d)

    # Then prose format (cover letter Re: line or body)
    if not result.docket_numbers:
        for m in _DOCKET_PROSE_RE.finditer(full_text):
            compound = m.group(1)
            for item in _DOCKET_ITEM_RE.findall(compound):
                d = _normalise_docket(item)
                if d not in dockets_seen:
                    dockets_seen.add(d)
                    result.docket_numbers.append(d)

    # --- Leaf numbers ---
    for m in _LEAF_HEADER_RE.finditer(full_text):
        ln = m.group(1)
        if ln not in leaves_seen:
            leaves_seen.add(ln)
            result.leaf_nos.append(ln)

    for m in _SUPERSEDES_RE.finditer(full_text):
        ln = m.group(1)
        if ln not in supersedes_seen:
            supersedes_seen.add(ln)
            result.supersedes_leaf_nos.append(ln)

    # --- Effective dates ---
    eff_dates = [m.group(1) for m in _EFFECTIVE_RE.finditer(full_text)]
    if len(eff_dates) >= 2:
        # First occurrence = old, second = new (redline replaces old with new)
        result.old_effective_date = eff_dates[0]
        result.new_effective_date = eff_dates[-1]
    elif len(eff_dates) == 1:
        result.new_effective_date = eff_dates[0]

    # --- Filing date (first date-looking thing in letter, before "VIA ELECTRONIC") ---
    via_idx = full_text.find("VIA ELECTRONIC")
    if via_idx == -1:
        via_idx = len(full_text)
    preamble = full_text[:via_idx]
    dates_in_preamble = _DATE_RE.findall(preamble)
    if dates_in_preamble:
        result.filing_date = dates_in_preamble[-1]  # last before VIA ELECTRONIC

    # --- Utility ---
    has_dep = bool(_DEP_RE.search(full_text))
    has_dec = bool(_DEC_RE.search(full_text))
    if has_dep and has_dec:
        result.utility = "both"
    elif has_dep:
        result.utility = "DEP"
    elif has_dec:
        result.utility = "DEC"

    return result


def _normalise_docket(raw: str) -> str:
    """Normalise docket string: 'E-2, Sub 931' → 'E-2, Sub 931'."""
    # Upper-case the E-N part, Title-case "Sub"
    raw = raw.strip()
    raw = re.sub(r"SUB\s+", "Sub ", raw, flags=re.I)
    return raw


def scan_redlines_for_crossrefs(
    db_path: str,
    family_key_pattern: str = "%",
    max_pages: int = 3,
) -> list[dict]:
    """Scan all redline-candidate documents matching a family key pattern.

    Returns a list of dicts with extracted cross-reference metadata, sorted
    by source_pdf so duplicate paths are grouped.
    """
    import sqlite3, os

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        WITH matched_fingerprints AS (
            SELECT
                hd.id AS historical_document_id,
                hd.family_key,
                hd.local_path AS source_pdf,
                hd.start_page,
                hd.end_page,
                COALESCE(df.is_redline_candidate, 0) AS is_redline_candidate,
                ROW_NUMBER() OVER (
                    PARTITION BY hd.id
                    ORDER BY
                        CASE
                            WHEN df.page_start IS hd.start_page AND df.page_end IS hd.end_page THEN 2
                            WHEN df.page_start IS NULL AND df.page_end IS NULL THEN 1
                            ELSE 0
                        END DESC,
                        df.id DESC
                ) AS rn
            FROM historical_documents hd
            LEFT JOIN document_fingerprints df
              ON df.source_pdf = hd.local_path
             AND (
                (df.page_start IS hd.start_page AND df.page_end IS hd.end_page)
                OR (df.page_start IS NULL AND df.page_end IS NULL)
             )
            WHERE hd.family_key LIKE ?
              AND hd.local_path IS NOT NULL
              AND TRIM(hd.local_path) <> ''
        )
        SELECT DISTINCT
            source_pdf,
            family_key,
            start_page,
            end_page
        FROM matched_fingerprints
        WHERE rn = 1
          AND is_redline_candidate = 1
        ORDER BY source_pdf, start_page, end_page
        """,
        (family_key_pattern,),
    ).fetchall()
    conn.close()

    # Deduplicate by slice — one PDF may cover multiple family keys or page slices.
    seen_slices: set[tuple[str, int | None, int | None]] = set()
    results = []
    for path, fk, start_page, end_page in rows:
        if not path or not os.path.exists(path):
            continue
        slice_key = (
            str(path),
            int(start_page) if start_page is not None else None,
            int(end_page) if end_page is not None else None,
        )
        if slice_key in seen_slices:
            continue
        seen_slices.add(slice_key)
        try:
            ref = extract_crossref(
                path,
                max_pages=max_pages,
                start_page=slice_key[1],
                end_page=slice_key[2],
            )
            results.append({
                "source_pdf": path,
                "page_start": slice_key[1],
                "page_end": slice_key[2],
                "docket_numbers": ref.docket_numbers,
                "filing_date": ref.filing_date,
                "leaf_nos": ref.leaf_nos,
                "supersedes_leaf_nos": ref.supersedes_leaf_nos,
                "old_effective_date": ref.old_effective_date,
                "new_effective_date": ref.new_effective_date,
                "utility": ref.utility,
            })
        except Exception as e:
            results.append({
                "source_pdf": path,
                "page_start": slice_key[1],
                "page_end": slice_key[2],
                "docket_numbers": [],
                "filing_date": None,
                "leaf_nos": [],
                "supersedes_leaf_nos": [],
                "old_effective_date": None,
                "new_effective_date": None,
                "utility": None,
                "error": str(e),
            })

    return results
