from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from duke_rates.analytics.dec_carolinas import (
    export_dec_rs_history,
    load_dec_rs_all_in_history,
    load_dec_rs_rider_history,
)
from duke_rates.analytics.dec_validation import export_dec_rs_validation_report
from duke_rates.analytics.regional import load_residential_comparison_history
from duke_rates.charts import rate_history_chart, regional_comparison_chart


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export DEC RS rate history from the SQLite database.")
    parser.add_argument("--database", type=Path, default=Path("data/db/duke_rates.db"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/dec_rs_history"))
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date", default="2026-12-31")
    parser.add_argument("--representative-kwh", type=float, default=1000.0)
    parser.add_argument("--write-html", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = export_dec_rs_history(
        args.output_dir,
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
    )
    print("Wrote CSV exports:")
    for label, path in paths.items():
        print(f"  {label}: {path}")

    all_in_df = load_dec_rs_all_in_history(
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
    )
    rider_totals_df, _ = load_dec_rs_rider_history(
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print()
    print(
        "Coverage summary:",
        f"base rows={len(all_in_df)}",
        f"rider rows={len(rider_totals_df)}",
        f"carried_forward_rows={int(all_in_df.get('rider_coverage_status', []).eq('carried_forward').sum()) if not all_in_df.empty and 'rider_coverage_status' in all_in_df else 0}",
        f"base_only_rows={int(all_in_df.get('bill_coverage_status', []).eq('base_only').sum()) if not all_in_df.empty and 'bill_coverage_status' in all_in_df else 0}",
    )

    comparison_df = load_residential_comparison_history(
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
    )
    comparison_path = args.output_dir / "dep_dec_residential_comparison.csv"
    comparison_df.to_csv(comparison_path, index=False)
    print(f"  comparison_csv: {comparison_path}")

    validation_paths = export_dec_rs_validation_report(
        args.output_dir,
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
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
                "title": "DEC RS Base, Rider, and All-In History",
                "columns": [
                    "summer_base_cents_per_kwh",
                    "winter_base_cents_per_kwh",
                    "total_rider_cents_per_kwh",
                    "blended_all_in_cents_per_kwh",
                ],
            },
        )
        comparison_figure = regional_comparison_chart(comparison_df, args.end_date)
    except ModuleNotFoundError as exc:
        print()
        print(f"Skipping HTML charts: {exc}")
        return

    rate_path = args.output_dir / "dec_rs_rate_history.html"
    comparison_path = args.output_dir / "dec_vs_dep_comparison.html"
    rate_figure.write_html(rate_path)
    comparison_figure.write_html(comparison_path)
    print()
    print("Wrote HTML charts:")
    print(f"  rate_history: {rate_path}")
    print(f"  comparison: {comparison_path}")


if __name__ == "__main__":
    main()
