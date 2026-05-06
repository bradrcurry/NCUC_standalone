from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from duke_rates.analytics.dep_progress import _connect, _require_pandas
from duke_rates.parse.rider_parser import parse_rider_text


TARGET_RIDER_CODES = ("BA", "JAA", "CPRE", "STS", "SCR")
RES_SCHEDULES = {"RES", "R-TOUD", "R-TOU", "R-TOU-CPP", "R-TOUE", "R-TOU-EV"}
SUPPLEMENTAL_RIDER_ROOTS = (
    Path("data/processed/search_leads/downloads/dep_pre2023_rider_targets"),
    Path("data/processed/search_leads/downloads/e2_sub_1206"),
)


def load_dep_res_provisional_rider_history(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2022-12-31",
):
    """Load DEP RES provisional rider history from the DB cache (dep_provisional_rider_components).

    Reads from the pre-parsed DB table instead of re-parsing PDFs via pdfplumber.
    Falls back to the legacy PDF-parsing path only if the DB table is empty for the
    requested date range.
    """
    pd = _require_pandas()

    with _connect(database_path) as conn:
        rows = conn.execute(
            """
            SELECT
                c.effective_date,
                c.rider_code,
                c.rider_effective_date,
                c.cents_per_kwh,
                c.source_pages,
                c.parser_source,
                c.docket_dir,
                c.source_pdf,
                c.component_source_pdf,
                c.component_source_docket_dir
            FROM dep_provisional_rider_components c
            WHERE c.effective_date BETWEEN ? AND ?
            ORDER BY c.effective_date, c.rider_code, c.rider_effective_date
            """,
            (start_date, end_date),
        ).fetchall()

    if not rows:
        return _load_dep_res_provisional_rider_history_from_pdfs(
            database_path=database_path,
            start_date=start_date,
            end_date=end_date,
        )

    components_df = pd.DataFrame(
        [dict(r) for r in rows],
        columns=[
            "effective_date",
            "rider_code",
            "rider_effective_date",
            "cents_per_kwh",
            "source_pages",
            "parser_source",
            "docket_dir",
            "source_pdf",
            "component_source_pdf",
            "component_source_docket_dir",
        ],
    )
    components_df["effective_date"] = pd.to_datetime(components_df["effective_date"])
    components_df["rider_effective_date"] = pd.to_datetime(
        components_df["rider_effective_date"], format="mixed", errors="coerce"
    )
    components_df = (
        components_df.sort_values(["effective_date", "rider_code", "rider_effective_date"])
        .reset_index(drop=True)
    )

    totals_rows: list[dict[str, Any]] = []
    for effective_date, group in components_df.groupby("effective_date"):
        present_codes = sorted(group["rider_code"].tolist())
        total = round(float(group["cents_per_kwh"].sum()), 6)
        totals_rows.append(
            {
                "effective_date": effective_date,
                "docket_dir": group["docket_dir"].iloc[0],
                "source_pdf": group["source_pdf"].iloc[0],
                "component_count": len(present_codes),
                "component_codes": ",".join(present_codes),
                "provisional_rider_cents_per_kwh": total,
                "coverage_status": "provisional_partial_components",
            }
        )

    totals_df = (
        pd.DataFrame(totals_rows)
        .sort_values("effective_date")
        .reset_index(drop=True)
    )
    return totals_df, components_df


