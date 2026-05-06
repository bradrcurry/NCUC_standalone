from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class JurisdictionSeed(BaseModel):
    key: str
    state: str
    company: str | None = None
    jurisdiction_code: str | None = None
    service_key: str | None = None
    market: str = "residential"
    label: str
    seed_urls: list[HttpUrl] = Field(default_factory=list)
    api_content_path: str | None = None
    notes: str | None = None


class JurisdictionQuery(BaseModel):
    state: str | None = None
    company: str | None = None
    crawl_all: bool = False
