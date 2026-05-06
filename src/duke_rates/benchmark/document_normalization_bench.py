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
    DocumentNormalizationRouter,
    GlmOcrNormalizer,
    NativePdfNormalizer,
    PaddleStructureNormalizer,
)

logger = logging.getLogger(__name__)


@dataclass
class NormalizationBenchmarkResult:
    label: str
    pdf_path: str
    requested_backend: str
    actual_backend: str
    page_count: int
    elapsed_s: float
    text_chars: int
    markdown_chars: int
    low_text_pages: int
    table_pages: int
    warning_count: int
    used_gpu: bool
    available: bool
    similarity_vs_native: float | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_normalization_benchmark(
    pdf_path: str,
    *,
    label: str,
    max_pages: int = 2,
    enable_glm: bool = True,
    ollama_host: str | None = None,
) -> dict[str, Any]:
    source_pdf = Path(pdf_path)
    raw_text, total_pages = _extract_native_text(source_pdf, max_pages=max_pages)
    doc = {
        "id": None,
        "local_path": str(source_pdf),
        "content_hash": None,
        "company": None,
        "state": "NC",
        "family_key": None,
        "title": source_pdf.stem,
        "start_page": 1,
        "end_page": min(total_pages, max_pages),
    }

    config = DocumentNormalizationConfig(
        enable_glm_ocr=enable_glm,
        page_level_escalation=True,
        glm_max_pages=max_pages,
        ollama_host=ollama_host or "http://localhost:11434",
    )

    native = _run_backend(
        label,
        doc,
        raw_text=raw_text,
        backend_name="native",
        normalizer=NativePdfNormalizer(),
    )
    native_representation = native[1]

    paddle = _run_backend(
        label,
        doc,
        raw_text=raw_text,
        backend_name="paddle",
        normalizer=PaddleStructureNormalizer(config),
        baseline=native_representation,
    )
    glm = _run_backend(
        label,
        doc,
        raw_text=raw_text,
        backend_name="glm",
        normalizer=GlmOcrNormalizer(config),
        baseline=native_representation,
    )
    router = _run_router(
        label,
        doc,
        raw_text=raw_text,
        config=config,
        baseline=native_representation,
    )

    return {
        "label": label,
        "pdf_path": str(source_pdf),
        "max_pages": max_pages,
        "native": native[0].as_dict(),
        "paddle": paddle[0].as_dict(),
        "glm": glm[0].as_dict(),
        "router": router[0].as_dict(),
    }


def print_normalization_benchmark(result: dict[str, Any]) -> None:
    print(f"\n=== {result['label']} ===")
    print(result["pdf_path"])
    print(f"Pages benchmarked: {result['max_pages']}")
    print("")
    print(
        f"{'Variant':<10} {'Backend':<18} {'Avail':<5} {'Secs':>8} {'Chars':>8} {'Tables':>6} {'LowTxt':>6} {'Sim':>6}"
    )
    print("-" * 78)
    for key in ("native", "paddle", "glm", "router"):
        row = result[key]
        sim = row["similarity_vs_native"]
        sim_text = f"{sim:.2f}" if isinstance(sim, (int, float)) else "-"
        print(
            f"{key:<10} {row['actual_backend']:<18} "
            f"{str(row['available']):<5} {row['elapsed_s']:>8.2f} {row['text_chars']:>8} "
            f"{row['table_pages']:>6} {row['low_text_pages']:>6} {sim_text:>6}"
        )
        if row.get("error"):
            print(f"  error: {row['error']}")


