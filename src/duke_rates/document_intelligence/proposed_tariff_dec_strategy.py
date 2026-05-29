"""DEC E-7 specific helpers for proposed-tariff parsing.

Duke Energy Carolinas application exhibits follow a different layout than DEP
filings:

* Each proposed Exhibit B/B_1/B_2 opens with a ``LEAF NO. / DESCRIPTION /
  REVISION NO.`` index table that lists schedules in body order.
* Schedule body pages do not carry inline schedule headings — they begin with
  the ``AVAILABILITY`` paragraph and run until the next ``AVAILABILITY`` page.
* Rate cells live on a line by themselves under a separate label line, e.g.::

      For the first 6,000 kWh per month, per kWh
      8.4138¢

The helpers here are deliberately small and pure so they can be unit-tested
without a PDF in hand and reused both from the standalone CLI scanner and from
the SQLite section pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from duke_rates.document_intelligence.proposed_tariff_detector import (
    ProposedTariffBlock,
)


_INDEX_HEADER_RE = re.compile(
    r"\bLEAF\s+NO\.?\s*\n\s*DESCRIPTION\s*\n\s*REVISION\s+NO\.?",
    re.IGNORECASE,
)
_INDEX_LINE_RE = re.compile(
    r"^(?P<leaf>\d{1,4})\s+"
    r"(?P<code>[A-Z][A-Z0-9-]{0,15})\s+"
    r"(?P<desc>[A-Za-z0-9].*?)"
    r"(?:\s*[.�…]{2,}\s*\d+\s*)$"
)
_DESC_TAIL_NOISE_RE = re.compile(r"[�….\s]+$")

_VALUE_ONLY_RE = re.compile(
    r"^\s*(?P<sign>-)?\s*\$\s*(?P<dollar>[0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
)
_CENTS_VALUE_RE = re.compile(
    r"^\s*\(?\s*(?P<sign>-)?\s*"
    r"(?P<cents>[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?:¢|cents?)\s*\)?\s*/?\s*(?:kWh)?\s*$",
    re.IGNORECASE,
)

_RATE_LABEL_KEYWORDS_RE = re.compile(
    r"\b("
    r"basic\s+customer\s+charge|"
    r"per\s+kWh|"
    r"per\s+kW\b|"
    r"per\s+month|"
    r"per\s+bill|"
    r"per\s+day|"
    r"per\s+fixture|"
    r"per\s+lamp|"
    r"per\s+pole|"
    r"per\s+block|"
    r"demand\s+charge|"
    r"energy\s+charge|"
    r"on[-\s]?peak|off[-\s]?peak|"
    r"discount|"
    r"rider\s+adjustment|"
    r"facilities\s+charge|"
    r"minimum\s+bill|"
    r"reactive\s+demand"
    r")\b",
    re.IGNORECASE,
)
_THRESHOLD_TOKEN_RE = re.compile(
    r"^\s*\d{1,3}(?:,\d{3})+\s*$"
)
_MONEY_PER_UNIT_INLINE_RE = re.compile(
    r"\bper\s+(month|kW|kWh|bill|day|fixture|lamp|pole|block)\b",
    re.IGNORECASE,
)

_EXHIBIT_FOOTER_RE = re.compile(
    r"Application\s+Exhibit\s+(?P<key>B(?:_[12])?)",
    re.IGNORECASE,
)
_RATE_YEAR_TITLE_RE = re.compile(
    r"Rate\s+Year\s+(?P<year>[0-2])\s+North\s+Carolina\s+Tariffs\s+Proposed\s+for\s+Change",
    re.IGNORECASE,
)
_DEC_UTILITY_RE = re.compile(r"Duke\s+Energy\s+Carolinas", re.IGNORECASE)
_AVAILABILITY_START_RE = re.compile(r"^\s*AVAILABILITY\s*$")

_DEC_RIDER_CATALOG_HEADER_RE = re.compile(r"\bRETAIL\s+RIDERS\b", re.IGNORECASE)
_DEC_RIDER_LINE_RE = re.compile(
    r"^(?P<code>[A-Z][A-Z0-9-]{0,7})\s+"
    r"(?P<title>[A-Za-z0-9][A-Za-z0-9\s,'/&\-]*?)\s+Rider\b"
)
_DEC_RIDER_TERMINATOR_RE = re.compile(r"\bOther\s+Tariffs\b", re.IGNORECASE)

_DEC_RIDER_BODY_HEADER_RE = re.compile(
    r"^\s*RIDER\s+(?P<code>[A-Z][A-Z0-9-]{0,7})\b",
)
_DEC_RIDER_BODY_PARENS_RE = re.compile(
    r"^\s*(?P<title>[A-Za-z][A-Za-z0-9\s'/&\-]*?)\s+Rider\s*\((?P<code>[A-Z][A-Z0-9-]{0,7})\)",
)
_DEC_RIDER_CONTENT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("FCAR", "FUEL COST ADJUSTMENT", re.compile(
        r"\b(?:Fuel\s+Cost\s+Adjustment|fuel\s+charge\s+adjustment)\b", re.IGNORECASE
    )),
    ("EDPR", "EXISTING DSM PROGRAM COSTS ADJUSTMENT", re.compile(
        r"\bExisting\s+DSM\s+Program\s+Costs\b", re.IGNORECASE
    )),
    ("PTC", "PRODUCTION TAX CREDITS", re.compile(
        r"\bproduction\s+tax\s+credits?\b.*?[\"'‘’“”�]?PTC[\"'‘’“”�]?",
        re.IGNORECASE | re.DOTALL,
    )),
    ("EE", "ENERGY EFFICIENCY", re.compile(
        r"\bEnergy\s+Efficiency\s+Rider\b", re.IGNORECASE
    )),
    ("US", "UNMETERED SERVICE", re.compile(
        r"\bUnmetered\s+Service\s+Rider\b", re.IGNORECASE
    )),
    ("BPM-P", "BPM PROSPECTIVE", re.compile(
        r"\bBPM\s+Prospective\s+Rider\b", re.IGNORECASE
    )),
    ("BPM-T", "BPM TRUE-UP", re.compile(
        r"\bBPM\s+True[-\s]?Up\s+Rider\b", re.IGNORECASE
    )),
    ("CPRE", "COMPETITIVE PROCUREMENT OF RENEWABLE ENERGY", re.compile(
        r"\bCPRE\s+Rider\b", re.IGNORECASE
    )),
    ("EDIT-4", "EXCESS DEFERRED INCOME TAX", re.compile(
        r"\bEDIT[-\s]?4\s+Rider\b", re.IGNORECASE
    )),
    ("ESM", "EARNINGS SHARING MECHANISM", re.compile(
        r"\bEarnings\s+Sharing\s+Mechanism\b", re.IGNORECASE
    )),
    ("PIM", "PERFORMANCE INCENTIVE MECHANISM", re.compile(
        r"\bPerformance\s+Incentive\s+Mechanism\b", re.IGNORECASE
    )),
)
_DEC_ADMIN_SECTION_RE = re.compile(
    r"^\s*("
    r"FORWARD|"
    r"DISTRIBUTION\s+LINE\s+EXTENSION\s+PLAN|"
    r"SERVICE\s+REGULATIONS|"
    r"OUTDOOR\s+LIGHTING\s+SERVICE\s+REGULATIONS|"
    r"DEFINITIONS\s*$|"
    r"I\.\s+APPLICABILITY\s+This\s+Plan"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DecIndexEntry:
    leaf_no: int
    schedule_code: str
    description: str


@dataclass(frozen=True)
class DecRiderCatalogEntry:
    leaf_no: int
    schedule_code: str
    description: str
    normalized_name: str


@dataclass(frozen=True)
class DecRiderBody:
    """A contiguous page range attributed to a single rider body in DEC."""

    exhibit_key: str
    rate_year_context: str
    rider_code: str
    rider_name: str
    start_page: int
    end_page: int


@dataclass(frozen=True)
class DecSplitCharge:
    charge_type: str
    charge_label: str
    rate_value: float
    rate_unit: str
    raw_line: str
    confidence: float


def has_dec_exhibit_index_header(text: str) -> bool:
    """Return True when the text contains the DEC ``LEAF NO. / DESCRIPTION /
    REVISION NO.`` table header that anchors a proposed exhibit index page."""
    return bool(_INDEX_HEADER_RE.search(text or ""))


def parse_dec_exhibit_index(text: str) -> list[DecIndexEntry]:
    """Parse a DEC exhibit index page into ordered ``(leaf, code, description)``
    rate-schedule entries. The DEC index covers retail classification sections
    A.-C. (residential / general / lighting schedules), then a ``RETAIL RIDERS``
    section, then ``OTHER TARIFFS``. We stop at ``RETAIL RIDERS`` so the rider
    catalog rows (US, FCAR, EDPR, PTC, PC, RAL-2, ...) do not end up
    masquerading as schedules — those are parsed separately by
    ``parse_dec_rider_catalog``."""
    raw_lines = [
        line.strip()
        for line in (text or "").splitlines()
        if line and line.strip()
    ]
    entries: list[DecIndexEntry] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if _DEC_RIDER_CATALOG_HEADER_RE.search(line):
            break
        if line.isdigit() and i + 1 < len(raw_lines):
            combined = f"{line} {raw_lines[i + 1]}"
            match = _INDEX_LINE_RE.match(combined)
            if match:
                desc = _DESC_TAIL_NOISE_RE.sub("", " ".join(match.group("desc").split()))
                entries.append(
                    DecIndexEntry(
                        leaf_no=int(match.group("leaf")),
                        schedule_code=match.group("code").upper(),
                        description=desc,
                    )
                )
                i += 2
                continue
        match = _INDEX_LINE_RE.match(line)
        if match:
            desc = _DESC_TAIL_NOISE_RE.sub("", " ".join(match.group("desc").split()))
            entries.append(
                DecIndexEntry(
                    leaf_no=int(match.group("leaf")),
                    schedule_code=match.group("code").upper(),
                    description=desc,
                )
            )
        i += 1
    return entries


def parse_dec_rider_catalog(text: str) -> list[DecRiderCatalogEntry]:
    """Parse DEC's ``RETAIL RIDERS`` table and return only NEW riders.

    In DEC application Exhibit B indexes, each rider row is two lines: a
    leaf number on one line, then a ``CODE Description Rider ... Applicability
    ... Revision`` row. New riders are marked with ``Orig.`` (i.e. original
    revision) in the revision column; existing riders carry a numeric
    revision. We only emit the ``Orig.`` rows so existing riders are not
    relabeled as proposed.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if _DEC_RIDER_CATALOG_HEADER_RE.search(line):
            start_idx = idx + 1
            break
    if start_idx is None:
        return []

    entries: list[DecRiderCatalogEntry] = []
    i = start_idx
    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue
        if _DEC_RIDER_TERMINATOR_RE.search(line):
            break
        if not line.isdigit():
            i += 1
            continue
        leaf_no = int(line)
        j = i + 1
        while j < len(lines) and not lines[j]:
            j += 1
        if j >= len(lines):
            break
        candidate = _strip_leaders(lines[j])
        match = _DEC_RIDER_LINE_RE.match(candidate)
        i = j + 1
        if not match:
            continue
        if not _orig_revision_marker(candidate):
            continue
        code = match.group("code").upper()
        title = " ".join(match.group("title").upper().split())
        entries.append(
            DecRiderCatalogEntry(
                leaf_no=leaf_no,
                schedule_code=code,
                description=title.title(),
                normalized_name=f"RIDER {code} {title}",
            )
        )
    return entries


