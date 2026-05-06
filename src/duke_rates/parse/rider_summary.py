"""
Parser for Leaf 600 "Summary of Rider Adjustments" tariff pages.

These pages list every active rider adjustment (in ¢/kWh and optionally $/kW)
broken out by rate class, with individual effective dates per line item.  They
appear in multi-leaf compliance tariff books and are the authoritative source
for total rider adders needed to reconstruct a complete customer bill.

Typical format (one section per rate class)::

    cents Effective
    Residential Service Schedules /kWh Date
    Annual Billing Adjustments Rider BA
    Fuel and Fuel-Related Adjustment Rate       0.000  10/1/23
    Fuel EMF                                    0.650  12/1/22
    Demand Side Management DSM & EE Rate        0.640   1/1/23
    Annual Billing Adjustments Rider BA - Net Adjustment  1.290
    RAL-2 Rider                                -0.009  10/1/23
    EDIT-4 Rider                               -0.249  10/1/23
    Joint Agency Asset Rider JAA                0.631  12/1/22
    CPRE Rider                                  0.013  12/1/22
    TOTAL cents/kWh                             1.676

Usage::

    text = segment.full_text()
    result = parse_rider_summary(text, source_pdf="...", leaf_no="600")
    for rc in result.rate_classes:
        print(rc.rate_class, rc.total_cents_per_kwh)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RiderLineItem:
    """One line in a rate-class rider summary block."""
    label: str
    rider_code: str | None = None       # e.g. "BA", "EDIT-4", "JAA"; None for sub-items
    cents_per_kwh: float | None = None
    dollars_per_kw: float | None = None
    effective_date: str | None = None   # as printed, e.g. "10/1/23"
    is_section_header: bool = False     # True for "Annual Billing Adjustments Rider BA" header row
    is_subtotal: bool = False           # True for "BA - Net Adjustment" rows
    is_total: bool = False              # True for "TOTAL cents/kWh" rows
    indented: bool = False              # True for BA sub-items (x≈87 vs x≈78)


@dataclass
class RiderRateClassBlock:
    """All rider adjustments for one rate class."""
    rate_class: str                             # e.g. "Residential Service Schedules"
    applicable_schedules: list[str] = field(default_factory=list)
    line_items: list[RiderLineItem] = field(default_factory=list)
    total_cents_per_kwh: float | None = None
    total_dollars_per_kw: float | None = None


@dataclass
class RiderSummaryResult:
    """Parsed output from a Leaf 600 rider summary document."""
    source_pdf: str
    leaf_no: str | None
    effective_date: str | None
    docket_number: str | None
    order_date: str | None
    supersedes: str | None
    rate_classes: list[RiderRateClassBlock] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rate class → applicable schedule codes mapping
# ---------------------------------------------------------------------------

_RATE_CLASS_SCHEDULE_MAP: dict[str, list[str]] = {
    "residential": ["RES", "R-TOUD", "R-TOU", "R-TOU-CPP"],
    "residential schedules": ["RS", "RE", "ES", "RT", "RSTC", "RETC"],
    "small general service": ["SGS", "SGS-TOUE", "SGS-TOU", "SGS-TOU-CLR", "SGS-TOU-CPP"],
    "medium general service": ["MGS", "MGS-TOU", "SI", "CH-TOUE", "GS-TES", "APH-TES"],
    "seasonal or intermittent": ["SI"],
    "general service schedules": ["SGS", "BC", "LGS", "TS", "S", "HLF", "OPT-V", "PG", "SGSTC"],
    "industrial schedules": ["I", "HLF", "OPT-V", "PG"],
    "large general service": ["LGS", "LGS-TOU", "LGS-RTP", "HP", "LGS-HLF"],
    "outdoor lighting": ["OL", "SLR", "SFLS", "SLS", "ALS", "TFS"],
    "lighting schedules": ["PL", "OL", "NL"],
    "sports field lighting": ["SFLS"],
    "traffic signal": ["TSS"],
    "schedule hp": ["HP"],
    "schedule lgs-rtp": ["LGS-RTP"],
}

# Rider code extraction: look for known patterns in line labels
_KNOWN_RIDER_CODES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bRider\s+BA\b|\bBA\s+[-–]\s*Net\b', re.I), "BA"),
    (re.compile(r'\bRAL-2\b', re.I), "RAL-2"),
    (re.compile(r'\bRAL\b(?!-)', re.I), "RAL"),
    (re.compile(r'\bEDIT-4\b', re.I), "EDIT-4"),
    (re.compile(r'\bEDIT-3\b', re.I), "EDIT-3"),
    (re.compile(r'\bEDIT-1\b', re.I), "EDIT-1"),
    (re.compile(r'\bEDIT\b(?!-)', re.I), "EDIT"),
    (re.compile(r'\bRider\s+JAA\b|\bJoint\s+Agency\s+Asset\b', re.I), "JAA"),
    (re.compile(r'\bRider\s+CPRE\b|\bCPRE\s+Rider\b|\bCompetitive\s+Procurement\b', re.I), "CPRE"),
    (re.compile(r'\bRider\s+RDM\b|\bDecoupling\s+Mechanism\b', re.I), "RDM"),
    (re.compile(r'\bRider\s+ESM\b|\bEarnings\s+Sharing\b', re.I), "ESM"),
    (re.compile(r'\bRider\s+PIM\b|\bPerformance\s+Incentive\b', re.I), "PIM"),
    (re.compile(r'\bRider\s+CAR\b|\bCustomer\s+Affordability\b', re.I), "CAR"),
    (re.compile(r'\bRider\s+STS\b|\bStorm\s+Securitization\b', re.I), "STS"),
    (re.compile(r'\bRider\s+NM\b|\bNet\s+Metering\b', re.I), "NM"),
    (re.compile(r'\bRider\s+REPS\b|\bRenewable\s+Energy\s+Portfolio\b', re.I), "REPS"),
    (re.compile(r'\bFuel\s+Cost\s+Adjustment\s+Rider\b', re.I), "FCA"),
    (re.compile(r'\bEnergy\s+Efficiency\s+Rider\b', re.I), "EE"),
    (re.compile(r'\bExisting\s+DSM\s+Program\s+Costs\s+Adjustment\s+Rider\b', re.I), "DSM"),
    (re.compile(r'\bBPM\s+Prospective\s+Rider\b', re.I), "BPM-P"),
    (re.compile(r'\bBPM\s+True[-\s]?Up\s+Rider\b', re.I), "BPM-T"),
    (re.compile(r'\bRegulatory\s+Asset\s+and\s+Liability\s+Rider\b', re.I), "RAL"),
    (re.compile(r'\bCustomer\s+Assistance\s+Recovery\s+Rider\b', re.I), "CAR"),
    (re.compile(r'\bFuel.*Adjustment\s+Rate\b', re.I), "BA-Fuel"),
    (re.compile(r'\bExperience\s+Modification\s+Factor\b|\bEMF\b', re.I), "BA-EMF"),
    (re.compile(r'\bDemand\s+Side\s+Management.*Rate\b|\bDSM.*Rate\b', re.I), "BA-DSM"),
    (re.compile(r'\bEnergy\s+Efficiency.*Rate\b|\bEE.*Rate\b', re.I), "BA-EE"),
]

# Lines to skip (footnotes, headers, page labels)
_SKIP_LINE_RE = re.compile(
    r"""
    ^\*+\s* |               # footnote lines starting with *
    ^Page\s+\d+\s+of\s+\d+ |
    ^Duke\s+Energy |
    ^\(North\s+Carolina |
    ^NC\s+(Original|First|Second) |
    ^Effective\s+for\s+service |
    ^NCUC\s+Docket |
    ^The\s+following\s+is |
    ^More\s+specific |
    ^rate\s+schedule |
    ^below\. |
    ^regulatory\s+fees |
    ^applicable\s+DSM |
    ^opted\s+out |
    ^No\.\s+601 |
    ^Electricity\s+No\. |           # PDF page-header: "Electricity No. N" (leaf number reference)
    ^(?:North\s+Carolina\s+)?(?:\w+\s+)*Revised\s+Leaf\s+No\. |  # "NC Fifteenth Revised Leaf No."
    ^Revised\s+Leaf\s+No\. |        # short form
    ^Leaf\s+No\.\s+\d+ |            # standalone "Leaf No. 99" lines
    ^(?:Amended|Superseding)\s+North\s+Carolina |  # "Amended NC ..." / "Superseding NC ..."
    ^Original\s+Leaf\s+No\.         # "Original Leaf No. N"
    """,
    re.I | re.X,
)


# Section header: a rate class name followed by "/kWh" or "Date" in the column headers
_SECTION_HEADER_RE = re.compile(
    r"^(.+?)\s{2,}(?:cents\s+)?(?:/kWh|Effective)\s*(?:Date)?",
    re.I,
)

# Data row: label + number(s) + optional date.
# The label is separated from numbers by whitespace; numbers may be single-spaced.
# e.g. "Fuel and Fuel-Related Adjustment Rate 0.000 10/1/23"
# e.g. "Annual Billing Adjustments Rider BA - Net Adjustment 1.290"
# e.g. "TOTAL cents/kWh 1.676"
# e.g. "Joint Agency Asset Rider JAA 1.43 12/1/22"  ($/kW row with two values)
# e.g. "EDIT-4 Rider (0.249) 10/1/23"  (parenthesized negative)
_NUM_RE = r"(?:-?\d+(?:\.\d+)?|\(\d+(?:\.\d+)?\))"  # plain or parenthesized negative
_DATA_ROW_RE = re.compile(
    r"^(.+?)\s+"                            # label
    r"(" + _NUM_RE + r")"                   # first number
    r"(?:\s+(" + _NUM_RE + r"))?"           # optional second number
    r"(?:\s+(\d{1,2}/\d{1,2}/\d{2,4}))?"  # optional date mm/dd/yy or mm/dd/yyyy
    r"\s*$",
)

# Total rows
_TOTAL_ROW_RE = re.compile(r"^TOTAL\s+(?:cents/kWh|dollars/kW)", re.I)

# Rate class section header: ends with "Schedules", "Schedule", "Service"
_RATE_CLASS_RE = re.compile(
    r"^(.+?(?:Schedules?|Service|Lighting))\*{0,3}\s{2,}(?:cents|/kWh)",
    re.I,
)

# BA section header (no number on the line)
_BA_HEADER_RE = re.compile(r"^Annual\s+Billing\s+Adjustments\s+Rider\s+BA\s*$", re.I)

# Net adjustment subtotal
_SUBTOTAL_RE = re.compile(r"Net\s+Adjustment", re.I)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_rider_summary(
    text: str,
    *,
    source_pdf: str = "",
    leaf_no: str | None = None,
) -> RiderSummaryResult:
    """Parse the full text of a Leaf 600 rider summary document.

    Args:
        text:       Full extracted text from the Leaf 600 segment (all pages).
        source_pdf: Path to the source PDF, stored for provenance.
        leaf_no:    Leaf number (usually "600"), stored for provenance.

    Returns:
        ``RiderSummaryResult`` with one ``RiderRateClassBlock`` per rate class.
    """
    from duke_rates.parse.heuristics import (
        extract_effective_date,
        extract_docket_footer,
        extract_supersedes,
    )

    # Normalize column-split text (PyMuPDF/fitz puts each table cell on its own line).
    # Detect by checking whether numbers appear on their own lines next to labels.
    normalized = _normalize_column_split_text(text)

    effective_date = _extract_summary_effective_date(text)
    docket_number, order_date = extract_docket_footer(text)
    supersedes = extract_supersedes(text)

    result = RiderSummaryResult(
        source_pdf=source_pdf,
        leaf_no=leaf_no,
        effective_date=effective_date,
        docket_number=docket_number,
        order_date=order_date,
        supersedes=supersedes,
    )

    rate_classes = _parse_rate_class_blocks(normalized, result.parse_warnings)
    result.rate_classes = _dedupe_rate_class_blocks(rate_classes)
    return result


def parse_rider_summary_from_pdf(
    pdf_path: str,
    *,
    page_range: tuple[int, int] | None = None,
    leaf_no: str | None = None,
) -> RiderSummaryResult:
    """Coordinate-aware parser for Leaf 600 PDFs.

    Reads span coordinates directly from PyMuPDF instead of going through
    text extraction.  Handles:

    * Bold detection for rate-class headers and TOTAL rows
    * Indentation (x≈87 vs x≈78) to flag BA sub-items
    * Variable column layouts: single (cents/kWh + Date), double (cents/kWh +
      dollars/kW + Date), and triple (Baseline + Incremental + dollars/kW +
      Date) as used by HP/LGS-RTP
    * Page-spanning sections — active rate class carried across page breaks
    * Parenthesized negatives (0.249) → -0.249

    Args:
        pdf_path:   Absolute or ROOT-relative path to the source PDF.
        page_range: Optional (start, end) 0-based page indices (inclusive).
                    If None, all pages are processed.
        leaf_no:    Leaf number string stored for provenance.

    Returns:
        ``RiderSummaryResult`` identical in shape to ``parse_rider_summary``.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError("PyMuPDF (fitz) is required for coordinate-aware parsing")

    doc = fitz.open(pdf_path)
    try:
        pages = list(range(len(doc)))
        if page_range is not None:
            pages = [p for p in pages if page_range[0] <= p <= page_range[1]]

        # --- collect metadata from full text of first page ---
        first_text = doc[pages[0]].get_text("text") if pages else ""
        effective_date = _extract_summary_effective_date(first_text)
        from duke_rates.parse.heuristics import extract_docket_footer, extract_supersedes
        docket_number, order_date = extract_docket_footer(first_text)
        supersedes = extract_supersedes(first_text)

        result = RiderSummaryResult(
            source_pdf=pdf_path,
            leaf_no=leaf_no,
            effective_date=effective_date,
            docket_number=docket_number,
            order_date=order_date,
            supersedes=supersedes,
        )

        # --- collect all logical rows across all pages ---
        all_rows = []  # list of _SpanRow
        for page_num in pages:
            page = doc[page_num]
            all_rows.extend(_extract_span_rows(page))

        result.rate_classes = _dedupe_rate_class_blocks(
            _build_blocks_from_rows(all_rows, result.parse_warnings)
        )
        return result
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Coordinate-aware internals
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dc


@_dc
class _SpanRow:
    """One logical row assembled from same-y spans on a PDF page."""
    label: str
    bold: bool
    indented: bool          # x≈87 vs x≈78
    col1: float | None      # first numeric column value (cents/kWh baseline)
    col2: float | None      # second numeric column (incremental cents/kWh or $/kW)
    col3: float | None      # third numeric column ($/kW for HP/LGS-RTP)
    date: str | None
    is_col_header: bool     # "cents /kWh Effective Date" header rows
    has_dollar_kw: bool     # page has a dollars/kW column


# x-coordinate thresholds — derived from span dump of leaf-600 PDF
_X_LABEL_MAX = 300.0        # labels end before this
_X_COL1_MIN = 340.0         # first value column (baseline cents/kWh)
_X_COL1_MAX = 390.0
_X_COL2_MIN = 415.0         # second value column (incremental or $/kW)
_X_COL2_MAX = 450.0
_X_COL3_MIN = 465.0         # third value column ($/kW for HP/LGS-RTP)
_X_COL3_MAX = 500.0
_X_DATE_MIN = 400.0         # dates appear after values; min x shifts with layout
_X_INDENT_THRESHOLD = 85.0  # x > this → BA sub-item (indented)

_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
_NUM_FRAG_RE = re.compile(r"^(?:-?\d+(?:\.\d+)?|\(\d+(?:\.\d+)?\))$")

# Column header text fragments to skip
_COL_HEADER_FRAGS = frozenset({
    "cents", "/kwh", "effective", "date", "dollars", "/kw",
    "baseline", "incremental", "e",  # "Effective" split as "Effectiv"+"e"
    "effectiv",
})


def _is_bold(span: dict) -> bool:
    return "Bold" in span.get("font", "") or bool(span.get("flags", 0) & 16)


def _extract_span_rows(page) -> list[_SpanRow]:
    """Group same-y spans into logical rows, assign columns by x-coordinate."""
    # Gather all content spans, skip boilerplate (sz>9.5 = header/footer text)
    raw: list[tuple[float, float, str, bool, float]] = []  # y, x, text, bold, size
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                text = span["text"].strip()
                if not text:
                    continue
                size = span["size"]
                if size > 9.5:  # skip header/footer boilerplate (size 10+)
                    continue
                y = round(span["bbox"][1], 0)
                x = span["bbox"][0]
                raw.append((y, x, text, _is_bold(span), size))

    if not raw:
        return []

    # Detect whether this page has a dollars/kW column header
    all_text_lower = " ".join(t for _, _, t, _, _ in raw).lower()
    page_has_dollar_kw = "dollars" in all_text_lower and "/kw" in all_text_lower

    # Group by y (same logical row within ±2pt)
    raw.sort()
    rows_by_y: dict[float, list[tuple[float, str, bool]]] = {}
    for y, x, text, bold, _ in raw:
        # snap y to nearest 2pt bucket
        bucket = round(y / 2) * 2
        rows_by_y.setdefault(bucket, []).append((x, text, bold))

    # Merge wrapped label continuations.
    # Pattern: a label-only row (no values) is immediately followed by a row
    # whose label starts with "(" — that's the "(EMF)" continuation which also
    # carries the actual numeric values.  Prepend the first row's label text to
    # the continuation row so the combined row has label + values.
    sorted_buckets = sorted(rows_by_y)
    merged_rows_by_y: dict[float, list[tuple[float, str, bool]]] = {}
    skip_next: float | None = None
    for idx, bucket in enumerate(sorted_buckets):
        if bucket == skip_next:
            skip_next = None
            continue
        spans = rows_by_y[bucket]
        has_nums = any(_NUM_FRAG_RE.match(t) or _DATE_RE.match(t) for _, t, _ in spans)
        all_label = all(x < _X_LABEL_MAX for x, _, _ in spans)

        # Look ahead: if this row is label-only and next row's label starts with "("
        if not has_nums and all_label and idx + 1 < len(sorted_buckets):
            next_bucket = sorted_buckets[idx + 1]
            next_spans = rows_by_y[next_bucket]
            next_labels = [t for x, t, _ in next_spans if x < _X_LABEL_MAX]
            if next_labels and next_labels[0].startswith("("):
                # Prepend this row's label text to the next row (which has the value)
                my_label_text = " ".join(t for x, t, b in sorted(spans))
                my_bold = any(b for _, _, b in spans)
                my_x = min(x for x, _, _ in spans)
                combined = [(my_x, my_label_text, my_bold)] + list(next_spans)
                merged_rows_by_y[bucket] = combined
                skip_next = next_bucket
                continue

        merged_rows_by_y[bucket] = list(spans)

    span_rows: list[_SpanRow] = []
    for bucket in sorted(merged_rows_by_y):
        spans = sorted(merged_rows_by_y[bucket])  # sort by x

        # Detect column-header rows — contain only header fragments and no label text
        texts_lower = [t.lower() for _, t, _ in spans]
        if all(t in _COL_HEADER_FRAGS for t in texts_lower):
            span_rows.append(_SpanRow(
                label="", bold=False, indented=False,
                col1=None, col2=None, col3=None, date=None,
                is_col_header=True, has_dollar_kw=page_has_dollar_kw,
            ))
            continue

        # Separate label spans from numeric/date spans
        label_parts: list[str] = []
        label_bold = False
        label_x = None
        col1 = col2 = col3 = None
        date_val = None

        for x, text, bold in spans:
            if x < _X_LABEL_MAX:
                label_parts.append(text)
                label_bold = label_bold or bold
                if label_x is None:
                    label_x = x
            elif _DATE_RE.match(text):
                date_val = text
            elif _NUM_FRAG_RE.match(text):
                # Assign to column band by x
                if x < _X_COL1_MAX:
                    col1 = _parse_num(text)
                elif x < _X_COL2_MAX:
                    col2 = _parse_num(text)
                elif x < _X_COL3_MAX:
                    col3 = _parse_num(text)
                else:
                    # Beyond col3 — treat as $/kW standalone (JAA demand row)
                    if col3 is None:
                        col3 = _parse_num(text)
            # skip non-label text that isn't a number or date (rare artifacts)

        label = " ".join(label_parts).strip()
        if not label and col1 is None and col2 is None and col3 is None:
            continue  # blank row

        indented = label_x is not None and label_x > _X_INDENT_THRESHOLD

        span_rows.append(_SpanRow(
            label=label,
            bold=label_bold,
            indented=indented,
            col1=col1,
            col2=col2,
            col3=col3,
            date=date_val,
            is_col_header=False,
            has_dollar_kw=page_has_dollar_kw,
        ))

    return span_rows


def _build_blocks_from_rows(
    rows: list[_SpanRow],
    warnings: list[str],
) -> list[RiderRateClassBlock]:
    """Convert a flat list of SpanRows (across all pages) into RiderRateClassBlocks."""
    blocks: list[RiderRateClassBlock] = []
    current_block: RiderRateClassBlock | None = None
    in_ba_section = False
    # Track whether current section has a Baseline+Incremental layout (HP/LGS-RTP)
    has_incremental = False

    _RATE_CLASS_END_RE = re.compile(
        r"(?:Schedules?|Service|Lighting|Signal|LGS-RTP)\*{0,3}$", re.I
    )

    def flush():
        if current_block and current_block.line_items:
            blocks.append(current_block)

    for row in rows:
        if row.is_col_header:
            continue

        label = row.label
        bold = row.bold

        # Skip boilerplate lines with no label and no values
        if not label and row.col1 is None and row.col2 is None and row.col3 is None:
            continue

        # Skip page-header/footer text that leaked through (no values, matches skip patterns)
        if not label:
            continue
        if _SKIP_LINE_RE.match(label):
            continue

        # --- Rate class section header ---
        # Bold, no numeric values, label ends with Schedules/Service/Lighting/Signal
        if bold and row.col1 is None and row.col2 is None and row.col3 is None:
            if _RATE_CLASS_END_RE.search(label) or label.lower().startswith("total"):
                # "TOTAL dollars/kW" at page top — belongs to previous block
                if label.lower().startswith("total"):
                    _handle_total_row(label, row, current_block)
                    continue
                # New rate class
                flush()
                current_block = RiderRateClassBlock(
                    rate_class=label.rstrip("*").strip(),
                    applicable_schedules=_lookup_schedules(label),
                )
                in_ba_section = False
                # Detect HP/LGS-RTP incremental layout from col-header presence on same page
                has_incremental = "LGS-RTP" in label or "HP" in label
                continue

        # --- BA group header (bold, no values, doesn't end with class keywords) ---
        if bold and row.col1 is None and row.col2 is None and row.col3 is None:
            if current_block is not None:
                current_block.line_items.append(RiderLineItem(
                    label=label, rider_code="BA", is_section_header=True,
                ))
                in_ba_section = True
            continue

        # --- TOTAL rows (bold, has values) ---
        if bold and (row.col1 is not None or row.col2 is not None or row.col3 is not None):
            _handle_total_row(label, row, current_block)
            in_ba_section = False
            continue

        # --- Data rows ---
        if current_block is None:
            continue

        is_ba_header = _BA_HEADER_RE.match(label)
        if is_ba_header:
            current_block.line_items.append(RiderLineItem(
                label=label, rider_code="BA", is_section_header=True,
            ))
            in_ba_section = True
            continue

        is_subtotal = bool(_SUBTOTAL_RE.search(label))
        rider_code = _extract_rider_code(label)

        # Assign values based on layout
        if has_incremental:
            # HP/LGS-RTP: col1=baseline cents/kWh, col2=incremental cents/kWh, col3=$/kW
            # We store col1 (baseline) as cents_per_kwh, col3 as dollars_per_kw
            # col2 (incremental) stored as a note but not as a separate charge
            cents = row.col1
            dollars = row.col3
        else:
            # Standard layout: col1=cents/kWh, col2=$/kW (demand sections only)
            cents = row.col1
            dollars = row.col2

        item = RiderLineItem(
            label=label,
            rider_code=rider_code or ("BA" if in_ba_section else None),
            cents_per_kwh=cents,
            dollars_per_kw=dollars,
            effective_date=row.date,
            is_subtotal=is_subtotal,
            indented=row.indented,
        )
        current_block.line_items.append(item)

        if is_subtotal:
            in_ba_section = False

    flush()
    return blocks


def _handle_total_row(
    label: str,
    row: "_SpanRow",
    block: "RiderRateClassBlock | None",
) -> None:
    """Apply TOTAL cents/kWh or TOTAL dollars/kW values to the current block."""
    if block is None:
        return
    label_lower = label.lower()
    if "cents" in label_lower or "kwh" in label_lower:
        val = row.col1
        if val is not None:
            block.total_cents_per_kwh = val
        block.line_items.append(RiderLineItem(
            label=label, is_total=True, cents_per_kwh=val,
        ))
    elif "dollar" in label_lower or "/kw" in label_lower:
        # col3 for HP/LGS-RTP (x≈477), col2 for standard demand (x≈393)
        val = row.col3 if row.col3 is not None else row.col2
        if val is not None:
            block.total_dollars_per_kw = val
        block.line_items.append(RiderLineItem(
            label=label, is_total=True, dollars_per_kw=val,
        ))


_SUMMARY_LEAF_EFFECTIVE_RE = re.compile(
    r"Leaf\s+No\.\s*\d+[\s\S]{0,300}?Effective\s+for\s+service\s+rendered\s+on\s+and\s+after\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
    re.I,
)
_SUMMARY_EFFECTIVE_RE = re.compile(
    r"Effective\s+for\s+service\s+rendered\s+on\s+and\s+after\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
    re.I,
)


def _extract_summary_effective_date(text: str) -> str | None:
    from duke_rates.parse.heuristics import extract_effective_date

    candidates: list[str] = []
    candidates.extend(match.group(1).strip() for match in _SUMMARY_LEAF_EFFECTIVE_RE.finditer(text))
    if not candidates:
        candidates.extend(match.group(1).strip() for match in _SUMMARY_EFFECTIVE_RE.finditer(text))
    if candidates:
        dated_candidates = [
            (_parse_summary_effective_candidate(candidate), candidate)
            for candidate in candidates
        ]
        dated_candidates = [item for item in dated_candidates if item[0] is not None]
        if dated_candidates:
            dated_candidates.sort(key=lambda item: item[0])
            return dated_candidates[0][1]
        return candidates[0]
    return extract_effective_date(text)


def _parse_summary_effective_candidate(value: str) -> datetime | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _normalize_column_split_text(text: str) -> str:
    """Re-join column-split rows where label, number, and date appear on separate lines.

    PyMuPDF (fitz) sometimes extracts multi-column table rows as one line per cell::

        'Fuel and Fuel-Related Adjustment Rate '
        '0.000 '
        '10/1/23 '

    This function joins such triplets/pairs into the single-line format that the
    main parser expects::

        'Fuel and Fuel-Related Adjustment Rate 0.000 10/1/23'

    Detection heuristic: if a number-only line (``-?\\d+\\.\\d+``) immediately
    follows a non-number text-only line, they belong to the same row.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    if not lines:
        return text

    _PURE_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?|\(\d+(?:\.\d+)?\)")
    _PURE_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")

    def _is_pure_num(s: str) -> bool:
        return bool(_PURE_NUM_RE.fullmatch(s))

    def _is_pure_date(s: str) -> bool:
        return bool(_PURE_DATE_RE.fullmatch(s))

    # Count how many lines are purely numeric — if < 3 the text is already joined
    num_only = sum(1 for ln in lines if _is_pure_num(ln.strip()))
    if num_only < 3:
        return text

    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            out.append("")
            i += 1
            continue

        # Is the NEXT line a pure number?
        is_next_num = i + 1 < len(lines) and _is_pure_num(lines[i + 1].strip())

        # Join if current line is not itself a number or date (it's a label)
        if is_next_num and not _is_pure_num(line) and not _is_pure_date(line):
            # This line is a label; join with the following number(s) and optional date
            combined = line
            i += 1
            while i < len(lines):
                part = lines[i].strip()
                if not part:
                    i += 1
                    break
                if _is_pure_num(part) or _is_pure_date(part):
                    combined += " " + part
                    i += 1
                else:
                    break
            out.append(combined)
        else:
            out.append(line)
            i += 1

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Internal parsing
# ---------------------------------------------------------------------------

_PAREN_NEG_RE = re.compile(r"^\((\d+(?:\.\d+)?)\)$")


def _parse_num(s: str) -> float:
    """Parse a number string, handling parenthesized negatives like (0.249)."""
    m = _PAREN_NEG_RE.match(s.strip())
    if m:
        return -float(m.group(1))
    return float(s)


def _parse_rate_class_blocks(
    text: str, warnings: list[str]
) -> list[RiderRateClassBlock]:
    """Split the full document text into rate class blocks and parse each.

    Handles two PDF extraction styles:

    * pdfplumber:  ``"Residential Service Schedules /kWh Date"``  (single line)
    * PyMuPDF/fitz: ``"Residential Service Schedules"`` / ``"/kWh"``  (split lines)
    * DEC Leaf 99: ``"Residential Schedules RS, RE, ES, RT, RSTC, RETC cents/kWh Effective Date"``
    """
    blocks: list[RiderRateClassBlock] = []
    current_class: str | None = None
    current_schedules: list[str] | None = None
    current_lines: list[str] = []
    prev_potential_class: str | None = None  # line that might be a class name

    def flush() -> None:
        if current_class and current_lines:
            block = _parse_one_block(current_class, current_lines, warnings, current_schedules)
            if block.line_items:
                blocks.append(block)

    # Column-header / boilerplate line patterns
    _COL_HEADER_RE = re.compile(
        r"^(?:cents\s*$|cents\s|/kWh|/kW|Baseline\s+Incr|dollars\s+Effective|dollars\s*$|Date\s*$|Effective\s*$)",
        re.I,
    )
    # A potential class-name line: ends with "Schedules", "Schedule", "Service", "Lighting", "Signal"
    _CLASS_NAME_RE = re.compile(
        (
            r"^([\w\s:,&/-]+?(?:Service\s+Schedules?|General\s+Service\s+Schedules?|"
            r"Schedules?|Service|Lighting|Signal|LGS-RTP\*{0,3}))\*{0,3}\s*$"
        ),
        re.I,
    )
    # Combined single-line format: "Residential Service Schedules /kWh Date"
    _CLASS_INLINE_RE = re.compile(
        r"^([\w\s:,&/-]+?(?:Service\s+Schedules?|General\s+Service\s+Schedules?|Schedules?|Service|Lighting|Signal|LGS-RTP\*{0,3}))"
        r"\*{0,3}\s+(?:cents\s+)?(?:/kWh|/kW|Baseline|dollars)",
        re.I,
    )

    for raw_line in _coalesce_wrapped_header_lines(text.splitlines()):
        line = raw_line.strip()
        if not line:
            prev_potential_class = None
            continue

        # Skip boilerplate lines
        if _SKIP_LINE_RE.match(line):
            prev_potential_class = None
            continue

        # Column header row
        if _COL_HEADER_RE.match(line):
            # If the previous non-blank line was a class-name candidate, promote it
            if prev_potential_class is not None:
                flush()
                current_class = prev_potential_class
                current_schedules = None
                current_lines = []
                prev_potential_class = None
            continue

        header = _extract_block_header(line)
        if header is not None:
            prev_potential_class = None
            flush()
            current_class, current_schedules = header
            current_lines = []
            continue

        # Single-line combined format: "Residential Service Schedules /kWh Date"
        class_m = _CLASS_INLINE_RE.match(line)
        if class_m:
            prev_potential_class = None
            flush()
            current_class = class_m.group(1).strip().rstrip("*").strip()
            current_schedules = None
            current_lines = []
            continue

        # Check whether this line alone looks like a rate class name
        # (multi-word ending in Schedules/Service/etc., no numbers)
        if not re.search(r"\d", line):
            name_m = _CLASS_NAME_RE.match(line)
            if name_m:
                prev_potential_class = name_m.group(1).strip().rstrip("*").strip()
                continue

        prev_potential_class = None

        if current_class is not None:
            current_lines.append(line)

    flush()
    return blocks


def _parse_one_block(
    rate_class: str,
    lines: list[str],
    warnings: list[str],
    applicable_schedules: list[str] | None = None,
) -> RiderRateClassBlock:
    """Parse the lines belonging to one rate class into a ``RiderRateClassBlock``."""
    block = RiderRateClassBlock(
        rate_class=rate_class,
        applicable_schedules=list(applicable_schedules or _lookup_schedules(rate_class)),
    )
    in_ba_section = False

    for line in lines:
        # BA section header (no numbers)
        if _BA_HEADER_RE.match(line):
            in_ba_section = True
            block.line_items.append(RiderLineItem(
                label=line,
                rider_code="BA",
                is_section_header=True,
            ))
            continue

        # Total row
        if _TOTAL_ROW_RE.match(line):
            in_ba_section = False
            m = re.match(
                r"^TOTAL\s+(cents/kWh|dollars/kW)\s+(-?\d+(?:\.\d+)?)",
                line, re.I,
            )
            if m:
                val = float(m.group(2))
                is_kwh = "cents" in m.group(1).lower()
                item = RiderLineItem(label=line, is_total=True)
                if is_kwh:
                    item.cents_per_kwh = val
                    block.total_cents_per_kwh = val
                else:
                    item.dollars_per_kw = val
                    block.total_dollars_per_kw = val
                block.line_items.append(item)
            continue

        # Data row: label + number(s) + optional date
        m = _DATA_ROW_RE.match(line)
        if m:
            label = m.group(1).strip()
            val1 = _parse_num(m.group(2))
            val2 = _parse_num(m.group(3)) if m.group(3) else None
            date = m.group(4)
            rider_code = _extract_rider_code(label)
            is_subtotal = bool(_SUBTOTAL_RE.search(label))
            item = RiderLineItem(
                label=label,
                rider_code=rider_code,
                cents_per_kwh=val1,
                dollars_per_kw=val2,
                effective_date=date,
                is_subtotal=is_subtotal,
            )
            # BA sub-items retain parent rider code
            if in_ba_section and not rider_code:
                item.rider_code = "BA"
            block.line_items.append(item)
            if is_subtotal:
                in_ba_section = False
        else:
            # Could be a section header with no numbers (e.g. "Joint Agency Asset Rider JAA  1.43  12/1/22" split)
            # or a footnote — skip silently
            pass

    return block


def _lookup_schedules(rate_class: str) -> list[str]:
    """Return the list of schedule codes that belong to a named rate class."""
    lower = rate_class.lower()
    for key, codes in _RATE_CLASS_SCHEDULE_MAP.items():
        if key in lower:
            return list(codes)
    return []


def _extract_rider_code(label: str) -> str | None:
    """Return the standardized rider code for a label string, or None."""
    for pattern, code in _KNOWN_RIDER_CODES:
        if pattern.search(label):
            return code
    return None


def _extract_block_header(line: str) -> tuple[str, list[str]] | None:
    """Parse inline rate-class headers that also include explicit schedule codes."""
    dec_match = re.match(
        r"^(?P<rate_class>.+?\bSchedules?)\s+"
        r"(?P<schedules>[A-Z0-9][A-Z0-9,\s&/-]*)\s+"
        r"(?:cents(?:/kWh)?|/kWh|/kW|dollars(?:/kW)?)\b",
        line,
        re.I,
    )
    if dec_match:
        rate_class = dec_match.group("rate_class").strip().rstrip("*").strip()
        schedules = _split_schedule_codes(dec_match.group("schedules"))
        if schedules:
            return rate_class, schedules

    hp_match = re.match(
        r"^(?P<rate_class>Schedule\s+HP\s+[–-]\s+.+?)\s+Baseline\s+Incremental\s+Effective\s+Date$",
        line,
        re.I,
    )
    if hp_match:
        return hp_match.group("rate_class").strip(), ["HP"]

    return None


def _split_schedule_codes(raw: str) -> list[str]:
    cleaned = re.sub(r"\b(?:and|&)\b", ",", raw, flags=re.I)
    codes = [part.strip().rstrip(".,;") for part in cleaned.split(",")]
    return [code for code in codes if code and re.fullmatch(r"[A-Z0-9-]+", code)]


def _coalesce_wrapped_header_lines(lines: list[str]) -> list[str]:
    """Join wrapped class headers split across multiple lines."""
    merged: list[str] = []
    i = 0
    while i < len(lines):
        current = lines[i].rstrip()
        current_stripped = current.strip()

        if (
            current_stripped
            and ("Schedules" in current_stripped or "Schedule HP" in current_stripped)
            and not re.search(r"(?:cents(?:/kWh)?|/kWh|/kW|dollars(?:/kW)?)\b", current_stripped, re.I)
        ):
            header_parts = [current_stripped]
            j = i + 1
            found_unit = False
            while j < len(lines):
                candidate = lines[j].strip()
                if not candidate:
                    break
                header_parts.append(candidate)
                if candidate.lower() == "cents":
                    j += 1
                    continue
                if re.search(r"(?:cents(?:/kWh)?|/kWh|/kW|dollars(?:/kW)?)\b", candidate, re.I):
                    found_unit = True
                    break
                j += 1
            if found_unit:
                merged.append(" ".join(header_parts))
                i = j + 1
                continue

        merged.append(current)
        i += 1
    return merged


def _dedupe_rate_class_blocks(blocks: list[RiderRateClassBlock]) -> list[RiderRateClassBlock]:
    deduped: list[RiderRateClassBlock] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for block in blocks:
        key = (block.rate_class, tuple(block.applicable_schedules))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(block)
    return deduped
