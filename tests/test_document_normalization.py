from __future__ import annotations

import json
from pathlib import Path

from duke_rates.document_intelligence.models import (
    DocumentType,
    NormalizationBackend,
    NormalizationMetrics,
    PageRepresentation,
)
from duke_rates.document_intelligence.normalization import (
    DocumentNormalizationConfig,
    DocumentNormalizationRouter,
    DocumentNormalizer,
)
from duke_rates.document_intelligence.service import (
    DocumentIntelligenceOrchestrator,
    HistoricalDocumentIntelligenceContext,
)
from duke_rates.document_intelligence.normalization import PaddleStructureNormalizer


class _StubNormalizer(DocumentNormalizer):
    def __init__(
        self,
        backend: NormalizationBackend,
        *,
        available: bool = True,
        text: str = "",
        fail: bool = False,
    ) -> None:
        self.backend = backend
        self._available = available
        self._text = text
        self._fail = fail

    def is_available(self) -> tuple[bool, str | None]:
        return self._available, None if self._available else "disabled for test"

    def normalize(self, doc: dict, *, raw_text: str, page_artifacts: list[dict] | None):
        if self._fail:
            raise RuntimeError(f"{self.backend.value} failed")
        page_number = int(doc.get("start_page") or 1)
        text = self._text or raw_text
        pages = [
            PageRepresentation(
                page_number=page_number,
                text=text,
                markdown=text,
                source=f"stub:{self.backend.value}",
                backend=self.backend,
            )
        ]
        return (
            pages,
            text,
            text,
            NormalizationMetrics(
                backend=self.backend,
                page_count=1,
                text_char_count=len(text),
            ),
            [],
        )


def test_router_prefers_native_for_good_text_layer() -> None:
    router = DocumentNormalizationRouter(
        DocumentNormalizationConfig(
            enable_paddle_structure=False,
            enable_glm_ocr=False,
            page_level_escalation=False,
        ),
        native_normalizer=_StubNormalizer(NormalizationBackend.NATIVE_PDF, text="good native text"),
        paddle_normalizer=_StubNormalizer(NormalizationBackend.PADDLE_STRUCTURE, available=False),
        glm_normalizer=_StubNormalizer(NormalizationBackend.GLM_OCR, available=False),
    )

    decision = router.route_historical_document(
        {"id": 1, "start_page": 1, "local_path": "sample.pdf"},
        raw_text="This PDF already has a substantial text layer that should stay native.",
        page_artifacts=None,
    )

    assert decision.backend == NormalizationBackend.NATIVE_PDF


def test_router_chooses_paddle_for_low_text_when_available() -> None:
    router = DocumentNormalizationRouter(
        DocumentNormalizationConfig(
            native_min_text_chars=120,
            page_level_escalation=False,
        ),
        native_normalizer=_StubNormalizer(NormalizationBackend.NATIVE_PDF, text=""),
        paddle_normalizer=_StubNormalizer(
            NormalizationBackend.PADDLE_STRUCTURE,
            text="normalized with paddle",
        ),
        glm_normalizer=_StubNormalizer(NormalizationBackend.GLM_OCR, text="normalized with glm"),
    )

    decision = router.route_historical_document(
        {"id": 2, "start_page": 1, "local_path": "scan.pdf"},
        raw_text="too short",
        page_artifacts=None,
    )

    assert decision.backend == NormalizationBackend.PADDLE_STRUCTURE


def test_router_falls_back_to_glm_when_paddle_fails() -> None:
    router = DocumentNormalizationRouter(
        DocumentNormalizationConfig(page_level_escalation=False),
        native_normalizer=_StubNormalizer(NormalizationBackend.NATIVE_PDF, text=""),
        paddle_normalizer=_StubNormalizer(
            NormalizationBackend.PADDLE_STRUCTURE,
            text="",
            fail=True,
        ),
        glm_normalizer=_StubNormalizer(NormalizationBackend.GLM_OCR, text="glm recovered text"),
    )

    representation = router.normalize_historical_document(
        {"id": 3, "start_page": 1, "local_path": "scan.pdf"},
        raw_text="short",
        page_artifacts=None,
    )

    assert representation.normalizer_backend == NormalizationBackend.GLM_OCR
    assert representation.raw_text == "glm recovered text"
    assert representation.normalization_metrics.fallback_backend == NormalizationBackend.PADDLE_STRUCTURE
    assert any(w.code == "paddle_fallback_to_glm" for w in representation.warnings)
    assert representation.document_metadata["requested_normalizer_backend"] == "paddle_structure"
    assert representation.document_metadata["actual_normalizer_backend"] == "glm_ocr"
    assert representation.document_metadata["normalization_fallback_backend"] == "paddle_structure"


