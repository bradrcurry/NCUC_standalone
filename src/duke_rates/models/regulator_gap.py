from __future__ import annotations

from pydantic import BaseModel, Field


class RegulatorGapHint(BaseModel):
    title: str
    docket_numbers: list[str] = Field(default_factory=list)
    basis: list[str] = Field(default_factory=list)


class RegulatorGapRecord(BaseModel):
    family_key: str
    title: str
    leaf_no: str | None = None
    category: str
    version_count: int = 0
    evidence_authorities: list[str] = Field(default_factory=list)
    evidence_source_types: list[str] = Field(default_factory=list)
    gap_priority: int = 0
    reason: str
    suggested_dockets: list[str] = Field(default_factory=list)
    hints: list[RegulatorGapHint] = Field(default_factory=list)
