from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl


class DocumentKind(StrEnum):
    PDF = "pdf"
    HTML = "html"
    OTHER = "other"


class DocumentCategory(StrEnum):
    RATE = "rate"
    RIDER = "rider"
    TARIFF = "tariff"
    PUBLIC_NOTICE = "public_notice"
    INDEX = "index"
    PROGRAM = "program"
    OTHER = "other"


class DiscoveryRecord(BaseModel):
    title: str
    source_page_url: HttpUrl
    document_url: HttpUrl
    state: str | None = None
    company: str | None = None
    category: DocumentCategory = DocumentCategory.OTHER
    kind: DocumentKind = DocumentKind.OTHER
    effective_date: str | None = None
    retrieval_timestamp: datetime
    local_path: str | None = None
    content_hash: str | None = None
    content_type: str | None = None
    status_code: int | None = None
    notes: list[str] = Field(default_factory=list)


class StoredDocument(BaseModel):
    id: int
    title: str
    source_page_url: str
    document_url: str
    state: str | None = None
    company: str | None = None
    category: str
    kind: str
    effective_date: str | None = None
    local_path: Path
    content_hash: str
    content_type: str | None = None
    status_code: int | None = None
    retrieved_at: datetime
    discovered_at: datetime
    metadata_json: str | None = None
    tariff_identifier: str | None = None
    schedule_code: str | None = None
    rev_token: str | None = None
