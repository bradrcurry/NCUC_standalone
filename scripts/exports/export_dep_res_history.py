from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from duke_rates.analytics.dep_progress import export_dep_res_history, load_dep_res_all_in_history
from duke_rates.analytics.dep_provisional_riders import (
    export_dep_res_provisional_rider_history,
    load_dep_res_provisional_rider_history,
)
from duke_rates.analytics.dep_validation import export_dep_res_validation_report
from duke_rates.charts import rate_history_chart, rider_stack_chart


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export DEP RES rate and rider history from the SQLite database.")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/db/duke_rates.db"),
        help="Path to the SQLite database.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed/dep_res_history"),
        help="Directory for CSV and optional HTML outputs.",
    )
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date", default="2026-12-31")
    parser.add_argument(
        "--representative-kwh",
        type=float,
        default=1000.0,
        help="Representative monthly usage used for base/all-in bill normalization.",
    )
    parser.add_argument(
        "--write-html",
        action="store_true",
        help="Write Plotly HTML charts if plotly is installed.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_paths = export_dep_res_history(
        args.output_dir,
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
    )

    print("Wrote CSV exports:")
    for label, path in output_paths.items():
        print(f"  {label}: {path}")

    all_in_df, rider_totals_df, rider_components_df = load_dep_res_all_in_history(
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
    )

    print()
    print(
        "Coverage summary:",
        f"base rows={len(all_in_df)}",
        f"rider total rows={len(rider_totals_df)}",
        f"rider component rows={len(rider_components_df)}",
    )

    provisional_paths = export_dep_res_provisional_rider_history(
        args.output_dir,
        database_path=args.database,
        start_date=args.start_date,
        end_date=min(args.end_date, "2022-12-31"),
        representative_kwh=args.representative_kwh,
    )
    provisional_totals_df, provisional_components_df = load_dep_res_provisional_rider_history(
        database_path=args.database,
        start_date=args.start_date,
        end_date=min(args.end_date, "2022-12-31"),
    )
    print(
        "Provisional pre-Leaf-600 backfill:",
        f"rider total rows={len(provisional_totals_df)}",
        f"rider component rows={len(provisional_components_df)}",
    )
    for label, path in provisional_paths.items():
        print(f"  {label}: {path}")

    validation_paths = export_dep_res_validation_report(
        args.output_dir,
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print("Validation report:")
    for label, path in validation_paths.items():
        print(f"  {label}: {path}")

    if not args.write_html:
        return

    try:
        rate_figure = rate_history_chart(
            all_in_df,
            filters={
                "title": "DEP RES Base, Rider, and All-In History",
                "columns": [
                    "summer_base_cents_per_kwh",
                    "winter_base_cents_per_kwh",
                    "total_rider_cents_per_kwh",
                    "blended_all_in_cents_per_kwh",
                ],
            },
        )
        rider_figure = rider_stack_chart(
            rider_components_df,
            utility="Duke Energy Progress",
            schedule="RES",
        )
    except ModuleNotFoundError as exc:
        print()
        print(f"Skipping HTML charts: {exc}")
        return

    rate_path = args.output_dir / "dep_res_rate_history.html"
    rider_path = args.output_dir / "dep_res_rider_stack.html"
    rate_figure.write_html(rate_path)
    rider_figure.write_html(rider_path)
    print()
    print("Wrote HTML charts:")
    print(f"  rate_history: {rate_path}")
    print(f"  rider_stack: {rider_path}")


if __name__ == "__main__":
    main()
