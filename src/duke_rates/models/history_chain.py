from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class HistoryVersion(BaseModel):
    source_kind: Literal["current", "historical"]
    document_id: int
    current_document_id: int | None = None
    family_key: str
    title: str
    state: str | None = None
    company: str | None = None
    category: str
    kind: str
    leaf_no: str | None = None
    revision_label: str | None = None
    supersedes_label: str | None = None
    effective_start: str | None = None
    effective_end: str | None = None
    tariff_id: str | None = None
    schedule_code: str | None = None
    rider_id: str | None = None
    source_url: str
    archived_url: str | None = None
    local_path: Path
    direct_downloadable: bool | None = None
    retrieved_at: datetime


class HistoryChain(BaseModel):
    family_key: str
    title: str
    category: str
    state: str | None = None
    company: str | None = None
    leaf_no: str | None = None
    versions: list[HistoryVersion] = Field(default_factory=list)
