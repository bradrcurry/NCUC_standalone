from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DUKE_RATES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("data"))
    database_path: Path = Field(default=Path("data/db/duke_rates.db"))
    manifest_path: Path = Field(default=Path("data/manifests/discovery.jsonl"))
    log_level: str = Field(default="INFO")
    request_timeout: float = Field(default=30.0)
    max_retries: int = Field(default=3)
    rate_limit_seconds: float = Field(default=0.5)
    max_crawl_depth: int = Field(default=1)
    max_pages_per_jurisdiction: int = Field(default=12)
    use_playwright: bool = Field(default=False)
    user_agent: str = Field(
        default="duke-rates/0.1 (+local archival and tariff analysis)",
    )
    openai_api_key: str | None = Field(default=None)
    openai_model: str = Field(default="gpt-4.1-mini")
    openei_api_key: str | None = Field(default=None)

    # Google Custom Search Engine (CSE) — for dork-based document discovery
    # Set up at https://programmablesearchengine.google.com/
    # API key from Google Cloud Console (Custom Search JSON API)
    google_api_key: str | None = Field(default=None)
    google_cse_id: str | None = Field(default=None)

    # NCID credentials for authenticated NCUC portal access
    ncid_username: str | None = Field(default=None)
    ncid_password: str | None = Field(default=None)

    # EIA Open Data API v2 key — https://www.eia.gov/opendata/register.php
    # Set EIA_API_KEY in .env (no DUKE_RATES_ prefix — read directly from env).
    eia_api_key: str | None = Field(default=None, validation_alias="EIA_API_KEY")

    # EIA ingestion settings
    eia_cache_dir: Path = Field(default=Path("data/eia_cache"))
    eia_request_delay: float = Field(default=0.25)  # seconds between API calls

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def manifests_dir(self) -> Path:
        return self.data_dir / "manifests"

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def historical_dir(self) -> Path:
        return self.data_dir / "historical"

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.raw_dir,
            self.processed_dir,
            self.manifests_dir,
            self.db_dir,
            self.historical_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
