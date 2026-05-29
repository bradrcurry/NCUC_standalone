"""Detect forward-looking proposed tariff sections in NCUC filings.

This module is intentionally read-only. It identifies candidate proposed
rate/rider/tariff blocks from page-bounded ``document_sections`` and
``ncuc_page_artifacts`` text, but it does not write to approved tariff lineage
tables. Promotion to tariff_versions should remain an explicit later step.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from duke_rates.document_intelligence.section_text_extractor import (
    fetch_section_text,
)


TARGET_EXHIBIT_KEYS = {"B", "B_1", "B_2"}

_WHITESPACE_RE = re.compile(r"\s+")
_EXHIBIT_RE = re.compile(
    r"\b(?:APPLICATION\s+)?EXHIBIT\s+([A-Z])(?:\s*[_-]\s*(\d+))?\b",
    re.IGNORECASE,
)
_MYRP_RATE_YEAR_RE = re.compile(r"\bMYRP\s+RATE\s+YEAR\s+([12])\b", re.IGNORECASE)
_RATE_YEAR_RE = re.compile(r"\bRATE\s+YEAR\s+([12])\b", re.IGNORECASE)
_NC_TARIFFS_PROPOSED_RE = re.compile(
    r"\bRATE\s+YEAR\s+([12])\s+NORTH\s+CAROLINA\s+TARIFFS\s+PROPOSED\s+FOR\s+CHANGE\b",
    re.IGNORECASE,
)
_CURRENT_BASELINE_RE = re.compile(
    r"\b(?:CURRENT\s+NORTH\s+CAROLINA\s+SCHEDULES|CURRENT\s+SCHEDULES|"
    r"EXHIBIT\s+A\b|APPLICATION\s+EXHIBIT\s+A\b)\b",
    re.IGNORECASE,
)
_TARGET_DOCUMENT_RE = re.compile(
    r"\b(?:APPLICATION\s+TO\s+ADJUST\s+RETAIL\s+BASE\s+RATES|"
    r"PBR\s+APPLICATION|PERFORMANCE[-\s]+BASED\s+REGULATION|"
    r"APPLICATION\s+AND\s+REQUEST\s+FOR\s+AN\s+ACCOUNTING\s+ORDER|"
    r"MYRP|MULTI[-\s]+YEAR\s+RATE\s+PLAN)\b",
    re.IGNORECASE,
)
_SCHEDULE_HEADER_RE = re.compile(
    r"\b("
    r"RESIDENTIAL\s+SERVICE\s+SCHEDULE\s+[A-Z0-9][A-Z0-9-]*|"
    r"SMALL\s+GENERAL\s+SERVICE\s+SCHEDULE\s+[A-Z0-9][A-Z0-9-]*|"
    r"MEDIUM\s+GENERAL\s+SERVICE\s+SCHEDULE\s+[A-Z0-9][A-Z0-9-]*|"
    r"LARGE\s+GENERAL\s+SERVICE(?:\s+\(REAL\s+TIME\s+PRICING\))?\s+"
    r"SCHEDULE\s+[A-Z0-9][A-Z0-9-]*|"
    r"HOURLY\s+PRICING\s+SCHEDULE\s+HP|"
    r"SCHEDULE\s+[A-Z0-9][A-Z0-9-]*)\b",
    re.IGNORECASE,
)
_RIDER_HEADER_RE = re.compile(
    r"^\s*(RIDER\s+[A-Z0-9][A-Z0-9-]*(?:\s+[A-Z0-9][A-Z0-9&/() -]{0,80})?)\b",
    re.IGNORECASE,
)
_DEP_RIDER_BODY_RIDER_FIRST_RE = re.compile(
    r"^\s*RIDER\s+(?P<code>[A-Z][A-Z0-9-]{0,7})\s*$"
)
_DEP_RIDER_BODY_TITLE_THEN_RIDER_CODE_RE = re.compile(
    r"^\s*(?P<title>[A-Z][A-Z0-9\s&'/()-]{2,80}?)\s+RIDER\s+(?P<code>[A-Z][A-Z0-9-]{0,7})\s*$"
)
_DEP_RIDER_BODY_TITLE_THEN_RIDER_RE = re.compile(
    r"^\s*(?P<title>[A-Z][A-Z0-9\s&'/()-]{2,80}?)\s+RIDER\s*$"
)
_DEP_RIDER_BODY_KNOWN_CODES = {
    "BPM PROSPECTIVE": "BPM-P",
    "BPM TRUE-UP": "BPM-T",
}
_BASIC_CHARGE_RE = re.compile(
    r"\bBASIC\s+CUSTOMER\s+CHARGE\s*:?\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    r"(?:\s+PER\s+MONTH)?",
    re.IGNORECASE,
)
_RATE_LINE_RE = re.compile(
    r"(?im)^.{0,80}(?:KILOWATT[-\s]?HOUR|KWH|CENTS?\s+PER\s+KWH|"
    r"\bPER\s+KWH\b|[¢C]\s*/\s*KWH).{0,120}$"
)
_TOU_LINE_RE = re.compile(
    r"(?im)^.{0,60}(?:ON[-\s]?PEAK|OFF[-\s]?PEAK|DISCOUNT|SUPER[-\s]?OFF[-\s]?PEAK)"
    r".{0,140}$"
)
_INTERCLASS_RE = re.compile(
    r"PRESENT\s+BASE\s+RATE\s+REVENUES.{0,300}TOTAL\s+INCREASE",
    re.IGNORECASE | re.DOTALL,
)
_RIDER_CATALOG_HEADER_RE = re.compile(r"\bRETAIL\s+RIDERS\b", re.IGNORECASE)
_RIDER_CATALOG_LEAF_RE = re.compile(
    r"^(?P<star>\*)?\s*Leaf\s+(?P<leaf>\d+)\b",
    re.IGNORECASE,
)
_RIDER_CATALOG_TITLE_RE = re.compile(
    r"^\s*(?P<title>[A-Za-z0-9][A-Za-z0-9\s/&\-]*?)\s+Rider\s+"
    r"(?P<code>[A-Z][A-Z0-9-]*)\s*$",
)
_GENERIC_SCHEDULE_STOPWORDS = {
    "AND",
    "AFTER",
    "ALL",
    "APPLIES",
    "ARE",
    "AS",
    "AT",
    "DELAY",
    "DOES",
    "DRIVERS",
    "FIRST",
    "FOR",
    "IN",
    "IS",
    "MAY",
    "NOR",
    "OF",
    "ON",
    "OR",
    "PRIOR",
    "SHALL",
    "SITE",
    "THE",
    "TO",
    "UNLESS",
    "WAS",
    "WILL",
    "WITH",
}


@dataclass(frozen=True)
class ExhibitContext:
    """Forward-looking exhibit context inferred from anchors."""

    exhibit_key: str
    rate_year_context: str
    evidence: str


@dataclass(frozen=True)
class ProposedTariffBlock:
    """One candidate proposed rate/rider/tariff segment."""

    source_pdf: str
    section_id: int | None
    section_index: int
    start_page: int
    end_page: int
    section_type: str
    exhibit_key: str
    rate_year_context: str
    schedule_name: str
    basic_customer_charge: str | None
    volumetric_energy_charge_lines: list[str]
    time_of_use_lines: list[str]
    has_interclass_impact_table: bool
    confidence: float
    evidence: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_text(text: str) -> str:
    """Collapse OCR whitespace while preserving enough text for anchors."""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def detect_exhibit_context(text: str) -> ExhibitContext | None:
    """Return a target proposed exhibit context, or ``None``.

    Exhibit A/current-baseline anchors deliberately win over generic Exhibit B
    wording because applications often index current schedules before proposed
    alternatives.
    """
    norm = normalize_text(text)
    if not norm:
        return None
    if _CURRENT_BASELINE_RE.search(norm):
        return None

    proposed_tariffs_match = _NC_TARIFFS_PROPOSED_RE.search(norm)
    if proposed_tariffs_match:
        year = proposed_tariffs_match.group(1)
        return ExhibitContext(
            exhibit_key=f"B_{year}",
            rate_year_context=f"Rate Year {year} North Carolina Tariffs Proposed for Change",
            evidence=proposed_tariffs_match.group(0),
        )

    myrp_match = _MYRP_RATE_YEAR_RE.search(norm)
    if myrp_match:
        year = myrp_match.group(1)
        return ExhibitContext(
            exhibit_key=f"B_{year}",
            rate_year_context=f"MYRP Rate Year {year}",
            evidence=myrp_match.group(0),
        )

    for match in _EXHIBIT_RE.finditer(norm):
        letter = match.group(1).upper()
        suffix = match.group(2)
        key = f"{letter}_{suffix}" if suffix else letter
        if key not in TARGET_EXHIBIT_KEYS:
            continue
        if key == "B_1":
            context = "Rate Year 1"
        elif key == "B_2":
            context = "Rate Year 2"
        else:
            context = "Flat Rate Alternative" if "flat rate alternative" in norm.lower() else "Proposed Exhibit B"
        return ExhibitContext(
            exhibit_key=key,
            rate_year_context=context,
            evidence=match.group(0),
        )

    rate_year_match = _RATE_YEAR_RE.search(norm)
    if rate_year_match and (
        "MYRP" in norm.upper() or "MULTI-YEAR RATE PLAN" in norm.upper()
    ):
        year = rate_year_match.group(1)
        return ExhibitContext(
            exhibit_key=f"B_{year}",
            rate_year_context=f"Rate Year {year}",
            evidence=rate_year_match.group(0),
        )

    return None


def is_current_baseline(text: str) -> bool:
    """Return True when text is explicitly about current baseline schedules."""
    return bool(_CURRENT_BASELINE_RE.search(normalize_text(text)))


def find_dep_rider_body_name(text: str) -> str | None:
    """Return a normalized ``RIDER <CODE> <TITLE>`` name when a DEP rider body
    page is recognized by its header lines.

    DEP rider body pages carry a clear three-part header — page provenance
    (``NC Original Leaf No. ...``), the rider title, and ``RIDER <CODE>``.
    Layouts seen in E-2 Sub 1380:

    * ``PENSIONS COSTS`` / ``RIDER PC`` (title then code)
    * ``RIDER PTC`` / ``PRODUCTION TAX CREDITS`` (code then title)
    * ``BPM PROSPECTIVE RIDER`` (title-only, code embedded)
    * ``SUPPLEMENTARY AND FIRM STANDBY SERVICE RIDER SS`` (title plus
      trailing code on the same line)

    We scan the first ~12 lines so the page-provenance lines do not pollute
    the match, and prefer this over generic ``Schedule <X>`` cross-references
    that appear later in the page body.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:14]
    if not lines:
        return None

    for idx, line in enumerate(lines):
        match = _DEP_RIDER_BODY_TITLE_THEN_RIDER_CODE_RE.match(line)
        if match:
            title = " ".join(match.group("title").upper().split())
            code = match.group("code").upper()
            return f"RIDER {code} {title}"

    for idx, line in enumerate(lines):
        match = _DEP_RIDER_BODY_TITLE_THEN_RIDER_RE.match(line)
        if match:
            title = " ".join(match.group("title").upper().split())
            code = _DEP_RIDER_BODY_KNOWN_CODES.get(title)
            if code is None:
                code = title.split()[0]
            return f"RIDER {code} {title}"

    for idx, line in enumerate(lines):
        match = _DEP_RIDER_BODY_RIDER_FIRST_RE.match(line)
        if not match:
            continue
        code = match.group("code").upper()
        title: str | None = None
        for offset in (-1, 1):
            neighbor_idx = idx + offset
            if 0 <= neighbor_idx < len(lines):
                candidate = lines[neighbor_idx]
                if (
                    candidate.isupper()
                    and 3 <= len(candidate) <= 80
                    and not candidate.startswith(("RIDER ", "SCHEDULE ", "NC ", "NCUC", "DUKE", "APPLICATION"))
                    and "LEAF" not in candidate
                    and "EFFECTIVE" not in candidate
                ):
                    title = " ".join(candidate.split())
                    break
        if title:
            return f"RIDER {code} {title}"
        return f"RIDER {code}"

    return None


