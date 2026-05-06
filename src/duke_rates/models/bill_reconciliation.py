from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from duke_rates.billing.engine import BillEstimate
from duke_rates.historical.tariff_selector import HistoricalTariffSelection


class ReconciledLineItem(BaseModel):
    key: str
    label: str
    actual_amount: float | None = None
    estimated_amount: float | None = None
    delta: float | None = None
    status: str
    actual_labels: list[str] = Field(default_factory=list)
    estimated_labels: list[str] = Field(default_factory=list)


class BillReconciliation(BaseModel):
    bill_id: int
    source_path: str
    service_date: date
    rate_code: str
    billed_kwh: float
    actual_electric_total: float | None = None
    estimated_electric_total: float
    total_delta: float | None = None
    selected_tariff: HistoricalTariffSelection
    estimate: BillEstimate
    line_items: list[ReconciledLineItem] = Field(default_factory=list)
    unsupported_actual_labels: list[str] = Field(default_factory=list)
    unsupported_estimated_labels: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
