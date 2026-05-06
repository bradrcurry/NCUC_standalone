from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pdfplumber

from duke_rates.document_intelligence.models import DocumentRepresentation, NormalizationBackend
from duke_rates.document_intelligence.normalization import (
    DocumentNormalizationConfig,
    GlmOcrNormalizer,
    PaddleStructureNormalizer,
)
from duke_rates.document_intelligence.text_quality import analyze_text_quality

logger = logging.getLogger(__name__)


@dataclass
class PageTextVariantResult:
    requested_backend: str
    actual_backend: str
    available: bool
    elapsed_s: float
    text_chars: int
    warning_count: int
    suspicious_hits: int
    suspicious_codes: list[str]
    similarity_vs_native: float | None = None
    expected_token_hits: list[str] | None = None
    missing_expected_tokens: list[str] | None = None
    error: str | None = None
    text_preview: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_document_page_text_comparison(
    pdf_path: str,
    *,
    page_number: int,
    label: str,
    expected_tokens: list[str] | None = None,
    enable_glm: bool = True,
    enable_paddle: bool = True,
    ollama_host: str | None = None,
    preview_chars: int = 500,
) -> dict[str, Any]:
    source_pdf = Path(pdf_path)
    native_text = _extract_native_page_text(source_pdf, page_number=page_number)
    doc = {
        "id": None,
        "local_path": str(source_pdf),
        "content_hash": None,
        "company": None,
        "state": "NC",
        "family_key": None,
        "title": source_pdf.stem,
        "start_page": page_number,
        "end_page": page_number,
    }
    config = DocumentNormalizationConfig(
        enable_glm_ocr=enable_glm,
        enable_paddle_structure=enable_paddle,
        page_level_escalation=False,
        glm_max_pages=1,
        paddle_max_pages_per_batch=1,
        ollama_host=ollama_host or "http://localhost:11434",
    )

    native_result, native_representation = _native_result(
        native_text,
        expected_tokens=expected_tokens,
        preview_chars=preview_chars,
    )
    paddle_result = _run_normalizer(
        doc,
        raw_text=native_text,
        requested_backend="paddle",
        normalizer=PaddleStructureNormalizer(config),
        baseline=native_representation,
        expected_tokens=expected_tokens,
        preview_chars=preview_chars,
    )
    glm_result = _run_normalizer(
        doc,
        raw_text=native_text,
        requested_backend="glm",
        normalizer=GlmOcrNormalizer(config),
        baseline=native_representation,
        expected_tokens=expected_tokens,
        preview_chars=preview_chars,
    )

    recommended_backend = _recommend_backend(
        native_result,
        paddle_result[0],
        glm_result[0],
    )
    return {
        "label": label,
        "pdf_path": str(source_pdf),
        "page_number": page_number,
        "expected_tokens": expected_tokens or [],
        "recommended_backend": recommended_backend,
        "native": native_result.as_dict(),
        "paddle": paddle_result[0].as_dict(),
        "glm": glm_result[0].as_dict(),
    }


def print_document_page_text_comparison(result: dict[str, Any]) -> None:
    print(f"\n=== {result['label']} | page {result['page_number']} ===")
    print(result["pdf_path"])
    if result["expected_tokens"]:
        print(f"Expected tokens: {', '.join(result['expected_tokens'])}")
    print(f"Recommended backend: {result['recommended_backend']}")
    print("")
    print(
        f"{'Variant':<8} {'Backend':<18} {'Avail':<5} {'Secs':>7} {'Chars':>7} {'Warn':>5} {'Susp':>5} {'Sim':>6}"
    )
    print("-" * 74)
    for key in ("native", "paddle", "glm"):
        row = result[key]
        similarity = row.get("similarity_vs_native")
        similarity_text = f"{similarity:.2f}" if isinstance(similarity, (int, float)) else "-"
        print(
            f"{key:<8} {row['actual_backend']:<18} {str(row['available']):<5} "
            f"{row['elapsed_s']:>7.2f} {row['text_chars']:>7} {row['warning_count']:>5} "
            f"{row['suspicious_hits']:>5} {similarity_text:>6}"
        )
        if row.get("missing_expected_tokens"):
            print(f"  missing expected: {', '.join(row['missing_expected_tokens'])}")
        if row.get("suspicious_codes"):
            print(f"  suspicious: {', '.join(row['suspicious_codes'])}")
        if row.get("error"):
            print(f"  error: {row['error']}")
        if row.get("text_preview"):
            print(f"  preview: {row['text_preview']}")


