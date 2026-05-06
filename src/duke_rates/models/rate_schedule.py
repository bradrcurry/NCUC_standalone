from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class EnergyCharge(BaseModel):
    label: str
    rate: float | None = None
    unit: str = "kWh"
    block_from: float | None = None
    block_to: float | None = None
    season: str | None = None
    period: str | None = None


class DemandCharge(BaseModel):
    label: str
    rate: float | None = None
    unit: str = "kW"


class FixedCharge(BaseModel):
    label: str
    amount: float | None = None
    unit: str = "month"


class TOUPeriod(BaseModel):
    name: str
    months: list[str] = Field(default_factory=list)
    weekday_hours: str | None = None
    weekend_hours: str | None = None


class TariffReference(BaseModel):
    code: str | None = None
    title: str
    role: Literal["schedule", "rider", "adjustment", "notice"] = "schedule"


class RateScheduleData(BaseModel):
    tariff_id: str
    utility: str = "Duke Energy"
    state: str | None = None
    company: str | None = None
    schedule_code: str | None = None
    schedule_title: str
    customer_class: str | None = None
    effective_start: date | None = None
    effective_end: date | None = None
    fixed_charges: list[FixedCharge] = Field(default_factory=list)
    energy_charges: list[EnergyCharge] = Field(default_factory=list)
    demand_charges: list[DemandCharge] = Field(default_factory=list)
    tou_periods: list[TOUPeriod] = Field(default_factory=list)
    riders: list[TariffReference] = Field(default_factory=list)
    credits: list[str] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)
    eligibility: str | None = None
    raw_summary: str | None = None