def _run_backend(
    label: str,
    doc: dict[str, Any],
    *,
    raw_text: str,
    backend_name: str,
    normalizer: Any,
    baseline: DocumentRepresentation | None = None,
) -> tuple[NormalizationBenchmarkResult, DocumentRepresentation | None]:
    available, reason = normalizer.is_available()
    if not available:
        return (
            NormalizationBenchmarkResult(
                label=label,
                pdf_path=str(doc["local_path"]),
                requested_backend=backend_name,
                actual_backend=normalizer.backend.value,
                page_count=int(doc.get("end_page") or 1),
                elapsed_s=0.0,
                text_chars=0,
                markdown_chars=0,
                low_text_pages=0,
                table_pages=0,
                warning_count=0,
                used_gpu=False,
                available=False,
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
            file_hash=None,
            historical_document_id=None,
            company=doc.get("company"),
            state=doc.get("state"),
            family_key=doc.get("family_key"),
            title=doc.get("title"),
            page_start=doc.get("start_page"),
            page_end=doc.get("end_page"),
            raw_text=normalized_text,
            markdown_text=markdown_text,
            normalizer_backend=metrics.backend,
            pages=pages,
            warnings=warnings,
            normalization_metrics=metrics,
            document_metadata={},
        )
        return (
            _representation_to_result(
                label,
                str(doc["local_path"]),
                requested_backend=backend_name,
                representation=representation,
                elapsed_s=time.perf_counter() - start,
                baseline=baseline,
            ),
            representation,
        )
    except Exception as exc:
        logger.warning("Normalization benchmark backend %s failed for %s: %s", backend_name, doc["local_path"], exc)
        return (
            NormalizationBenchmarkResult(
                label=label,
                pdf_path=str(doc["local_path"]),
                requested_backend=backend_name,
                actual_backend=normalizer.backend.value,
                page_count=int(doc.get("end_page") or 1),
                elapsed_s=time.perf_counter() - start,
                text_chars=0,
                markdown_chars=0,
                low_text_pages=0,
                table_pages=0,
                warning_count=0,
                used_gpu=False,
                available=True,
                error=str(exc),
            ),
            None,
        )


def _run_router(
    label: str,
    doc: dict[str, Any],
    *,
    raw_text: str,
    config: DocumentNormalizationConfig,
    baseline: DocumentRepresentation | None = None,
) -> tuple[NormalizationBenchmarkResult, DocumentRepresentation | None]:
    router = DocumentNormalizationRouter(config=config)
    start = time.perf_counter()
    try:
        representation = router.normalize_historical_document(
            doc,
            raw_text=raw_text,
            page_artifacts=None,
        )
        return (
            _representation_to_result(
                label,
                str(doc["local_path"]),
                requested_backend="router",
                representation=representation,
                elapsed_s=time.perf_counter() - start,
                baseline=baseline,
            ),
            representation,
        )
    except Exception as exc:
        return (
            NormalizationBenchmarkResult(
                label=label,
                pdf_path=str(doc["local_path"]),
                requested_backend="router",
                actual_backend="router_failed",
                page_count=int(doc.get("end_page") or 1),
                elapsed_s=time.perf_counter() - start,
                text_chars=0,
                markdown_chars=0,
                low_text_pages=0,
                table_pages=0,
                warning_count=0,
                used_gpu=False,
                available=True,
                error=str(exc),
            ),
            None,
        )


def _representation_to_result(
    label: str,
    pdf_path: str,
    *,
    requested_backend: str,
    representation: DocumentRepresentation,
    elapsed_s: float,
    baseline: DocumentRepresentation | None,
) -> NormalizationBenchmarkResult:
    similarity = None
    if baseline is not None and baseline.raw_text and representation.raw_text:
        similarity = round(
            SequenceMatcher(None, baseline.raw_text[:12000], representation.raw_text[:12000]).ratio(),
            4,
        )
    return NormalizationBenchmarkResult(
        label=label,
        pdf_path=pdf_path,
        requested_backend=requested_backend,
        actual_backend=representation.normalizer_backend.value,
        page_count=len(representation.pages),
        elapsed_s=elapsed_s,
        text_chars=len(representation.raw_text),
        markdown_chars=len(representation.markdown_text or ""),
        low_text_pages=representation.normalization_metrics.low_text_page_count,
        table_pages=representation.normalization_metrics.table_page_count,
        warning_count=len(representation.warnings),
        used_gpu=representation.normalization_metrics.used_gpu,
        available=True,
        similarity_vs_native=similarity,
    )


def _extract_native_text(pdf_path: Path, *, max_pages: int) -> tuple[str, int]:
    text_parts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        for page in pdf.pages[:max_pages]:
            text_parts.append(page.extract_text() or "")
    return "\n\n".join(part for part in text_parts if part).strip(), total_pages


def write_results_json(results: list[dict[str, Any]], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
