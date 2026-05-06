from __future__ import annotations

from pathlib import Path

import fitz

from duke_rates.benchmark import document_normalization_bench as bench
from duke_rates.document_intelligence.models import (
    DocumentRepresentation,
    NormalizationBackend,
    NormalizationMetrics,
    PageRepresentation,
)


class _StubNormalizer:
    def __init__(self, backend: NormalizationBackend, text: str, *, available: bool = True) -> None:
        self.backend = backend
        self._text = text
        self._available = available

    def is_available(self):
        return self._available, None if self._available else "not available"

    def normalize(self, doc, *, raw_text: str, page_artifacts):
        page = PageRepresentation(
            page_number=1,
            text=self._text,
            markdown=self._text,
            source=f"stub:{self.backend.value}",
            backend=self.backend,
        )
        return (
            [page],
            self._text,
            self._text,
            NormalizationMetrics(
                backend=self.backend,
                page_count=1,
                text_char_count=len(self._text),
                table_page_count=1 if self.backend == NormalizationBackend.PADDLE_STRUCTURE else 0,
            ),
            [],
        )


class _StubRouter:
    def __init__(self, config=None):
        self.config = config

    def normalize_historical_document(self, doc, *, raw_text: str, page_artifacts):
        return DocumentRepresentation(
            source_pdf=str(doc["local_path"]),
            raw_text="router text",
            markdown_text="router text",
            normalizer_backend=NormalizationBackend.PADDLE_STRUCTURE,
            pages=[
                PageRepresentation(
                    page_number=1,
                    text="router text",
                    markdown="router text",
                    source="stub:router",
                    backend=NormalizationBackend.PADDLE_STRUCTURE,
                )
            ],
            normalization_metrics=NormalizationMetrics(
                backend=NormalizationBackend.PADDLE_STRUCTURE,
                page_count=1,
                text_char_count=len("router text"),
                table_page_count=1,
            ),
        )


def _make_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Sample benchmark text for native extraction")
    doc.save(path)
    doc.close()


def test_run_normalization_benchmark_returns_all_variants(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)

    monkeypatch.setattr(
        bench,
        "NativePdfNormalizer",
        lambda: _StubNormalizer(NormalizationBackend.NATIVE_PDF, "native text"),
    )
    monkeypatch.setattr(
        bench,
        "PaddleStructureNormalizer",
        lambda config: _StubNormalizer(NormalizationBackend.PADDLE_STRUCTURE, "paddle text"),
    )
    monkeypatch.setattr(
        bench,
        "GlmOcrNormalizer",
        lambda config: _StubNormalizer(NormalizationBackend.GLM_OCR, "glm text"),
    )
    monkeypatch.setattr(bench, "DocumentNormalizationRouter", _StubRouter)

    result = bench.run_normalization_benchmark(
        str(pdf_path),
        label="sample",
        max_pages=1,
        enable_glm=True,
    )

    assert result["label"] == "sample"
    assert result["native"]["actual_backend"] == "native_pdf"
    assert result["paddle"]["actual_backend"] == "paddle_structure"
    assert result["glm"]["actual_backend"] == "glm_ocr"
    assert result["router"]["actual_backend"] == "paddle_structure"
