"""Extract Duke schedule / rider codes from section text.

Used to backfill ``document_sections.schedule_codes_json`` for sections
that the original splitter left empty. Only 16% of the 14,360 sections
currently have schedule_codes populated, which is the main reason the
RAG eval ``recall@5`` is held at 0.571 — half the time we retrieve a
semantically-correct section but cannot verify it via metadata.

Strategy:
- Look only at the first 1500 chars of section text (titles + first
  leaf header typically live there).
- Apply ordered patterns, most specific first.
- Run against a blocklist of false-positive tokens that look like codes
  but are common English words.
- Validate against the 2,324 sections that already have curated codes:
  precision is the fraction of extracted codes that appear in the
  existing list; recall is the fraction of existing codes the extractor
  finds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Codes that look like our pattern but are false positives. Most are
# section-heading boilerplate. Keep this list short and reviewable.
_BLOCKLIST: set[str] = {
    "NC",
    "NORTH",
    "CAROLINA",
    "AVAILABILITY",
    "APPLICABILITY",
    "ADJUSTMENT",
    "ADJUSTMENTS",
    "DUKE",
    "ENERGY",
    "PROGRESS",
    "CAROLINAS",
    "LLC",
    "TYPE",
    "OF",
    "SERVICE",
    "MONTHLY",
    "RATE",
    "RATES",
    "RIDER",
    "RIDERS",
    "SCHEDULE",
    "SCHEDULES",
    "TARIFF",
    "PAGE",
    "LEAF",
    "REVISED",
    "ORIGINAL",
    "SUPERSEDING",
    "COMMISSION",
    "DOCKET",
    "EXHIBIT",
    "EFFECTIVE",
    "DATE",
    "FOR",
    "AND",
    "THE",
    "ALL",
    "ANY",
    "MAY",
    "INC",
    "SUB",
    "TOTAL",
    "BASE",
    "SUMMARY",
    "RIDER ADJUSTMENTS",
    "FUEL",  # appears in "Fuel charge" not as code
    "TAX",
    "TIME-OF-USE",  # heading text picked up by compound regex
    "ALL-ENERGY",
}

# Docket numbers (E-N, E-NN) — these reference NCUC filings, not schedules.
# Specific Duke NC dockets observed in the corpus.
_DOCKET_CODES: set[str] = {
    "E-1", "E-2", "E-3", "E-4", "E-4-E", "E-5", "E-7", "E-22",
    "E-100",  # generic commission rule docket
    "G-1", "G-2", "G-3", "G-6", "M-100", "M-1000",
}

# NC Utilities Commission rule references: R8-XX, R12-XX, R25-XX, etc.
# These are Commission rules, not schedules.
_COMMISSION_RULE_RE = re.compile(r"^R\d+-\d+$", re.IGNORECASE)

# Single-token codes we'll accept even though they're short (2 chars).
# This list is small on purpose — most short matches are noise.
_KNOWN_SHORT_CODES: set[str] = {
    "RS", "RES", "PL", "PG", "PS", "PV", "WC", "EB", "BA",
    "JAA", "MFS", "SGS", "LGS", "EE", "OS", "OSM", "STS",
    "EDR", "SLR", "FCAR", "REPS", "CEPS",
}

# Tokens within an explicit SCHEDULE/RIDER prefix construction.
# E.g. "SCHEDULE RES" or "RIDER EB (NC)" — captures the code.
_EXPLICIT_PREFIX_RE = re.compile(
    r"\b(?:SCHEDULE|RIDER|RATE\s+SCHEDULE)\s+"
    r"([A-Z][A-Z0-9]{0,2}(?:-[A-Z0-9]+){0,3})"
    r"(?:\s*\(NC\))?\b",
    re.IGNORECASE,
)

# Compound codes like R-TOU-CPP, LGS-TOU, SGS-TOUE-79. At least one
# dash, all parts uppercase letters/digits. Standalone, no prefix.
_COMPOUND_CODE_RE = re.compile(
    r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,4})\b"
)

# Position-anchored title-block extractor — only run on first 600 chars.
_TITLE_BLOCK_CHARS = 600
_FULL_SCAN_CHARS = 1500


@dataclass(frozen=True)
class ExtractionResult:
    codes: list[str]
    sources: dict[str, str]  # code -> "explicit_prefix" | "compound" | "known_short"


def _normalize_code(raw: str) -> str:
    """Uppercase + strip whitespace + drop leading "SCHEDULE "/"RIDER "."""
    s = raw.upper().strip()
    for prefix in ("SCHEDULE ", "RIDER "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    return s


def _accept(code: str) -> bool:
    if not code or code in _BLOCKLIST:
        return False
    # Docket numbers (E-N, G-N, M-NN, etc.)
    if code in _DOCKET_CODES:
        return False
    # NC Utilities Commission rules (R8-XX, R12-XX, R25-XX, etc.)
    if _COMMISSION_RULE_RE.match(code):
        return False
    # Length guard
    if len(code) > 25:
        return False
    # Pure digits aren't codes (they're page numbers)
    if code.isdigit():
        return False
    # Single-character codes are rejected
    if len(code) < 2:
        return False
    # Two-char codes only if in known-shorts list
    if len(code) == 2 and code not in _KNOWN_SHORT_CODES:
        return False
    return True


def extract_codes(text: str) -> ExtractionResult:
    """Return the codes found in ``text``, preserving discovery order.

    Looks only at the first 1500 chars (title block + first page header).
    Codes are deduplicated case-insensitively. Each code is annotated with
    the pattern that found it for debugging.
    """
    if not text:
        return ExtractionResult(codes=[], sources={})

    head = text[:_FULL_SCAN_CHARS]
    title = text[:_TITLE_BLOCK_CHARS]
    found: list[str] = []
    sources: dict[str, str] = {}
    seen: set[str] = set()

    # 1. Explicit prefix patterns (highest precision)
    for m in _EXPLICIT_PREFIX_RE.finditer(head):
        code = _normalize_code(m.group(1))
        if _accept(code) and code not in seen:
            seen.add(code)
            found.append(code)
            sources[code] = "explicit_prefix"

    # 2. Compound dashed codes (R-TOU-CPP etc.) anywhere in head
    for m in _COMPOUND_CODE_RE.finditer(head):
        code = _normalize_code(m.group(1))
        if _accept(code) and code not in seen:
            seen.add(code)
            found.append(code)
            sources[code] = "compound"

    # 3. Known short codes appearing prominently in the title block.
    # Strict: must be a whole word match, only in title region.
    title_upper = title.upper()
    for short in _KNOWN_SHORT_CODES:
        if short in seen:
            continue
        # Word-boundary check
        if re.search(rf"\b{re.escape(short)}\b", title_upper):
            found.append(short)
            sources[short] = "known_short"
            seen.add(short)

    return ExtractionResult(codes=found, sources=sources)
