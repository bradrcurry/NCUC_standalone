"""Three-pass remediation of null effective_start on historical_documents."""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import pdfplumber

from duke_rates.db.ncuc_loader import _normalize_date

# ---------------------------------------------------------------------------
# Date extraction from PDF page text
# ---------------------------------------------------------------------------

# Duke standard footer patterns, ordered most-specific first.
# Captures the date portion after the keyword.
# OCR note: older scans substitute "I" for "1" and "-" for digits in years,
# e.g. "August I, 2017" or "January 4,-4996". _normalize_date handles these.
_FOOTER_PATTERNS = [
    # "Effective for service rendered on and after October 31, 2023"
    # "Effective for bills rendered on and after September 30, 2024"
    re.compile(
        r"(?i)effective\s+for\s+(?:service|bills?)\s+rendered\s+(?:on\s+(?:and\s+)?after|from)\s+"
        r"([A-Za-z]+[\s\w,.I-]+\d{4})"
    ),
    # "Effective August I, 2017" / "Effective January 1, 2014" / "Effective August 1, 2017"
    # Allow "I" as OCR artifact for "1" in day position; also handle standalone line
    re.compile(
        r"(?i)^Effective\s+([A-Za-z]+\s+[\dI]{1,2}[,.I\s-]+\d{4})"
    ),
    # "Effective August I, 2017" anywhere in line (not just line-start)
    re.compile(
        r"(?i)\bEffective\s+([A-Z][a-z]+\s+[\dI]{1,2}[,.I\s-]+\d{4})\b"
    ),
    # "Effective: 2023-10-01" / "Effective date: 2023-10-01"
    re.compile(
        r"(?i)effective(?:\s+(?:date|as\s+of|on|for))?[:\s]+(\d{4}-\d{2}-\d{2})"
    ),
    # "Eff. 12.01.2025" / "Eff. 12/01/2025"
    re.compile(
        r"(?i)\bEff\.\s+(\d{1,2}[./]\d{1,2}[./]\d{2,4})"
    ),
    # "on and after January 4, 1996"
    re.compile(
        r"(?i)on\s+and\s+after\s+([A-Za-z]+\s+[\dI]{1,2}[,.\s-]+\d{4})"
    ),
    # "service on and after ___ " — handled by _BLANK_DATE_RE
]

# Patterns that indicate a blank/unfilled date — skip these matches
_BLANK_DATE_RE = re.compile(r"_{3,}|order\s+dated\s*$|\bafter\s*$", re.IGNORECASE)


_MIN_YEAR = 1990
_MAX_YEAR = 2040

# Matches a 4-digit year that looks garbled (>2040 or starts with a weird digit).
# OCR overlapping digits produce patterns like "4996" (1+4 over 9+9+6 → 1996)
# or "4014" (1 over 2 producing 4, giving 4014 → 2014).
_GARBLED_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _is_plausible_date(iso: str) -> bool:
    """Reject dates outside the plausible tariff range (1990-2040)."""
    try:
        year = int(iso[:4])
        return _MIN_YEAR <= year <= _MAX_YEAR
    except (ValueError, IndexError):
        return False


def _rescue_garbled_year(raw: str) -> Optional[str]:
    """
    OCR on redline overlays can produce garbled 4-digit years like 4996 → 1996
    or 4014 → 2014 by merging struck-through and proposed digits.

    Strategy: for each out-of-range year found in raw, try dropping the leading
    digit and prepending "1" or "2" (the two valid century prefixes for tariff
    years 1990-2040). Accept the first substitution that yields a plausible date.
    """
    m = _GARBLED_YEAR_RE.search(raw)
    if not m:
        return None
    year_str = m.group(1)
    year = int(year_str)
    if _MIN_YEAR <= year <= _MAX_YEAR:
        return None  # already plausible, no rescue needed

    suffix = year_str[1:]  # last 3 digits, e.g. "996" from "4996"
    for prefix in ("1", "2"):
        candidate_year = prefix + suffix
        candidate_raw = raw.replace(year_str, candidate_year, 1)
        normed = _normalize_date(candidate_raw)
        if normed and _is_plausible_date(normed):
            return normed
    return None


