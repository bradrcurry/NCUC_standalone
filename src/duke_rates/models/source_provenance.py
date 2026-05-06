from __future__ import annotations

from pydantic import BaseModel, Field


class SourceProvenance(BaseModel):
    authority: str
    source_type: str
    source_label: str | None = None
    source_url: str | None = None
    docket_number: str | None = None
    confidence_rank: int = 0
    notes: list[str] = Field(default_factory=list)


class ChainSourceCoverage(BaseModel):
    family_key: str
    title: str
    leaf_no: str | None = None
    category: str
    version_count: int = 0
    authorities: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    dockets: list[str] = Field(default_factory=list)
    evidence: list[SourceProvenance] = Field(default_factory=list)
