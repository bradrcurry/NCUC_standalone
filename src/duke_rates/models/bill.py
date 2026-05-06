from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class BillLineItem(BaseModel):
    label: str
    amount: float | None = None
    quantity: float | None = None
    unit: str | None = None
    rate: float | None = None
    detail: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    is_subperiod_detail: bool = False


class BillSection(BaseModel):
    name: str
    billing_period_start: date | None = None
    billing_period_end: date | None = None
    meter_number: str | None = None
    rate_name: str | None = None
    rate_code: str | None = None
    total_current_charges: float | None = None
    line_items: list[BillLineItem] = Field(default_factory=list)


class BillingSummaryData(BaseModel):
    previous_amount_due: float | None = None
    payment_received: float | None = None
    payment_received_date: date | None = None
    current_lighting_charges: float | None = None
    current_electric_charges: float | None = None
    taxes: float | None = None
    total_amount_due: float | None = None
    due_date: date | None = None


class BillStatementData(BaseModel):
    source_path: str
    account_number: str | None = None
    customer_name: str | None = None
    service_address_lines: list[str] = Field(default_factory=list)
    bill_date: date | None = None
    due_date: date | None = None
    service_start: date | None = None
    service_end: date | None = None
    bill_days: int | None = None
    billing_summary: BillingSummaryData = Field(default_factory=BillingSummaryData)
    electric_section: BillSection | None = None
    lighting_section: BillSection | None = None
    tax_section: BillSection | None = None
    notes: list[str] = Field(default_factory=list)


class StoredBillStatement(BaseModel):
    id: int
    source_path: str
    account_number: str | None = None
    bill_date: date | None = None
    due_date: date | None = None
    service_start: date | None = None
    service_end: date | None = None
    total_amount_due: float | None = None
    content_hash: str
    raw_text_path: str | None = None
    statement_json: str