def _load_dep_res_provisional_rider_history_from_pdfs(
    *,
    database_path: Path | None,
    start_date: str,
    end_date: str,
):
    """Legacy PDF-parsing path. Only called when dep_provisional_rider_components is empty."""
    pd = _require_pandas()
    snapshots = _dep_res_snapshot_pdfs(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )
    observation_rows: list[dict[str, Any]] = []

    for snapshot in snapshots:
        for component in _collapse_source_components(_extract_snapshot_components(snapshot["source_pdf"])):
            rider_effective_date = _coerce_observation_rider_effective_date(
                observation_effective_date=snapshot["effective_date"],
                parsed_rider_effective_date=component["rider_effective_date"],
            )
            observation_rows.append(
                {
                    "effective_date": snapshot["effective_date"],
                    "docket_dir": snapshot["docket_dir"],
                    "source_pdf": snapshot["source_pdf"],
                    "rider_code": component["rider_code"],
                    "rider_effective_date": rider_effective_date,
                    "cents_per_kwh": component["cents_per_kwh"],
                    "source_pages": component["source_pages"],
                    "parser_source": component["parser_source"],
                    "source_priority": 0,
                }
            )

    for component in _extract_supplemental_components(
        start_date=start_date,
        end_date=end_date,
    ):
        observation_rows.append(component)

    if not observation_rows:
        return pd.DataFrame(), pd.DataFrame()

    observations_df = pd.DataFrame(observation_rows)
    observations_df["effective_date"] = pd.to_datetime(observations_df["effective_date"])
    observations_df["rider_effective_date"] = pd.to_datetime(observations_df["rider_effective_date"])
    observations_df = (
        observations_df.sort_values(
            ["rider_code", "rider_effective_date", "source_priority", "effective_date"]
        )
        .drop_duplicates(subset=["rider_code", "rider_effective_date"], keep="last")
        .reset_index(drop=True)
    )

    exact_origins = (
        observations_df.sort_values(["effective_date", "source_priority", "rider_effective_date"])
        .drop_duplicates(subset=["effective_date"], keep="last")
        .set_index("effective_date")[["docket_dir", "source_pdf"]]
    )

    carried_rows: list[dict[str, Any]] = []
    snapshot_dates = sorted(observations_df["effective_date"].drop_duplicates().tolist())
    for snapshot_date in snapshot_dates:
        eligible = observations_df[observations_df["rider_effective_date"] <= snapshot_date]
        if eligible.empty:
            continue
        current = (
            eligible.sort_values(["rider_code", "rider_effective_date", "source_priority"])
            .drop_duplicates(subset=["rider_code"], keep="last")
            .copy()
        )
        origin = exact_origins.loc[snapshot_date] if snapshot_date in exact_origins.index else None
        for _, row in current.iterrows():
            carried_rows.append(
                {
                    "effective_date": snapshot_date,
                    "docket_dir": origin["docket_dir"] if origin is not None else row["docket_dir"],
                    "source_pdf": origin["source_pdf"] if origin is not None else row["source_pdf"],
                    "rider_code": row["rider_code"],
                    "rider_effective_date": row["rider_effective_date"],
                    "cents_per_kwh": row["cents_per_kwh"],
                    "source_pages": row["source_pages"],
                    "parser_source": row["parser_source"],
                    "component_source_pdf": row["source_pdf"],
                    "component_source_docket_dir": row["docket_dir"],
                }
            )

    components_df = pd.DataFrame(carried_rows)
    components_df = (
        components_df.sort_values(["effective_date", "rider_code", "rider_effective_date"])
        .reset_index(drop=True)
    )

    totals_rows: list[dict[str, Any]] = []
    for effective_date, group in components_df.groupby("effective_date"):
        present_codes = sorted(group["rider_code"].tolist())
        total = round(float(group["cents_per_kwh"].sum()), 6)
        totals_rows.append(
            {
                "effective_date": effective_date,
                "docket_dir": group["docket_dir"].iloc[0],
                "source_pdf": group["source_pdf"].iloc[0],
                "component_count": len(present_codes),
                "component_codes": ",".join(present_codes),
                "provisional_rider_cents_per_kwh": total,
                "coverage_status": "provisional_partial_components",
            }
        )

    totals_df = (
        pd.DataFrame(totals_rows)
        .sort_values("effective_date")
        .reset_index(drop=True)
    )
    return totals_df, components_df


def load_dep_res_provisional_all_in_history(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2022-12-31",
    representative_kwh: float = 1000.0,
):
    from duke_rates.analytics.dep_progress import load_dep_res_base_history

    pd = _require_pandas()
    base_df = load_dep_res_base_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    totals_df, components_df = load_dep_res_provisional_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )
    if base_df.empty:
        return base_df, totals_df, components_df

    merged = pd.merge_asof(
        base_df.sort_values("effective_date"),
        totals_df[
            [
                "effective_date",
                "docket_dir",
                "source_pdf",
                "component_count",
                "component_codes",
                "provisional_rider_cents_per_kwh",
                "coverage_status",
            ]
        ]
        .rename(
            columns={
                "effective_date": "provisional_effective_date",
                "docket_dir": "docket_dir_provisional",
                "source_pdf": "source_pdf_provisional",
            }
        )
        .sort_values("provisional_effective_date"),
        left_on="effective_date",
        right_on="provisional_effective_date",
        direction="backward",
    )
    rider_bill_add = merged["provisional_rider_cents_per_kwh"] * representative_kwh / 100.0
    merged["provisional_summer_all_in_bill"] = merged["summer_base_bill"] + rider_bill_add
    merged["provisional_winter_all_in_bill"] = merged["winter_base_bill"] + rider_bill_add
    merged["provisional_blended_all_in_cents_per_kwh"] = (
        merged["blended_base_cents_per_kwh"] + merged["provisional_rider_cents_per_kwh"]
    )
    merged["provisional_rider_status"] = merged["coverage_status"].fillna("no_provisional_riders")
    return merged, totals_df, components_df


