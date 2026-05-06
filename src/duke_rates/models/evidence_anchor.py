from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class EvidenceAnchorRecord(BaseModel):
    id: int | None = None
    family_key: str
    anchor_type: str
    anchor_value: str
    start_date: str | None = None
    end_date: str | None = None
    source_type: str
    source_location: str | None = None
    confidence_score: float = 0.0
    notes: list[str] = Field(default_factory=list)
    metadata_json: str | None = None
    created_at: datetime | None = None
