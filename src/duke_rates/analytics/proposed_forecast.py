"""Harvest Duke's multi-year load & customer forecast tables from the proposed
rate-case application PDFs (DEC E-7 Sub 1329, DEP E-2 Sub 1380).

Both filings carry a "Spring 2025 Forecast" suite — Customer Growth, Forecasted
Retail Sales by class, a Gross-to-Net decomposition (Energy Efficiency, Rooftop
Solar, Electric Vehicles, Voltage Control, **Economic Development**), and a
Winter/Summer peak forecast. These tables are the load basis for the rate ask.

Two extraction wrinkles handled here:

* The DEP forecast pages render their *titles* (and the peak table's numbers)
  in an embedded Calibri subset whose glyphs land in the Coptic block. The
  digits map cleanly (U+03EC–U+03F5 -> 0–9, U+0355 -> comma), so ``_degarble``
  restores them; data rows on the other tables are plain Times and need no fix.
* Because titles are unreliable, tables are classified by **values-per-year
  row** (7 = gross-to-net, 5 + "GWh" header = retail sales, 5 = customer growth,
  2 = peak), gated on a page having a long run of sequential year rows so dense
  financial schedules are never mistaken for a forecast.

Read-only, on demand (mirrors ``proposed_revenue``): no new tables, no writes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from duke_rates.analytics.dep_progress import _require_pandas
from duke_rates.db.sqlite import connect

# Calibri-subset glyphs (Coptic block) used by the DEP forecast pages.
_DEGARBLE = {0x03EC + i: str(i) for i in range(10)}
_DEGARBLE[0x0355] = ","
_DEGARBLE_TABLE = {k: ord(v) for k, v in _DEGARBLE.items()}

_YEAR_RE = re.compile(r"^(20[2-4]\d)$")
_NUM_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
_LABEL_RE = re.compile(r"\b(Spring|Summer|Fall|Winter)\s+(20\d{2})\s+Forecast", re.I)

# Column schema (left-to-right) per table type.
SCHEMAS: dict[str, tuple[str, ...]] = {
    "customer_growth": ("Residential", "Commercial", "Industrial", "Lighting", "Total"),
    "retail_sales": ("Residential", "Commercial", "Industrial", "Other", "Total"),
    "gross_to_net": (
        "Gross Retail Sales",
        "Energy Efficiency",
        "Rooftop Solar",
        "Electric Vehicles",
        "Voltage Control",
        "Economic Development",
        "Net Retail Sales",
    ),
    "peak": ("Winter", "Summer"),
}
UNITS = {
    "customer_growth": "customers_thousands",
    "retail_sales": "gwh",
    "gross_to_net": "gwh",
    "peak": "mw",
}


@dataclass(frozen=True)
class ForecastPoint:
    utility: str
    docket_number: str | None
    source_pdf: str
    source_page: int
    forecast_label: str
    table_type: str
    year: int
    segment: str
    value: float
    unit: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _degarble(text: str) -> str:
    return (text or "").translate(_DEGARBLE_TABLE)


def _parse_num(token: str) -> float | None:
    t = token.strip()
    if not _NUM_RE.match(t):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "")
    if t in {"", "-"}:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _year_value_runs(lines: list[str]) -> list[tuple[int, list[float]]]:
    """Return (year, [values…]) for each year row, reading the consecutive
    numeric tokens that follow until the next year row."""
    runs: list[tuple[int, list[float]]] = []
    i = 0
    while i < len(lines):
        ym = _YEAR_RE.match(lines[i])
        if not ym:
            i += 1
            continue
        year = int(ym.group(1))
        vals: list[float] = []
        j = i + 1
        while j < len(lines):
            if _YEAR_RE.match(lines[j]):
                break
            v = _parse_num(lines[j])
            if v is not None:
                vals.append(v)
            j += 1
        runs.append((year, vals))
        i = j
    return runs


def classify_table(text: str, runs: list[tuple[int, list[float]]]) -> str | None:
    """Classify a forecast page by its values-per-year and header keywords.

    Requires a long run of sequential year rows so dense financial schedules
    (which also contain years) are never misread as a forecast.
    """
    years = [y for y, _ in runs]
    if len(years) < 6 or years != sorted(years):
        return None
    # Modal column count across year rows.
    counts = [len(v) for _, v in runs if v]
    if not counts:
        return None
    k = max(set(counts), key=counts.count)
    low = text.lower()
    if "rooftop" in low or k == 7:
        return "gross_to_net"
    if k == 2:
        return "peak"
    if k == 5 and "gwh" in low:
        return "retail_sales"
    if k == 5:
        return "customer_growth"
    return None


def parse_forecast_page(text: str) -> tuple[str | None, list[tuple[int, str, float]]]:
    """Return ``(table_type, [(year, segment, value)…])`` for one page."""
    degarbled = _degarble(text)
    lines = [ln.strip() for ln in degarbled.splitlines() if ln.strip()]
    runs = _year_value_runs(lines)
    table_type = classify_table(degarbled, runs)
    if table_type is None:
        return None, []
    schema = SCHEMAS[table_type]
    out: list[tuple[int, str, float]] = []
    for year, vals in runs:
        if len(vals) != len(schema):
            continue
        for seg, val in zip(schema, vals):
            out.append((year, seg, val))
    return table_type, out


def extract_forecast_from_pdf(
    pdf_path: Path | str,
    *,
    utility: str,
    docket_number: str | None = None,
) -> list[ForecastPoint]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyMuPDF/fitz is required for forecast parsing") from exc

    pdf = Path(pdf_path)
    doc = fitz.open(pdf)
    points: list[ForecastPoint] = []
    label = "Spring 2025"
    try:
        # First pass: recover the forecast label from any clean title.
        for page_number in range(1, doc.page_count + 1):
            m = _LABEL_RE.search(doc.load_page(page_number - 1).get_text("text") or "")
            if m:
                label = f"{m.group(1).title()} {m.group(2)}"
                break
        for page_number in range(1, doc.page_count + 1):
            text = doc.load_page(page_number - 1).get_text("text") or ""
            table_type, rows = parse_forecast_page(text)
            if not table_type:
                continue
            unit = UNITS[table_type]
            for year, segment, value in rows:
                points.append(
                    ForecastPoint(
                        utility=utility,
                        docket_number=docket_number,
                        source_pdf=str(pdf),
                        source_page=page_number,
                        forecast_label=label,
                        table_type=table_type,
                        year=year,
                        segment=segment,
                        value=value,
                        unit=unit,
                    )
                )
    finally:
        doc.close()
    return points


def load_proposed_load_forecast(*, database_path: Path | None = None):
    """Return the harvested forecast points for every registered proposed PDF."""
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
        pdf = Path(str(doc["source_pdf"]))
        if not pdf.exists():
            continue
        for point in extract_forecast_from_pdf(
            pdf,
            utility=_utility_code(str(doc["utility"] or "")),
            docket_number=doc["docket_number"],
        ):
            records.append(point.to_dict())
    return pd.DataFrame(records)


_UTILITY_CODES = {"Duke Energy Progress": "DEP", "Duke Energy Carolinas": "DEC"}


def _utility_code(utility_name: str) -> str:
    return _UTILITY_CODES.get(utility_name, utility_name)


def _database_path(database_path: Path | None) -> Path:
    if database_path is not None:
        return Path(database_path)
    from duke_rates.config import get_settings

    return get_settings().database_path
