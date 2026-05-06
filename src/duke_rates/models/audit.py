from __future__ import annotations

from pydantic import BaseModel, Field


class VersionTimelineEntry(BaseModel):
    """One tariff_version row with pre-computed charge statistics."""

    version_id: int
    family_key: str
    effective_start: str | None
    effective_end: str | None
    revision_label: str | None
    supersedes_label: str | None
    source_type: str
    confidence_score: float
    charge_count: int
    null_rate_count: int

    @property
    def has_charges(self) -> bool:
        return self.charge_count > 0

    @property
    def has_null_rates(self) -> bool:
        return self.null_rate_count > 0

    @property
    def charge_status(self) -> str:
        if self.charge_count == 0:
            return "no_charges"
        if self.null_rate_count > 0:
            return "null_rates"
        return "ok"


class VersionGap(BaseModel):
    """A date range where no tariff_version covers a given family."""

    family_key: str
    gap_start: str | None   # exclusive end of predecessor (or None if first version is undated)
    gap_end: str | None     # start of successor (or None if open-ended gap)
    gap_days: int | None    # None when either boundary is unknown
    predecessor_version_id: int | None
    successor_version_id: int | None
    gap_type: str           # "between_versions" | "undated_version" | "open_start"


class FamilyTemporalMap(BaseModel):
    """Full timeline for one tariff family: versions, gaps, and supersession chain."""

    family_key: str
    family_type: str
    title: str | None
    versions: list[VersionTimelineEntry] = Field(default_factory=list)
    gaps: list[VersionGap] = Field(default_factory=list)
    supersession_chain: list[str] = Field(default_factory=list)  # revision_labels in order
    orphaned_revisions: list[str] = Field(default_factory=list)  # not linked into main chain
    timeline_status: str = "empty"  # "complete" | "gaps_exist" | "undated" | "empty"


class RiderCoverageEntry(BaseModel):
    """Coverage status for one (rider, schedule, date) triple."""

    rider_family_key: str
    rider_title: str | None
    applies_to_family_key: str
    mandatory: bool
    in_rider_summary: bool
    enrollment_type: str
    # Rider-side version as of audit date
    rider_version_id: int | None = None
    rider_effective_start: str | None = None
    rider_effective_end: str | None = None
    rider_charge_count: int = 0
    rider_null_rate_count: int = 0
    # Rate contribution (sum of $/kWh adjustment charges for residential class)
    rate_cents_per_kwh: float | None = None
    coverage_status: str = "ok"  # "ok" | "no_version" | "no_charges" | "null_rates"
    notes: str = ""


class TariffCoverageMap(BaseModel):
    """Complete coverage audit for one (schedule, date) pair."""

    as_of_date: str
    schedule_family_key: str
    schedule_title: str | None
    schedule_version_id: int | None
    schedule_revision_label: str | None
    schedule_charge_status: str  # "ok" | "no_version" | "no_charges" | "null_rates"
    riders: list[RiderCoverageEntry] = Field(default_factory=list)
    # Leaf-600 cross-check (None when no leaf-600 exists for this utility)
    leaf600_total_cents_per_kwh: float | None = None
    engine_summary_total_cents_per_kwh: float | None = None
    delta_cents_per_kwh: float | None = None
    delta_within_tolerance: bool | None = None
    audit_verdict: str = "no_data"  # "complete" | "partial" | "missing_riders" | "no_data"
    warnings: list[str] = Field(default_factory=list)

    @property
    def riders_ok(self) -> int:
        return sum(1 for r in self.riders if r.coverage_status == "ok")

    @property
    def riders_missing(self) -> int:
        return sum(1 for r in self.riders if r.coverage_status != "ok")


class TariffSearchWorkItem(BaseModel):
    """One entry in the NCUC search work list — a family that needs rate data."""

    family_key: str
    family_type: str          # "rate_schedule" | "rider"
    title: str | None
    leaf_no: str | None       # e.g. "532" extracted from family_key
    current_revision_label: str | None   # most recent known revision label
    current_effective_start: str | None  # start date of that version
    gap_reason: str           # why it's on the list: "no_charges" | "no_versions"
    priority: str             # "high" | "medium" | "low"
    category: str = ""        # "residential" | "commercial" | "lighting" | "ee_program" | "ev_program" | "regulation" | "rider" | etc.

    # Cross-reference to known NCUC filings
    known_dockets: list[str] = Field(default_factory=list)
    # Pre-formed NCUC search queries, ranked best-first
    suggested_queries: list[str] = Field(default_factory=list)
    # Any downloaded PDFs we already have that may contain this leaf
    local_pdf_count: int = 0
    notes: str = ""
