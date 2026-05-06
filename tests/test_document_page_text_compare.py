from __future__ import annotations

from pathlib import Path

import fitz

from duke_rates.benchmark import document_page_text_compare as compare
from duke_rates.document_intelligence.models import (
    NormalizationBackend,
    NormalizationMetrics,
    PageRepresentation,
)


class _StubNormalizer:
    def __init__(
        self,
        backend: NormalizationBackend,
        text: str,
        *,
        available: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.backend = backend
        self._text = text
        self._available = available
        self._error = error

    def is_available(self):
        return self._available, None if self._available else "not available"

    def normalize(self, doc, *, raw_text: str, page_artifacts):
        if self._error:
            raise self._error
        page = PageRepresentation(
            page_number=int(doc["start_page"]),
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
            ),
            [],
        )


def _make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_find_suspicious_codes_detects_known_symbol_artifacts() -> None:
    text = "decrement of 0.0067 cVkWh and 0.0642 S/kWh plus 14.3914.94"
    codes = compare._find_suspicious_codes(text)
    assert "cent_as_cv" in codes
    assert "dollar_as_s" in codes
    assert "merged_decimal_values" in codes


def test_run_document_page_text_comparison_prefers_glm_when_expected_tokens_improve(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, "decrement of 0.0067 cVkWh")

    monkeypatch.setattr(
        compare,
        "PaddleStructureNormalizer",
        lambda config: _StubNormalizer(NormalizationBackend.PADDLE_STRUCTURE, "0.0067 cVkWh"),
    )
    monkeypatch.setattr(
        compare,
        "GlmOcrNormalizer",
        lambda config: _StubNormalizer(NormalizationBackend.GLM_OCR, "0.0067 ¢/kWh"),
    )

    result = compare.run_document_page_text_comparison(
        str(pdf_path),
        page_number=1,
        label="sample",
        expected_tokens=["¢/kWh"],
    )

    assert result["recommended_backend"] == "glm_ocr"
    assert result["native"]["missing_expected_tokens"] == ["¢/kWh"]
    assert result["glm"]["missing_expected_tokens"] == []


def test_run_document_page_text_comparison_reports_backend_failure(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, "plain text")

    monkeypatch.setattr(
        compare,
        "PaddleStructureNormalizer",
        lambda config: _StubNormalizer(
            NormalizationBackend.PADDLE_STRUCTURE,
            "",
            error=RuntimeError("paddle runtime failed"),
        ),
    )
    monkeypatch.setattr(
        compare,
        "GlmOcrNormalizer",
        lambda config: _StubNormalizer(NormalizationBackend.GLM_OCR, "plain text"),
    )

    result = compare.run_document_page_text_comparison(
        str(pdf_path),
        page_number=1,
        label="sample",
    )

    assert result["paddle"]["error"] == "paddle runtime failed"
    assert result["recommended_backend"] in {"native_pdf", "glm_ocr"}


def test_write_page_comparison_markdown_creates_summary(tmp_path: Path) -> None:
    output_path = tmp_path / "compare.md"
    compare.write_page_comparison_markdown(
        [
            {
                "label": "case-a",
                "pdf_path": "sample.pdf",
                "page_number": 4,
                "expected_tokens": ["¢/kWh"],
                "recommended_backend": "glm_ocr",
                "native": compare.PageTextVariantResult(
                    requested_backend="native",
                    actual_backend="native_pdf",
                    available=True,
                    elapsed_s=0.1,
                    text_chars=10,
                    warning_count=0,
                    suspicious_hits=1,
                    suspicious_codes=["cent_as_cv"],
                    missing_expected_tokens=["¢/kWh"],
                ).as_dict(),
                "paddle": compare.PageTextVariantResult(
                    requested_backend="paddle",
                    actual_backend="paddle_structure",
                    available=False,
                    elapsed_s=0.0,
                    text_chars=0,
                    warning_count=0,
                    suspicious_hits=0,
                    suspicious_codes=[],
                ).as_dict(),
                "glm": compare.PageTextVariantResult(
                    requested_backend="glm",
                    actual_backend="glm_ocr",
                    available=True,
                    elapsed_s=2.1,
                    text_chars=12,
                    warning_count=0,
                    suspicious_hits=0,
                    suspicious_codes=[],
                    missing_expected_tokens=[],
                ).as_dict(),
            }
        ],
        str(output_path),
    )

    content = output_path.read_text(encoding="utf-8")
    assert "case-a" in content
    assert "glm_ocr" in content
    assert "¢/kWh" in content
