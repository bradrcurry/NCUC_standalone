from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from duke_rates.analytics.canonical_residential import (
    export_canonical_residential_timeline,
    load_canonical_residential_timeline,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export canonical DEP/DEC residential timeline from SQLite.")
    parser.add_argument("--database", type=Path, default=Path("data/db/duke_rates.db"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/canonical_residential"))
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date", default="2026-12-31")
    parser.add_argument("--representative-kwh", type=float, default=1000.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = export_canonical_residential_timeline(
        args.output_dir,
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
    )
    print("Wrote canonical exports:")
    for label, path in paths.items():
        print(f"  {label}: {path}")

    canonical_df = load_canonical_residential_timeline(
        database_path=args.database,
        start_date=args.start_date,
        end_date=args.end_date,
        representative_kwh=args.representative_kwh,
    )
    print()
    print(
        "Summary:",
        f"rows={len(canonical_df)}",
        f"utilities={canonical_df['utility'].nunique() if not canonical_df.empty else 0}",
        f"same_day={int((canonical_df.get('rider_coverage_status') == 'same_day').sum()) if not canonical_df.empty and 'rider_coverage_status' in canonical_df else 0}",
        f"carried_forward={int((canonical_df.get('rider_coverage_status') == 'carried_forward').sum()) if not canonical_df.empty and 'rider_coverage_status' in canonical_df else 0}",
    )


if __name__ == "__main__":
    main()
