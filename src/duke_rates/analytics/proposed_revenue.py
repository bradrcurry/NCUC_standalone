from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from duke_rates.analytics.dep_progress import _require_pandas
from duke_rates.db.sqlite import connect


SUMMARY_ANCHOR = "SUMMARY OF PROPOSED REVENUE ADJUSTMENTS"

_LABELS = (
    "Traditional Base Rate Revenue Requirement",
    "Rate Year 1 - Incremental Revenue Requirement for MYRP Projects",
    "Rate Year 1 - Total (L1 + L2)",
    "Rate Year 2 - Incremental Revenue Requirement for MYRP Projects",
    "Cumulative Rate year 2 Revenue Increase (L3 + L4)",
)

_UTILITY_COLUMNS = {
    "Duke Energy Progress": (
        "base_rates_millions",
        "over_amortization_rider_millions",
        "jaar_rider_millions",
        "ptc_rider_millions",
        "bpm_rider_millions",
        "total_impact_millions",
    ),
    "Duke Energy Carolinas": (
        "base_rates_millions",
        "over_amortization_rider_millions",
        "edpr_rider_millions",
        "total_impact_millions",
    ),
}


@dataclass(frozen=True)
class ProposedRevenueAdjustment:
    utility: str
    docket_number: str | None
    source_pdf: str
    source_page: int
    line_no: int
    description: str
    base_rates_millions: float | None = None
    over_amortization_rider_millions: float | None = None
    jaar_rider_millions: float | None = None
    ptc_rider_millions: float | None = None
    bpm_rider_millions: float | None = None
    edpr_rider_millions: float | None = None
    total_impact_millions: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_proposed_revenue_adjustments(*, database_path: Path | None = None):
    """Return proposed revenue-adjustment summary rows from downloaded PDFs."""
    pd = _require_pandas()
    db_path = _database_path(database_path)
    conn = connect(db_path)
    try:
        docs = conn.execute(
            """
            SELECT source_pdf, docket_number, utility
            FROM proposed_tariff_documents
            ORDER BY utility, docket_number
            """
        ).fetchall()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

    records: list[dict[str, object]] = []
    for doc in docs:
        source_pdf = Path(str(doc["source_pdf"]))
        utility = str(doc["utility"] or "")
        if not source_pdf.exists():
            continue
        rows = extract_proposed_revenue_adjustments_from_pdf(
            source_pdf,
            utility=utility,
            docket_number=doc["docket_number"],
        )
        records.extend(row.to_dict() for row in rows)
    return pd.DataFrame(records)


def extract_proposed_revenue_adjustments_from_pdf(
    pdf_path: Path | str,
    *,
    utility: str,
    docket_number: str | None = None,
) -> list[ProposedRevenueAdjustment]:
    """Find and parse the Exhibit 1 revenue-adjustment summary page."""
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is required for proposed revenue parsing") from exc

    pdf = Path(pdf_path)
    doc = fitz.open(pdf)
    try:
        for page_number in range(1, doc.page_count + 1):
            text = doc.load_page(page_number - 1).get_text("text") or ""
            if SUMMARY_ANCHOR in text.upper():
                return parse_proposed_revenue_adjustment_page(
                    text,
                    utility=utility,
                    docket_number=docket_number,
                    source_pdf=str(pdf),
                    source_page=page_number,
                )
    finally:
        doc.close()
    return []


def parse_proposed_revenue_adjustment_page(
    text: str,
    *,
    utility: str,
    docket_number: str | None = None,
    source_pdf: str = "",
    source_page: int = 0,
) -> list[ProposedRevenueAdjustment]:
    """Parse the line-item summary table from Exhibit 1.

    The PDF text stream renders table columns as stacked lines. We anchor on
    the row descriptions, then collect numeric amount lines until the next
    description. References like ``[1]`` and standalone dollar signs are ignored.
    """
    columns = _UTILITY_COLUMNS.get(utility)
    if columns is None:
        return []
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    rows: list[ProposedRevenueAdjustment] = []
    for idx, label in enumerate(_LABELS):
        start = _find_line(lines, label)
        if start is None:
            continue
        next_starts = [
            pos
            for other in _LABELS[idx + 1 :]
            for pos in [_find_line(lines, other)]
            if pos is not None and pos > start
        ]
        end = min(next_starts) if next_starts else len(lines)
        values = _numeric_values(lines[start + 1 : end])
        if values and idx + 2 <= len(_LABELS) and values[-1] == float(idx + 2):
            values = values[:-1]
        values = _align_values(values, columns)
        payload = dict(zip(columns, values, strict=False))
        rows.append(
            ProposedRevenueAdjustment(
                utility=utility,
                docket_number=docket_number,
                source_pdf=source_pdf,
                source_page=source_page,
                line_no=idx + 1,
                description=label,
                **payload,
            )
        )
    return rows


_REVREQ_ANCHOR = "CALCULATION OF ADDITIONAL REVENUE REQUIREMENT"
_RATEBASE_ANCHOR = "COST RATE BASE-ELECTRIC OPERATIONS"
_REVREQ_LABEL_RE = re.compile(
    r"Additional traditional base rate revenue requirement", re.I
)
_PLANT_LABEL_RE = re.compile(r"^Electric plant in service", re.I)
_TOTAL_LABEL_RE = re.compile(r"^Total$", re.I)


