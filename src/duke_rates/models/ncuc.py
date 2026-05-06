from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class NcucFilingClassification(str, Enum):
    ORDER = "order"
    NOTICE = "notice"
    COMPLIANCE_FILING = "compliance_filing"
    TARIFF_SHEETS = "tariff_sheets"
    EXHIBIT = "exhibit"
    TESTIMONY = "testimony"
    ATTACHMENT = "attachment"
    APPLICATION = "application"
    SETTLEMENT = "settlement"
    OTHER = "other"


class NcucAcquisitionMethod(str, Enum):
    SEARCH_ENGINE = "search_engine"
    DIRECT_HTTP = "direct_http"
    PLAYWRIGHT = "playwright"
    MANUAL_SEED = "manual_seed"
    DOCKET_SCRAPE = "docket_scrape"


class NcucFetchStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADED = "downloaded"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    REQUIRES_BROWSER = "requires_browser"
    BLOCKED = "blocked"


class NcucDiscoveryRecord(BaseModel):
    """A discovered NCUC document lead, not yet downloaded."""

    id: int | None = None

    # Docket metadata
    docket_number: str | None = None
    sub_number: str | None = None
    utility: str = "Duke Energy Progress"
    filing_title: str | None = None
    filing_date: str | None = None
    proceeding_type: str | None = None

    # Classification
    filing_classification: NcucFilingClassification = NcucFilingClassification.OTHER
    exhibit_label: str | None = None

    # Schedule/rider references
    referenced_schedule_codes: list[str] = Field(default_factory=list)
    referenced_rider_codes: list[str] = Field(default_factory=list)
    referenced_leaf_nos: list[str] = Field(default_factory=list)

    # Target family linkage (for pipeline integration)
    family_keys: list[str] = Field(default_factory=list)

    # URLs
    discovered_url: str | None = None
    viewer_url: str | None = None
    attachment_url: str | None = None
    download_url: str | None = None

    # Acquisition
    acquisition_method: NcucAcquisitionMethod = NcucAcquisitionMethod.MANUAL_SEED
    fetch_status: NcucFetchStatus = NcucFetchStatus.PENDING

    # Storage
    local_path: str | None = None
    content_hash: str | None = None
    content_type: str | None = None
    file_size_bytes: int | None = None

    # Provenance
    provenance_notes: list[str] = Field(default_factory=list)
    search_query: str | None = None
    page_title: str | None = None

    # Document quality tier — set at ingest time or backfilled by migration.
    # "T1" = official Duke website PDF
    # "T2" = NCUC compliance docket download (playwright / docket_scrape)
    # "T3" = NCUC search-engine / direct-HTTP discovery
    doc_quality_tier: str | None = None

    # Search confidence signals — populated when record is created from a
    # portal search result scored by ResultScorer / IdealityAssessment.
    search_confidence_score: float | None = None
    search_ideality: str | None = None   # "ideal" | "probable" | "possible" | "skip"

    # Audit
    created_at: datetime | None = None
    fetched_at: datetime | None = None
    error_detail: str | None = None
    metadata_json: str | None = None


class NcucDocketSeed(BaseModel):
    """A manually or programmatically seeded docket to drive discovery."""

    docket_number: str
    utility: str = "Duke Energy Progress"
    proceeding_type: str | None = None
    description: str | None = None
    referenced_schedule_codes: list[str] = Field(default_factory=list)
    referenced_rider_codes: list[str] = Field(default_factory=list)
    family_keys: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class NcucSearchQuery(BaseModel):
    """A search engine or portal query for NCUC document discovery."""

    query_text: str
    docket_hint: str | None = None
    family_key_hint: str | None = None
    schedule_code_hint: str | None = None
    rider_code_hint: str | None = None
    date_from: str | None = None
    date_to: str | None = None
