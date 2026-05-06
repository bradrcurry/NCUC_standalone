from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ObservedComponentHistoryEntry(BaseModel):
    component_key: str
    rate_code: str | None = None
    component_label: str
    normalized_unit: str
    normalized_value: float
    start_date: date
    end_date: date
    sample_count: int
    bill_ids: list[int] = Field(default_factory=list)
    source_labels: list[str] = Field(default_factory=list)
    min_confidence: float
    max_confidence: float
    notes: list[str] = Field(default_factory=list)
