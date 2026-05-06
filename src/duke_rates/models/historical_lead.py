from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HistoricalLeadRecord(BaseModel):
    id: int | None = None
    family_key: str
    target_leaf_no: str | None = None
    target_code: str | None = None
    target_title: str
    family_type: str
    category: str
    source_class: str
    provenance_class: str
    source_label: str | None = None
    source_location: str | None = None
    source_url: str | None = None
    extracted_url: str | None = None
    extracted_title: str | None = None
    attachment_url: str | None = None
    viewer_url: str | None = None
    hostname: str | None = None
    path_fragment: str | None = None
    filename: str | None = None
    docket_number: str | None = None
    schedule_code: str | None = None
    rider_code: str | None = None
    leaf_reference: str | None = None
    effective_start: str | None = None
    effective_end: str | None = None
    extraction_method: str
    confidence_score: float = 0.0
    disposition: str = "new"
    score_notes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata_json: str | None = None
    created_at: datetime | None = None

