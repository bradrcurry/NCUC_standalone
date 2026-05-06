from __future__ import annotations

from pydantic import BaseModel, Field


class NoticeLinkMatch(BaseModel):
    family_key: str
    title: str
    category: str
    basis: str


class NoticeLinkRecord(BaseModel):
    historical_id: int
    title: str
    docket_numbers: list[str] = Field(default_factory=list)
    related_rider_codes: list[str] = Field(default_factory=list)
    related_schedule_codes: list[str] = Field(default_factory=list)
    matches: list[NoticeLinkMatch] = Field(default_factory=list)