def _extract_date_from_lines(lines: list[str]) -> Optional[str]:
    """Try to extract an ISO effective date from a list of text lines."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        for pat in _FOOTER_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            raw = m.group(1).strip()
            if _BLANK_DATE_RE.search(raw):
                continue
            # _normalize_date handles "Month D, YYYY", "MM/DD/YYYY", "YYYY-MM-DD"
            # and rejects redlined multi-month strings
            normed = _normalize_date(raw)
            if normed and _is_plausible_date(normed):
                return normed
            # Fallback: try to rescue a garbled year from OCR digit overlap
            rescued = _rescue_garbled_year(raw)
            if rescued:
                return rescued
    return None


def _extract_date_from_pdf(
    local_path: str,
    start_page: Optional[int],
    end_page: Optional[int],
) -> tuple[Optional[str], str]:
    """
    Open the PDF and scan the specific span pages for an effective date.

    Strategy per page:
      1. Footer zone (last 14 lines) — most reliable for tariff schedules.
      2. Header zone (first 6 lines) — catches some older formats.

    We scan every page in the span, plus the page immediately after the span
    (some multi-page schedules put the footer only on the final continuation
    page), and the very last page of the whole document as a last resort.

    Returns (iso_date_or_None, source_detail).
    """
    try:
        path = Path(local_path)
        if not path.exists():
            return None, "file_missing"

        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            pg_start = max(0, (start_page or 1) - 1)
            pg_end = min(total, end_page or pg_start + 1)

            # Build candidate page indices: span pages + one-after + last page
            indices = list(range(pg_start, pg_end))
            if pg_end < total:
                indices.append(pg_end)          # page right after span end
            if total - 1 not in indices:
                indices.append(total - 1)       # last page of PDF

            for idx in indices:
                text = pdf.pages[idx].extract_text() or ""
                lines = [ln for ln in text.split("\n") if ln.strip()]

                # Footer first (most reliable for tariff schedule pages)
                date = _extract_date_from_lines(lines[-14:])
                if date:
                    return date, "footer_scan"

                # Then header / first lines
                date = _extract_date_from_lines(lines[:6])
                if date:
                    return date, "header_scan"

    except Exception as exc:
        return None, f"pdf_error:{exc}"

    return None, "no_match"


# ---------------------------------------------------------------------------
# Pass 1B: Redline document handling
# ---------------------------------------------------------------------------
# Redlined PDFs overlay two versions of text. OCR reads both layers, producing
# concatenated patterns like:
#   "January 1, 2014October 31, 2023"   (two month-name dates back-to-back)
#   "September 30, 2024January 1, 2025"
#   "January 4,-4996"                   (garbled year from overlapping digits)
#
# Strategy: split on month-name boundaries, parse both halves, store the
# earlier as the superseded date and the later as the proposed/approved date.
# The proposed date becomes effective_start; both are recorded in metadata.
# If neither half is plausible, fall through to the next pass.

_MONTH_BOUNDARY_RE = re.compile(
    r"(?<=[0-9,.\s])"
    r"(?=January|February|March|April|May|June|July|August|September|October|November|December)",
    re.IGNORECASE,
)

# Detect a redline signature: two month names in what looks like a single date
_TWO_MONTHS_RE = re.compile(
    r"(?i)(January|February|March|April|May|June|July|August|September|"
    r"October|November|December).*"
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)"
)


def _split_redline_dates(raw: str) -> tuple[Optional[str], Optional[str]]:
    """
    Split a concatenated redline date string into (superseded, proposed).

    Returns (earlier_iso, later_iso) where either may be None if unparseable.
    """
    parts = _MONTH_BOUNDARY_RE.split(raw)
    if len(parts) < 2:
        return None, None

    dates = []
    for part in parts:
        normed = _normalize_date(part.strip())
        if normed and _is_plausible_date(normed):
            dates.append(normed)

    if len(dates) < 2:
        return None, None

    dates.sort()
    return dates[0], dates[-1]  # (superseded, proposed)


def _extract_redline_from_lines(
    lines: list[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Scan lines for a redlined effective-date footer.

    Returns (proposed_iso, superseded_iso, raw_match) or (None, None, None).
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        for pat in _FOOTER_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            raw = m.group(1).strip()
            if _BLANK_DATE_RE.search(raw):
                continue
            # Check for redline signature: two month names in the captured text
            if _TWO_MONTHS_RE.search(raw):
                superseded, proposed = _split_redline_dates(raw)
                if proposed:
                    return proposed, superseded, raw
    return None, None, None


def _extract_redline_from_pdf(
    local_path: str,
    start_page: Optional[int],
    end_page: Optional[int],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Scan span pages for a redlined effective date.

    Returns (proposed_iso, superseded_iso, raw_match) or (None, None, None).
    """
    try:
        path = Path(local_path)
        if not path.exists():
            return None, None, None

        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            pg_start = max(0, (start_page or 1) - 1)
            pg_end = min(total, end_page or pg_start + 1)

            indices = list(range(pg_start, pg_end))
            if pg_end < total:
                indices.append(pg_end)
            if total - 1 not in indices:
                indices.append(total - 1)

            for idx in indices:
                text = pdf.pages[idx].extract_text() or ""
                lines = [ln for ln in text.split("\n") if ln.strip()]

                proposed, superseded, raw = _extract_redline_from_lines(lines[-14:])
                if proposed:
                    return proposed, superseded, raw

                proposed, superseded, raw = _extract_redline_from_lines(lines[:6])
                if proposed:
                    return proposed, superseded, raw

    except Exception:
        pass

    return None, None, None