def find_schedule_name(text: str) -> str | None:
    """Find the first schedule/rider header-like name in section text."""
    rider_body = find_dep_rider_body_name(text)
    if rider_body is not None:
        return rider_body
    for match in _SCHEDULE_HEADER_RE.finditer(normalize_text(text)):
        name = " ".join(match.group(1).upper().split())
        if name.startswith("SCHEDULE "):
            code = name.removeprefix("SCHEDULE ").strip()
            if code in _GENERIC_SCHEDULE_STOPWORDS:
                continue
            if len(code) > 5 and not any(ch.isdigit() or ch == "-" for ch in code):
                continue
        return name
    for line in (text or "").splitlines()[:40]:
        match = _RIDER_HEADER_RE.search(line)
        if not match:
            continue
        name = " ".join(match.group(1).upper().split())
        parts = name.split()
        if len(parts) < 2:
            continue
        code = parts[1].strip()
        if code in _GENERIC_SCHEDULE_STOPWORDS:
            continue
        if len(code) > 8 and not any(ch.isdigit() or ch == "-" for ch in code):
            continue
        return name[:120]
    return None


def is_target_application_text(text: str) -> bool:
    """Return True when filing text looks like a rate-case/PBR application."""
    return bool(_TARGET_DOCUMENT_RE.search(normalize_text(text)))


