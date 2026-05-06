from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from duke_rates.models.public_notice import PublicNoticeData
from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.models.rider import RiderData


class ParseStatus(StrEnum):
    PARSED = "parsed"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceSnippet(BaseModel):
    label: str
    text: str


class ParsedField(BaseModel):
    name: str
    value: str | float | int | None
    confidence: float = 0.0
    source_snippet: SourceSnippet | None = None


class DocumentParseResult(BaseModel):
    document_id: int
    status: ParseStatus
    parser_name: str
    raw_text_path: str | None = None
    leaf_no: str | None = None
    extracted_fields: list[ParsedField] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    schedule: RateScheduleData | None = None
    rider: RiderData | None = None
    notice: PublicNoticeData | None = None
    errors: list[str] = Field(default_factory=list)
