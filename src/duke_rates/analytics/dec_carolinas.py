from __future__ import annotations

from pathlib import Path
from typing import Any

from duke_rates.analytics.dep_progress import (
    DEFAULT_KWH,
    _connect,
    _docket_sort_value,
    _loads_json_list,
    _safe_float,
    _summarize_block_items,
    _representative_base_metrics,
    _require_pandas,
)


def load_dec_rs_base_history(
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
                   docket_number,
                   revision_label, status, confidence,
                   energy_charges_json, fixed_charges_json
            FROM ncuc_ingest_segments
            WHERE effective_date IS NOT NULL
              AND effective_date BETWEEN ? AND ?
              AND status IN ('parsed', 'partial')
              AND schedule_code = 'RS'
              AND leaf_no = '11'
              AND (
                    docket_dir LIKE 'e-7-%'
                    OR COALESCE(docket_number, '') LIKE 'E-7,%'
                    OR COALESCE(source_pdf, '') LIKE 'data\\raw\\nc\\carolinas\\rate%'
                    OR COALESCE(source_pdf, '') LIKE 'data\\historical\\raw\\nc\\carolinas\\rate%'
                  )
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
                "revision_label": row["revision_label"],
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
        + (df["source_leaf_no"].fillna("") == "11").astype(int)
    )
    df = (
        df.sort_values(
            by=["effective_date", "completeness_score", "docket_sort", "source_row_id"],
            ascending=[True, False, False, False],
        )
        .drop_duplicates(subset=["effective_date"], keep="first")
        .sort_values("effective_date")
        .reset_index(drop=True)
    )
    return df.drop(columns=["docket_sort", "completeness_score"])


def load_dec_rs_all_in_history(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
):
    pd = _require_pandas()
    base_df = load_dec_rs_base_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    rider_totals_df, rider_components_df = load_dec_rs_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )
    if base_df.empty:
        return base_df

    rider_coverage_df = rider_totals_df[
        [
            "effective_date",
            "total_rider_cents_per_kwh",
            "source_pdf",
            "docket_dir",
            "total_source",
            "quality_flag",
        ]
    ].rename(
        columns={
            "effective_date": "rider_effective_date",
            "source_pdf": "rider_source_pdf",
            "docket_dir": "rider_docket_dir",
            "total_source": "rider_total_source",
            "quality_flag": "rider_quality_flag",
        }
    )
    if not rider_coverage_df.empty:
        rider_coverage_df["rider_source_kind"] = "clean"

    all_dates = pd.DataFrame(
        pd.concat([base_df["effective_date"], rider_coverage_df["rider_effective_date"]]).drop_duplicates().sort_values(),
        columns=["effective_date"]
    ).reset_index(drop=True)

    base_merged = pd.merge_asof(
        all_dates,
        base_df.sort_values("effective_date"),
        on="effective_date",
        direction="backward"
    ).dropna(subset=["source_schedule_code"])

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
    merged["summer_all_in_cents_per_kwh"] = (
        merged["summer_base_cents_per_kwh"] + merged["total_rider_cents_per_kwh"]
    )
    merged["winter_all_in_cents_per_kwh"] = (
        merged["winter_base_cents_per_kwh"] + merged["total_rider_cents_per_kwh"]
    )
    merged["blended_all_in_cents_per_kwh"] = (
        merged["blended_base_cents_per_kwh"] + merged["total_rider_cents_per_kwh"]
    )
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
    return merged