def extract_rate_fields(text: str) -> dict[str, Any]:
    """Extract lightweight fields from a proposed section.

    This is deliberately conservative: ambiguous rate rows are returned as
    evidence lines rather than normalized charge rows.
    """
    norm_lines = [
        _WHITESPACE_RE.sub(" ", line).strip()
        for line in (text or "").splitlines()
        if line and line.strip()
    ]
    line_text = "\n".join(norm_lines)

    basic_match = _BASIC_CHARGE_RE.search(line_text)
    return {
        "basic_customer_charge": basic_match.group(1) if basic_match else None,
        "volumetric_energy_charge_lines": _dedupe_preserve_order(
            m.group(0).strip() for m in _RATE_LINE_RE.finditer(line_text)
        )[:12],
        "time_of_use_lines": _dedupe_preserve_order(
            m.group(0).strip() for m in _TOU_LINE_RE.finditer(line_text)
        )[:12],
        "has_interclass_impact_table": bool(_INTERCLASS_RE.search(line_text)),
    }


def extract_rider_catalog_entries(text: str) -> list[tuple[str, int]]:
    """Return (normalized_name, leaf_number) for starred new-rider catalog entries.

    NCUC rate-case applications often include an Index of Tariffs that lists
    riders under a ``RETAIL RIDERS`` heading. New riders proposed by the
    application are marked with a leading asterisk on the ``Leaf NNN`` line
    (commonly captioned ``*New Riders`` at the bottom of the index). We capture
    only those starred entries so existing/unchanged riders are not re-emitted
    as proposed.

    Each catalog entry looks like::

        *Leaf 614
        Pensions Costs Rider PC

    and is normalized to ``RIDER PC PENSIONS COSTS``.
    """
    lines = (text or "").splitlines()
    header_idx: int | None = None
    for idx, line in enumerate(lines):
        if _RIDER_CATALOG_HEADER_RE.search(line):
            header_idx = idx
            break
    if header_idx is None:
        return []

    entries: list[tuple[str, int]] = []
    i = header_idx + 1
    while i < len(lines):
        leaf_line = lines[i].strip()
        leaf_match = _RIDER_CATALOG_LEAF_RE.match(leaf_line)
        if not leaf_match:
            i += 1
            continue
        starred = bool(leaf_match.group("star"))
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            break
        title_match = _RIDER_CATALOG_TITLE_RE.match(lines[j].strip())
        i = j + 1
        if not (starred and title_match):
            continue
        title = " ".join(title_match.group("title").upper().split())
        code = title_match.group("code").upper()
        entries.append((f"RIDER {code} {title}", int(leaf_match.group("leaf"))))
    return entries