def export_dep_res_provisional_rider_history(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2022-12-31",
    representative_kwh: float = 1000.0,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_in_df, totals_df, components_df = load_dep_res_provisional_all_in_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    if all_in_df.empty:
        raise RuntimeError("No provisional DEP RES rider history could be derived.")

    paths = {
        "provisional_rider_totals_csv": output_dir / "dep_res_provisional_rider_totals.csv",
        "provisional_rider_components_csv": output_dir / "dep_res_provisional_rider_components.csv",
        "provisional_all_in_csv": output_dir / "dep_res_provisional_all_in_history.csv",
    }
    totals_df.to_csv(paths["provisional_rider_totals_csv"], index=False)
    components_df.to_csv(paths["provisional_rider_components_csv"], index=False)
    all_in_df.to_csv(paths["provisional_all_in_csv"], index=False)
    return paths


def _dep_res_snapshot_pdfs(
    *,
    database_path: Path | None,
    start_date: str,
    end_date: str,
) -> list[dict[str, str]]:
    with _connect(database_path) as conn:
        rows = conn.execute(
            """
            SELECT effective_date, docket_dir, source_pdf, revision_label, id
            FROM ncuc_ingest_segments
            WHERE schedule_code = 'RES'
              AND status IN ('parsed', 'partial')
              AND effective_date BETWEEN ? AND ?
              AND json_extract(energy_charges_json, '$[0].rate') BETWEEN 0.05 AND 0.20
            ORDER BY effective_date, id
            """,
            (start_date, end_date),
        ).fetchall()

    deduped: list[dict[str, str]] = []
    seen_dates: set[str] = set()
    for row in rows:
        eff = row["effective_date"]
        if eff in seen_dates:
            continue
        seen_dates.add(eff)
        deduped.append(
            {
                "effective_date": eff,
                "docket_dir": row["docket_dir"],
                "source_pdf": row["source_pdf"],
                "revision_label": row["revision_label"],
            }
        )
    return deduped


def _extract_snapshot_components(source_pdf: str) -> list[dict[str, Any]]:
    try:
        import pdfplumber  # type: ignore
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("pdfplumber is required for provisional DEP rider backfill.") from exc

    pdf_path = Path(source_pdf)
    with pdfplumber.open(pdf_path) as pdf:
        page_text = [page.extract_text() or "" for page in pdf.pages]

    title_pages = _find_rider_title_pages(page_text)
    components: list[dict[str, Any]] = []
    for idx, (page_no, rider_code, title) in enumerate(title_pages):
        if rider_code not in TARGET_RIDER_CODES:
            continue
        next_page = title_pages[idx + 1][0] - 1 if idx + 1 < len(title_pages) else page_no
        text = "\n".join(page_text[page_no - 1 : next_page])
        parsed = parse_rider_text(
            document_id=0,
            title=title,
            state="NC",
            company="progress",
            text=text,
        )
        cents = _extract_residential_cents(parsed.rider, rider_code, text)
        rider_effective_date = parsed.rider.effective_date
        if cents is None or rider_effective_date is None:
            continue
        components.append(
            {
                "rider_code": rider_code,
                "rider_effective_date": rider_effective_date,
                "cents_per_kwh": cents,
                "source_pages": f"{page_no}-{next_page}",
                "parser_source": title,
            }
        )
    return components


def _extract_supplemental_components(
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    start_key = start_date[:10]
    end_key = end_date[:10]

    for root in SUPPLEMENTAL_RIDER_ROOTS:
        if not root.exists():
            continue
        for pdf_path in sorted(root.glob("*.pdf")):
            filename = pdf_path.name.lower()
            if not any(token in filename for token in ("rider", "tariff", "compliance")):
                continue
            if any(token in filename for token in ("corrected rider", "correction", "application")):
                continue
            try:
                extracted = _collapse_source_components(_extract_snapshot_components(str(pdf_path)))
            except Exception:
                continue
            for component in extracted:
                effective_key = _normalize_effective_date_key(component["rider_effective_date"])
                rider_effective_date = _coerce_observation_rider_effective_date(
                    observation_effective_date=effective_key,
                    parsed_rider_effective_date=component["rider_effective_date"],
                )
                if not effective_key or not (start_key <= effective_key <= end_key):
                    continue
                components.append(
                    {
                        "effective_date": effective_key,
                        "docket_dir": pdf_path.parent.name,
                        "source_pdf": str(pdf_path),
                        "rider_code": component["rider_code"],
                        "rider_effective_date": rider_effective_date,
                        "cents_per_kwh": component["cents_per_kwh"],
                        "source_pages": component["source_pages"],
                        "parser_source": component["parser_source"],
                        "source_priority": 1,
                    }
                )
    return components


def _find_rider_title_pages(page_text: list[str]) -> list[tuple[int, str, str]]:
    title_pages: list[tuple[int, str, str]] = []
    for idx, text in enumerate(page_text, start=1):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        top = " | ".join(lines[:5]).upper()
        if "RR-" not in top or "DUKE ENERGY PROGRESS" not in top:
            continue
        code = _rider_code_from_top(top)
        if not code:
            continue
        title_pages.append((idx, code, " | ".join(lines[:4])))
    return title_pages


def _rider_code_from_top(top: str) -> str | None:
    match = re.search(r"RIDER\s+([A-Z]+(?:-[A-Z0-9]+)?)", top)
    if match:
        return match.group(1).split("-")[0]
    if "STORM COST RECOVERY" in top:
        return "SCR"
    if "STORM SECURITIZATION" in top:
        return "STS"
    return None


def _extract_residential_cents(rider, rider_code: str, text: str) -> float | None:
    for component in rider.charge_components:
        schedules = set(component.applicable_schedules or [])
        if schedules & RES_SCHEDULES:
            if component.unit == "cents_per_kwh":
                return float(component.value)

    for row in rider.adjustment_rows:
        if row.rate_class == "Residential" and row.net_adjustment_cents_per_kwh is not None:
            return float(row.net_adjustment_cents_per_kwh)

    if rider_code == "SCR":
        match = re.search(
            r"applicable kilowatt-hour rider increment.*?is\s+(-?\d+(?:\.\d+)?)¢\s+per\s+kilowatt-hour",
            text,
            re.I | re.S,
        )
        if match:
            return float(match.group(1))
    return None


def _normalize_effective_date_key(raw: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.search(
        r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
        text,
    )
    if not match:
        return None
    month_name, day, year = match.groups()
    month_lookup = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
    }
    month = month_lookup.get(month_name.lower())
    if not month:
        return None
    return f"{year}-{month}-{int(day):02d}"


def _coerce_observation_rider_effective_date(
    *,
    observation_effective_date: str,
    parsed_rider_effective_date: str | None,
) -> str:
    parsed_key = _normalize_effective_date_key(parsed_rider_effective_date)
    if not parsed_key:
        return observation_effective_date
    try:
        observed_dt = datetime.strptime(observation_effective_date, "%Y-%m-%d")
        parsed_dt = datetime.strptime(parsed_key, "%Y-%m-%d")
    except ValueError:
        return observation_effective_date
    if abs((observed_dt - parsed_dt).days) > 120:
        return observation_effective_date
    return parsed_key


def _collapse_source_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        grouped.setdefault(component["rider_code"], []).append(component)

    collapsed: list[dict[str, Any]] = []
    for rider_code, items in grouped.items():
        items = sorted(
            items,
            key=lambda item: (
                _normalize_effective_date_key(item.get("rider_effective_date")) or "",
                _page_sort_key(item.get("source_pages")),
            ),
        )
        collapsed.append(items[-1])
    return collapsed


def _page_sort_key(source_pages: str | None) -> int:
    if not source_pages:
        return -1
    match = re.match(r"(\d+)", str(source_pages))
    return int(match.group(1)) if match else -1
