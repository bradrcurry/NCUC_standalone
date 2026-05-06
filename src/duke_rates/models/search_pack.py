from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HistoricalSearchPackRecord(BaseModel):
    id: int | None = None
    family_key: str
    target_leaf_no: str | None = None
    target_code: str | None = None
    target_title: str
    family_type: str
    payload_json: str
    notes: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

