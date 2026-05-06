from pathlib import Path

from duke_rates.analytics.bill_validation_summary import (
    export_progress_nc_bill_validation_summary,
)
from duke_rates.config import get_settings
from duke_rates.db.repository import Repository


def main() -> None:
    settings = get_settings()
    repository = Repository(settings.database_path)
    paths = export_progress_nc_bill_validation_summary(
        repository=repository,
        usage_xml_path=Path(r"C:\Python\Duke\Standalone\data\usage\Energy Usage.xml"),
        output_dir=settings.processed_dir / "bill_accuracy",
    )
    print(paths)


if __name__ == "__main__":
    main()
