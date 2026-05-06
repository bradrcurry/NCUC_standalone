from __future__ import annotations

from pathlib import Path

import pandas as pd

from duke_rates.analytics.dec_carolinas import load_dec_rs_all_in_history
from duke_rates.analytics.dep_progress import load_dep_res_all_in_history


def load_residential_comparison_history(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = 1000.0,
):
    import pandas as pd

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

    columns = [
        "effective_date",
        "utility",
        "schedule",
        "blended_base_cents_per_kwh",
        "total_rider_cents_per_kwh",
        "blended_all_in_cents_per_kwh",
        "rider_coverage_status",
        "bill_coverage_status",
        "rider_effective_date",
        "rider_source_kind",
        "source_pdf",
        "docket_dir",
    ]
    numeric_columns = [
        "blended_base_cents_per_kwh",
        "total_rider_cents_per_kwh",
        "blended_all_in_cents_per_kwh",
    ]
    frames = []
    if not dep_df.empty:
        temp = dep_df.copy()
        temp["utility"] = "DEP"
        temp["schedule"] = "RES"
        temp["effective_date"] = pd.to_datetime(temp["effective_date"])
        for column in numeric_columns:
            temp[column] = pd.to_numeric(temp[column], errors="coerce")
        frames.append(temp[columns])
    if not dec_df.empty:
        temp = dec_df.copy()
        temp["utility"] = "DEC"
        temp["schedule"] = "RS"
        temp["effective_date"] = pd.to_datetime(temp["effective_date"])
        for column in numeric_columns:
            temp[column] = pd.to_numeric(temp[column], errors="coerce")
        frames.append(temp[columns])
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined["comparison_metric_name"] = combined["bill_coverage_status"].apply(
        lambda status: "Blended All-In Cents per kWh" if status == "base_plus_riders" else "Blended Base Cents per kWh"
    )
    combined["comparison_metric_value"] = combined["blended_all_in_cents_per_kwh"].where(
        combined["bill_coverage_status"] == "base_plus_riders",
        combined["blended_base_cents_per_kwh"],
    )
    combined["metric_name"] = combined["comparison_metric_name"]
    combined["metric_value"] = combined["comparison_metric_value"]
    return combined