def test_router_escalates_symbol_noise_page_to_glm() -> None:
    router = DocumentNormalizationRouter(
        DocumentNormalizationConfig(
            enable_native_pdf=True,
            enable_paddle_structure=False,
            enable_glm_ocr=True,
            page_level_escalation=True,
            native_min_text_chars=20,
            suspicious_page_text_chars=20,
            suspicious_text_ratio=0.9,
            suspicious_symbol_min_hits=1,
        ),
        native_normalizer=_StubNormalizer(
            NormalizationBackend.NATIVE_PDF,
            text="Prospective Rider decrement of 0.0067 cVkWh established September 25 2013",
        ),
        paddle_normalizer=_StubNormalizer(NormalizationBackend.PADDLE_STRUCTURE, available=False),
        glm_normalizer=_StubNormalizer(
            NormalizationBackend.GLM_OCR,
            text="Prospective Rider decrement of 0.0067 ¢/kWh established September 25 2013",
        ),
    )

    representation = router.normalize_historical_document(
        {"id": 5, "start_page": 1, "local_path": "scan.pdf"},
        raw_text="Prospective Rider decrement of 0.0067 cVkWh established September 25 2013",
        page_artifacts=None,
    )

    assert representation.raw_text.endswith("0.0067 ¢/kWh established September 25 2013")
    assert any(w.code == "page_level_glm_escalation" for w in representation.warnings)


def test_router_falls_back_to_native_when_paddle_fails_and_glm_disabled() -> None:
    router = DocumentNormalizationRouter(
        DocumentNormalizationConfig(
            enable_native_pdf=True,
            enable_paddle_structure=True,
            enable_glm_ocr=False,
            page_level_escalation=False,
        ),
        native_normalizer=_StubNormalizer(NormalizationBackend.NATIVE_PDF, text="native recovered text"),
        paddle_normalizer=_StubNormalizer(
            NormalizationBackend.PADDLE_STRUCTURE,
            fail=True,
        ),
        glm_normalizer=_StubNormalizer(NormalizationBackend.GLM_OCR, available=False),
    )

    representation = router.normalize_historical_document(
        {"id": 4, "start_page": 1, "local_path": "scan.pdf"},
        raw_text="short",
        page_artifacts=None,
    )

    assert representation.normalizer_backend == NormalizationBackend.NATIVE_PDF
    assert representation.raw_text == "native recovered text"
    assert representation.normalization_metrics.fallback_backend == NormalizationBackend.PADDLE_STRUCTURE
    assert any(w.code == "backend_failed_fallback_to_native" for w in representation.warnings)
    assert representation.document_metadata["requested_normalizer_backend"] == "paddle_structure"
    assert representation.document_metadata["actual_normalizer_backend"] == "native_pdf"
    assert representation.document_metadata["normalization_fallback_backend"] == "paddle_structure"


def test_orchestrator_training_record_captures_normalization_metadata(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    pdf_path = project_root / "scan.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    router = DocumentNormalizationRouter(
        DocumentNormalizationConfig(
            enable_native_pdf=False,
            enable_paddle_structure=True,
            enable_glm_ocr=False,
            page_level_escalation=False,
        ),
        native_normalizer=_StubNormalizer(NormalizationBackend.NATIVE_PDF, text="unused"),
        paddle_normalizer=_StubNormalizer(
            NormalizationBackend.PADDLE_STRUCTURE,
            text="NC Original Leaf No. 503\nRider CPP\n0.1234 $/kWh",
        ),
        glm_normalizer=_StubNormalizer(NormalizationBackend.GLM_OCR, available=False),
    )
    orchestrator = DocumentIntelligenceOrchestrator(
        project_root=project_root,
        normalization_router=router,
    )

    snapshot = orchestrator.analyze_historical_document(
        {
            "id": 42,
            "local_path": str(pdf_path),
            "content_hash": "hash-42",
            "company": "progress",
            "state": "NC",
            "family_key": "nc-progress-leaf-503",
            "title": "Rider CPP",
            "effective_start": "2025-01-01",
            "leaf_no": "503",
            "start_page": 1,
            "end_page": 1,
        },
        raw_text="short",
        page_artifacts=None,
        context=HistoricalDocumentIntelligenceContext(
            parser_profile="progress_single_value_rider",
            charge_count=1,
            status="ok",
            errors=[],
        ),
    )

    assert snapshot.representation.normalizer_backend == NormalizationBackend.PADDLE_STRUCTURE
    assert snapshot.fingerprint.doc_type == DocumentType.RIDER

    training_path = (
        project_root / "data" / "processed" / "document_intelligence" / "training_records.jsonl"
    )
    payload = json.loads(training_path.read_text(encoding="utf-8").strip())
    assert payload["input_features"]["normalizer_backend"] == "paddle_structure"
    assert payload["input_features"]["markdown_available"] is True


def test_paddle_predict_page_uses_predict_and_to_dict() -> None:
    class _ResultObject:
        def to_dict(self):
            return {
                "type": "text",
                "text": "hello world",
                "score": 0.95,
                "bbox": [1, 2, 3, 4],
            }

    class _Engine:
        def predict(self, image_array):
            return [_ResultObject()]

    items = PaddleStructureNormalizer._predict_page(_Engine(), image_array=[[0]])
    assert items == [
        {
            "type": "text",
            "text": "hello world",
            "score": 0.95,
            "bbox": [1, 2, 3, 4],
        }
    ]