@dataclass(frozen=True)
class ProposedCapexAnchor:
    """Single-number 'basis for the ask' anchors, in $ thousands.

    These tie the load-growth forecast to the dollar request: the utility must
    build plant (``electric_plant_in_service``) which enters ``rate_base``;
    applying the allowed return yields the ``revenue_requirement`` increase.
    """

    utility: str
    docket_number: str | None
    source_pdf: str
    revenue_requirement_thousands: float | None
    rate_base_thousands: float | None
    plant_in_service_thousands: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_proposed_capex_anchors(*, database_path: Path | None = None):
    """Return the rate-base / plant / revenue-requirement anchors per utility."""
    pd = _require_pandas()
    db_path = _database_path(database_path)
    conn = connect(db_path)
    try:
        docs = conn.execute(
            "SELECT source_pdf, docket_number, utility FROM proposed_tariff_documents"
            " ORDER BY utility"
        ).fetchall()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()
    records = []
    for doc in docs:
        pdf = Path(str(doc["source_pdf"]))
        if not pdf.exists():
            continue
        anchor = extract_capex_anchors_from_pdf(
            pdf, utility=str(doc["utility"] or ""), docket_number=doc["docket_number"]
        )
        if anchor is not None:
            records.append(anchor.to_dict())
    return pd.DataFrame(records)


def extract_capex_anchors_from_pdf(
    pdf_path: Path | str,
    *,
    utility: str,
    docket_number: str | None = None,
) -> ProposedCapexAnchor | None:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyMuPDF/fitz is required for capex anchor parsing") from exc

    pdf = Path(pdf_path)
    doc = fitz.open(pdf)
    rev_req = rate_base = plant = None
    try:
        for page_number in range(1, doc.page_count + 1):
            text = doc.load_page(page_number - 1).get_text("text") or ""
            upper = text.upper()
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if rev_req is None and _REVREQ_ANCHOR in upper:
                vals = _values_after(lines, _REVREQ_LABEL_RE, limit=8)
                if vals:
                    rev_req = vals[0]
            if _RATEBASE_ANCHOR in upper:
                # 4-column schedules: Total Company / Per Books / Adjustments /
                # As Adjusted (NC retail). The NC-retail figure is column 4.
                # Values are interleaved with standalone "$" lines, so widen
                # the scan window to clear all four columns.
                plant_vals = _values_after(lines, _PLANT_LABEL_RE, limit=12)
                if plant is None and len(plant_vals) >= 4:
                    plant = plant_vals[3]
                base_vals = _values_after(lines, _TOTAL_LABEL_RE, limit=12)
                if rate_base is None and len(base_vals) >= 4:
                    rate_base = base_vals[3]
    finally:
        doc.close()
    if rev_req is None and rate_base is None and plant is None:
        return None
    return ProposedCapexAnchor(
        utility=utility,
        docket_number=docket_number,
        source_pdf=str(pdf),
        revenue_requirement_thousands=rev_req,
        rate_base_thousands=rate_base,
        plant_in_service_thousands=plant,
    )


def _values_after(lines: list[str], label_re: re.Pattern[str], *, limit: int) -> list[float]:
    """Return the numeric run following the first label occurrence that has one.

    Column headers can repeat a label (e.g. ``Total`` over ``Total Company``)
    before the data row; such matches yield no numbers and are skipped so the
    real data row wins.
    """
    for idx, line in enumerate(lines):
        if not label_re.search(line):
            continue
        out: list[float] = []
        for nxt in lines[idx + 1 : idx + 1 + limit]:
            if label_re.search(nxt):
                break
            v = _scalar(nxt)
            if v is not None:
                out.append(v)
        if out:
            return out
    return []


def _scalar(token: str) -> float | None:
    t = token.strip()
    if not re.fullmatch(r"\(?-?[\d,]+(?:\.\d+)?\)?", t):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    # Drop tiny stray footnote refs like a trailing "2".
    if v < 100:
        return None
    return -v if neg else v


def _find_line(lines: list[str], label: str) -> int | None:
    wanted = _normalize_label(label)
    for idx, line in enumerate(lines):
        if _normalize_label(line) == wanted:
            return idx
    return None


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _numeric_values(lines: Iterable[str]) -> list[float]:
    values: list[float] = []
    for line in lines:
        cleaned = line.replace("$", "").replace(",", "").strip()
        if not cleaned or cleaned == "-":
            continue
        if re.fullmatch(r"\[\d+\]", cleaned):
            continue
        negative = cleaned.startswith("(") and cleaned.endswith(")")
        cleaned = cleaned.strip("()")
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
            continue
        value = float(cleaned)
        values.append(-value if negative else value)
    return values


def _align_values(values: list[float], columns: tuple[str, ...]) -> list[float | None]:
    if len(values) == len(columns):
        return values
    if len(values) == 2 and len(columns) > 2:
        return [values[0], *([None] * (len(columns) - 2)), values[1]]
    return [*values[: len(columns)], *([None] * max(0, len(columns) - len(values)))]


def _database_path(database_path: Path | None) -> Path:
    if database_path is not None:
        return Path(database_path)
    from duke_rates.config import get_settings

    return get_settings().database_path
