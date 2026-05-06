from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CandidateUrlVariantRecord(BaseModel):
    id: int | None = None
    family_key: str
    lead_id: int | None = None
    variant_url: str
    hostname: str
    path_family: str
    filename: str | None = None
    heuristic: str
    direct_status_code: int | None = None
    direct_downloadable: bool = False
    wayback_snapshot_count: int = 0
    wayback_first_timestamp: str | None = None
    score: float = 0.0
    disposition: str = "candidate"
    notes: list[str] = Field(default_factory=list)
    metadata_json: str | None = None
    created_at: datetime | None = None