def detect_blocks_from_sections(
    sections: Iterable[dict[str, Any]],
    text_loader: Callable[[dict[str, Any]], str],
    *,
    require_target_document: bool = True,
) -> list[ProposedTariffBlock]:
    """Detect proposed tariff blocks from ordered section rows.

    ``sections`` should be ordered by ``source_pdf`` then ``section_index``.
    The exhibit context is carried forward within a source PDF until a new
    exhibit anchor or current-baseline marker is encountered.
    """
    blocks: list[ProposedTariffBlock] = []
    active_context_by_pdf: dict[str, ExhibitContext | None] = {}
    doc_target_by_pdf: dict[str, bool] = {}
    last_rider_by_pdf: dict[str, str | None] = {}

    for section in sections:
        source_pdf = str(section.get("source_pdf") or "")
        if not source_pdf:
            continue
        text = text_loader(section)
        norm = normalize_text(text)
        if not norm:
            continue

        doc_target_by_pdf[source_pdf] = doc_target_by_pdf.get(
            source_pdf, False
        ) or is_target_application_text(norm)

        context = detect_exhibit_context(norm)
        if context is not None:
            previous = active_context_by_pdf.get(source_pdf)
            active_context_by_pdf[source_pdf] = context
            if previous is None or previous.exhibit_key != context.exhibit_key:
                last_rider_by_pdf[source_pdf] = None
        elif is_current_baseline(norm) or _has_non_target_exhibit(norm):
            active_context_by_pdf[source_pdf] = None
            last_rider_by_pdf[source_pdf] = None

        active_context = active_context_by_pdf.get(source_pdf)
        if active_context is None:
            continue
        if require_target_document and not doc_target_by_pdf.get(source_pdf, False):
            continue

        for catalog_name, leaf_number in extract_rider_catalog_entries(text):
            blocks.append(
                ProposedTariffBlock(
                    source_pdf=source_pdf,
                    section_id=_optional_int(section.get("id")),
                    section_index=int(section.get("section_index") or 0),
                    start_page=int(section.get("start_page") or 0),
                    end_page=int(section.get("end_page") or 0),
                    section_type="rider_catalog",
                    exhibit_key=active_context.exhibit_key,
                    rate_year_context=active_context.rate_year_context,
                    schedule_name=catalog_name,
                    basic_customer_charge=None,
                    volumetric_energy_charge_lines=[],
                    time_of_use_lines=[],
                    has_interclass_impact_table=False,
                    confidence=0.7,
                    evidence=[
                        active_context.evidence,
                        f"*Leaf {leaf_number}",
                        catalog_name,
                    ],
                )
            )

        rider_body_name = find_dep_rider_body_name(text)
        schedule_name = find_schedule_name(norm)
        section_type = str(section.get("section_type") or "unknown")
        page_starts_new_schedule = _page_starts_new_schedule(text)
        page_is_admin = _page_is_admin_section(text)
        if rider_body_name is not None:
            last_rider_by_pdf[source_pdf] = rider_body_name
        elif page_starts_new_schedule or page_is_admin:
            last_rider_by_pdf[source_pdf] = None
        carry_rider = last_rider_by_pdf.get(source_pdf)
        if (
            schedule_name is None
            and rider_body_name is None
            and carry_rider is None
            and section_type not in {"rate_schedule", "rider"}
        ):
            continue
        if schedule_name is None:
            schedule_name = _first_code_from_json(section.get("schedule_codes_json")) or section_type
        if rider_body_name is not None:
            schedule_name = rider_body_name
            section_type = "rider"
        elif carry_rider is not None and not page_starts_new_schedule and not page_is_admin:
            schedule_name = carry_rider
            section_type = "rider"

        fields = extract_rate_fields(text)
        evidence = _build_evidence(norm, active_context.evidence, schedule_name)
        confidence = _score_candidate(
            norm=norm,
            context=active_context,
            schedule_name=schedule_name,
            section_type=section_type,
            document_looks_targeted=doc_target_by_pdf.get(source_pdf, False),
            fields=fields,
        )
        blocks.append(
            ProposedTariffBlock(
                source_pdf=source_pdf,
                section_id=_optional_int(section.get("id")),
                section_index=int(section.get("section_index") or 0),
                start_page=int(section.get("start_page") or 0),
                end_page=int(section.get("end_page") or 0),
                section_type=section_type,
                exhibit_key=active_context.exhibit_key,
                rate_year_context=active_context.rate_year_context,
                schedule_name=schedule_name,
                basic_customer_charge=fields["basic_customer_charge"],
                volumetric_energy_charge_lines=fields["volumetric_energy_charge_lines"],
                time_of_use_lines=fields["time_of_use_lines"],
                has_interclass_impact_table=fields["has_interclass_impact_table"],
                confidence=confidence,
                evidence=evidence,
            )
        )

    return blocks


