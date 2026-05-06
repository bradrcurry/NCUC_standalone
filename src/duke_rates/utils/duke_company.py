from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple


@dataclass(frozen=True)
class DukeCompanyAlias:
    canonical_company: str
    literal_name: str
    pattern: re.Pattern[str]


def _pat(expr: str) -> re.Pattern[str]:
    return re.compile(expr, re.I)


# ---------------------------------------------------------------------------
# Primary matching — explicit company name aliases
# ---------------------------------------------------------------------------
# Aliases are ordered most-specific first within each company group.
# detect_duke_company() collects ALL matches, so ordering is for readability.
# normalize_duke_company() resolves ambiguity via the fallback mechanism.
#
# "Duke Energy" (the parent holding company) maps to carolinas as a last-resort
# signal: it appears in both DEP and DEC documents, but a specific alias
# (e.g. "Duke Energy Progress") already matched -> the set logic picks that.
# Negative lookahead prevents double-firing on "Duke Energy Progress/Carolinas".
#
# "Southern Power Company" is a DEC generation subsidiary.
DUKE_COMPANY_ALIASES: tuple[DukeCompanyAlias, ...] = (
    # --- Duke Energy Progress (DEP) ---
    # Current name (post-2012 Progress Energy merger)
    DukeCompanyAlias("progress", "Duke Energy Progress",          _pat(r"\bduke\s+energy\s+progress\b")),
    DukeCompanyAlias("progress", "Duke Energy Progress Inc",      _pat(r"\bduke\s+energy\s+progress(?:,?\s*inc\.?)?\b")),
    # 2000–2012: Progress Energy restructuring
    DukeCompanyAlias("progress", "Progress Energy Carolinas",     _pat(r"\bprogress\s+energy\s+carolinas\b")),
    DukeCompanyAlias("progress", "Progress Energy Carolinas Inc", _pat(r"\bprogress\s+energy\s+carolinas(?:,?\s*inc\.?)?\b")),
    # "Progress Energy" alone — in NC context almost always DEP
    DukeCompanyAlias("progress", "Progress Energy",               _pat(r"\bprogress\s+energy\b")),
    # Pre-2000: Carolina Power & Light Company
    DukeCompanyAlias("progress", "Carolina Power & Light",        _pat(r"\bcarolina\s+power\s*(?:&|and)\s*light\b")),
    DukeCompanyAlias("progress", "Carolina Power and Light",      _pat(r"\bcarolina\s+power\s+and\s+light\b")),
    DukeCompanyAlias("progress", "CP&L",                          _pat(r"\bcp\s*&\s*l\b")),
    DukeCompanyAlias("progress", "CP and L",                      _pat(r"\bcp\s+and\s+l\b")),
    DukeCompanyAlias("progress", "CPL",                           _pat(r"\bcpl\b")),

    # --- Duke Energy Carolinas (DEC) ---
    # Current name (post-2006 Duke Power rename)
    DukeCompanyAlias("carolinas", "Duke Energy Carolinas",        _pat(r"\bduke\s+energy\s+carolinas\b")),
    DukeCompanyAlias("carolinas", "Duke Energy Carolinas Inc",    _pat(r"\bduke\s+energy\s+carolinas(?:,?\s*inc\.?)?\b")),
    # Pre-2006: Duke Power Company
    DukeCompanyAlias("carolinas", "Duke Power Company",           _pat(r"\bduke\s+power\s+company\b")),
    DukeCompanyAlias("carolinas", "Duke Power",                   _pat(r"\bduke\s+power\b")),
    # DEC generation subsidiary
    DukeCompanyAlias("carolinas", "Southern Power Company",       _pat(r"\bsouthern\s+power\s+company\b")),
    # DEC subsidiary serving western NC (Nantahala basin)
    DukeCompanyAlias("carolinas", "Nantahala Power and Light",    _pat(r"\bnantahala\s+power\b")),
    # "Duke Energy" alone — last-resort DEC signal (negative lookahead avoids
    # double-firing on "Duke Energy Progress" or "Duke Energy Carolinas")
    DukeCompanyAlias("carolinas", "Duke Energy",                  _pat(r"\bduke\s+energy\b(?!\s+(?:progress|carolinas))")),
)