def _strip_leaders(line: str) -> str:
    """Replace OCR'd leader-dot artifacts (Unicode � … .) with single spaces.

    DEC index rows render leaders as a long run of ``.`` or replacement
    characters; we normalize them to single spaces so the line parser sees
    a uniform token stream.
    """
    return re.sub(r"[.�…]{2,}", " ", line)


def _orig_revision_marker(line: str) -> bool:
    """Return True when the line's revision column is ``Orig.`` (any spacing)."""
    return bool(re.search(r"\bOrig\.?\s*$", line, re.IGNORECASE))


def extract_dec_split_line_charges(text: str) -> list[DecSplitCharge]:
    """Pair label lines with value-only rate lines to emit candidate charges.

    A line that is "just a value" (``$16.00``, ``8.4138¢``, ``(0.0030¢)``) is
    paired with the most recent non-empty preceding line. The pair is only
    emitted as a charge when the label line contains one of a small set of
    rate-bearing keywords (``per kWh``, ``per month``, ``Basic Customer
    Charge``, ``demand``, ``on-peak``...), which suppresses noise from leaf
    numbers, page numbers, and threshold-only label rows.
    """
    lines = [line.rstrip() for line in (text or "").splitlines()]
    charges: list[DecSplitCharge] = []
    label_buffer: list[str] = []
    seen: set[tuple[str, str, float]] = set()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        dollar = _VALUE_ONLY_RE.match(stripped)
        cents = _CENTS_VALUE_RE.match(stripped) if not dollar else None

        if dollar or cents:
            label = _select_label(label_buffer)
            if label is None:
                label_buffer.clear()
                continue
            if dollar:
                value = _to_float(dollar.group("dollar"))
                if value is None:
                    label_buffer.clear()
                    continue
                if dollar.group("sign"):
                    value = -value
                unit = _infer_dollar_unit(label)
                charge_type = _infer_charge_type(label)
            else:
                cents_val = _to_float(cents.group("cents"))
                if cents_val is None:
                    label_buffer.clear()
                    continue
                value = round(cents_val / 100.0, 8)
                if cents.group("sign"):
                    value = -value
                unit = "$/kWh"
                charge_type = _infer_charge_type(label)

            key = (charge_type, label[:120], round(value, 8))
            if key in seen:
                label_buffer.clear()
                continue
            seen.add(key)
            charges.append(
                DecSplitCharge(
                    charge_type=charge_type,
                    charge_label=label[:120],
                    rate_value=value,
                    rate_unit=unit,
                    raw_line=f"{label.strip()} | {stripped}",
                    confidence=0.7,
                )
            )
            label_buffer.clear()
            continue

        label_buffer.append(stripped)

    return charges


