from __future__ import annotations

from pydantic import BaseModel, Field


class PublicNoticeData(BaseModel):
    notice_id: str
    title: str
    state: str | None = None
    company: str | None = None
    filing_date: str | None = None
    docket_numbers: list[str] = Field(default_factory=list)
    related_rider_codes: list[str] = Field(default_factory=list)
    related_schedule_codes: list[str] = Field(default_factory=list)
    customer_classes: list[str] = Field(default_factory=list)
    summary: str | None = None