DUKE_COMPANY_LITERALS_BY_CANONICAL: dict[str, tuple[str, ...]] = {
    canonical: tuple(alias.literal_name for alias in DUKE_COMPANY_ALIASES if alias.canonical_company == canonical)
    for canonical in ("progress", "carolinas")
}

PROGRESS_OPENEI_ALIASES: tuple[str, ...] = (
    "Progress Energy Carolinas Inc",
    "Duke Energy Progress",
)

VALID_DUKE_COMPANIES_BY_STATE = {
    "NC": {"carolinas", "progress"},
    "SC": {"carolinas", "progress"},
    "FL": {"florida"},
    "IN": {"indiana"},
    "KY": {"kentucky"},
    "OH": {"ohio"},
}

# ---------------------------------------------------------------------------
# Secondary context clues — used when primary matching is ambiguous
# ---------------------------------------------------------------------------
# Each tuple is (signal_text, canonical_company, weight).
# Positive weight = evidence FOR that company; all weights are positive here
# and we accumulate per-company totals, then compare.
#
# Rate schedule codes:
#   DEP uses "RES", "SGS", "LGS", "HP", "RT" style codes
#   DEC uses "RS", "PG", "LGS", "OL", "PL" style codes
#   (some codes overlap — treated as weak evidence only)
#
# Geography:
#   DEP serves eastern/central NC (Raleigh, Durham, Sanford, Fayetteville)
#   DEC serves western/piedmont NC (Charlotte, Gastonia, Asheville)
#
# Docket numbers: E-2 = DEP, E-7 = DEC (NCUC docket series)

class _ContextClue(NamedTuple):
    pattern: re.Pattern[str]
    company: str
    weight: float


_CONTEXT_CLUES: tuple[_ContextClue, ...] = (
    # --- Rate schedule markers ---
    # DEP schedule codes
    _ContextClue(_pat(r"\bschedule\s+res\b"),             "progress",  1.5),
    _ContextClue(_pat(r"\bschedule\s+sgs\b"),             "progress",  1.0),
    _ContextClue(_pat(r"\bschedule\s+hp\b"),              "progress",  1.0),
    _ContextClue(_pat(r"\brider\s+pps\b"),                "progress",  1.5),
    _ContextClue(_pat(r"\bleaf\s+no\.?\s*\d{3}\b"),       "progress",  0.5),  # 3-digit leaf = DEP style
    # DEC schedule codes
    _ContextClue(_pat(r"\bschedule\s+rs\b"),              "carolinas", 1.5),
    _ContextClue(_pat(r"\bschedule\s+re\b"),              "carolinas", 1.5),
    _ContextClue(_pat(r"\bschedule\s+pl\b"),              "carolinas", 1.5),  # Street/Public Lighting = DEC only
    _ContextClue(_pat(r"\bschedule\s+ol\b"),              "carolinas", 1.5),  # Outdoor Lighting = DEC only
    _ContextClue(_pat(r"\belectricity\s+no\.?\s*4\b"),    "carolinas", 2.0),  # DEC tariff book = Electricity No. 4
    _ContextClue(_pat(r"\belectricity\s+no\.?\s*3\b"),    "progress",  2.0),  # DEP tariff book = Electricity No. 3

    # --- Docket number series ---
    _ContextClue(_pat(r"\be-2\b"),                        "progress",  2.0),
    _ContextClue(_pat(r"\be-7\b"),                        "carolinas", 2.0),

    # --- Geographic service territory ---
    # DEP territory: eastern NC (weight 1.0 — reliable individual signals)
    _ContextClue(_pat(r"\braleigh\b"),                    "progress",  1.0),
    _ContextClue(_pat(r"\bdurham\b"),                     "progress",  1.0),
    _ContextClue(_pat(r"\bfayetteville\b"),               "progress",  1.0),
    _ContextClue(_pat(r"\bgoldsboro\b"),                  "progress",  1.0),
    _ContextClue(_pat(r"\bwilson,?\s*nc\b"),              "progress",  1.0),
    _ContextClue(_pat(r"\bsanford\b"),                    "progress",  1.0),
    # DEC territory: western/piedmont NC (weight 1.0)
    _ContextClue(_pat(r"\bcharlotte\b"),                  "carolinas", 1.0),
    _ContextClue(_pat(r"\bgastonia\b"),                   "carolinas", 1.0),
    _ContextClue(_pat(r"\basheville\b"),                  "carolinas", 1.0),
    _ContextClue(_pat(r"\bstatesville\b"),                "carolinas", 1.0),
    _ContextClue(_pat(r"\bhickory\b"),                    "carolinas", 1.0),
    _ContextClue(_pat(r"\bconco[r]d\b"),                  "carolinas", 1.0),
)