def _select_label(buffer: Iterable[str]) -> str | None:
    """Return the most recent label-bearing line, or None if no candidate.

    The buffer holds non-value, non-empty lines that preceded a value line.
    We walk backwards looking for a line that carries a rate-bearing keyword
    (``per kWh``, ``per month``, ``Basic Customer Charge``, ``demand``, etc.).
    Lines without such cues — including pure-threshold tokens like ``4,500``
    or ``A.`` — are skipped, because a value pinned to a numeric-only label
    is almost always a column from a wattage/luminaire pricing table rather
    than a real rate row.
    """
    for candidate in reversed(list(buffer)):
        if _THRESHOLD_TOKEN_RE.match(candidate):
            continue
        if _RATE_LABEL_KEYWORDS_RE.search(candidate):
            return candidate
    return None


def _infer_dollar_unit(label: str) -> str:
    inline = _MONEY_PER_UNIT_INLINE_RE.search(label)
    if inline:
        return f"$/{inline.group(1).lower()}"
    low = label.lower()
    if "basic customer charge" in low:
        return "$/month"
    if "demand" in low:
        return "$/kW"
    if "fixture" in low:
        return "$/fixture"
    return "$"


def _infer_charge_type(label: str) -> str:
    low = label.lower()
    if "basic customer charge" in low or "facilities charge" in low:
        return "fixed"
    if "demand" in low and "kwh" not in low:
        return "demand"
    if "on-peak" in low or "off-peak" in low or "discount" in low:
        return "tou_energy"
    if "rider" in low:
        return "adjustment"
    if "kwh" in low or "energy" in low:
        return "energy"
    return "energy"


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def is_dec_filing(text: str) -> bool:
    """Return True when the text looks like a Duke Energy Carolinas filing."""
    return bool(_DEC_UTILITY_RE.search(text or ""))


