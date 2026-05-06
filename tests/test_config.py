from pathlib import Path

from duke_rates.config import Settings


def test_settings_ensure_directories(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/db/test.db",
        manifest_path=tmp_path / "data/manifests/test.jsonl",
    )
    settings.ensure_directories()
    assert settings.raw_dir.exists()
    assert settings.processed_dir.exists()
    assert settings.db_dir.exists()
    assert settings.historical_dir.exists()