# ---------------------------------------------------------------------------
# Pass 1C: LLM-assisted date extraction (fallback when regex fails)
# ---------------------------------------------------------------------------
# Only invoked when Pass 1 and Pass 1B both return nothing.
# Uses a local Ollama text model to read the raw page text and identify any
# effective date, filing date, or proposed date mentioned in prose.
#
# Confidence rules:
#   "high"   → use as effective_start (source="llm_extracted")
#   "medium" → store in metadata only; do NOT set effective_start
#   "low"    → discard
#
# The LLM is asked to distinguish between date types so we know whether the
# extracted date is an actual effective date or just a filing/order date.

_LLM_DATE_PROMPT_TEMPLATE = (
    "You are analyzing a scanned Duke Energy utility tariff document page from North Carolina.\n\n"
    "Extract the effective date from the text below. Duke tariff pages typically contain a footer like:\n"
    '  "Effective for service rendered on and after <DATE>"\n'
    '  "Effective <DATE>"\n'
    '  "Eff. <DATE>"\n\n'
    "Sometimes the document is a cover letter or procedural filing — in that case there may only be a\n"
    "filing date (the date the letter was written), not an effective date.\n\n"
    "Respond ONLY with valid JSON in exactly this format:\n"
    '{{"effective_date": "YYYY-MM-DD or null", '
    '"date_type": "effective | filing | proposed | order | unknown", '
    '"confidence": "high | medium | low", '
    '"evidence": "verbatim text snippet containing the date (under 100 chars)"}}\n\n'
    "Rules:\n"
    '- Set confidence="high" only when you see a clear "Effective for service on and after" or "Effective <DATE>" footer\n'
    '- Set confidence="medium" for filing dates, cover letter dates, or ambiguous cases\n'
    '- Set confidence="low" when no date is present or the text is mostly garbled\n'
    "- effective_date must be ISO format YYYY-MM-DD or the string null (not JSON null)\n"
    "- Do NOT invent dates; only extract what is explicitly in the text\n\n"
    "Page text:\n"
    "{page_text}"
)


def _build_llm_prompt(page_text: str) -> str:
    return _LLM_DATE_PROMPT_TEMPLATE.format(page_text=page_text)

_LLM_DATE_JSON_RE = re.compile(
    r'\{[^{}]*"effective_date"[^{}]*\}', re.DOTALL
)

_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
_LLM_DATE_MODEL = "qwen2.5:7b-instruct"
_LLM_TIMEOUT = 30.0


def _extract_page_text_for_llm(
    local_path: str,
    start_page: Optional[int],
    end_page: Optional[int],
    max_chars: int = 800,
) -> str:
    """Extract up to max_chars of text from span pages for LLM analysis."""
    try:
        path = Path(local_path)
        if not path.exists():
            return ""
        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            pg_start = max(0, (start_page or 1) - 1)
            pg_end = min(total, end_page or pg_start + 1)
            indices = list(range(pg_start, pg_end))
            if total - 1 not in indices:
                indices.append(total - 1)
            combined = []
            for idx in indices:
                text = pdf.pages[idx].extract_text() or ""
                combined.append(text)
                if sum(len(t) for t in combined) >= max_chars:
                    break
        return "\n".join(combined)[:max_chars]
    except Exception:
        return ""


