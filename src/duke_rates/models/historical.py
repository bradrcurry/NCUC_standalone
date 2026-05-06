from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class HistoricalDocumentRecord(BaseModel):
    id: int | None = None
    current_document_id: int | None = None
    family_key: str
    title: str
    state: str | None = None
    company: str | None = None
    category: str
    kind: str
    canonical_url: str
    archived_url: str
    snapshot_timestamp: datetime
    local_path: Path
    raw_text_path: Path | None = None
    content_hash: str
    content_type: str | None = None
    direct_status_code: int | None = None
    direct_downloadable: bool = False
    revision_label: str | None = None
    supersedes_label: str | None = None
    leaf_no: str | None = None
    
    # New page-aware span storage fields
    start_page: int | None = None
    end_page: int | None = None
    evidence_json: str | None = None
    
    effective_start: str | None = None
    effective_end: str | None = None
    retrieved_at: datetime
    metadata_json: str | None = None
    parsed_result_json: str | None = None
    notes: list[str] = Field(default_factory=list)
