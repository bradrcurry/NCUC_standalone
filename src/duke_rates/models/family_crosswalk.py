from __future__ import annotations

from pydantic import BaseModel


class HistoricalFamilyCrosswalkRecord(BaseModel):
    historical_id: int
    old_family_key: str
    new_family_key: str
    current_document_id: int | None = None
    historical_title: str
    target_title: str
    target_leaf_no: str | None = None
    target_code: str | None = None
    matched_code: str | None = None
    basis: str
    confidence: float