# Minimum net score difference to declare a winner from context clues alone.
# Single strong clue (weight 2.0) or two geography hits (2x1.0) exceed this.
_CONTEXT_MIN_MARGIN = 1.5


def detect_duke_company(text: str) -> set[str]:
    """
    Primary matching: return canonical company keys found via explicit name aliases.

    Returns an empty set if no alias matches, a singleton if unambiguous, or
    both keys if the text contains aliases from both companies.
    """
    haystack = text or ""
    matched: set[str] = set()
    for alias in DUKE_COMPANY_ALIASES:
        if alias.pattern.search(haystack):
            matched.add(alias.canonical_company)
    return matched


def infer_duke_company_from_context(text: str) -> str | None:
    """
    Secondary matching: infer company from context clues when primary matching
    is ambiguous or absent.

    Scores each context clue against the text and returns the company with the
    higher net score, provided the margin exceeds _CONTEXT_MIN_MARGIN.
    Returns None if evidence is weak or balanced.
    """
    haystack = (text or "").lower()
    scores: dict[str, float] = {"progress": 0.0, "carolinas": 0.0}
    for clue in _CONTEXT_CLUES:
        if clue.pattern.search(haystack):
            scores[clue.company] += clue.weight

    progress_score = scores["progress"]
    carolinas_score = scores["carolinas"]
    margin = abs(progress_score - carolinas_score)

    if margin < _CONTEXT_MIN_MARGIN:
        return None
    return "progress" if progress_score > carolinas_score else "carolinas"


def normalize_duke_company(
    text: str,
    *,
    fallback: str | None = None,
    state: str | None = None,
) -> str | None:
    """
    Normalize free-text Duke utility naming to a canonical company key.

    Resolution order:
      1. Primary: explicit alias match → unambiguous singleton → return it
      2. Primary: both companies matched → use fallback if it's one of them
      3. Secondary: no primary match or unresolvable → context clues
      4. Fallback: return caller-supplied default

    Returns None only when both primary and secondary matching are ambiguous
    and no fallback is provided.
    """
    matched = detect_duke_company(text)

    if len(matched) == 1:
        candidate = next(iter(matched))
    elif len(matched) == 0:
        # No name alias found — try context clues
        candidate = infer_duke_company_from_context(text) or fallback
    else:
        # Both companies matched — prefer fallback if it's one of them,
        # otherwise try to break the tie with context clues
        if fallback in matched:
            candidate = fallback
        else:
            inferred = infer_duke_company_from_context(text)
            candidate = inferred if inferred in matched else None

    if not state or not candidate:
        return candidate

    allowed = VALID_DUKE_COMPANIES_BY_STATE.get(state.upper())
    if not allowed:
        return candidate
    return candidate if candidate in allowed else fallback


def is_duke_company_related(text: str, canonical_company: str) -> bool:
    return canonical_company in detect_duke_company(text)