def detect_proposed_tariff_blocks(
    conn: sqlite3.Connection,
    *,
    source_pdf: str | None = None,
    limit: int = 0,
    max_chars: int = 8000,
    require_target_document: bool = True,
) -> list[ProposedTariffBlock]:
    """Read document sections from SQLite and return proposed tariff blocks."""
    where = ""
    params: list[Any] = []
    if source_pdf:
        where = "WHERE source_pdf LIKE ?"
        params.append(f"%{source_pdf}%")

    limit_sql = ""
    if limit > 0:
        limit_sql = "LIMIT ?"
        params.append(limit)

    rows = conn.execute(
        f"""
        SELECT id, source_pdf, section_index, start_page, end_page, section_type,
               schedule_codes_json, rider_codes_json, detected_titles_json
        FROM document_sections
        {where}
        ORDER BY source_pdf, section_index
        {limit_sql}
        """,
        params,
    ).fetchall()
    sections = [dict(row) for row in rows]

    def _load_text(section: dict[str, Any]) -> str:
        fetched = fetch_section_text(
            conn,
            section["source_pdf"],
            int(section["start_page"]),
            int(section["end_page"]),
            max_chars=max_chars,
        )
        return fetched.text

    return detect_blocks_from_sections(
        sections,
        _load_text,
        require_target_document=require_target_document,
    )