@dataclass(frozen=True)
class DecExhibitSection:
    """A contiguous range of body pages attributed to one DEC schedule."""

    exhibit_key: str
    rate_year_context: str
    schedule_code: str
    description: str
    leaf_no: int
    start_page: int
    end_page: int


@dataclass
class DecExhibitContext:
    """Per-exhibit scratch state used while walking DEC body pages."""

    exhibit_key: str
    rate_year_context: str
    index_page: int
    entries: list[DecIndexEntry] = field(default_factory=list)
    body_starts: list[int] = field(default_factory=list)
    end_page: int = 0


def _detect_exhibit_context(text: str) -> tuple[str | None, str | None]:
    """Return ``(exhibit_key, rate_year_context)`` from page footer/headers."""
    footer = _EXHIBIT_FOOTER_RE.search(text or "")
    year = _RATE_YEAR_TITLE_RE.search(text or "")
    if footer:
        key = footer.group("key").upper()
        if year:
            context = (
                f"Rate Year {year.group('year')} North Carolina Tariffs Proposed for Change"
            )
        elif key == "B":
            context = "Proposed Exhibit B"
        elif key == "B_1":
            context = "Rate Year 1"
        elif key == "B_2":
            context = "Rate Year 2"
        else:
            context = "Proposed Exhibit B"
        return key, context
    if year:
        digit = year.group("year")
        key = "B" if digit == "0" else f"B_{digit}"
        return key, (
            f"Rate Year {digit} North Carolina Tariffs Proposed for Change"
        )
    return None, None


