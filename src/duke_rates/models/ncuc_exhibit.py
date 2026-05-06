from __future__ import annotations

from pydantic import BaseModel, Field


class NcucExhibitCandidate(BaseModel):
    record_id: int
    family_key: str
    docket_number: str | None = None
    filing_date: str | None = None
    filing_title: str | None = None
    local_path: str | None = None
    score: float
    reasons: list[str] = Field(default_factory=list)
    contains_tariff_text: bool = False
    filing_classification: str | None = None
    extracted_schedule_codes: list[str] = Field(default_factory=list)
    extracted_rider_codes: list[str] = Field(default_factory=list)
    extracted_leaf_nos: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    derived_title: str | None = None
