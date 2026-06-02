"""Parse the ``Summary of Rider Adjustments`` pages in proposed NCUC filings.

Both DEP (E-2) and DEC (E-7) rate-case exhibits include a *Summary of Rider
Adjustments* sheet (DEC leaf 99, DEP leaf 600) that lists, per schedule group,
every rider's ``cents/kWh`` increment/decrement and the effective date it took.
This is the single highest-value page for forward (projected-year) bill
modelling, yet the line-anchored charge regexes used elsewhere cannot read it:
the rider name, the numeric value, and the effective date each sit on their own
line, and the ``cents/kWh`` unit only appears once as a column header.

The two utilities lay the table out differently:

* **DEC** groups wrapped rider names together, then lists their values, then
  their dates (``name name name / val val val / date date date``). Because the
  per-group counts of names, values, and dates match, we pair them by position
  (FIFO zip).
* **DEP** is strictly interleaved (``name / val / date``), but interleaves
  section headers that carry no value (e.g. ``Annual Billing Adjustments Rider
  BA``) and dateless subtotals (``... - Net Adjustment``). We pair each value
  with the most recent pending name and attach a following date when present.

Everything here is pure text-in / dataclass-out so it can be unit-tested
without a PDF and reused from both the CLI scanner and the SQLite pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "RiderSummaryCharge",
    "is_rider_summary_page",
    "extract_rider_summary_charges",
]

_SUMMARY_HEADING_RE = re.compile(
    r"summary\s+of\s+rider\s+adjustments", re.IGNORECASE
)
_SUMMARY_INTRO_RE = re.compile(
    r"following\s+is\s+a\s+summary\s+of\s+rider\s+adjustments", re.IGNORECASE
)
# A schedule-group header is keyed off the *plural* word "Schedules"; rider
# rows never use it. DEC headers also carry an inline comma list of codes
# ("Residential Schedules RS, RE, ES, RT, RSTC, RETC"); DEP headers are
# name-only ("Residential Service Schedules**").
_GROUP_HEADER_RE = re.compile(r"\bSchedules\b", re.IGNORECASE)
_VALUE_RE = re.compile(r"^\(?\s*(?P<sign>-)?\s*(?P<num>\d*\.\d+)\s*\)?$")
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
_TOTAL_RE = re.compile(r"\bTOTAL\b", re.IGNORECASE)
_CODE_LIST_RE = re.compile(r"\b[A-Z][A-Z0-9-]{0,9}\b")
# Lines that are pure column furniture, not rider names.
_FURNITURE_RE = re.compile(
    r"^(?:cents|/?\s*kwh|cents\s*/\s*kwh|effective|date|effective\s+date|"
    r"revision\s+no\.?|leaf\s+no\.?|description)\s*$",
    re.IGNORECASE,
)
_FOOTER_RE = re.compile(
    r"(duke\s+energy|docket\s+no\.?|application\s+exhibit|page\s+\d+\s+of|"
    r"superseding|effective\s+for\s+service|north\s+carolina\s+only|"
    r"ncuc\s+docket)",
    re.IGNORECASE,
)
# Rider-name lines end in one of these role words in both filings.
_NAME_TAIL_RE = re.compile(
    r"(rider|rate|factor|adjustment|mechanism|credit|discount|"
    r"\bBA\b|\bEMF\b|net\s+adjustment)\b[^a-z]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RiderSummaryCharge:
    """One rider increment row lifted from a summary table.

    ``is_total`` marks the group's own printed ``TOTAL cents/kWh`` line rather
    than an individual rider — the utility's authoritative combined rider
    adder for that schedule group, which downstream callers use for all-in
    pricing (it already nets sub-components and any embedded base-fuel, so it
    must not be summed together with the individual rider rows).
    """

    schedule_group: str
    schedule_codes: tuple[str, ...]
    rider_label: str
    cents_per_kwh: float
    rate_value: float  # signed $/kWh
    effective_date: str | None
    raw_line: str
    is_total: bool = False


def is_rider_summary_page(text: str) -> bool:
    """Return True when a page is a Summary of Rider Adjustments body page.

    We require the explanatory intro sentence (or the heading plus a group
    header) so index rows that merely *list* ``Summary of Rider Adjustments``
    as a leaf entry are not mistaken for the body sheet.
    """
    if not text:
        return False
    if _SUMMARY_INTRO_RE.search(text):
        return True
    if _SUMMARY_HEADING_RE.search(text):
        # Heading present and at least one group header below it.
        return any(_is_group_header(line) for line in text.splitlines())
    return False


def extract_rider_summary_charges(
    text: str,
    *,
    strategy: Literal["dec", "dep"] = "dec",
) -> list[RiderSummaryCharge]:
    """Parse a summary page into per-group rider increment rows."""
    groups = _split_into_groups(text)
    charges: list[RiderSummaryCharge] = []
    for header, body in groups:
        label, codes = _parse_group_header(header)
        if strategy == "dec":
            rows, total = _pair_dec(body)
        else:
            rows, total = _pair_dep(body)
        for name, value, date in rows:
            cents = value
            charges.append(
                RiderSummaryCharge(
                    schedule_group=label,
                    schedule_codes=codes,
                    rider_label=name,
                    cents_per_kwh=round(cents, 6),
                    rate_value=round(cents / 100.0, 8),
                    effective_date=date,
                    raw_line=f"{label} | {name} | {value}¢/kWh"
                    + (f" | eff {date}" if date else ""),
                )
            )
        if total is not None:
            charges.append(
                RiderSummaryCharge(
                    schedule_group=label,
                    schedule_codes=codes,
                    rider_label="TOTAL cents/kWh",
                    cents_per_kwh=round(total, 6),
                    rate_value=round(total / 100.0, 8),
                    effective_date=None,
                    raw_line=f"{label} | TOTAL cents/kWh | {total}¢/kWh",
                    is_total=True,
                )
            )
    return charges


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


def _clean_lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _is_group_header(line: str) -> bool:
    if not _GROUP_HEADER_RE.search(line):
        return False
    if _NAME_TAIL_RE.search(line):
        return False
    if _VALUE_RE.match(line) or _DATE_RE.match(line):
        return False
    return len(line) < 140


def _split_into_groups(text: str) -> list[tuple[str, list[str]]]:
    """Return ``(header_text, body_lines)`` per schedule group on the page."""
    lines = _clean_lines(text)
    groups: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_body: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_group_header(line):
            if current_header is not None:
                groups.append((current_header, current_body))
            header = line
            # DEC headers that wrap end with a trailing comma; join the next
            # line (the rest of the inline code list) onto the header.
            while header.rstrip().endswith(",") and i + 1 < len(lines):
                i += 1
                header = f"{header} {lines[i]}"
            current_header = header
            current_body = []
            i += 1
            continue
        if current_header is not None:
            current_body.append(line)
        i += 1
    if current_header is not None:
        groups.append((current_header, current_body))
    return groups


def _parse_group_header(header: str) -> tuple[str, tuple[str, ...]]:
    label = re.sub(r"\s+", " ", header).strip().rstrip("*").strip()
    # Inline code list lives after the word "Schedules" (DEC only).
    tail = re.split(r"\bSchedules\b", header, maxsplit=1, flags=re.IGNORECASE)
    codes: tuple[str, ...] = ()
    if len(tail) == 2:
        candidate = tail[1]
        if "," in candidate:
            found = [
                c
                for c in _CODE_LIST_RE.findall(candidate.upper())
                if not c.isalpha() or len(c) <= 6
            ]
            codes = tuple(dict.fromkeys(found))
    return label, codes


# --------------------------------------------------------------------------
# Line classification + pairing
# --------------------------------------------------------------------------


def _classify(line: str) -> tuple[str, float | None]:
    if _TOTAL_RE.search(line):
        return "total", None
    if _FURNITURE_RE.match(line) or _FOOTER_RE.search(line):
        return "skip", None
    m = _VALUE_RE.match(line)
    if m:
        value = float(m.group("num"))
        if m.group("sign") or _looks_parenthesized(line):
            value = -value
        return "value", value
    if _DATE_RE.match(line):
        return "date", None
    if any(ch.isalpha() for ch in line):
        return "name", None
    return "skip", None


def _looks_parenthesized(line: str) -> bool:
    return line.strip().startswith("(") and ")" in line


def _pair_dec(
    body: list[str],
) -> tuple[list[tuple[str, float, str | None]], float | None]:
    """FIFO-zip names, values, and dates within a DEC group.

    DEC wraps multiple rider names together before their values, but the
    per-group counts match, so positional pairing is exact. Parsing of rider
    rows stops at the ``TOTAL`` row; the first value after ``TOTAL`` is the
    group's combined adder, returned alongside the rows.
    """
    names: list[str] = []
    values: list[float] = []
    dates: list[str] = []
    total = None
    for idx, line in enumerate(body):
        kind, value = _classify(line)
        if kind == "total":
            total = _next_value(body[idx + 1 :])
            break
        if kind == "name":
            names.append(_clean_name(line))
        elif kind == "value" and value is not None:
            values.append(value)
        elif kind == "date":
            dates.append(line.strip())
    rows: list[tuple[str, float, str | None]] = []
    for idx, value in enumerate(values):
        name = names[idx] if idx < len(names) else f"Rider {idx + 1}"
        date = dates[idx] if idx < len(dates) else None
        rows.append((name, value, date))
    return rows, total


def _pair_dep(
    body: list[str],
) -> tuple[list[tuple[str, float, str | None]], float | None]:
    """Sequentially pair DEP rows: each value binds to the most recent name,
    and a following date line (if any) attaches to that value.

    A name immediately followed by another name is a section header
    (``Annual Billing Adjustments Rider BA``); it is simply superseded by the
    next name. Dateless subtotals (``... - Net Adjustment``) are kept with a
    ``None`` date. The first value after ``TOTAL`` is the group's combined
    adder, returned alongside the rows.
    """
    rows: list[tuple[str, float, str | None]] = []
    pending_name: str | None = None
    total = None
    for idx, line in enumerate(body):
        kind, value = _classify(line)
        if kind == "total":
            total = _next_value(body[idx + 1 :])
            break
        if kind == "name":
            pending_name = _clean_name(line)
        elif kind == "value" and value is not None and pending_name is not None:
            rows.append((pending_name, value, None))
            pending_name = None
        elif kind == "date" and rows and rows[-1][2] is None:
            name, val, _ = rows[-1]
            rows[-1] = (name, val, line.strip())
    return rows, total


def _next_value(lines: list[str]) -> float | None:
    """Return the first classifiable value in ``lines`` (the TOTAL amount)."""
    for line in lines:
        kind, value = _classify(line)
        if kind == "value" and value is not None:
            return value
    return None


def _clean_name(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip(" :*").strip()