def detect_dec_exhibit_sections(
    pdf_path: Path | str,
) -> list[DecExhibitSection]:
    """Scan a DEC application PDF and return per-schedule page ranges.

    The strategy:

    1. Walk pages in order, tracking the active Exhibit B/B_1/B_2 from the page
       footer.
    2. When a page contains the ``LEAF NO. / DESCRIPTION / REVISION NO.`` index
       header, parse it into ordered ``DecIndexEntry`` rows for that exhibit.
    3. Subsequent body pages that begin with ``AVAILABILITY`` are treated as
       schedule starts and matched one-by-one to the index entries.
    """
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is required for DEC scans") from exc

    doc = fitz.open(Path(pdf_path))
    try:
        page_count = doc.page_count
        exhibits: dict[str, DecExhibitContext] = {}
        order: list[str] = []
        page_texts: dict[int, str] = {}
        page_exhibit: dict[int, str] = {}
        for page_number in range(1, page_count + 1):
            text = doc.load_page(page_number - 1).get_text("text") or ""
            page_texts[page_number] = text
            key, year_context = _detect_exhibit_context(text)
            if key is None:
                continue
            page_exhibit[page_number] = key
            ctx = exhibits.get(key)
            if ctx is None:
                ctx = DecExhibitContext(
                    exhibit_key=key,
                    rate_year_context=year_context or "Proposed Exhibit B",
                    index_page=page_number,
                )
                exhibits[key] = ctx
                order.append(key)
            ctx.end_page = page_number
            if not ctx.entries and has_dec_exhibit_index_header(text):
                ctx.entries = parse_dec_exhibit_index(text)
                ctx.index_page = page_number
                continue
            if ctx.entries:
                first_line = next(
                    (ln for ln in text.splitlines() if ln.strip()),
                    "",
                )
                if _AVAILABILITY_START_RE.match(first_line):
                    ctx.body_starts.append(page_number)
    finally:
        doc.close()

    sections: list[DecExhibitSection] = []
    for key in order:
        ctx = exhibits[key]
        if not ctx.entries or not ctx.body_starts:
            continue
        starts = ctx.body_starts
        for idx, entry in enumerate(ctx.entries):
            if idx >= len(starts):
                break
            start_page = starts[idx]
            if idx + 1 < len(starts):
                end_page = starts[idx + 1] - 1
            else:
                end_page = ctx.end_page
            sections.append(
                DecExhibitSection(
                    exhibit_key=ctx.exhibit_key,
                    rate_year_context=ctx.rate_year_context,
                    schedule_code=entry.schedule_code,
                    description=entry.description,
                    leaf_no=entry.leaf_no,
                    start_page=start_page,
                    end_page=end_page,
                )
            )
    return sections


