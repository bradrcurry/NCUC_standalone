from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TariffFamilyRecord(BaseModel):
    """One row per logical tariff (rate schedule or rider), independent of version."""

    id: int | None = None
    family_key: str
    state: str
    company: str
    tariff_identifier: str | None = None
    schedule_code: str | None = None
    family_type: str  # rate_schedule | rider | program | regulation | index | overhead
    title: str | None = None
    aliases: list[str] = Field(default_factory=list)
    current_document_id: int | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TariffVersionRecord(BaseModel):
    """One row per filed version of a tariff, linked to its source document."""

    id: int | None = None
    family_key: str
    document_id: int | None = None
    historical_document_id: int | None = None
    effective_start: str | None = None
    effective_end: str | None = None
    revision_label: str | None = None
    supersedes_label: str | None = None
    docket_number: str | None = None
    order_date: str | None = None
    leaf_no: str | None = None
    source_pdf: str | None = None
    docket_dir: str | None = None
    source_type: str  # utility_current | utility_historical | wayback | regulator | bill_observed
    confidence_score: float = 0.0
    notes: str | None = None
    created_at: datetime | None = None
    # Projected/proposed rates support (PR #34 schema migration)
    status: str = "approved"  # approved | proposed | withdrawn | superseded
    requested_effective_date: str | None = None
    approved_version_id: int | None = None


class TariffChargeRecord(BaseModel):
    """One extracted charge component from a tariff version."""

    id: int | None = None
    version_id: int
    family_key: str
    charge_type: str  # fixed | energy_block | demand | tou_energy | minimum | maximum | credit | adjustment
    charge_label: str | None = None
    rate_value: float | None = None
    rate_unit: str | None = None  # cents/kWh | $/kWh | $/kW | $/bill | $/month | %
    tier_min: float | None = None  # kWh lower bound for block rates
    tier_max: float | None = None  # kWh upper bound (None = unlimited)
    tou_period: str | None = None  # on_peak | off_peak | super_off_peak | shoulder
    season: str | None = None  # summer | winter | all_year
    customer_class: str | None = None  # residential | commercial_* | industrial | general_service | primary | secondary | transmission
    source_snippet: str | None = None  # exact text from the PDF that was parsed
    confidence_score: float = 0.0
    notes: str | None = None
    created_at: datetime | None = None


class RiderApplicabilityRecord(BaseModel):
    """Links a rider to the base rate schedules it applies to."""

    id: int | None = None
    rider_family_key: str
    applies_to_family_key: str
    mandatory: bool = True
    enrollment_type: str = "mandatory"  # mandatory | opt_in | opt_out | conditional | geographic
    in_rider_summary: bool = True  # True = appears in leaf-600 Summary of Rider Adjustments; False = direct bill addition (e.g. STS, SSR storm riders)
    applicability_notes: str | None = None
    effective_start: str | None = None
    effective_end: str | None = None
    source_type: str  # tariff_text | bill_observed | manual
    confidence_score: float = 0.0
    created_at: datetime | None = None