def write_page_comparison_json(results: list[dict[str, Any]], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")


def write_page_comparison_markdown(results: list[dict[str, Any]], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Document Page Text Comparison",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"## {result['label']} (page {result['page_number']})",
                "",
                f"- PDF: `{result['pdf_path']}`",
                f"- Recommended backend: `{result['recommended_backend']}`",
            ]
        )
        if result["expected_tokens"]:
            lines.append(f"- Expected tokens: `{', '.join(result['expected_tokens'])}`")
        lines.extend(
            [
                "",
                "| Variant | Backend | Available | Seconds | Chars | Warnings | Suspicious | Missing Expected |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for key in ("native", "paddle", "glm"):
            row = result[key]
            missing = ", ".join(row.get("missing_expected_tokens") or []) or "-"
            lines.append(
                "| "
                + " | ".join(
                    [
                        key,
                        row["actual_backend"],
                        str(row["available"]),
                        f"{row['elapsed_s']:.2f}",
                        str(row["text_chars"]),
                        str(row["warning_count"]),
                        str(row["suspicious_hits"]),
                        missing,
                    ]
                )
                + " |"
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _native_result(
    text: str,
    *,
    expected_tokens: list[str] | None,
    preview_chars: int,
) -> tuple[PageTextVariantResult, DocumentRepresentation]:
    suspicious_codes = _find_suspicious_codes(text)
    token_hits, missing_tokens = _evaluate_expected_tokens(text, expected_tokens)
    representation = DocumentRepresentation(
        source_pdf="",
        raw_text=text,
        normalizer_backend=NormalizationBackend.NATIVE_PDF,
    )
    return (
        PageTextVariantResult(
            requested_backend="native",
            actual_backend=NormalizationBackend.NATIVE_PDF.value,
            available=True,
            elapsed_s=0.0,
            text_chars=len(text),
            warning_count=0,
            suspicious_hits=len(suspicious_codes),
            suspicious_codes=suspicious_codes,
            similarity_vs_native=1.0,
            expected_token_hits=token_hits,
            missing_expected_tokens=missing_tokens,
            text_preview=_preview_text(text, preview_chars),
        ),
        representation,
    )


def _run_normalizer(
    doc: dict[str, Any],
    *,
    raw_text: str,
    requested_backend: str,
    normalizer: Any,
    baseline: DocumentRepresentation,
    expected_tokens: list[str] | None,
    preview_chars: int,
) -> tuple[PageTextVariantResult, DocumentRepresentation | None]:
    available, reason = normalizer.is_available()
    if not available:
        return (
            PageTextVariantResult(
                requested_backend=requested_backend,
                actual_backend=normalizer.backend.value,
                available=False,
                elapsed_s=0.0,
                text_chars=0,
                warning_count=0,
                suspicious_hits=0,
                suspicious_codes=[],
                expected_token_hits=[],
                missing_expected_tokens=expected_tokens or [],
                error=reason,
            ),
            None,
        )

    start = time.perf_counter()
    try:
        pages, normalized_text, markdown_text, metrics, warnings = normalizer.normalize(
            doc,
            raw_text=raw_text,
            page_artifacts=None,
        )
        representation = DocumentRepresentation(
            source_pdf=str(doc["local_path"]),
            raw_text=normalized_text,
            markdown_text=markdown_text,
            normalizer_backend=metrics.backend,
            pages=pages,
            warnings=warnings,
            normalization_metrics=metrics,
        )
        suspicious_codes = _find_suspicious_codes(representation.raw_text)
        token_hits, missing_tokens = _evaluate_expected_tokens(representation.raw_text, expected_tokens)
        similarity = None
        if baseline.raw_text and representation.raw_text:
            similarity = round(
                SequenceMatcher(None, baseline.raw_text[:12000], representation.raw_text[:12000]).ratio(),
                4,
            )
        return (
            PageTextVariantResult(
                requested_backend=requested_backend,
                actual_backend=representation.normalizer_backend.value,
                available=True,
                elapsed_s=time.perf_counter() - start,
                text_chars=len(representation.raw_text),
                warning_count=len(representation.warnings),
                suspicious_hits=len(suspicious_codes),
                suspicious_codes=suspicious_codes,
                similarity_vs_native=similarity,
                expected_token_hits=token_hits,
                missing_expected_tokens=missing_tokens,
                text_preview=_preview_text(representation.raw_text, preview_chars),
            ),
            representation,
        )
    except Exception as exc:
        logger.warning("Page text comparison backend %s failed for %s: %s", requested_backend, doc["local_path"], exc)
        return (
            PageTextVariantResult(
                requested_backend=requested_backend,
                actual_backend=normalizer.backend.value,
                available=True,
                elapsed_s=time.perf_counter() - start,
                text_chars=0,
                warning_count=0,
                suspicious_hits=0,
                suspicious_codes=[],
                expected_token_hits=[],
                missing_expected_tokens=expected_tokens or [],
                error=str(exc),
            ),
            None,
        )


def _extract_native_page_text(pdf_path: Path, *, page_number: int) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number - 1]
        return (page.extract_text() or "").strip()


def _find_suspicious_codes(text: str) -> list[str]:
    return analyze_text_quality(text).suspicious_codes


def _evaluate_expected_tokens(text: str, expected_tokens: list[str] | None) -> tuple[list[str], list[str]]:
    hits: list[str] = []
    missing: list[str] = []
    haystack = text or ""
    for token in expected_tokens or []:
        if token in haystack:
            hits.append(token)
        else:
            missing.append(token)
    return hits, missing


def _preview_text(text: str, preview_chars: int) -> str | None:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return None
    if len(cleaned) <= preview_chars:
        return cleaned
    return cleaned[: preview_chars - 3] + "..."


def _recommend_backend(
    native: PageTextVariantResult,
    paddle: PageTextVariantResult,
    glm: PageTextVariantResult,
) -> str:
    candidates = [native, paddle, glm]
    best = native
    best_score = _score_variant(native)
    for candidate in candidates[1:]:
        if not candidate.available or candidate.error:
            continue
        score = _score_variant(candidate)
        if score > best_score:
            best = candidate
            best_score = score
    return best.actual_backend


def _score_variant(result: PageTextVariantResult) -> tuple[int, int, int]:
    return (
        -len(result.missing_expected_tokens or []),
        -result.suspicious_hits,
        result.text_chars,
    )
