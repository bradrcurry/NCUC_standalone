"""Incremental document-intelligence layer for Duke/NCUC parsing workflows."""

from .models import (
    DocumentConfidence,
    DocumentFingerprintResult,
    DocumentRepresentation,
    DocumentSchemaType,
    DocumentType,
    ExtractionResult,
    NormalizedDocument,
    NormalizedPage,
    ParseLane,
    TrainingRecord,
    ValidationReport,
)
from .normalization import (
    DocumentNormalizationConfig,
    DocumentNormalizationRouter,
    DocumentNormalizer,
    GlmOcrNormalizer,
    NativePdfNormalizer,
    PaddleStructureNormalizer,
)
from .ollama_orchestrator import (
    OllamaOrchestrator,
    OllamaRunResult,
    RoleConfig,
    RoleHealth,
)
from .service import DocumentIntelligenceOrchestrator, HistoricalDocumentIntelligenceContext

__all__ = [
    "DocumentConfidence",
    "DocumentFingerprintResult",
    "DocumentNormalizationConfig",
    "DocumentNormalizationRouter",
    "DocumentNormalizer",
    "DocumentIntelligenceOrchestrator",
    "DocumentRepresentation",
    "DocumentSchemaType",
    "DocumentType",
    "ExtractionResult",
    "GlmOcrNormalizer",
    "HistoricalDocumentIntelligenceContext",
    "NativePdfNormalizer",
    "NormalizedDocument",
    "NormalizedPage",
    "OllamaOrchestrator",
    "OllamaRunResult",
    "ParseLane",
    "PaddleStructureNormalizer",
    "RoleConfig",
    "RoleHealth",
    "TrainingRecord",
    "ValidationReport",
]
