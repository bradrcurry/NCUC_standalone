from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RegulatoryDocketLeadRecord(BaseModel):
    id: int | None = None
    family_key: str
    docket_number: str
    utility: str
    proceeding_type: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    referenced_codes: list[str] = Field(default_factory=list)
    evidence_source: str
    evidence_source_type: str
    evidence_source_location: str | None = None
    title: str | None = None
    contains_tariff_text: bool = False
    clue_only: bool = True
    confidence_score: float = 0.0
    notes: list[str] = Field(default_factory=list)
    metadata_json: str | None = None
    created_at: datetime | None = None