def load_dec_rs_rider_history(
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
                   b.source_pdf, b.docket_dir, b.docket_number, b.supersedes, b.applicable_schedules_json
            FROM rider_summary_blocks b
            WHERE b.effective_date IS NOT NULL
              AND b.effective_date BETWEEN ? AND ?
              AND (
                    b.rate_class = 'Residential Schedules'
                    OR b.applicable_schedules_json LIKE '%RS%'
                  )
              AND (
                    b.docket_dir LIKE 'e-7-%'
                    OR COALESCE(b.docket_number, '') LIKE 'E-7,%'
                    OR COALESCE(b.source_pdf, '') LIKE '%DEC-%'
                    OR COALESCE(b.source_pdf, '') LIKE 'data\\processed\\search_leads\\downloads\\dec%'
                    OR COALESCE(b.source_pdf, '') LIKE 'data\\raw\\nc\\carolinas\\rider%'
                    OR COALESCE(b.source_pdf, '') LIKE 'data\\historical\\raw\\nc\\carolinas\\rider%'
                  )
            ORDER BY b.effective_date, b.id
            """,
            (start_date, end_date),
        ).fetchall()

        if not block_rows:
            return pd.DataFrame(), pd.DataFrame()

        line_items_by_block: dict[int, list[Any]] = {}
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
        schedules = _loads_json_list(block["applicable_schedules_json"])
        if schedules and "RS" not in schedules:
            continue

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


# ---------------------------------------------------------------------------
# Tariff-charges-based base history (v2) — reads from tariff_versions +
# tariff_charges rather than the legacy ncuc_ingest_segments table.
# ---------------------------------------------------------------------------

#: DEC RS season → months mapping
#: "july-october" = summer billing season (Jul/Aug/Sep/Oct)
#: "november-june" = winter billing season (Nov/Dec/Jan/Feb/Mar/Apr/May/Jun)
_DEC_RS_SUMMER_MONTHS = {7, 8, 9, 10}
_DEC_RS_WINTER_MONTHS = {11, 12, 1, 2, 3, 4, 5, 6}


def _dec_rs_season_for_month(month: int) -> str:
    """Return 'july-october' or 'november-june' for DEC RS season labels."""
    return "july-october" if month in _DEC_RS_SUMMER_MONTHS else "november-june"


def load_dec_rs_base_history_from_charges(
    *,
    database_path: Path | None = None,
    start_date: str = "2013-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
    representative_month: int = 8,
):
    """Load DEC RS base rate history from tariff_charges (clean DB path).

    Unlike ``load_dec_rs_base_history`` (which uses ncuc_ingest_segments),
    this function reads directly from ``tariff_versions`` + ``tariff_charges``
    for ``nc-carolinas-schedule-RS``.  It covers all registered versions from
    2013-11 through 2026-01+.

    Parameters
    ----------
    representative_kwh:
        kWh used to compute a representative monthly bill figure.
    representative_month:
        Month (1–12) used to select the applicable seasonal energy rate.
        Default 8 (August) → selects "july-october" summer rates.

    Returns
    -------
    DataFrame with columns:
        effective_start, fixed_monthly_charge, energy_rate_$/kwh,
        season_applied, base_bill, source_pdf, docket_dir
    """
    pd = _require_pandas()
    season = _dec_rs_season_for_month(representative_month)

    with _connect(database_path) as conn:
        rows = conn.execute(
            """
            SELECT tv.id AS version_id, tv.effective_start, tv.effective_end,
                   tv.source_pdf, tv.docket_dir, tv.docket_number,
                   tc.charge_type, tc.charge_label, tc.rate_value, tc.rate_unit,
                   tc.season, tc.tier_min, tc.tier_max
            FROM tariff_versions tv
            JOIN tariff_charges tc ON tc.version_id = tv.id
            WHERE tv.family_key = 'nc-carolinas-schedule-RS'
              AND tv.effective_start BETWEEN ? AND ?
            ORDER BY tv.effective_start, tc.charge_type
            """,
            (start_date, end_date),
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    # Group by version
    from collections import defaultdict
    versions: dict[str, dict[str, Any]] = {}
    charges_by_version: dict[str, list] = defaultdict(list)
    for row in rows:
        vs = row["effective_start"]
        if vs not in versions:
            versions[vs] = {
                "effective_start": vs,
                "effective_end": row["effective_end"],
                "source_pdf": row["source_pdf"],
                "docket_dir": row["docket_dir"],
                "docket_number": row["docket_number"],
            }
        charges_by_version[vs].append(row)

    result_rows = []
    for vs, meta in sorted(versions.items()):
        charges = charges_by_version[vs]

        # Fixed charge
        fixed_charges = [c for c in charges if c["charge_type"] == "fixed"]
        fixed_val = fixed_charges[0]["rate_value"] if fixed_charges else None

        # Energy charges: prefer season-matching, fall back to all_year
        energy_charges = [c for c in charges if c["charge_type"] == "energy_block"]
        seasonal = [c for c in energy_charges if c["season"] == season]
        all_year_energy = [c for c in energy_charges if c["season"] in ("all_year", None)]
        applicable_energy = seasonal if seasonal else all_year_energy
        season_label = season if seasonal else "all_year"

        energy_rate = None
        if applicable_energy:
            # DEC RS is a flat single-rate schedule (no tiers)
            energy_rate = applicable_energy[0]["rate_value"]
            # Convert cents/kWh → $/kWh if needed
            unit = (applicable_energy[0]["rate_unit"] or "").lower()
            if "cent" in unit:
                energy_rate = energy_rate / 100.0

        base_bill = None
        if fixed_val is not None and energy_rate is not None:
            base_bill = round(fixed_val + energy_rate * representative_kwh, 2)

        result_rows.append({
            "effective_start": vs,
            "effective_end": meta["effective_end"],
            "fixed_monthly_charge": fixed_val,
            "energy_rate_per_kwh": energy_rate,
            "season_applied": season_label,
            "base_bill": base_bill,
            "source_pdf": meta["source_pdf"],
            "docket_dir": meta["docket_dir"],
        })

    df = pd.DataFrame(result_rows)
    df["effective_start"] = pd.to_datetime(df["effective_start"])
    return df.sort_values("effective_start").reset_index(drop=True)


def load_dec_rs_all_in_history_v2(
    *,
    database_path: Path | None = None,
    start_date: str = "2013-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
    representative_month: int = 8,
):
    """All-in DEC RS bill history: base + riders, using clean tariff_charges data.

    Merges ``load_dec_rs_base_history_from_charges`` with
    ``load_dec_rs_canonical_rider_components`` using a backward-looking
    as-of join so that the most recent prior rider snapshot is used for
    each base rate period.

    Returns a DataFrame with columns:
        effective_start, fixed_monthly_charge, energy_rate_$/kwh,
        base_bill, rider_effective_date, total_rider_cents_per_kwh,
        rider_bill_add, all_in_bill, rider_coverage_status,
        source_pdf, docket_dir
    """
    pd = _require_pandas()
    from duke_rates.analytics.canonical_rider_components import (
        load_dec_rs_canonical_rider_components,
    )

    base_df = load_dec_rs_base_history_from_charges(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
        representative_month=representative_month,
    )
    if base_df.empty:
        return base_df

    rider_df = load_dec_rs_canonical_rider_components(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )

    if rider_df.empty:
        base_df["rider_effective_date"] = pd.NaT
        base_df["total_rider_cents_per_kwh"] = float("nan")
        base_df["rider_bill_add"] = float("nan")
        base_df["all_in_bill"] = base_df["base_bill"]
        base_df["rider_coverage_status"] = "uncovered"
        return base_df

    # Aggregate rider components to total ¢/kWh per effective_date
    rider_totals = (
        rider_df.groupby("effective_date", as_index=False)["cents_per_kwh"]
        .sum()
        .rename(columns={
            "effective_date": "rider_effective_date",
            "cents_per_kwh": "total_rider_cents_per_kwh",
        })
        .sort_values("rider_effective_date")
    )

    # As-of merge: for each base rate period, find the latest prior rider snapshot
    merged = pd.merge_asof(
        base_df.sort_values("effective_start"),
        rider_totals,
        left_on="effective_start",
        right_on="rider_effective_date",
        direction="backward",
    )

    rider_bill_add = merged["total_rider_cents_per_kwh"] * representative_kwh / 100.0
    merged["rider_bill_add"] = rider_bill_add.round(2)
    merged["all_in_bill"] = (merged["base_bill"] + rider_bill_add).round(2)
    merged["rider_coverage_status"] = merged.apply(
        lambda row: (
            "uncovered"
            if pd.isna(row["total_rider_cents_per_kwh"])
            else (
                "same_day"
                if pd.notna(row.get("rider_effective_date"))
                and row["effective_start"] == row["rider_effective_date"]
                else "carried_forward"
            )
        ),
        axis=1,
    )
    return merged.sort_values("effective_start").reset_index(drop=True)


def export_dec_rs_history(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_in_df = load_dec_rs_all_in_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    rider_totals_df, rider_components_df = load_dec_rs_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )
    if all_in_df.empty:
        raise RuntimeError("No DEC RS base history was found in the database.")

    base_df = all_in_df[
        [
            "effective_date",
            "source_pdf",
            "docket_dir",
            "source_schedule_code",
            "source_leaf_no",
            "revision_label",
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
        "base_csv": output_dir / "dec_rs_base_history.csv",
        "rider_totals_csv": output_dir / "dec_rs_rider_totals.csv",
        "rider_components_csv": output_dir / "dec_rs_rider_components.csv",
        "all_in_csv": output_dir / "dec_rs_all_in_history.csv",
    }
    base_df.to_csv(paths["base_csv"], index=False)
    rider_totals_df.to_csv(paths["rider_totals_csv"], index=False)
    rider_components_df.to_csv(paths["rider_components_csv"], index=False)
    all_in_df.to_csv(paths["all_in_csv"], index=False)
    return paths
