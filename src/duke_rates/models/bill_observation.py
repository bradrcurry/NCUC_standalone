from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class BillComponentObservation(BaseModel):
    id: int | None = None
    bill_id: int
    source_path: str
    section_name: str
    rate_code: str | None = None
    component_key: str
    component_label: str
    amount: float
    service_start: date | None = None
    service_end: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    days_in_period: int | None = None
    quantity_basis_kwh: float | None = None
    inferred_unit: str | None = None
    inferred_value: float | None = None
    confidence: float = 0.5
    notes: list[str] = Field(default_factory=list)
