from __future__ import annotations

from pathlib import Path

from duke_rates.analytics.dep_progress import DEFAULT_KWH, _require_pandas
from duke_rates.analytics.dec_carolinas import load_dec_rs_all_in_history
from duke_rates.analytics.dep_progress import load_dep_res_all_in_history


def load_canonical_residential_timeline(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
):
    pd = _require_pandas()

    dep_df, _, _ = load_dep_res_all_in_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    dec_df = load_dec_rs_all_in_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )

    frames = []
    if not dep_df.empty:
        frames.append(_canonicalize_utility_frame(pd, dep_df, utility="DEP", schedule="RES"))
    if not dec_df.empty:
        frames.append(_canonicalize_utility_frame(pd, dec_df, utility="DEC", schedule="RS"))
    if not frames:
        return pd.DataFrame()

    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["utility", "effective_date"])
        .reset_index(drop=True)
    )


def export_canonical_residential_timeline(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = DEFAULT_KWH,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical_df = load_canonical_residential_timeline(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    paths = {
        "canonical_csv": output_dir / "canonical_residential_timeline.csv",
    }
    canonical_df.to_csv(paths["canonical_csv"], index=False)
    return paths


def _canonicalize_utility_frame(pd, df, *, utility: str, schedule: str):
    canonical = df.copy()
    canonical["effective_date"] = pd.to_datetime(canonical["effective_date"])
    if "rider_effective_date" in canonical.columns:
        canonical["rider_effective_date"] = pd.to_datetime(canonical["rider_effective_date"])
    canonical["utility"] = utility
    canonical["schedule"] = schedule
    canonical["representative_kwh"] = canonical.get("representative_kwh", DEFAULT_KWH)
    canonical["base_cents_per_kwh"] = canonical["blended_base_cents_per_kwh"]
    canonical["rider_cents_per_kwh"] = canonical["total_rider_cents_per_kwh"]
    canonical["all_in_cents_per_kwh"] = canonical["blended_all_in_cents_per_kwh"]
    canonical["base_bill_amount"] = (
        canonical["summer_base_bill"].combine_first(canonical.get("winter_base_bill"))
        if "summer_base_bill" in canonical.columns
        else None
    )
    canonical["all_in_bill_amount"] = (
        canonical["summer_all_in_bill"].combine_first(canonical.get("winter_all_in_bill"))
        if "summer_all_in_bill" in canonical.columns
        else None
    )
    canonical["timeline_kind"] = canonical["bill_coverage_status"].map(
        {
            "base_plus_riders": "all_in",
            "base_only": "base_only",
        }
    ).fillna("unknown")

    columns = [
        "utility",
        "schedule",
        "effective_date",
        "rider_effective_date",
        "representative_kwh",
        "base_cents_per_kwh",
        "rider_cents_per_kwh",
        "all_in_cents_per_kwh",
        "base_bill_amount",
        "all_in_bill_amount",
        "fixed_monthly_charge",
        "rider_coverage_status",
        "bill_coverage_status",
        "timeline_kind",
        "rider_source_kind",
        "rider_total_source",
        "rider_quality_flag",
        "source_pdf",
        "docket_dir",
        "rider_source_pdf",
        "rider_docket_dir",
    ]
    available_columns = [column for column in columns if column in canonical.columns]
    return canonical[available_columns]
