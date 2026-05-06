from __future__ import annotations

import enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class PipelineRoute(str, enum.Enum):
    SKIP_IRRELEVANT = "skip_irrelevant"
    TEXT_PARSE = "text_parse"
    OCR_REQUIRED = "ocr_required"
    PAGE_SEGMENT = "page_segment"
    TABLE_HEAVY = "table_heavy"
    MANUAL_REVIEW = "manual_review"

class DateCandidate(BaseModel):
    date_value: str
    date_type: str  # effective, service_rendered, issue, order, superseding
    evidence_text: str
    page_number: int
    confidence: float

class PageEvidence(BaseModel):
    page_number: int
    text_length: int
    
    # Text content (may be None if dropped to save memory later, but kept during extraction)
    text_content: Optional[str] = None
    
    # Feature signals
    has_leaf_header: bool = False
    has_revised_header: bool = False
    has_schedule_heading: bool = False
    
    tariff_vocab_density: float = 0.0
    procedural_vocab_density: float = 0.0
    numeric_density: float = 0.0
    table_like_density: float = 0.0
    
    header_candidates: List[str] = Field(default_factory=list)
    footer_candidates: List[str] = Field(default_factory=list)
    
    has_effective_date_phrase: bool = False
    has_docket_phrase: bool = False

    extracted_leaf_nos: List[str] = Field(default_factory=list)
    extracted_schedule_codes: List[str] = Field(default_factory=list)

    # Redline / tracked-changes signals
    has_redline_markers: bool = False      # "NEW", "OLD", "PROPOSED", "DRAFT" etc.
    redline_marker_count: int = 0          # raw hit count across the page
    has_dual_rate_pair: bool = False       # "0.0464/0.0512" slash-separated rate pair
    has_toc_page: bool = False             # table-of-contents / index-of-tariff-leaves marker

class DocumentTriage(BaseModel):
    file_path: str
    file_hash: Optional[str] = None
    file_size_bytes: int = 0
    page_count: int = 0
    
    text_char_count_first_pages: int = 0
    text_char_count_sampled: int = 0
    
    is_likely_scanned: bool = False
    is_likely_tariff_related: bool = False
    likely_document_class: Optional[str] = None
    document_archetype_candidate: Optional[str] = None
    ocr_confidence_score: float = 0.0
    native_text_quality_score: float = 0.0
    structure_complexity_score: float = 0.0
    reading_order_risk_score: float = 0.0
    sampled_chars_per_page: float = 0.0
    gpu_ocr_candidate: bool = False
    table_mode_candidate: Optional[str] = None
    native_text_backend: Optional[str] = None
    
    keyword_hits: Dict[str, int] = Field(default_factory=dict)
    has_leaf_phrases: bool = False
    triage_flags: List[str] = Field(default_factory=list)
    
    route_recommendation: PipelineRoute = PipelineRoute.MANUAL_REVIEW
    confidence_score: float = 0.0


class DocumentFingerprint(BaseModel):
    source_pdf: str
    docket_dir: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    leaf_no: Optional[str] = None
    schedule_code: Optional[str] = None
    title: Optional[str] = None
    text_length: int = 0
    line_count: int = 0
    numeric_line_count: int = 0
    has_table_rows: bool = False
    has_rider_summary: bool = False

    # Redline / quality tier signals (populated by bulk_extractor)
    is_redline_candidate: bool = False     # any page had redline markers or dual-rate pairs
    redline_confidence: float = 0.0        # 0.0–1.0 fraction of lines with redline indicators
    doc_quality_tier: Optional[str] = None # "T1" | "T2" | "T3" | None
    is_compliance_book: bool = False       # spans multiple tariff leaves (TOC or ≥2 leaf_nos)

    review_flags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ParseAttemptLog(BaseModel):
    source_pdf: str
    docket_dir: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    parser_stage: str
    parser_profile: Optional[str] = None
    status: str
    confidence: float = 0.0
    utility: Optional[str] = None
    schedule_code: Optional[str] = None
    effective_date: Optional[str] = None
    charge_count: int = 0
    review_flags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ParseReviewOutcome(BaseModel):
    parse_attempt_id: Optional[int] = None
    source_pdf: str
    docket_dir: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    parser_stage: Optional[str] = None
    parser_profile: Optional[str] = None
    utility: Optional[str] = None
    review_source: str
    outcome: str
    correction_count: int = 0
    notes: Dict[str, Any] = Field(default_factory=dict)
    corrections: Dict[str, Any] = Field(default_factory=dict)


class DocumentPageArtifact(BaseModel):
    discovery_record_id: Optional[int] = None
    source_pdf: str
    file_hash: Optional[str] = None
    artifact_version: str
    page_number: int
    text_length: int = 0
    text_content: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DocumentSpanArtifact(BaseModel):
    discovery_record_id: Optional[int] = None
    source_pdf: str
    file_hash: Optional[str] = None
    artifact_version: str
    span_index: int
    start_page: int
    end_page: int
    doc_type: str = "unknown"
    confidence: float = 0.0
    extracted_leaf_nos: List[str] = Field(default_factory=list)
    extracted_schedule_titles: List[str] = Field(default_factory=list)
    header_footer_snippets: List[str] = Field(default_factory=list)
    dates: List[DateCandidate] = Field(default_factory=list)
    evidence_score_breakdown: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TariffSpan(BaseModel):
    parent_discovery_id: Optional[int] = None
    start_page: int
    end_page: int
    
    doc_type: str = "unknown"
    confidence: float = 0.0
    
    extracted_leaf_nos: set[str] = Field(default_factory=set)
    extracted_schedule_titles: set[str] = Field(default_factory=set)
    header_footer_snippets: List[str] = Field(default_factory=list)
    
    dates: List[DateCandidate] = Field(default_factory=list)
    
    evidence_score_breakdown: Dict[str, Any] = Field(default_factory=dict)
