from pathlib import Path

from duke_rates.analytics.bill_accuracy import export_progress_nc_bill_accuracy_report
from duke_rates.config import get_settings
from duke_rates.db.repository import Repository


def main() -> None:
    settings = get_settings()
    repository = Repository(settings.database_path)
    report = export_progress_nc_bill_accuracy_report(
        repository=repository,
        usage_xml_path=Path(r"C:\Python\Duke\Standalone\data\usage\Energy Usage.xml"),
        output_dir=settings.processed_dir / "bill_accuracy",
    )
    print(report.summary)


if __name__ == "__main__":
    main()
