from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RiderAdjustmentRow(BaseModel):
    rate_class: str
    fuel_adjustment_cents_per_kwh: float | None = None
    fuel_emf_cents_per_kwh: float | None = None
    dsm_ee_adjustment_cents_per_kwh: float | None = None
    dsm_ee_emf_cents_per_kwh: float | None = None
    net_adjustment_cents_per_kwh: float | None = None
    applicable_schedules: list[str] = Field(default_factory=list)


class RiderChargeComponent(BaseModel):
    bill_label: str
    rate_class: str | None = None
    value: float
    unit: Literal["cents_per_kwh", "fixed_monthly", "percent_of_energy_charges"] = (
        "cents_per_kwh"
    )
    applicable_schedules: list[str] = Field(default_factory=list)


class RiderData(BaseModel):
    rider_id: str
    state: str | None = None
    company: str | None = None
    code: str | None = None
    version_code: str | None = None
    title: str
    effective_date: str | None = None
    applicability: str | None = None
    charge_description: str | None = None
    formula_based: bool = False
    applicable_schedules: list[str] = Field(default_factory=list)
    adjustment_rows: list[RiderAdjustmentRow] = Field(default_factory=list)
    charge_components: list[RiderChargeComponent] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