def classify_dec_page_role(text: str) -> tuple[str, str | None]:
    """Classify a DEC body page into one of ``schedule`` / ``rider`` /
    ``admin`` / ``unknown``. For rider pages, also return the rider code if
    a clear ``RIDER X`` or ``... Rider (X)`` heading is present near the top.

    The role is decided from the first handful of non-empty lines so that
    headers (RIDER PC, Regulatory Asset and Liability Rider (RAL-2)) win over
    schedule cross-references that may appear later in the page text.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    head = lines[:6]
    if not head:
        return "unknown", None
    if _AVAILABILITY_START_RE.match(head[0]):
        return "schedule", None
    head_text = " ".join(head)
    if _DEC_ADMIN_SECTION_RE.search(head_text):
        return "admin", None
    if re.search(r"This\s+Plan\s+is\s+applicable", head_text, re.IGNORECASE):
        return "admin", None
    for line in head:
        match = _DEC_RIDER_BODY_HEADER_RE.match(line)
        if match:
            return "rider", match.group("code").upper()
        match = _DEC_RIDER_BODY_PARENS_RE.match(line)
        if match:
            return "rider", match.group("code").upper()
    # Content-based fallback for pages that carry rider rate text but no
    # explicit header (FCAR, EDPR, PTC bodies often look like an
    # "APPLICABILITY" page that names the rider only in the prose).
    leading = "\n".join(lines[:25])
    for code, _name, pattern in _DEC_RIDER_CONTENT_PATTERNS:
        if pattern.search(leading):
            return "rider", code
    return "unknown", None


def detect_dec_rider_bodies(
    pdf_path: Path | str,
    exhibit_ranges: dict[str, tuple[int, int]],
    schedule_end_by_exhibit: dict[str, int],
) -> list[DecRiderBody]:
    """Find rider body page ranges within each DEC exhibit.

    Scanning starts one page after the last schedule body ended (so the page
    where the next AVAILABILITY would have appeared is checked first) and
    walks forward through that exhibit. ``rider`` and ``admin`` page-role
    classifications terminate the current run; pages between a rider head
    and the next role transition belong to that rider.
    """
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is required for DEC scans") from exc

    doc = fitz.open(Path(pdf_path))
    try:
        bodies: list[DecRiderBody] = []
        for exhibit_key, (start, end) in exhibit_ranges.items():
            scan_start = schedule_end_by_exhibit.get(exhibit_key, start)
            current_code: str | None = None
            current_start: int | None = None
            current_name: str | None = None
            rate_year_context: str | None = None
            for page in range(scan_start, end + 1):
                text = doc.load_page(page - 1).get_text("text") or ""
                if rate_year_context is None:
                    _, rate_year_context = _detect_exhibit_context(text)
                role, code = classify_dec_page_role(text)
                if role == "rider" and code:
                    if current_code and current_start:
                        bodies.append(
                            DecRiderBody(
                                exhibit_key=exhibit_key,
                                rate_year_context=rate_year_context or "Proposed Exhibit B",
                                rider_code=current_code,
                                rider_name=current_name or f"RIDER {current_code}",
                                start_page=current_start,
                                end_page=page - 1,
                            )
                        )
                    current_code = code
                    current_start = page
                    current_name = _rider_name_from_text(text, code)
                    continue
                if role in {"admin", "schedule"} and current_code and current_start:
                    bodies.append(
                        DecRiderBody(
                            exhibit_key=exhibit_key,
                            rate_year_context=rate_year_context or "Proposed Exhibit B",
                            rider_code=current_code,
                            rider_name=current_name or f"RIDER {current_code}",
                            start_page=current_start,
                            end_page=page - 1,
                        )
                    )
                    current_code = None
                    current_start = None
                    current_name = None
                    if role == "admin":
                        break
            if current_code and current_start:
                bodies.append(
                    DecRiderBody(
                        exhibit_key=exhibit_key,
                        rate_year_context=rate_year_context or "Proposed Exhibit B",
                        rider_code=current_code,
                        rider_name=current_name or f"RIDER {current_code}",
                        start_page=current_start,
                        end_page=end,
                    )
                )
    finally:
        doc.close()
    return bodies


def _rider_name_from_text(text: str, code: str) -> str:
    """Derive a normalized ``RIDER <CODE> <TITLE>`` name from rider body text."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for line in lines[:6]:
        parens = _DEC_RIDER_BODY_PARENS_RE.match(line)
        if parens and parens.group("code").upper() == code:
            title = " ".join(parens.group("title").upper().split())
            return f"RIDER {code} {title}"
    upper = code.upper()
    for idx, line in enumerate(lines[:6]):
        if line.upper() == f"RIDER {upper}" and idx + 1 < len(lines):
            title_line = lines[idx + 1].upper()
            title = re.sub(r"\s+RIDER\s*$", "", title_line)
            title = " ".join(title.split())
            if title:
                return f"RIDER {code} {title}"
    for cat_code, name, _pattern in _DEC_RIDER_CONTENT_PATTERNS:
        if cat_code == code:
            return f"RIDER {code} {name}"
    return f"RIDER {code}"


