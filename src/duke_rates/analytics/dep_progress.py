from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from duke_rates.config import get_settings


MONTH_ALIASES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

RESIDENTIAL_RATE_CLASS = "Residential Service Schedules"
DEFAULT_KWH = 1000.0


def _require_pandas():
    try:
        import pandas as pd
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "pandas is required for duke_rates.analytics.dep_progress. "
            "Install it with `pip install pandas`."
        ) from exc
    return pd


def _database_path(database_path: Path | None) -> Path:
    return database_path or get_settings().database_path


def _connect(database_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(_database_path(database_path))
    conn.row_factory = sqlite3.Row
    return conn


def _loads_json_list(payload: str | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _month_token_to_number(token: str) -> int | None:
    normalized = token.strip().lower().rstrip(".")
    return MONTH_ALIASES.get(normalized)


def _season_matches_month(season: str | None, month: int) -> bool:
    if not season:
        return True
    tokens = re.findall(r"[A-Za-z]+", season)
    months = [_month_token_to_number(token) for token in tokens]
    months = [item for item in months if item is not None]
    if not months:
        return True
    unique_months = list(dict.fromkeys(months))
    if len(unique_months) == 1:
        return month == unique_months[0]
    if len(unique_months) >= 2:
        start = unique_months[0]
        end = unique_months[1]
        if start <= end:
            return start <= month <= end
        return month >= start or month <= end
    return True


def _filter_charges_for_month(
    charges: Iterable[dict[str, Any]],
    month: int,
) -> list[dict[str, Any]]:
    filtered = []
    for charge in charges:
        period = (charge.get("period") or "").strip().lower()
        if period:
            continue
        if _season_matches_month(charge.get("season"), month):
            filtered.append(charge)
    return filtered or [charge for charge in charges if not (charge.get("period") or "").strip()]


def _normalized_block_key(charge: dict[str, Any]) -> tuple[float, float | None]:
    block_from = _safe_float(charge.get("block_from"))
    block_to = _safe_float(charge.get("block_to"))
    return (block_from or 0.0, block_to)


def _sort_energy_charges(charges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        charges,
        key=lambda charge: (
            _safe_float(charge.get("block_from")) or 0.0,
            _safe_float(charge.get("block_to")) or 999999999.0,
            _safe_float(charge.get("rate")) or 0.0,
        ),
    )


def _energy_cost_for_kwh(charges: Iterable[dict[str, Any]], kwh: float) -> float | None:
    rows = _sort_energy_charges(charges)
    if not rows:
        return None

    total = 0.0
    next_block_starts: list[float | None] = []
    for idx, charge in enumerate(rows):
        next_start = None
        current_start = _safe_float(charge.get("block_from")) or 0.0
        for later in rows[idx + 1 :]:
            later_start = _safe_float(later.get("block_from"))
            if later_start is not None and later_start > current_start:
                next_start = later_start
                break
        next_block_starts.append(next_start)

    for charge, next_start in zip(rows, next_block_starts, strict=True):
        rate = _safe_float(charge.get("rate"))
        if rate is None:
            continue
        unit = (charge.get("unit") or "$/kWh").lower()
        if "cent" in unit:
            rate /= 100.0
        block_from = _safe_float(charge.get("block_from")) or 0.0
        block_to = _safe_float(charge.get("block_to"))
        upper = block_to if block_to is not None else next_start
        if upper is None:
            quantity = max(0.0, kwh - block_from)
        else:
            quantity = max(0.0, min(kwh, upper) - block_from)
        total += quantity * rate

    return round(total, 6)


def _basic_customer_charge(charges: Iterable[dict[str, Any]]) -> float | None:
    candidates: list[float] = []
    for charge in charges:
        label = (charge.get("label") or "").lower()
        amount = _safe_float(charge.get("amount"))
        if amount is None:
            continue
        if "basic customer" in label and 5.0 <= amount <= 30.0:
            candidates.append(amount)
    if candidates:
        return min(candidates)
    fallback = [
        _safe_float(charge.get("amount"))
        for charge in charges
        if _safe_float(charge.get("amount")) is not None
    ]
    return min(fallback) if fallback else None


def _representative_base_metrics(
    energy_charges: list[dict[str, Any]],
    fixed_charges: list[dict[str, Any]],
    representative_kwh: float,
) -> dict[str, float | None]:
    fixed = _basic_customer_charge(fixed_charges)
    january = _filter_charges_for_month(energy_charges, 1)
    july = _filter_charges_for_month(energy_charges, 7)
    winter_energy = _energy_cost_for_kwh(january, representative_kwh)
    summer_energy = _energy_cost_for_kwh(july, representative_kwh)

    winter_bill = None if winter_energy is None or fixed is None else fixed + winter_energy
    summer_bill = None if summer_energy is None or fixed is None else fixed + summer_energy

    winter_cents = None if winter_bill is None else winter_bill * 100.0 / representative_kwh
    summer_cents = None if summer_bill is None else summer_bill * 100.0 / representative_kwh

    winter_sorted = _sort_energy_charges(january)
    summer_sorted = _sort_energy_charges(july)

    winter_first = None
    winter_additional = None
    summer_first = None
    if winter_sorted:
        winter_first = _safe_float(winter_sorted[0].get("rate"))
        if winter_first is not None:
            winter_first *= 100.0
        if len(winter_sorted) > 1:
            winter_additional = _safe_float(winter_sorted[1].get("rate"))
            if winter_additional is not None:
                winter_additional *= 100.0
    if summer_sorted:
        summer_first = _safe_float(summer_sorted[0].get("rate"))
        if summer_first is not None:
            summer_first *= 100.0

    blended = None
    if winter_cents is not None and summer_cents is not None:
        blended = (winter_cents + summer_cents) / 2.0
    elif winter_cents is not None:
        blended = winter_cents
    elif summer_cents is not None:
        blended = summer_cents

    return {
        "fixed_monthly_charge": fixed,
        "summer_energy_cents_per_kwh": summer_first,
        "winter_first_block_cents_per_kwh": winter_first,
        "winter_additional_cents_per_kwh": winter_additional,
        "summer_base_bill": summer_bill,
        "winter_base_bill": winter_bill,
        "summer_base_cents_per_kwh": summer_cents,
        "winter_base_cents_per_kwh": winter_cents,
        "blended_base_cents_per_kwh": blended,
    }


def _docket_sort_value(docket_dir: str | None) -> int:
    if not docket_dir:
        return -1
    match = re.search(r"sub-(\d+)", docket_dir)
    if not match:
        return -1
    return int(match.group(1))


def load_dep_res_base_history(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
):
    pd = _require_pandas()

    with _connect(database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, docket_dir, source_pdf, leaf_no, schedule_code, effective_date,
                   revision_label, status, confidence,
                   energy_charges_json, fixed_charges_json
            FROM ncuc_ingest_segments
            WHERE effective_date IS NOT NULL
              AND effective_date BETWEEN ? AND ?
              AND status IN ('parsed', 'partial')
              AND (schedule_code = 'RES' OR schedule_code LIKE 'RES-%')
              AND (leaf_no = '500' OR leaf_no IS NULL)
            ORDER BY effective_date, id
            """,
            (start_date, end_date),
        ).fetchall()

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        energy = _loads_json_list(row["energy_charges_json"])
        fixed = _loads_json_list(row["fixed_charges_json"])
        if not energy:
            continue

        metrics = _representative_base_metrics(energy, fixed, representative_kwh)
        normalized_rows.append(
            {
                "effective_date": row["effective_date"],
                "source_row_id": row["id"],
                "docket_dir": row["docket_dir"],
                "docket_sort": _docket_sort_value(row["docket_dir"]),
                "source_pdf": row["source_pdf"],
                "source_schedule_code": row["schedule_code"],
                "source_leaf_no": row["leaf_no"],
                "confidence": row["confidence"],
                "status": row["status"],
                **metrics,
            }
        )

    if not normalized_rows:
        return pd.DataFrame()

    df = pd.DataFrame(normalized_rows)
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df["completeness_score"] = (
        df["fixed_monthly_charge"].notna().astype(int)
        + df["summer_base_bill"].notna().astype(int)
        + df["winter_base_bill"].notna().astype(int)
        + (df["source_leaf_no"].fillna("") == "500").astype(int)
        + (df["source_schedule_code"].fillna("") == "RES").astype(int)
    )
    df = (
        df.sort_values(
            by=[
                "effective_date",
                "completeness_score",
                "docket_sort",
                "source_row_id",
            ],
            ascending=[True, False, False, False],
        )
        .drop_duplicates(subset=["effective_date"], keep="first")
        .sort_values("effective_date")
        .reset_index(drop=True)
    )
    return df.drop(columns=["docket_sort", "completeness_score"])


def _summarize_block_items(items: list[sqlite3.Row]) -> tuple[dict[str, float], float | None]:
    components: dict[str, float] = {}
    explicit_total = None
    for item in items:
        rider_code = item["rider_code"]
        cents = _safe_float(item["cents_per_kwh"])
        if item["is_total"]:
            if cents is not None and abs(cents) <= 10.0:
                explicit_total = cents
            continue
        if item["is_subtotal"] or not rider_code or cents is None:
            continue
        if abs(cents) > 5.0:
            continue
        components[rider_code] = cents

    derived_total = round(sum(components.values()), 6) if components else None
    total = explicit_total if explicit_total is not None else derived_total
    return components, total


def load_dep_res_rider_history(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
):
    pd = _require_pandas()

    with _connect(database_path) as conn:
        block_rows = conn.execute(
            """
            SELECT b.id, b.effective_date, b.rate_class, b.total_cents_per_kwh,
                   b.source_pdf, b.docket_dir, b.supersedes
            FROM rider_summary_blocks b
            WHERE b.rate_class = ?
              AND b.effective_date IS NOT NULL
              AND b.effective_date BETWEEN ? AND ?
            ORDER BY b.effective_date, b.id
            """,
            (RESIDENTIAL_RATE_CLASS, start_date, end_date),
        ).fetchall()

        if not block_rows:
            return pd.DataFrame(), pd.DataFrame()

        line_items_by_block: dict[int, list[sqlite3.Row]] = {}
        for block_row in block_rows:
            line_items_by_block[block_row["id"]] = conn.execute(
                """
                SELECT li.rider_code, li.label, li.cents_per_kwh, li.is_subtotal, li.is_total
                FROM rider_line_items li
                WHERE li.block_id = ?
                ORDER BY li.id
                """,
                (block_row["id"],),
            ).fetchall()

    blocks: list[dict[str, Any]] = []
    components_long: list[dict[str, Any]] = []

    for block in block_rows:
        items = line_items_by_block.get(block["id"], [])
        components, derived_total = _summarize_block_items(items)
        declared_total = _safe_float(block["total_cents_per_kwh"])
        if declared_total is not None and abs(declared_total) > 10.0:
            declared_total = None
        total = declared_total if declared_total is not None else derived_total
        if total is None:
            continue

        total_source = "explicit_total" if declared_total is not None else "derived_components"
        quality_flag = "ok" if declared_total is not None else "derived_from_partial_components"

        blocks.append(
            {
                "effective_date": block["effective_date"],
                "block_id": block["id"],
                "source_pdf": block["source_pdf"],
                "docket_dir": block["docket_dir"],
                "docket_sort": _docket_sort_value(block["docket_dir"]),
                "quality_sort": 1 if declared_total is not None else 0,
                "supersedes": block["supersedes"],
                "component_count": len(components),
                "total_rider_cents_per_kwh": total,
                "total_source": total_source,
                "quality_flag": quality_flag,
            }
        )
        for rider_code, cents in components.items():
            components_long.append(
                {
                    "effective_date": block["effective_date"],
                    "block_id": block["id"],
                    "source_pdf": block["source_pdf"],
                    "docket_dir": block["docket_dir"],
                    "rider_code": rider_code,
                    "cents_per_kwh": cents,
                }
            )

    if not blocks:
        return pd.DataFrame(), pd.DataFrame()

    blocks_df = pd.DataFrame(blocks)
    blocks_df["effective_date"] = pd.to_datetime(blocks_df["effective_date"])
    blocks_df = (
        blocks_df.sort_values(
            by=["effective_date", "quality_sort", "component_count", "docket_sort", "block_id"],
            ascending=[True, False, False, False, False],
        )
        .drop_duplicates(subset=["effective_date"], keep="first")
        .sort_values("effective_date")
        .reset_index(drop=True)
    )

    component_df = pd.DataFrame(components_long)
    if component_df.empty:
        return blocks_df.drop(columns=["docket_sort"]), component_df

    component_df["effective_date"] = pd.to_datetime(component_df["effective_date"])
    component_df = component_df.merge(
        blocks_df[["effective_date", "block_id"]],
        on=["effective_date", "block_id"],
        how="inner",
    )
    component_df = component_df.sort_values(["effective_date", "rider_code"]).reset_index(drop=True)
    return blocks_df.drop(columns=["docket_sort", "quality_sort"]), component_df


def load_dep_res_all_in_history(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
):
    pd = _require_pandas()
    from duke_rates.analytics.dep_provisional_riders import load_dep_res_provisional_rider_history

    base_df = load_dep_res_base_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    rider_totals_df, rider_components_df = load_dep_res_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )
    provisional_totals_df, _ = load_dep_res_provisional_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=min(end_date, "2022-12-31"),
    )

    if base_df.empty:
        return base_df, rider_totals_df, rider_components_df

    coverage_frames: list[Any] = []
    if not provisional_totals_df.empty:
        provisional_coverage_df = provisional_totals_df[
            [
                "effective_date",
                "provisional_rider_cents_per_kwh",
                "source_pdf",
                "docket_dir",
                "coverage_status",
            ]
        ].rename(
            columns={
                "provisional_rider_cents_per_kwh": "total_rider_cents_per_kwh",
                "source_pdf": "rider_source_pdf",
                "docket_dir": "rider_docket_dir",
                "coverage_status": "rider_total_source",
            }
        )
        provisional_coverage_df["rider_source_kind"] = "provisional"
        provisional_coverage_df["effective_date"] = pd.to_datetime(provisional_coverage_df["effective_date"])
        coverage_frames.append(provisional_coverage_df)

    if not rider_totals_df.empty:
        clean_coverage_df = rider_totals_df[
            [
                "effective_date",
                "total_rider_cents_per_kwh",
                "source_pdf",
                "docket_dir",
                "quality_flag",
                "total_source",
            ]
        ].rename(
            columns={
                "source_pdf": "rider_source_pdf",
                "docket_dir": "rider_docket_dir",
                "quality_flag": "rider_quality_flag",
                "total_source": "rider_total_source",
            }
        )
        clean_coverage_df["rider_source_kind"] = "clean"
        coverage_frames.append(clean_coverage_df)

    if coverage_frames:
        rider_coverage_df = pd.concat(coverage_frames, ignore_index=True, sort=False)
        rider_coverage_df = rider_coverage_df.sort_values(
            by=["effective_date", "rider_source_kind"],
            ascending=[True, True],
        ).reset_index(drop=True)
    else:
        rider_coverage_df = pd.DataFrame(
            columns=[
                "effective_date",
                "total_rider_cents_per_kwh",
                "rider_source_pdf",
                "rider_docket_dir",
                "rider_source_kind",
                "rider_total_source",
                "rider_quality_flag",
            ]
        )

    # Extract all unique dates from base_df and rider_coverage_df
    all_dates = pd.DataFrame(
        pd.concat([base_df["effective_date"], rider_coverage_df["effective_date"]]).drop_duplicates().sort_values(),
        columns=["effective_date"]
    ).reset_index(drop=True)

    # First merge base_df forwards onto the master timeline
    base_merged = pd.merge_asof(
        all_dates,
        base_df.sort_values("effective_date"),
        on="effective_date",
        direction="backward"
    ).dropna(subset=["source_schedule_code"]) # Drop strictly leading dates with no base

    # Now merge rider_coverage_df forwards onto the master timeline
    rider_coverage_df = rider_coverage_df.rename(columns={"effective_date": "rider_effective_date"})
    merged = pd.merge_asof(
        base_merged,
        rider_coverage_df.sort_values("rider_effective_date"),
        left_on="effective_date",
        right_on="rider_effective_date",
        direction="backward",
    )

    rider_bill_add = merged["total_rider_cents_per_kwh"] * representative_kwh / 100.0
    merged["summer_all_in_bill"] = merged["summer_base_bill"] + rider_bill_add
    merged["winter_all_in_bill"] = merged["winter_base_bill"] + rider_bill_add
    merged["summer_all_in_cents_per_kwh"] = merged["summer_base_cents_per_kwh"] + merged[
        "total_rider_cents_per_kwh"
    ]
    merged["winter_all_in_cents_per_kwh"] = merged["winter_base_cents_per_kwh"] + merged[
        "total_rider_cents_per_kwh"
    ]
    merged["blended_all_in_cents_per_kwh"] = merged["blended_base_cents_per_kwh"] + merged[
        "total_rider_cents_per_kwh"
    ]
    merged["rider_coverage_status"] = merged.apply(
        lambda row: (
            "uncovered"
            if pd.isna(row["total_rider_cents_per_kwh"])
            else (
                "same_day"
                if pd.notna(row["rider_effective_date"])
                and row["effective_date"] == row["rider_effective_date"]
                else "carried_forward"
            )
        ),
        axis=1,
    )
    merged["bill_coverage_status"] = merged["total_rider_cents_per_kwh"].apply(
        lambda value: "base_plus_riders" if pd.notna(value) else "base_only"
    )
    return merged, rider_totals_df, rider_components_df


def export_dep_res_history(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_in_df, rider_totals_df, rider_components_df = load_dep_res_all_in_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )

    pd = _require_pandas()
    if all_in_df.empty:
        raise RuntimeError("No DEP RES base history was found in the database.")

    base_df = all_in_df[
        [
            "effective_date",
            "source_pdf",
            "docket_dir",
            "source_schedule_code",
            "source_leaf_no",
            "fixed_monthly_charge",
            "summer_energy_cents_per_kwh",
            "winter_first_block_cents_per_kwh",
            "winter_additional_cents_per_kwh",
            "summer_base_bill",
            "winter_base_bill",
            "summer_base_cents_per_kwh",
            "winter_base_cents_per_kwh",
            "blended_base_cents_per_kwh",
        ]
    ].copy()

    paths = {
        "base_csv": output_dir / "dep_res_base_history.csv",
        "rider_totals_csv": output_dir / "dep_res_rider_totals.csv",
        "rider_components_csv": output_dir / "dep_res_rider_components.csv",
        "all_in_csv": output_dir / "dep_res_all_in_history.csv",
    }

    for frame, path in [
        (base_df, paths["base_csv"]),
        (rider_totals_df, paths["rider_totals_csv"]),
        (rider_components_df, paths["rider_components_csv"]),
        (all_in_df, paths["all_in_csv"]),
    ]:
        if isinstance(frame, pd.DataFrame):
            frame.to_csv(path, index=False)

    return paths
