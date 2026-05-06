from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DocumentType(StrEnum):
    TARIFF_SHEET = "tariff_sheet"
    RIDER = "rider"
    INDEX_PAGE = "index_page"
    REDLINE = "redline"
    COMMISSION_ORDER = "commission_order"
    TESTIMONY = "testimony"
    EXHIBIT = "exhibit"
    APPLICATION = "application"
    CORRESPONDENCE = "correspondence"
    PROGRAM = "program"
    SUMMARY = "summary"
    UNKNOWN = "unknown"


class ParseLane(StrEnum):
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    LLM_ASSISTED = "llm_assisted"
    SKIP = "skip"


class NormalizationBackend(StrEnum):
    NATIVE_PDF = "native_pdf"
    PAGE_ARTIFACT = "page_artifact"
    PYTESSERACT_CPU = "pytesseract_cpu"
    DOCLING = "docling"
    PADDLE_STRUCTURE = "paddle_structure"
    GLM_OCR = "glm_ocr"
    UNKNOWN = "unknown"


class DocumentSchemaType(StrEnum):
    TARIFF_SHEET = "tariff_sheet"
    RIDER = "rider"
    COMMISSION_ORDER = "commission_order"
    TESTIMONY = "testimony"
    UNKNOWN = "unknown"


class BoundingBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    coordinate_space: str = "page"


class NormalizationWarning(BaseModel):
    code: str
    message: str
    severity: str = "warning"
    page_number: int | None = None
    backend: str | None = None


class NormalizationMetrics(BaseModel):
    backend: NormalizationBackend = NormalizationBackend.UNKNOWN
    fallback_backend: NormalizationBackend | None = None
    used_gpu: bool = False
    page_count: int = 0
    page_batch_size: int = 1
    render_dpi: int | None = None
    elapsed_ms: int | None = None
    text_char_count: int = 0
    low_text_page_count: int = 0
    table_page_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class LayoutBlock(BaseModel):
    block_type: str
    text: str
    page_number: int
    confidence: float = 0.0
    bbox: BoundingBox | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TableCell(BaseModel):
    text: str = ""
    row_index: int
    column_index: int
    confidence: float = 0.0
    bbox: BoundingBox | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractedTable(BaseModel):
    page_number: int
    row_count: int = 0
    column_count: int = 0
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)
    markdown: str | None = None
    html: str | None = None
    bbox: BoundingBox | None = None
    confidence: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class PageRepresentation(BaseModel):
    page_number: int
    text: str = ""
    markdown: str | None = None
    width: float | None = None
    height: float | None = None
    source: str | None = None
    backend: NormalizationBackend = NormalizationBackend.UNKNOWN
    blocks: list[LayoutBlock] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    warnings: list[NormalizationWarning] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentRepresentation(BaseModel):
    source_pdf: str
    file_hash: str | None = None
    document_id: int | None = None
    historical_document_id: int | None = None
    company: str | None = None
    state: str | None = None
    family_key: str | None = None
    title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    raw_text: str = ""
    markdown_text: str | None = None
    normalizer_backend: NormalizationBackend = NormalizationBackend.UNKNOWN
    pages: list[PageRepresentation] = Field(default_factory=list)
    warnings: list[NormalizationWarning] = Field(default_factory=list)
    normalization_metrics: NormalizationMetrics = Field(default_factory=NormalizationMetrics)
    document_metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentFingerprintResult(BaseModel):
    doc_type: DocumentType = DocumentType.UNKNOWN
    subtype: str | None = None
    confidence: float = 0.0
    parse_lane: ParseLane = ParseLane.DETERMINISTIC
    features_detected: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TariffSheetSchema(BaseModel):
    family_key: str | None = None
    title: str | None = None
    schedule_code: str | None = None
    leaf_no: str | None = None
    effective_start: str | None = None
    revision_label: str | None = None
    supersedes_label: str | None = None
    charge_count: int = 0
    parser_profile: str | None = None


class RiderSchema(BaseModel):
    family_key: str | None = None
    title: str | None = None
    rider_code: str | None = None
    leaf_no: str | None = None
    effective_start: str | None = None
    charge_count: int = 0
    parser_profile: str | None = None


class CommissionOrderSchema(BaseModel):
    title: str | None = None
    docket_number: str | None = None
    order_date: str | None = None


class TestimonySchema(BaseModel):
    title: str | None = None
    docket_number: str | None = None
    witness_name: str | None = None


class ValidationMessage(BaseModel):
    code: str
    message: str
    severity: str = "warning"
    source_pages: list[int] = Field(default_factory=list)


class ValidationReport(BaseModel):
    passed: bool = False
    errors: list[ValidationMessage] = Field(default_factory=list)
    warnings: list[ValidationMessage] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    schema_type: DocumentSchemaType = DocumentSchemaType.UNKNOWN
    data: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    source_pages: list[int] = Field(default_factory=list)
    validation_passed: bool = False
    parser_used: str | None = None
    extraction_mode: str = "deterministic"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentConfidence(BaseModel):
    classification_confidence: float = 0.0
    extraction_confidence: float = 0.0
    validation_confidence: float = 0.0
    overall_confidence: float = 0.0


class TrainingRecord(BaseModel):
    source_pdf: str
    historical_document_id: int | None = None
    family_key: str | None = None
    doc_type: str = DocumentType.UNKNOWN.value
    parse_lane: str = ParseLane.DETERMINISTIC.value
    parser_used: str | None = None
    input_features: dict[str, Any] = Field(default_factory=dict)
    fingerprint_result: dict[str, Any] = Field(default_factory=dict)
    predicted_output: dict[str, Any] = Field(default_factory=dict)
    validated_output: dict[str, Any] = Field(default_factory=dict)
    validation_report: dict[str, Any] = Field(default_factory=dict)
    confidence: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    created_at: str


class DocumentIntelligenceSnapshot(BaseModel):
    representation: DocumentRepresentation
    fingerprint: DocumentFingerprintResult
    extraction: ExtractionResult
    validation: ValidationReport
    confidence: DocumentConfidence
    training_record: TrainingRecord


NormalizedPage = PageRepresentation
NormalizedDocument = DocumentRepresentation