def detect_dec_proposed_blocks_from_pdf(
    pdf_path: Path | str,
) -> tuple[list[ProposedTariffBlock], dict[int, str]]:
    """Return DEC proposed-tariff blocks and the per-page text cache.

    Each schedule section in the DEC exhibit becomes one ProposedTariffBlock
    per page it spans, so the existing charge persistence pipeline (which
    keys on one block per page) can re-use these. Returned page text is the
    raw PyMuPDF text for every page covered by any block — callers can then
    invoke ``extract_dec_split_line_charges`` directly without re-opening the
    PDF.
    """
    sections = detect_dec_exhibit_sections(pdf_path)
    blocks: list[ProposedTariffBlock] = []
    if not sections:
        return blocks, {}

    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is required for DEC scans") from exc

    pdf = Path(pdf_path)
    doc = fitz.open(pdf)
    try:
        # Compute exhibit ranges from the (possibly over-attributed) sections.
        exhibit_ranges: dict[str, tuple[int, int]] = {}
        for s in sections:
            if s.exhibit_key not in exhibit_ranges:
                exhibit_ranges[s.exhibit_key] = (s.start_page, s.end_page)
            else:
                lo, hi = exhibit_ranges[s.exhibit_key]
                exhibit_ranges[s.exhibit_key] = (
                    min(lo, s.start_page),
                    max(hi, s.end_page),
                )

        # Find each exhibit's last schedule section and walk forward to find
        # the real end of schedule content (the page before the first rider
        # body or admin section begins). Then rewrite that section's
        # end_page in place so blocks and downstream attribution agree.
        last_schedule_by_exhibit: dict[str, int] = {}
        for exhibit_key, (lo, hi) in exhibit_ranges.items():
            last = max(
                (s for s in sections if s.exhibit_key == exhibit_key),
                key=lambda s: s.start_page,
            )
            true_end = last.end_page
            for page in range(last.start_page + 1, hi + 1):
                text = doc.load_page(page - 1).get_text("text") or ""
                role, _ = classify_dec_page_role(text)
                if role in {"rider", "admin"}:
                    true_end = page - 1
                    break
            if true_end != last.end_page:
                idx = sections.index(last)
                sections[idx] = DecExhibitSection(
                    exhibit_key=last.exhibit_key,
                    rate_year_context=last.rate_year_context,
                    schedule_code=last.schedule_code,
                    description=last.description,
                    leaf_no=last.leaf_no,
                    start_page=last.start_page,
                    end_page=true_end,
                )
            last_schedule_by_exhibit[exhibit_key] = true_end

        rider_bodies = detect_dec_rider_bodies(
            pdf, exhibit_ranges, last_schedule_by_exhibit
        )

        page_texts: dict[int, str] = {}
        rider_catalog_seen: set[tuple[str, str, int]] = set()
        index_pages: dict[str, int] = {}
        for section in sections:
            for page in range(section.start_page, section.end_page + 1):
                if page not in page_texts:
                    page_texts[page] = doc.load_page(page - 1).get_text("text") or ""
        for body in rider_bodies:
            for page in range(body.start_page, body.end_page + 1):
                if page not in page_texts:
                    page_texts[page] = doc.load_page(page - 1).get_text("text") or ""
        # Also load index pages so we can scan their RETAIL RIDERS sections.
        seen_keys: set[str] = set()
        for section in sections:
            if section.exhibit_key in seen_keys:
                continue
            seen_keys.add(section.exhibit_key)
            idx_page = section.start_page - 1
            if idx_page >= 1 and idx_page not in page_texts:
                page_texts[idx_page] = doc.load_page(idx_page - 1).get_text("text") or ""
            index_pages[section.exhibit_key] = idx_page
        for section in sections:
            tariff_name = f"SCHEDULE {section.schedule_code}"
            schedule_label = (
                f"{section.description.upper()} SCHEDULE {section.schedule_code}"
                if section.description
                else tariff_name
            )
            for page in range(section.start_page, section.end_page + 1):
                blocks.append(
                    ProposedTariffBlock(
                        source_pdf=str(pdf),
                        section_id=None,
                        section_index=page,
                        start_page=page,
                        end_page=page,
                        section_type="rate_schedule",
                        exhibit_key=section.exhibit_key,
                        rate_year_context=section.rate_year_context,
                        schedule_name=schedule_label,
                        basic_customer_charge=None,
                        volumetric_energy_charge_lines=[],
                        time_of_use_lines=[],
                        has_interclass_impact_table=False,
                        confidence=0.78,
                        evidence=[
                            section.rate_year_context,
                            f"Leaf {section.leaf_no} {section.schedule_code}",
                            schedule_label,
                        ],
                    )
                )
        for body in rider_bodies:
            for page in range(body.start_page, body.end_page + 1):
                blocks.append(
                    ProposedTariffBlock(
                        source_pdf=str(pdf),
                        section_id=None,
                        section_index=page,
                        start_page=page,
                        end_page=page,
                        section_type="rider",
                        exhibit_key=body.exhibit_key,
                        rate_year_context=body.rate_year_context,
                        schedule_name=body.rider_name,
                        basic_customer_charge=None,
                        volumetric_energy_charge_lines=[],
                        time_of_use_lines=[],
                        has_interclass_impact_table=False,
                        confidence=0.75,
                        evidence=[
                            body.rate_year_context,
                            body.rider_name,
                            f"Pages {body.start_page}-{body.end_page}",
                        ],
                    )
                )

        for exhibit_key, idx_page in index_pages.items():
            if idx_page < 1:
                continue
            idx_text = page_texts.get(idx_page, "")
            rate_year_context = next(
                (s.rate_year_context for s in sections if s.exhibit_key == exhibit_key),
                "Proposed Exhibit B",
            )
            for entry in parse_dec_rider_catalog(idx_text):
                key = (exhibit_key, entry.normalized_name, entry.leaf_no)
                if key in rider_catalog_seen:
                    continue
                rider_catalog_seen.add(key)
                blocks.append(
                    ProposedTariffBlock(
                        source_pdf=str(pdf),
                        section_id=None,
                        section_index=idx_page,
                        start_page=idx_page,
                        end_page=idx_page,
                        section_type="rider_catalog",
                        exhibit_key=exhibit_key,
                        rate_year_context=rate_year_context,
                        schedule_name=entry.normalized_name,
                        basic_customer_charge=None,
                        volumetric_energy_charge_lines=[],
                        time_of_use_lines=[],
                        has_interclass_impact_table=False,
                        confidence=0.7,
                        evidence=[
                            rate_year_context,
                            f"Leaf {entry.leaf_no} Orig.",
                            entry.normalized_name,
                        ],
                    )
                )
    finally:
        doc.close()
    return blocks, page_texts
