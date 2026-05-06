"""
Document quality tier inference.

Assigns T1 / T2 / T3 quality tiers to NCUC discovery records and document
fingerprints based on acquisition method and local path patterns.

Tier definitions
----------------
T1  Official current-version tariff from the Duke Energy website.
    Stored under data/historical/manual/ with structured leaf-no-* or
    ncride* filenames.  These are the gold standard for rate extraction.

T2  NCUC compliance-docket tariff exhibit — a tariff sheet filed as part
    of an annual compliance or annual adjustment proceeding.  Downloaded
    via Playwright from a known compliance sub-docket (e.g. E-2 Sub 1354,
    E-7 Sub 1243) or stored under data/downloads/ncuc_tariff/.

T3  NCUC search-engine or direct-HTTP document — discovered via the NCUC
    text search or parameter search but not from a targeted compliance
    docket.  May include redline/proposed versions, compliance narratives,
    or rate-case exhibits of varying quality.

None  Tier cannot be determined from path/acquisition metadata alone.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Known compliance sub-dockets — filings here are T2 by definition.
# Keep in sync with TARGETS in scrape_ncuc_tariff_filings.py.
# ---------------------------------------------------------------------------
_COMPLIANCE_DOCKET_PATTERNS: list[re.Pattern] = [
    # DEP (E-2) compliance sub-dockets
    re.compile(r'e-2-sub-1354', re.I),   # DEP JAA current
    re.compile(r'e-2-sub-1143', re.I),   # DEP JAA historical
    re.compile(r'e-2-sub-1204', re.I),   # DEP STS
    re.compile(r'e-2-sub-1294', re.I),   # DEP RDM
    re.compile(r'e-2-sub-1196', re.I),   # DEP EDIT-4
    re.compile(r'e-2-sub-1343', re.I),   # DEP (general compliance)
    # DEC (E-7) compliance sub-dockets
    re.compile(r'e-7-sub-1243', re.I),   # DEC STS current
    re.compile(r'e-7-sub-1321', re.I),   # DEC STS Debby
    re.compile(r'e-7-sub-1325', re.I),   # DEC STS Helene
    re.compile(r'e-7-sub-1276', re.I),   # DEC EDPR current
    re.compile(r'e-7-sub-1146', re.I),   # DEC EDPR historical
    # NCUC tariff download directory (any sub-docket download goes here)
    re.compile(r'downloads[/\\]ncuc_tariff[/\\]', re.I),
]

# Filename patterns that identify T1 (Duke website) documents.
_T1_FILENAME_PATTERNS: list[re.Pattern] = [
    re.compile(r'leaf-no-\d{3}', re.I),          # leaf-no-602-rider-jaa-ry1.pdf
    re.compile(r'ncride\w+\.pdf$', re.I),          # ncridersts.pdf, ncrideredpr.pdf
    re.compile(r'ncschedule\w+\.pdf$', re.I),      # ncschedule*.pdf (DEC schedule pages)
    re.compile(r'-ry\d?\.pdf$', re.I),             # ...-ry1.pdf, ...-ry2.pdf
]


def _normalise_path(path: str) -> str:
    """Return path with backslashes normalised to forward slashes."""
    return path.replace('\\', '/')


def infer_doc_quality_tier(
    local_path: str | None,
    acquisition_method: str | None,
    docket_number: str | None = None,
) -> str | None:
    """
    Infer T1 / T2 / T3 quality tier from path and acquisition metadata.

    Parameters
    ----------
    local_path:
        The stored local file path (may use Windows or POSIX separators).
    acquisition_method:
        One of: "manual_seed", "playwright", "docket_scrape",
        "search_engine", "direct_http".
    docket_number:
        Optional NCUC docket number (e.g. "E-2 Sub 1354").  Used as a
        supplementary signal when local_path is not decisive.

    Returns
    -------
    "T1", "T2", "T3", or None.
    """
    if not local_path:
        return None

    path = _normalise_path(local_path)
    method = (acquisition_method or "").lower()

    # ------------------------------------------------------------------
    # T1 — official Duke Energy website tariff PDF
    # ------------------------------------------------------------------
    if method == "manual_seed" and "historical/manual/" in path:
        if any(p.search(path) for p in _T1_FILENAME_PATTERNS):
            return "T1"

    # ------------------------------------------------------------------
    # T2 — NCUC compliance docket download
    # ------------------------------------------------------------------
    if method in ("playwright", "docket_scrape"):
        if any(p.search(path) for p in _COMPLIANCE_DOCKET_PATTERNS):
            return "T2"

    # Playwright files not yet in the explicit docket list may still be T2
    # if the docket_number argument matches a known compliance docket.
    if method in ("playwright", "docket_scrape") and docket_number:
        dn_norm = _normalise_path(docket_number).lower().replace(" ", "-")
        if any(p.search(dn_norm) for p in _COMPLIANCE_DOCKET_PATTERNS):
            return "T2"

    # ------------------------------------------------------------------
    # T3 — NCUC search-engine / direct-HTTP discovery
    # ------------------------------------------------------------------
    if method in ("search_engine", "direct_http") and "historical/ncuc/" in path:
        return "T3"

    return None
