from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.document_intelligence.models import (
    DocumentConfidence,
    DocumentFingerprintResult,
    DocumentRepresentation,
    ExtractionResult,
    TrainingRecord,
    ValidationReport,
)


class TrainingRecordCollector:
    """Persist ML-ready training records as append-only JSONL sidecars."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_path = self.output_dir / "training_records.jsonl"
        self._lock = threading.Lock()

    def build_record(
        self,
        representation: DocumentRepresentation,
        fingerprint: DocumentFingerprintResult,
        extraction: ExtractionResult,
        validation: ValidationReport,
        confidence: DocumentConfidence,
        *,
        parser_used: str | None,
        errors: list[str] | None = None,
    ) -> TrainingRecord:
        return TrainingRecord(
            source_pdf=representation.source_pdf,
            historical_document_id=representation.historical_document_id,
            family_key=representation.family_key,
            doc_type=fingerprint.doc_type.value,
            parse_lane=fingerprint.parse_lane.value,
            parser_used=parser_used,
            input_features={
                "family_key": representation.family_key,
                "title": representation.title,
                "page_count": len(representation.pages),
                "text_length": len(representation.raw_text),
                "normalizer_backend": representation.normalizer_backend.value,
                "markdown_available": bool(representation.markdown_text),
                "normalization_warning_count": len(representation.warnings),
                "table_page_count": representation.normalization_metrics.table_page_count,
                "low_text_page_count": representation.normalization_metrics.low_text_page_count,
                "used_gpu": representation.normalization_metrics.used_gpu,
            },
            fingerprint_result=fingerprint.model_dump(mode="json"),
            predicted_output=extraction.model_dump(mode="json"),
            validated_output=extraction.data,
            validation_report=validation.model_dump(mode="json"),
            confidence=confidence.model_dump(mode="json"),
            errors=errors or [],
            created_at=datetime.now(UTC).isoformat(),
        )

    def append(self, record: TrainingRecord) -> None:
        with self._lock:
            with self.output_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True))
                handle.write("\n")