def detect_proposed_tariff_blocks_from_pdf(
    pdf_path: Path | str,
    *,
    max_pages: int = 0,
    require_target_document: bool = True,
) -> list[ProposedTariffBlock]:
    """Scan a PDF directly when page artifacts/sections are unavailable.

    This is a fallback for newly downloaded multi-hundred-page application
    PDFs before the normal NCUC page-artifact pipeline has mined them.
    """
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is required for direct PDF scans") from exc

    path = Path(pdf_path)
    doc = fitz.open(path)
    try:
        page_count = doc.page_count
        if max_pages > 0:
            page_count = min(page_count, max_pages)
        sections: list[dict[str, Any]] = []
        texts: dict[int, str] = {}
        for page_number in range(1, page_count + 1):
            text = doc.load_page(page_number - 1).get_text("text") or ""
            sections.append(
                {
                    "id": page_number,
                    "source_pdf": str(path),
                    "section_index": page_number,
                    "start_page": page_number,
                    "end_page": page_number,
                    "section_type": "unknown",
                    "schedule_codes_json": "[]",
                }
            )
            texts[page_number] = text
    finally:
        doc.close()

    return detect_blocks_from_sections(
        sections,
        lambda row: texts[int(row["id"])],
        require_target_document=require_target_document,
    )


_SCHEDULE_CODE_HEADER_RE = re.compile(r"^SCHEDULE\s+[A-Z0-9][A-Z0-9-]{0,12}\s*$")

