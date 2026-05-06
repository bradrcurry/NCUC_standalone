from __future__ import annotations

from pydantic import BaseModel, Field


class BillRelevantGapRecord(BaseModel):
    current_document_id: int
    leaf_no: str
    title: str
    category: str
    primary_code: str | None = None
    parse_status: str | None = None
    has_parsed_schedule: bool = False
    has_parsed_rider: bool = False
    parsed_component_labels: list[str] = Field(default_factory=list)
    current_applicable_schedules: list[str] = Field(default_factory=list)
    historical_version_count: int = 0
    historical_match_modes: list[str] = Field(default_factory=list)
    historical_effective_ranges: list[str] = Field(default_factory=list)
    observed_component_keys: list[str] = Field(default_factory=list)
    gap_flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
