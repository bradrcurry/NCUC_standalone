from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from duke_rates.analytics.dep_rider_date_audit import export_dep_rider_date_audit_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export DEP rider component effective-date audit.")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/db/duke_rates.db"),
        help="Path to the SQLite database.",
    )
    parser.add_argument(
        "--usage-xml",
        type=Path,
        default=Path(r"C:\Python\Duke\Standalone\data\usage\Energy Usage.xml"),
        help="Path to the Duke usage XML export used for bill-backed validation.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed/dep_res_history"),
        help="Directory for rider date audit outputs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_paths = export_dep_rider_date_audit_report(
        args.output_dir,
        database_path=args.database,
        usage_xml_path=args.usage_xml,
    )
    print("Wrote DEP rider date audit:")
    for label, path in output_paths.items():
        print(f"  {label}: {path}")


if __name__ == "__main__":
    main()
