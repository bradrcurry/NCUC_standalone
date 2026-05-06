from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from duke_rates.document_intelligence.dataset import TrainingRecordCollector
from duke_rates.document_intelligence.extraction import SchemaExtractionAdapter
from duke_rates.document_intelligence.fingerprinting import HybridDocumentFingerprinter
from duke_rates.document_intelligence.models import (
    DocumentConfidence,
    DocumentIntelligenceSnapshot,
)
from duke_rates.document_intelligence.normalization import (
    DocumentNormalizationConfig,
    DocumentNormalizationRouter,
)
from duke_rates.document_intelligence.representation import DocumentRepresentationBuilder
from duke_rates.document_intelligence.validation import ExtractionValidationEngine


@dataclass
class HistoricalDocumentIntelligenceContext:
    parser_profile: str | None
    charge_count: int
    status: str
    errors: list[str]


class DocumentIntelligenceOrchestrator:
    """Coordinate representation, fingerprinting, schema mapping, validation, and training capture."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        normalization_config: DocumentNormalizationConfig | None = None,
        normalization_router: DocumentNormalizationRouter | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.representation_builder = DocumentRepresentationBuilder()
        self.normalization_router = normalization_router or DocumentNormalizationRouter(
            normalization_config,
            builder=self.representation_builder,
        )
        self.fingerprinter = HybridDocumentFingerprinter()
        self.schema_adapter = SchemaExtractionAdapter()
        self.validator = ExtractionValidationEngine()
        self.collector = TrainingRecordCollector(
            self.project_root / "data" / "processed" / "document_intelligence"
        )

    def analyze_historical_document(
        self,
        doc: dict,
        *,
        raw_text: str,
        page_artifacts: list[dict] | None,
        context: HistoricalDocumentIntelligenceContext,
    ) -> DocumentIntelligenceSnapshot:
        representation = self.normalization_router.normalize_historical_document(
            doc,
            raw_text=raw_text,
            page_artifacts=page_artifacts,
        )
        fingerprint = self.fingerprinter.fingerprint(representation)
        extraction = self.schema_adapter.build_extraction_result(
            representation,
            fingerprint,
            parser_profile=context.parser_profile,
            charge_count=context.charge_count,
            status=context.status,
        )
        validation = self.validator.validate(representation, extraction)
        extraction.validation_passed = validation.passed

        validation_confidence = 1.0 if validation.passed and not validation.warnings else 0.7 if validation.passed else 0.35
        extraction_confidence = extraction.confidence
        classification_confidence = fingerprint.confidence
        overall = round(
            (classification_confidence * 0.3)
            + (extraction_confidence * 0.4)
            + (validation_confidence * 0.3),
            4,
        )
        confidence = DocumentConfidence(
            classification_confidence=classification_confidence,
            extraction_confidence=extraction_confidence,
            validation_confidence=validation_confidence,
            overall_confidence=overall,
        )
        training_record = self.collector.build_record(
            representation,
            fingerprint,
            extraction,
            validation,
            confidence,
            parser_used=context.parser_profile,
            errors=context.errors,
        )
        self.collector.append(training_record)
        return DocumentIntelligenceSnapshot(
            representation=representation,
            fingerprint=fingerprint,
            extraction=extraction,
            validation=validation,
            confidence=confidence,
            training_record=training_record,
        )