def _call_llm_for_date(page_text: str) -> dict:
    """
    Call local Ollama to extract an effective date from page text.

    Returns parsed JSON dict or empty dict on failure.
    """
    if not page_text.strip():
        return {}
    prompt = _build_llm_prompt(page_text)
    payload = {
        "model": _LLM_DATE_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 200},
    }
    try:
        with httpx.Client(timeout=_LLM_TIMEOUT) as client:
            resp = client.post(f"{_OLLAMA_HOST}/api/generate", json=payload)
            resp.raise_for_status()
            raw_response = str(resp.json().get("response") or "").strip()
    except Exception:
        return {}

    # Extract JSON from response (model may add surrounding prose)
    m = _LLM_DATE_JSON_RE.search(raw_response)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {}


def _extract_date_llm(
    local_path: str,
    start_page: Optional[int],
    end_page: Optional[int],
) -> tuple[Optional[str], str, dict]:
    """
    Pass 1C: LLM date extraction.

    Returns (effective_date_or_None, source_tag, llm_metadata).
    - effective_date is only set for high-confidence effective/proposed dates.
    - source_tag is "llm_extracted" or "llm_medium" (metadata-only).
    - llm_metadata contains the full LLM response for audit.
    """
    page_text = _extract_page_text_for_llm(local_path, start_page, end_page)
    if not page_text:
        return None, "llm_no_text", {}

    result = _call_llm_for_date(page_text)
    if not result:
        return None, "llm_parse_failed", {}

    raw_date = result.get("effective_date") or ""
    date_type = result.get("date_type", "unknown")
    confidence = result.get("confidence", "low")

    normed = None
    if raw_date and raw_date != "null":
        normed = _normalize_date(raw_date)
        if normed and not _is_plausible_date(normed):
            normed = None

    meta = {
        "llm_model": _LLM_DATE_MODEL,
        "llm_date_type": date_type,
        "llm_confidence": confidence,
        "llm_evidence": result.get("evidence", ""),
        "llm_raw_date": raw_date,
    }

    # Only promote to effective_start for high-confidence effective/proposed dates
    if (
        normed
        and confidence == "high"
        and date_type in ("effective", "proposed")
    ):
        return normed, "llm_extracted", meta

    # Medium confidence: store metadata but don't set effective_start
    if normed and confidence == "medium":
        meta["llm_medium_date"] = normed
        return None, "llm_medium", meta

    return None, "llm_low_or_filing", meta


# ---------------------------------------------------------------------------
# Pass 2: Rider summary cross-reference (leaf-600 / leaf-602 style sheets)
# ---------------------------------------------------------------------------

# Rider code extraction from charge_label like:
#   "Demand: Medium General Service Schedules - EDIT-4"
#   "Residential Schedules - ESM"
_RIDER_CODE_RE = re.compile(r"-\s+([A-Z0-9][A-Z0-9\-]+)\s*$")