_DEP_NEW_SCHEDULE_HEADERS = (
    "RESIDENTIAL SERVICE",
    "SMALL GENERAL SERVICE",
    "MEDIUM GENERAL SERVICE",
    "LARGE GENERAL SERVICE",
    "HOURLY PRICING",
    "OUTDOOR LIGHTING",
    "STREET LIGHTING",
    "STREET AND PUBLIC LIGHTING",
    "TRAFFIC SIGNAL",
    "SPORTS FIELD LIGHTING",
    "INDUSTRIAL SERVICE",
    "BUILDING CONSTRUCTION",
    "PARALLEL GENERATION",
    "SEASONAL OR INTERMITTENT",
    "CHURCH SERVICE",
    "GENERAL SERVICE",
    "AGRICULTURAL POST-HARVEST",
)
_DEP_ADMIN_HEADERS = (
    "FORWARD",
    "SERVICE REGULATIONS",
    "OUTDOOR LIGHTING SERVICE REGULATIONS",
    "DISTRIBUTION LINE EXTENSION",
)


def _page_starts_new_schedule(text: str) -> bool:
    """Return True when a page's top lines look like the start of a new
    schedule body — used to terminate any in-flight rider carry-forward.

    Only the first ten non-empty lines are inspected, and a leading
    ``SCHEDULE`` line must be a terse heading-style row (uppercase, no more
    than a couple of words long) so prose like
    ``Schedule and Rider(s) with which this Rider is used`` does not get
    misread as a new schedule.
    """
    head = [
        ln.strip().upper()
        for ln in (text or "").splitlines()
        if ln.strip()
    ][:10]
    for line in head:
        if _SCHEDULE_CODE_HEADER_RE.match(line):
            return True
        for header in _DEP_NEW_SCHEDULE_HEADERS:
            if line == header or line.startswith(header + " SCHEDULE"):
                return True
    return False


def _page_is_admin_section(text: str) -> bool:
    head = [
        ln.strip().upper()
        for ln in (text or "").splitlines()
        if ln.strip()
    ][:8]
    for line in head:
        for header in _DEP_ADMIN_HEADERS:
            if line.startswith(header):
                return True
    return False


def _has_non_target_exhibit(text: str) -> bool:
    match = _EXHIBIT_RE.search(text)
    if not match:
        return False
    letter = match.group(1).upper()
    suffix = match.group(2)
    key = f"{letter}_{suffix}" if suffix else letter
    return key not in TARGET_EXHIBIT_KEYS


def _score_candidate(
    *,
    norm: str,
    context: ExhibitContext,
    schedule_name: str,
    section_type: str,
    document_looks_targeted: bool,
    fields: dict[str, Any],
) -> float:
    score = 0.35
    if context.exhibit_key in {"B_1", "B_2"}:
        score += 0.15
    if document_looks_targeted:
        score += 0.12
    if schedule_name and schedule_name != section_type:
        score += 0.12
    if section_type in {"rate_schedule", "rider"}:
        score += 0.08
    if fields.get("basic_customer_charge"):
        score += 0.08
    if fields.get("volumetric_energy_charge_lines"):
        score += 0.06
    if fields.get("time_of_use_lines"):
        score += 0.03
    if "PROPOSED" in norm.upper():
        score += 0.04
    return round(min(score, 0.99), 4)


def _build_evidence(norm: str, exhibit_evidence: str, schedule_name: str) -> list[str]:
    snippets = [exhibit_evidence, schedule_name]
    for anchor in ("PROPOSED", "BASIC CUSTOMER CHARGE", "KILOWATT-HOUR", "TOTAL INCREASE"):
        idx = norm.upper().find(anchor)
        if idx >= 0:
            start = max(0, idx - 80)
            end = min(len(norm), idx + 180)
            snippets.append(norm[start:end])
    return _dedupe_preserve_order(s for s in snippets if s)[:6]


def _first_code_from_json(raw: Any) -> str | None:
    try:
        value = json.loads(str(raw or "[]"))
    except Exception:
        return None
    if isinstance(value, list) and value:
        return str(value[0]).upper()
    return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result
