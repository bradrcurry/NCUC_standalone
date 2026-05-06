"""
Parser for the "Riders" section embedded in DEP/DEC rate schedule leaves.

DEP format (nc-progress-leaf-*):
    Leaf No. 601    Rider BA
    Leaf No. 602    Rider JAA
    ...rider code follows "Rider " keyword

DEC format (nc-carolinas-schedule-*):
    Leaf No. 60
    Fuel Cost Adjustment Rider
    Leaf No. 62
    Energy Efficiency Rider
    ...rider name precedes "Rider" keyword; leaf and name on separate lines

Both utilities use "The following Riders are applicable to service supplied
under this schedule" as the section header cue, though DEC also uses
"RIDERS" as an all-caps header in older filings.

Prose references (Storm Securitization, CEPS) are picked up from the full
text using the same leaf-number patterns.

DEC leaf → family key mapping is maintained here so that leaf numbers found
in DEC rate schedules map to the correct nc-carolinas-rider-* family keys.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RiderReference:
    """One rider found in a rate schedule document."""
    rider_leaf_no: str          # e.g. "601" (DEP) or "60" (DEC)
    rider_code: str             # e.g. "BA", "EDIT-4", "FuelCostAdj"
    rider_family_key: str       # e.g. "nc-progress-leaf-601" or "nc-carolinas-rider-FCAR"
    mandatory: bool = True
    asterisk_note: str | None = None
    source_section: str = "riders"     # "riders", "prose"
    in_rider_summary: bool = True


@dataclass
class ScheduleRidersResult:
    """All riders extracted from one rate schedule document."""
    schedule_family_key: str
    schedule_leaf_no: str | None
    effective_start: str | None
    riders: list[RiderReference] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regexes — DEP format  ("Leaf No. 601  Rider BA")
# ---------------------------------------------------------------------------

_DEP_LEAF_RIDER_RE = re.compile(
    r"Leaf\s+No\.?\s*(\d+)\s+"
    r"(Rider\s+[\w\-]+[\w*]*)",
    re.I,
)

# ---------------------------------------------------------------------------
# Regexes — DEC format  ("Leaf No. 60\nFuel Cost Adjustment Rider")
# Leaf number on one line, rider name (ending in "Rider") on the next line.
# Allows up to ~120 chars of name before "Rider".
# ---------------------------------------------------------------------------

_DEC_LEAF_NAME_RIDER_RE = re.compile(
    r"Leaf\s+No\.?\s*(\d+)\s*\n"        # "Leaf No. 60\n"
    r"([\w][\w\s\-,/()]{0,120}?Rider)",  # "Fuel Cost Adjustment Rider"
    re.I,
)

# ---------------------------------------------------------------------------
# Prose / inline references (both utilities)
# "Leaf No. 607 Rider STS"  or  "Leaf No. 119 ... Storm Securitization ... Rider"
# ---------------------------------------------------------------------------

_PROSE_DEP_RE = re.compile(
    r"Leaf\s+No\.?\s*(\d+)\s+(Rider\s+[\w\-]+[\w*.]*)",
    re.I,
)

_PROSE_DEC_RE = re.compile(
    r"Leaf\s+No[s]?\.\s*(\d+)\s+(?:and\s+\d+\s+)?",
    re.I,
)

# Section boundary detectors
_RIDERS_SECTION_START = re.compile(
    r"(?:^|\n)\s*(?:The\s+following\s+Riders?\s+are\s+applicable|"
    r"(?:III|IV|V|VI|\d+)\.?\s+Riders?\s*$|"
    r"^RIDERS\s*$)",
    re.I | re.M,
)
_RIDERS_SECTION_END = re.compile(
    r"^\s*(?:IV|V|VI|VII|VIII|\d+)\.?\s+[A-Z]",
    re.I | re.M,
)

# Rider code extraction from "Rider BA*" → "BA"
_RIDER_CODE_RE = re.compile(r"Rider\s+([\w\-]+)\*{0,3}", re.I)

# Name extraction from "Fuel Cost Adjustment Rider" → "FuelCostAdj" code
_NAME_BEFORE_RIDER_RE = re.compile(r"^(.*?)\s+Rider$", re.I)

# ---------------------------------------------------------------------------
# DEP leaf → rider family key  (nc-progress-leaf-NNN)
# ---------------------------------------------------------------------------

# For DEP, the rider family key is always nc-progress-leaf-{leaf_no}.
# Codes here are informational only.
_DEP_LEAF_TO_CODE: dict[str, str] = {
    "601": "BA",
    "602": "JAA",
    "604": "EDIT-4",
    "605": "CPRE",
    "607": "STS",
    "608": "RDM",
    "609": "ESM",
    "610": "PIM",
    "611": "CAR",
    "612": "RAL-2",
    "613": "NM",
    "614": "REPS",
}

# ---------------------------------------------------------------------------
# DEC leaf → rider family key  (nc-carolinas-rider-*)
# Derived from rider PDF headers (Leaf No. NNN on page 1 of each rider doc).
# Multiple leaf numbers can map to the same family (older leaf superseded by newer).
# ---------------------------------------------------------------------------

_DEC_LEAF_TO_FAMILY: dict[str, str] = {
    # Fuel / energy adjustment
    "60": "nc-carolinas-rider-FCAR",
    "11": "nc-carolinas-rider-FCAR",        # older leaf number
    # Energy Efficiency / DSM
    "62": "nc-carolinas-rider-EE",
    "185": "nc-carolinas-rider-EE",         # newer version
    "64": "nc-carolinas-rider-EDPR",        # Existing DSM Program Costs
    # BPM
    "63": "nc-carolinas-rider-BPMPPTTRUEUP",
    "105": "nc-carolinas-rider-BPMPROSPECTIVERIDER",
    "106": "nc-carolinas-rider-BPMPROSPECTIVERIDER",  # true-up, same family
    # CEPS / CAR
    "68": "nc-carolinas-rider-CEI",         # CEPS flat-fee
    "144": "nc-carolinas-rider-CAR",
    "326": "nc-carolinas-rider-CAR",        # SC version same family
    # Storm Securitization
    "119": "nc-carolinas-rider-STS",
    "133": "nc-carolinas-rider-STS",
    # CPRE / EDIT
    "127": "nc-carolinas-rider-NSC",        # CPRE → NSC rider family
    "131": "nc-carolinas-rider-EDIT4",
    "59": "nc-carolinas-rider-EDIT4",       # older EDIT-1 same family key
    # ESM / PIM
    "148": "nc-carolinas-rider-ESM",
    "165": "nc-carolinas-rider-ESM",
    "149": "nc-carolinas-rider-PIM",
    # NM / SCG / MRM
    "75": "nc-carolinas-rider-SCG",
    "71": "nc-carolinas-rider-SCG",         # older SCG
    "121": "nc-carolinas-rider-MRM",
    "143": "nc-carolinas-rider-NMB",
    # NPTC / SSR / other
    "194": "nc-carolinas-rider-RIDERNPTC",
    "113": "nc-carolinas-rider-SSR",
    "147": "nc-carolinas-rider-RDM",
    "79": "nc-carolinas-rider-US",
    "78": "nc-carolinas-rider-PS",
    "80": "nc-carolinas-rider-IS",
    "226": "nc-carolinas-rider-EB",
    "176": "nc-carolinas-rider-SBES",   # Small Business Energy Saver Program
    "181": "nc-carolinas-rider-IQHEU",  # Residential Income-Qualified High-Energy Use (Pilot)
    "255": "nc-carolinas-rider-ED",
    "402": "nc-carolinas-rider-GS",
}

# DEC rider code labels (short names for display/notes)
_DEC_LEAF_TO_CODE: dict[str, str] = {
    "60": "FCAR", "11": "FCAR",
    "62": "EE", "185": "EE",
    "64": "EDPR",
    "63": "BPM-TU", "105": "BPM-P", "106": "BPM-P",
    "68": "CEI/CEPS", "144": "CAR", "326": "CAR",
    "119": "STS", "133": "STS",
    "127": "NSC/CPRE", "131": "EDIT4", "59": "EDIT1",
    "148": "ESM", "165": "ESM", "149": "PIM",
    "75": "SCG", "71": "SCG",
    "121": "MRM", "143": "NMB",
    "194": "NPTC", "113": "SSR", "147": "RDM",
    "79": "US", "78": "PS", "80": "IS",
    "226": "EB", "176": "SBES", "181": "IQHEU",
    "255": "ED", "402": "GS",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_schedule_riders(
    text: str,
    *,
    schedule_family_key: str,
    schedule_leaf_no: str | None = None,
    effective_start: str | None = None,
    utility_prefix: str = "nc-progress",
) -> ScheduleRidersResult:
    """Extract rider references from a rate schedule document's full text.

    Handles both DEP (nc-progress) and DEC (nc-carolinas) formats automatically
    based on ``utility_prefix``.

    Args:
        text:                  Full extracted text of the schedule PDF.
        schedule_family_key:   Family key of the rate schedule.
        schedule_leaf_no:      Leaf number string (DEP) or None (DEC uses schedule codes).
        effective_start:       Effective date string for provenance.
        utility_prefix:        "nc-progress" for DEP, "nc-carolinas" for DEC.

    Returns:
        ``ScheduleRidersResult`` with deduplicated rider references.
    """
    result = ScheduleRidersResult(
        schedule_family_key=schedule_family_key,
        schedule_leaf_no=schedule_leaf_no,
        effective_start=effective_start,
    )

    is_dec = utility_prefix == "nc-carolinas"

    # --- Find the "Riders" section ---
    riders_text = _extract_riders_section(text, result.parse_warnings)

    seen_leaves: set[str] = set()

    if is_dec:
        # DEC: "Leaf No. 60\nFuel Cost Adjustment Rider"
        for match in _DEC_LEAF_NAME_RIDER_RE.finditer(riders_text):
            leaf_no = match.group(1).strip()
            rider_name = match.group(2).strip()
            _add_dec_rider_ref(result, leaf_no, rider_name, seen_leaves, source_section="riders")

        # DEC prose: "Leaf No. 119 and 133" STS references
        for match in _DEP_LEAF_RIDER_RE.finditer(riders_text):
            leaf_no = match.group(1).strip()
            rider_raw = match.group(2).strip()
            if leaf_no not in seen_leaves:
                _add_dec_rider_ref(result, leaf_no, rider_raw, seen_leaves, source_section="riders")

        # Full-text scan for prose references not in the riders section
        for match in _DEC_LEAF_NAME_RIDER_RE.finditer(text):
            leaf_no = match.group(1).strip()
            if leaf_no in seen_leaves:
                continue
            rider_name = match.group(2).strip()
            _add_dec_rider_ref(result, leaf_no, rider_name, seen_leaves, source_section="prose")

    else:
        # DEP: "Leaf No. 601  Rider BA"
        for match in _DEP_LEAF_RIDER_RE.finditer(riders_text):
            leaf_no = match.group(1).strip()
            rider_raw = match.group(2).strip()
            _add_dep_rider_ref(result, leaf_no, rider_raw, utility_prefix, seen_leaves,
                               source_section="riders")

        # Full-text prose scan
        for match in _PROSE_DEP_RE.finditer(text):
            leaf_no = match.group(1).strip()
            if leaf_no in seen_leaves:
                continue
            rider_raw = match.group(2).strip()
            _add_dep_rider_ref(result, leaf_no, rider_raw, utility_prefix, seen_leaves,
                               source_section="prose")

    result.riders = _dedupe_riders(result.riders)
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _extract_riders_section(text: str, warnings: list[str]) -> str:
    """Return just the text of the Riders section, or full text as fallback."""
    start_m = _RIDERS_SECTION_START.search(text)
    if not start_m:
        warnings.append("riders_section_not_found")
        return text

    start = start_m.end()
    end_m = _RIDERS_SECTION_END.search(text, start)
    end = end_m.start() if end_m else len(text)
    return text[start:end]


def _add_dep_rider_ref(
    result: ScheduleRidersResult,
    leaf_no: str,
    rider_raw: str,
    utility_prefix: str,
    seen_leaves: set[str],
    source_section: str,
) -> None:
    if leaf_no in seen_leaves:
        return

    code_m = _RIDER_CODE_RE.match(rider_raw)
    rider_code = code_m.group(1).rstrip("*").strip() if code_m else rider_raw.strip()

    asterisk_note = "asterisk-annotated" if "*" in rider_raw else None
    rider_family_key = f"{utility_prefix}-leaf-{leaf_no}"

    seen_leaves.add(leaf_no)
    result.riders.append(RiderReference(
        rider_leaf_no=leaf_no,
        rider_code=rider_code,
        rider_family_key=rider_family_key,
        mandatory=True,
        asterisk_note=asterisk_note,
        source_section=source_section,
        in_rider_summary=True,
    ))


def _add_dec_rider_ref(
    result: ScheduleRidersResult,
    leaf_no: str,
    rider_name_raw: str,
    seen_leaves: set[str],
    source_section: str,
) -> None:
    """Add a DEC rider reference, mapping leaf number to nc-carolinas-rider-* family key."""
    if leaf_no in seen_leaves:
        return

    # Derive rider code from name ("Fuel Cost Adjustment Rider" → "FCAR")
    rider_code = _DEC_LEAF_TO_CODE.get(leaf_no)
    if not rider_code:
        # Fallback: use the name as code (strip trailing "Rider")
        m = _NAME_BEFORE_RIDER_RE.match(rider_name_raw.strip())
        if m:
            rider_code = m.group(1).strip()
        else:
            rider_code = rider_name_raw.strip()

    rider_family_key = _DEC_LEAF_TO_FAMILY.get(leaf_no)
    if not rider_family_key:
        # Unknown DEC leaf — use a placeholder leaf key
        rider_family_key = f"nc-carolinas-leaf-{leaf_no}"
        result.parse_warnings.append(f"unknown_dec_leaf_{leaf_no}")

    seen_leaves.add(leaf_no)
    result.riders.append(RiderReference(
        rider_leaf_no=leaf_no,
        rider_code=rider_code,
        rider_family_key=rider_family_key,
        mandatory=True,
        asterisk_note=None,
        source_section=source_section,
        in_rider_summary=True,
    ))


def _dedupe_riders(riders: list[RiderReference]) -> list[RiderReference]:
    seen: set[str] = set()
    out: list[RiderReference] = []
    for r in riders:
        if r.rider_leaf_no not in seen:
            seen.add(r.rider_leaf_no)
            out.append(r)
    return out
