from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from duke_rates.analytics.dep_progress import _require_pandas
from duke_rates.analytics.dec_carolinas import (
    load_dec_rs_all_in_history,
    load_dec_rs_all_in_history_v2,
    load_dec_rs_base_history,
    load_dec_rs_base_history_from_charges,
    load_dec_rs_rider_history,
)
from duke_rates.analytics.canonical_rider_components import (
    load_dec_rs_canonical_rider_components,
    load_dec_gs_canonical_rider_components,
    load_dec_industrial_canonical_rider_components,
)


KNOWN_PORTAL_CLUES = [
    {
        "date_filed": "2010-10-14",
        "docket_number": "E-7 Sub 909",
        "description": "Duke's Revised Coal Inventory Rider and Summary of Rider Adjustments Effective 10-1-10",
        "source": "data/processed/search_leads/dec_pre_2018_portal.json",
    }
]


def load_dec_rs_validation_report(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = 1000.0,
) -> dict[str, Any]:
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
    all_in_df = load_dec_rs_all_in_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )

    base_dates = (
        sorted(value.strftime("%Y-%m-%d") for value in base_df["effective_date"])
        if not base_df.empty
        else []
    )
    rider_dates = (
        sorted(value.strftime("%Y-%m-%d") for value in rider_totals_df["effective_date"])
        if not rider_totals_df.empty
        else []
    )

    uncovered_base_dates = []
    carried_forward_base_dates = []
    if not all_in_df.empty and "rider_coverage_status" in all_in_df.columns:
        for row in all_in_df.to_dict("records"):
            effective_date = row["effective_date"].strftime("%Y-%m-%d")
            if row["rider_coverage_status"] == "uncovered":
                uncovered_base_dates.append(effective_date)
            elif row["rider_coverage_status"] == "carried_forward":
                carried_forward_base_dates.append(effective_date)

    rider_code_universe = (
        sorted(set(rider_components_df["rider_code"]))
        if not rider_components_df.empty and "rider_code" in rider_components_df.columns
        else []
    )

    summary = {
        "base_distinct_effective_dates": len(base_dates),
        "rider_distinct_effective_dates": len(rider_dates),
        "base_effective_dates": base_dates,
        "rider_effective_dates": rider_dates,
        "uncovered_base_dates": uncovered_base_dates,
        "carried_forward_base_dates": carried_forward_base_dates,
        "known_rider_codes": rider_code_universe,
        "known_portal_clues": KNOWN_PORTAL_CLUES,
    }

    return {
        "summary": summary,
        "all_in_history": all_in_df,
        "rider_totals": rider_totals_df,
        "rider_components": rider_components_df,
    }


def export_dec_rs_validation_report(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = 1000.0,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = load_dec_rs_validation_report(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )

    paths = {
        "summary_json": output_dir / "dec_rs_validation_summary.json",
        "all_in_csv": output_dir / "dec_rs_validation_all_in.csv",
        "rider_totals_csv": output_dir / "dec_rs_validation_rider_totals.csv",
    }

    paths["summary_json"].write_text(json.dumps(report["summary"], indent=2))
    report["all_in_history"].to_csv(paths["all_in_csv"], index=False)
    report["rider_totals"].to_csv(paths["rider_totals_csv"], index=False)
    return paths


def load_dec_rs_validation_report_v2(
    *,
    database_path: Path | None = None,
    start_date: str = "2013-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = 1000.0,
) -> dict[str, Any]:
    """DEC RS validation report using the clean tariff_charges data path (v2).

    Uses ``tariff_versions``/``tariff_charges`` for base rates and
    ``rider_summary_blocks``/``rider_line_items`` (canonical Leaf 600 path)
    for rider components.  This supersedes the legacy ncuc_ingest_segments path.

    Returns a dict with keys:
        ``summary`` — dict of coverage statistics
        ``all_in_history`` — DataFrame from ``load_dec_rs_all_in_history_v2``
        ``rider_components`` — DataFrame from ``load_dec_rs_canonical_rider_components``
    """
    pd = _require_pandas()

    all_in_df = load_dec_rs_all_in_history_v2(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    rider_components_df = load_dec_rs_canonical_rider_components(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )

    base_dates = (
        sorted(all_in_df["effective_start"].dt.strftime("%Y-%m-%d").tolist())
        if not all_in_df.empty
        else []
    )
    rider_dates = (
        sorted(rider_components_df["effective_date"].dt.strftime("%Y-%m-%d").unique().tolist())
        if not rider_components_df.empty
        else []
    )
    uncovered = (
        all_in_df[all_in_df["rider_coverage_status"] == "uncovered"]["effective_start"]
        .dt.strftime("%Y-%m-%d")
        .tolist()
        if not all_in_df.empty
        else []
    )
    carried_forward = (
        all_in_df[all_in_df["rider_coverage_status"] == "carried_forward"]["effective_start"]
        .dt.strftime("%Y-%m-%d")
        .tolist()
        if not all_in_df.empty
        else []
    )
    rider_codes = (
        sorted(rider_components_df["rider_code"].unique().tolist())
        if not rider_components_df.empty
        else []
    )

    summary = {
        "base_distinct_effective_dates": len(base_dates),
        "rider_distinct_effective_dates": len(rider_dates),
        "base_effective_dates": base_dates,
        "rider_effective_dates": rider_dates,
        "uncovered_base_dates": uncovered,
        "carried_forward_base_dates": carried_forward,
        "known_rider_codes": rider_codes,
        "data_source": "tariff_charges + rider_summary_blocks (v2)",
    }

    return {
        "summary": summary,
        "all_in_history": all_in_df,
        "rider_components": rider_components_df,
    }


def export_dec_rs_validation_report_v2(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2013-01-01",
    end_date: str = "2026-12-31",
    representative_kwh: float = 1000.0,
) -> dict[str, Path]:
    """Export DEC RS validation report (v2) to CSV/JSON files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report = load_dec_rs_validation_report_v2(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
        representative_kwh=representative_kwh,
    )
    paths = {
        "summary_json": output_dir / "dec_rs_validation_summary_v2.json",
        "all_in_csv": output_dir / "dec_rs_validation_all_in_v2.csv",
        "rider_components_csv": output_dir / "dec_rs_validation_rider_components_v2.csv",
    }
    paths["summary_json"].write_text(json.dumps(report["summary"], indent=2))
    report["all_in_history"].to_csv(paths["all_in_csv"], index=False)
    report["rider_components"].to_csv(paths["rider_components_csv"], index=False)
    return paths