def _build_rider_date_index(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """
    Build a mapping: rider_code (upper) -> sorted list of effective dates
    drawn from the leaf-600 / leaf-602 Summary of Rider Adjustments sheets.

    These sheets list one row per rider per quarter; the version's
    effective_start is the quarter the summary applies to.
    """
    rows = conn.execute("""
        SELECT tv.effective_start, tc.charge_label
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        WHERE tv.family_key IN ('nc-progress-leaf-600', 'nc-progress-leaf-602')
          AND tv.effective_start IS NOT NULL
          AND tc.charge_type = 'adjustment'
    """).fetchall()

    index: dict[str, list[str]] = {}
    for eff_start, label in rows:
        m = _RIDER_CODE_RE.search(label or "")
        if not m:
            continue
        code = m.group(1).upper()
        index.setdefault(code, [])
        if eff_start not in index[code]:
            index[code].append(eff_start)

    # Sort each list ascending
    for code in index:
        index[code].sort()

    return index


def _lookup_rider_date(
    index: dict[str, list[str]],
    leaf_no: Optional[str],
    family_key: str,
) -> Optional[str]:
    """
    Return the earliest effective date for a rider from the summary index.

    We use the earliest (first appearance) as a conservative lower bound.
    The leaf_no is tried first, then the rider code parsed from family_key.
    """
    candidates = []
    for code in _rider_codes_for_doc(leaf_no, family_key):
        dates = index.get(code.upper(), [])
        candidates.extend(dates)

    if not candidates:
        return None
    return sorted(candidates)[0]  # earliest


def _rider_codes_for_doc(leaf_no: Optional[str], family_key: str) -> list[str]:
    """Derive possible rider codes to look up from leaf_no and family_key."""
    codes = []
    if leaf_no and len(leaf_no) <= 10 and leaf_no.upper() == leaf_no:
        codes.append(leaf_no)
    # family_key like "nc-progress-rider-EDIT4" or "nc-carolinas-rider-ESM"
    fk_parts = family_key.rsplit("-", 1)
    if len(fk_parts) == 2:
        code = fk_parts[-1]
        if code and code.upper() == code:
            codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# Pass 3: Docket filing-date fallback
# ---------------------------------------------------------------------------

_DOCKET_DIR_RE = re.compile(r"[eE]-(\d+)[_-][sS]ub[_-](\d+)", re.IGNORECASE)


def _docket_from_path(local_path: str) -> tuple[Optional[str], Optional[str]]:
    """Parse docket_number and sub_number from a local_path string."""
    m = _DOCKET_DIR_RE.search(local_path or "")
    if m:
        return m.group(1), m.group(2)
    return None, None


_MDY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _normalize_filing_date(raw: str) -> Optional[str]:
    """Normalize filing dates which may be M/D/YYYY, MM/DD/YYYY, or Month D, YYYY."""
    if not raw:
        return None
    m = _MDY_RE.match(raw.strip())
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = datetime.date(year, month, day)
            iso = d.isoformat()
            return iso if _is_plausible_date(iso) else None
        except ValueError:
            return None
    return _normalize_date(raw)


def _lookup_filing_date(
    conn: sqlite3.Connection,
    docket_number: str,
    sub_number: str,
) -> Optional[str]:
    """Return the earliest filing_date for this docket from ncuc_discovery_records."""
    rows = conn.execute(
        """
        SELECT filing_date FROM ncuc_discovery_records
        WHERE (docket_number LIKE ? OR docket_number LIKE ?)
          AND (sub_number = ? OR sub_number LIKE ?)
          AND filing_date IS NOT NULL
        ORDER BY filing_date ASC
        """,
        (
            f"%{docket_number}%",
            f"E-{docket_number}%",
            sub_number,
            f"%{sub_number}%",
        ),
    ).fetchall()
    # Try each candidate until one normalizes successfully
    dates = []
    for (raw,) in rows:
        normed = _normalize_filing_date(raw)
        if normed:
            dates.append(normed)
    if dates:
        dates.sort()
        return dates[0]  # earliest filing date
    return None


# ---------------------------------------------------------------------------
# Main remediation function
# ---------------------------------------------------------------------------

@dataclass
class RemediationResult:
    total_null: int = 0
    pass1_resolved: int = 0
    pass1b_resolved: int = 0   # redline regex extraction
    pass1c_resolved: int = 0   # LLM extraction (high-confidence only)
    pass1c_medium: int = 0     # LLM medium-confidence (metadata only, not set)
    pass2_resolved: int = 0
    pass3_resolved: int = 0
    unresolved: int = 0
    updated_ids: list[int] = field(default_factory=list)
    unresolved_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


def remediate_null_effective_dates(
    repository,
    *,
    state: str = "NC",
    family_key: Optional[str] = None,
    limit: int = 500,
    dry_run: bool = False,
    passes: tuple[int, ...] = (1, 2, 3),
    enable_llm: bool = False,
) -> RemediationResult:
    """
    Multi-pass remediation of null effective_start on historical_documents.

    Pass 1   — Footer/header PDF scan (clean tariff footer).
    Pass 1B  — Redline regex scan: concatenated date pairs → proposed date.
    Pass 1C  — LLM extraction (opt-in via enable_llm=True): reads full page
               text, high-confidence effective dates become effective_start;
               medium-confidence dates stored in metadata for review.
    Pass 2   — Rider summary cross-reference (leaf-600/602).
    Pass 3   — Docket filing-date fallback (low-confidence proxy).

    Pass 1B is always run when pass 1 is enabled.
    Pass 1C requires enable_llm=True and a running Ollama instance.
    """
    result = RemediationResult(dry_run=dry_run)

    with repository._connect() as conn:
        q = """
            SELECT hd.id, hd.family_key, hd.local_path,
                   hd.start_page, hd.end_page,
                   hd.leaf_no, hd.metadata_json
            FROM historical_documents hd
            WHERE hd.state = ? AND hd.effective_start IS NULL
              AND hd.local_path IS NOT NULL
        """
        params: list = [state]
        if family_key:
            q += " AND hd.family_key = ?"
            params.append(family_key)
        q += " ORDER BY hd.id LIMIT ?"
        params.append(limit)

        rows = conn.execute(q, params).fetchall()
        result.total_null = len(rows)

        # Build rider index for pass 2 once
        rider_index = _build_rider_date_index(conn) if 2 in passes else {}

        for hd_id, fam_key, local_path, start_pg, end_pg, leaf_no, meta_json in rows:
            resolved_date: Optional[str] = None
            source: Optional[str] = None
            extra_meta: dict = {}

            # --- Pass 1: PDF footer/header scan (clean dates) ---
            if 1 in passes and local_path:
                resolved_date, source = _extract_date_from_pdf(
                    local_path, start_pg, end_pg
                )
                if resolved_date:
                    result.pass1_resolved += 1

            # --- Pass 1B: Redline regex scan ---
            if not resolved_date and 1 in passes and local_path:
                proposed, superseded, raw = _extract_redline_from_pdf(
                    local_path, start_pg, end_pg
                )
                if proposed:
                    resolved_date = proposed
                    source = "redline_proposed"
                    extra_meta["redline_superseded_date"] = superseded
                    extra_meta["redline_raw_match"] = raw
                    result.pass1b_resolved += 1

            # --- Pass 1C: LLM extraction (opt-in) ---
            if not resolved_date and enable_llm and local_path:
                llm_date, llm_source, llm_meta = _extract_date_llm(
                    local_path, start_pg, end_pg
                )
                if llm_date:
                    resolved_date = llm_date
                    source = llm_source  # "llm_extracted"
                    extra_meta.update(llm_meta)
                    result.pass1c_resolved += 1
                elif llm_meta and llm_source == "llm_medium":
                    # Medium confidence: save metadata for review but don't set date
                    extra_meta.update(llm_meta)
                    result.pass1c_medium += 1
                    # Write metadata even without setting effective_start
                    try:
                        meta = json.loads(meta_json) if meta_json else {}
                    except Exception:
                        meta = {}
                    meta.update(extra_meta)
                    if not dry_run:
                        conn.execute(
                            "UPDATE historical_documents SET metadata_json = ? WHERE id = ?",
                            (json.dumps(meta), hd_id),
                        )
                    result.unresolved += 1
                    result.unresolved_ids.append(hd_id)
                    continue

            # --- Pass 2: Rider summary cross-reference ---
            if not resolved_date and 2 in passes:
                resolved_date = _lookup_rider_date(rider_index, leaf_no, fam_key)
                if resolved_date:
                    source = "rider_summary_xref"
                    result.pass2_resolved += 1

            # --- Pass 3: Docket filing-date fallback ---
            if not resolved_date and 3 in passes and local_path:
                docket_num, sub_num = _docket_from_path(local_path)
                if docket_num and sub_num:
                    resolved_date = _lookup_filing_date(conn, docket_num, sub_num)
                    if resolved_date:
                        source = "docket_filing_proxy"
                        result.pass3_resolved += 1

            if not resolved_date:
                result.unresolved += 1
                result.unresolved_ids.append(hd_id)
                continue

            # Record source + any extra metadata
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {}
            meta["effective_start_source"] = source
            meta.update(extra_meta)

            if not dry_run:
                conn.execute(
                    "UPDATE historical_documents SET effective_start = ?, metadata_json = ? WHERE id = ?",
                    (resolved_date, json.dumps(meta), hd_id),
                )

            result.updated_ids.append(hd_id)

    return result
