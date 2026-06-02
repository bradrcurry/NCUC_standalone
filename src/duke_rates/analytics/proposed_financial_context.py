from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from duke_rates.analytics.dep_progress import _require_pandas


DEC_E7_SUB_1329_WORKPAPERS = (
    "data/raw/historical/ncuc/e-7/"
    "e-7-nodate-e-7-sub-1329-initial-filing-candidate-2025-11-20-attachment-3.pdf",
    "data/raw/historical/ncuc/e-7/"
    "e-7-nodate-e-7-sub-1329-initial-filing-candidate-2025-11-20-attachment-4.pdf",
)

_CLASS_CODES = {
    "Jur Retail": "Jurisdictional Retail",
    "NCRS": "Residential Service",
    "NCRT": "Residential Time-of-Use",
    "NCRE": "Residential Energy",
    "NCSGS": "Small General Service",
    "NCMGS": "Medium General Service",
    "NCLGS": "Large General Service",
    "NCOL": "Outdoor Lighting",
    "NCNL": "Non-Roadway Lighting",
    "NCPL": "Public Lighting",
    "NCTS": "Traffic Signal",
    "NCI": "Industrial",
    "OPTVSecSmall": "Optional TOU Secondary Small",
    "OPTVSecMed": "Optional TOU Secondary Medium",
    "OPTVSecLg": "Optional TOU Secondary Large",
    "OPTVPriSmall": "Optional TOU Primary Small",
    "OPTVPriMed": "Optional TOU Primary Medium",
    "OPTVPriLg": "Optional TOU Primary Large",
    "OPTVTransLg": "Optional TOU Transmission Large",
}


@dataclass(frozen=True)
class ProposedClassRevenueImpact:
    utility: str
    docket_number: str
    scenario_label: str
    source_pdf: str
    source_page: int
    class_code: str
    class_name: str
    proposed_increase_millions: float
    proposed_revenue_millions: float | None
    current_revenue_millions: float | None
    percent_increase: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_proposed_class_revenue_impacts(*, root: Path | None = None):
    """Return class-level proposed revenue impacts from local E-1 workpapers.

    The workpapers state dollars in thousands. Returned values are normalized to
    millions so they can be read alongside the application Exhibit 1 summary.
    These are proposed figures only; nothing here is an approved rate.
    """
    pd = _require_pandas()
    base = Path(root) if root is not None else Path.cwd()
    rows: list[dict[str, object]] = []
    for pdf_name in DEC_E7_SUB_1329_WORKPAPERS:
        pdf_path = base / pdf_name
        if pdf_path.exists():
            rows.extend(row.to_dict() for row in extract_class_revenue_impacts_from_pdf(pdf_path))
    return pd.DataFrame(rows)


def extract_class_revenue_impacts_from_pdf(
    pdf_path: Path | str,
) -> list[ProposedClassRevenueImpact]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is required for proposed financial context parsing") from exc

    pdf = Path(pdf_path)
    rows: list[ProposedClassRevenueImpact] = []
    doc = fitz.open(pdf)
    try:
        for page_number in range(1, doc.page_count + 1):
            text = doc.load_page(page_number - 1).get_text("text") or ""
            row = parse_class_revenue_impact_page(
                text,
                source_pdf=str(pdf),
                source_page=page_number,
            )
            if row is not None:
                rows.append(row)
    finally:
        doc.close()
    return rows


def parse_class_revenue_impact_page(
    text: str,
    *,
    source_pdf: str = "",
    source_page: int = 0,
) -> ProposedClassRevenueImpact | None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not any("PR REV-PROPOSED INCREASE" in line for line in lines):
        return None

    scenario = _scenario_label(lines)
    class_code = _class_code(lines)
    if scenario is None or class_code is None:
        return None

    increase_values = _row_values(lines, "PR REV-PROPOSED INCREASE", max_values=9)
    if not increase_values:
        return None
    increase_thousands = increase_values[0]
    if increase_thousands == 0:
        return None

    proposed_values = _row_values(lines, "ELECTRIC OPERATING REVENUE - PROPOSED", max_values=9)
    proposed_thousands = proposed_values[0] if proposed_values else None
    current_thousands = (
        proposed_thousands - increase_thousands
        if proposed_thousands is not None
        else None
    )
    percent_increase = (
        (increase_thousands / current_thousands) * 100
        if current_thousands not in (None, 0)
        else None
    )

    return ProposedClassRevenueImpact(
        utility="Duke Energy Carolinas",
        docket_number="E-7 Sub 1329",
        scenario_label=scenario,
        source_pdf=source_pdf,
        source_page=source_page,
        class_code=class_code,
        class_name=_CLASS_CODES.get(class_code, class_code),
        proposed_increase_millions=round(increase_thousands / 1000, 3),
        proposed_revenue_millions=(
            round(proposed_thousands / 1000, 3) if proposed_thousands is not None else None
        ),
        current_revenue_millions=(
            round(current_thousands / 1000, 3) if current_thousands is not None else None
        ),
        percent_increase=round(percent_increase, 2) if percent_increase is not None else None,
    )


def _scenario_label(lines: Iterable[str]) -> str | None:
    text = "\n".join(lines)
    if re.search(r"FINAL ASK:\s*BASE", text, re.I):
        return "Traditional / base ask"
    if re.search(r"FINAL ASK:\s*MYRP YEAR 1", text, re.I):
        return "MYRP Rate Year 1"
    if re.search(r"FINAL ASK:\s*MYRP YEAR 2", text, re.I):
        return "MYRP Rate Year 2"
    return None


def _class_code(lines: list[str]) -> str | None:
    header = lines[:30]
    for idx, line in enumerate(header):
        if line == "Function Allocator" and idx + 1 < len(header):
            candidate = header[idx + 1]
            if candidate in _CLASS_CODES:
                return candidate
    for line in header:
        if line in _CLASS_CODES:
            return line
    return None


def _row_values(lines: list[str], label: str, *, max_values: int) -> list[float]:
    for idx, line in enumerate(lines):
        if label in line:
            values: list[float] = []
            for candidate in lines[idx + 1 :]:
                value = _parse_number(candidate)
                if value is None:
                    break
                values.append(value)
                if len(values) >= max_values:
                    break
            return values
    return []


def _parse_number(value: str) -> float | None:
    cleaned = value.replace(",", "").replace("$", "").strip()
    if cleaned == "-":
        return 0.0
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    parsed = float(cleaned)
    return -parsed if negative else parsed
