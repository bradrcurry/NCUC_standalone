from __future__ import annotations

from pydantic import BaseModel, Field


class OpenEIChargeCandidate(BaseModel):
    label: str
    rate: float | None = None
    unit: str | None = None
    block_from: float | None = None
    block_to: float | None = None
    period: str | None = None
    season: str | None = None


class OpenEIExportCandidate(BaseModel):
    source_kind: str
    document_id: int
    title: str
    utility: str | None = None
    rate_name: str | None = None
    schedule_code: str | None = None
    sector: str | None = None
    source_url: str | None = None
    source_parent_url: str | None = None
    effective_start: str | None = None
    effective_end: str | None = None
    fixed_charges: list[OpenEIChargeCandidate] = Field(default_factory=list)
    energy_charges: list[OpenEIChargeCandidate] = Field(default_factory=list)
    demand_charges: list[OpenEIChargeCandidate] = Field(default_factory=list)
    rider_codes: list[str] = Field(default_factory=list)
    tou_detected: bool = False
    approved: bool | None = None
    openei_label: str | None = None
    openei_uri: str | None = None
    openei_source_url: str | None = None
    openei_start_date: str | None = None
    openei_end_date: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    submission_guidance: list[str] = Field(default_factory=list)
