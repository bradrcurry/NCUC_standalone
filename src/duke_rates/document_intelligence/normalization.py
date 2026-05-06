from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

from duke_rates.document_intelligence.models import (
    BoundingBox,
    ExtractedTable,
    LayoutBlock,
    NormalizationBackend,
    NormalizationMetrics,
    NormalizationWarning,
    PageRepresentation,
)
from duke_rates.document_intelligence.native_tables import extract_native_tables_for_page
from duke_rates.document_intelligence.representation import DocumentRepresentationBuilder
from duke_rates.document_intelligence.text_quality import analyze_text_quality

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NormalizationRouteDecision:
    backend: NormalizationBackend
    reason: str
    fallback_backend: NormalizationBackend | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentNormalizationConfig:
    enable_native_pdf: bool = True
    enable_paddle_structure: bool = True
    enable_glm_ocr: bool = True
    prefer_gpu: bool = True
    native_min_text_chars: int = 120
    suspicious_page_text_chars: int = 40
    suspicious_text_ratio: float = 0.35
    enable_symbol_noise_escalation: bool = True
    suspicious_symbol_min_hits: int = 1
    paddle_use_gpu: bool | None = None
    paddle_page_batch_size: int = 1
    paddle_max_pages_per_batch: int = 4
    paddle_worker_count: int = 1
    paddle_render_dpi: int = 160
    glm_model: str = "glm-ocr"
    ollama_host: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    )
    glm_timeout_seconds: float = 90.0
    glm_max_pages: int = 3
    glm_retry_count: int = 1
    page_level_escalation: bool = True


class DocumentNormalizer(ABC):
    backend: NormalizationBackend

    @abstractmethod
    def is_available(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    @abstractmethod
    def normalize(
        self,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None,
    ) -> tuple[list[PageRepresentation], str, str | None, NormalizationMetrics, list[NormalizationWarning]]:
        raise NotImplementedError


class NativePdfNormalizer(DocumentNormalizer):
    backend = NormalizationBackend.NATIVE_PDF

    def __init__(self, *, builder: DocumentRepresentationBuilder | None = None) -> None:
        self.builder = builder or DocumentRepresentationBuilder()

    def is_available(self) -> tuple[bool, str | None]:
        if pdfplumber is None:
            return False, "pdfplumber not installed"
        return True, None

    def normalize(
        self,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None,
    ) -> tuple[list[PageRepresentation], str, str | None, NormalizationMetrics, list[NormalizationWarning]]:
        if page_artifacts:
            representation = self.builder.build_historical_document(
                doc,
                raw_text=raw_text,
                page_artifacts=page_artifacts,
            )
            metrics = representation.normalization_metrics.model_copy(
                update={
                    "backend": NormalizationBackend.PAGE_ARTIFACT,
                    "page_count": len(representation.pages),
                    "text_char_count": len(representation.raw_text),
                }
            )
            for page in representation.pages:
                page.backend = NormalizationBackend.PAGE_ARTIFACT
            return representation.pages, representation.raw_text, representation.markdown_text, metrics, []

        source_pdf = Path(str(doc.get("local_path") or ""))
        if not source_pdf.exists() or pdfplumber is None:
            page_number = int(doc.get("start_page") or 1)
            pages = [
                PageRepresentation(
                    page_number=page_number,
                    text=raw_text,
                    source="bounded_text",
                    backend=NormalizationBackend.NATIVE_PDF,
                    blocks=self.builder._build_blocks(raw_text, page_number),
                )
            ]
            metrics = NormalizationMetrics(
                backend=NormalizationBackend.NATIVE_PDF,
                page_count=1,
                text_char_count=len(raw_text),
            )
            return pages, raw_text, None, metrics, []

        warnings: list[NormalizationWarning] = []
        start = time.perf_counter()
        pages: list[PageRepresentation] = []
        page_texts: list[str] = []
        page_start = max(int(doc.get("start_page") or 1), 1)
        page_end = int(doc.get("end_page") or page_start)
        try:
            with pdfplumber.open(source_pdf) as pdf:
                selected_pages = pdf.pages[page_start - 1 : page_end]
                for idx, page in enumerate(selected_pages, start=page_start):
                    text = page.extract_text() or ""
                    table_result = extract_native_tables_for_page(
                        source_pdf=source_pdf,
                        pdfplumber_page=page,
                        page_number=idx,
                    )
                    page_texts.append(text)
                    pages.append(
                        PageRepresentation(
                            page_number=idx,
                            text=text,
                            width=float(page.width) if page.width is not None else None,
                            height=float(page.height) if page.height is not None else None,
                            source="pdfplumber",
                            backend=NormalizationBackend.NATIVE_PDF,
                            blocks=self.builder._build_blocks(text, idx),
                            tables=table_result.tables,
                            metadata={
                                "table_backend": table_result.backend,
                                **(table_result.metadata or {}),
                            },
                        )
                    )
        except Exception as exc:
            warnings.append(
                NormalizationWarning(
                    code="native_pdf_failed",
                    message=str(exc),
                    backend=self.backend.value,
                )
            )
            page_number = int(doc.get("start_page") or 1)
            pages = [
                PageRepresentation(
                    page_number=page_number,
                    text=raw_text,
                    source="bounded_text",
                    backend=NormalizationBackend.NATIVE_PDF,
                    blocks=self.builder._build_blocks(raw_text, page_number),
                )
            ]
            page_texts = [raw_text]

        combined = "\n\n".join(page_texts).strip() or raw_text
        metrics = NormalizationMetrics(
            backend=NormalizationBackend.NATIVE_PDF,
            page_count=len(pages),
            text_char_count=len(combined),
            low_text_page_count=sum(1 for item in pages if len(item.text.strip()) < 40),
            table_page_count=sum(1 for item in pages if item.tables),
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
        return pages, combined, None, metrics, warnings


class PaddleStructureNormalizer(DocumentNormalizer):
    backend = NormalizationBackend.PADDLE_STRUCTURE

    def __init__(self, config: DocumentNormalizationConfig, *, builder: DocumentRepresentationBuilder | None = None) -> None:
        self.config = config
        self.builder = builder or DocumentRepresentationBuilder()

    def is_available(self) -> tuple[bool, str | None]:
        try:
            import fitz  # noqa: F401
        except ImportError:
            return False, "pymupdf not installed"
        try:
            import paddleocr  # noqa: F401
        except ImportError:
            return False, "paddleocr not installed"
        return True, None

    def normalize(
        self,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None,
    ) -> tuple[list[PageRepresentation], str, str | None, NormalizationMetrics, list[NormalizationWarning]]:
        import fitz
        import numpy as np
        from PIL import Image

        use_gpu = _resolve_gpu_preference(self.config.paddle_use_gpu, self.config.prefer_gpu)
        warnings: list[NormalizationWarning] = []
        start = time.perf_counter()
        source_pdf = Path(str(doc.get("local_path") or ""))
        if not source_pdf.exists():
            raise FileNotFoundError(f"Source PDF not found: {source_pdf}")

        engine = self._build_engine(use_gpu=use_gpu)
        page_start = max(int(doc.get("start_page") or 1), 1)
        page_end = int(doc.get("end_page") or page_start)
        pages: list[PageRepresentation] = []
        markdown_parts: list[str] = []
        text_parts: list[str] = []
        table_page_count = 0
        with fitz.open(source_pdf) as pdf:
            selected_pages = pdf[page_start - 1 : page_end]
            for batch in _chunk_iterable(list(enumerate(selected_pages, start=page_start)), self.config.paddle_max_pages_per_batch):
                for page_number, page in batch:
                    image = _render_fitz_page(
                        page,
                        dpi=self.config.paddle_render_dpi,
                    )
                    page_representation, page_markdown, page_warning = self._normalize_page(
                        engine,
                        image,
                        page_number=page_number,
                    )
                    if page_warning:
                        warnings.append(page_warning)
                    pages.append(page_representation)
                    text_parts.append(page_representation.text)
                    if page_markdown:
                        markdown_parts.append(page_markdown)
                    if page_representation.tables:
                        table_page_count += 1

        combined_text = "\n\n".join(item for item in text_parts if item).strip() or raw_text
        markdown_text = "\n\n".join(item for item in markdown_parts if item).strip() or None
        metrics = NormalizationMetrics(
            backend=NormalizationBackend.PADDLE_STRUCTURE,
            used_gpu=use_gpu,
            page_count=len(pages),
            page_batch_size=self.config.paddle_page_batch_size,
            render_dpi=self.config.paddle_render_dpi,
            text_char_count=len(combined_text),
            low_text_page_count=sum(1 for item in pages if len(item.text.strip()) < self.config.suspicious_page_text_chars),
            table_page_count=table_page_count,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
            metadata={
                "worker_count": self.config.paddle_worker_count,
            },
        )
        return pages, combined_text, markdown_text, metrics, warnings

    def _build_engine(self, *, use_gpu: bool) -> Any:
        import inspect
        import paddleocr

        engine_ctor = getattr(paddleocr, "PPStructureV3", None) or getattr(paddleocr, "PPStructure", None)
        if engine_ctor is None:
            raise RuntimeError("PaddleOCR PP-Structure backend unavailable")
        signature = inspect.signature(engine_ctor)
        kwargs: dict[str, Any] = {}
        if "show_log" in signature.parameters:
            kwargs["show_log"] = False
        if "device" in signature.parameters:
            kwargs["device"] = "gpu:0" if use_gpu else "cpu"
        elif "use_gpu" in signature.parameters:
            kwargs["use_gpu"] = use_gpu
        return engine_ctor(**kwargs)

    def _normalize_page(
        self,
        engine: Any,
        image: Any,
        *,
        page_number: int,
    ) -> tuple[PageRepresentation, str | None, NormalizationWarning | None]:
        import numpy as np

        image_array = np.array(image)
        try:
            result = self._predict_page(engine, image_array)
        except Exception as exc:
            return (
                PageRepresentation(
                    page_number=page_number,
                    text="",
                    source="paddle_structure",
                    backend=NormalizationBackend.PADDLE_STRUCTURE,
                ),
                None,
                NormalizationWarning(
                    code="paddle_page_failed",
                    message=str(exc),
                    page_number=page_number,
                    backend=self.backend.value,
                ),
            )

        text_fragments: list[str] = []
        blocks: list[LayoutBlock] = []
        tables: list[ExtractedTable] = []
        markdown_lines: list[str] = []
        items = result if isinstance(result, list) else [result]
        for item in items:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or item.get("label") or "text").lower()
            if block_type == "table":
                table = self._table_from_paddle(item, page_number)
                tables.append(table)
                if table.markdown:
                    markdown_lines.append(table.markdown)
                if table.rows:
                    text_fragments.append("\n".join(" | ".join(row) for row in table.rows))
                continue
            text = self._extract_text_from_paddle_item(item)
            if text:
                text_fragments.append(text)
                markdown_lines.append(text)
            blocks.append(
                LayoutBlock(
                    block_type=block_type,
                    text=text,
                    page_number=page_number,
                    confidence=float(item.get("score") or item.get("confidence") or 0.0),
                    bbox=_bbox_from_iterable(item.get("bbox") or item.get("box")),
                    metadata={"source_backend": self.backend.value},
                )
            )

        page_text = "\n".join(fragment for fragment in text_fragments if fragment).strip()
        return (
            PageRepresentation(
                page_number=page_number,
                text=page_text,
                markdown="\n\n".join(markdown_lines).strip() or None,
                source="paddle_structure",
                backend=NormalizationBackend.PADDLE_STRUCTURE,
                blocks=blocks,
                tables=tables,
                metadata={"source_backend": self.backend.value},
            ),
            "\n\n".join(markdown_lines).strip() or None,
            None,
        )

    @staticmethod
    def _predict_page(engine: Any, image_array: Any) -> list[dict[str, Any]]:
        raw_result: Any
        if hasattr(engine, "predict"):
            raw_result = engine.predict(image_array)
        else:
            raw_result = engine(image_array)

        items = raw_result if isinstance(raw_result, list) else list(raw_result)
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                normalized.append(item)
                continue
            to_dict = getattr(item, "to_dict", None)
            if callable(to_dict):
                try:
                    as_dict = to_dict()
                    if isinstance(as_dict, dict):
                        normalized.append(as_dict)
                        continue
                except Exception:
                    pass
        return normalized

    def _table_from_paddle(self, item: dict[str, Any], page_number: int) -> ExtractedTable:
        html = item.get("res", {}).get("html") if isinstance(item.get("res"), dict) else item.get("html")
        rows: list[list[str]] = []
        if isinstance(item.get("res"), dict) and isinstance(item["res"].get("cells"), list):
            rows = [[str(cell) for cell in row] for row in item["res"]["cells"]]
        return ExtractedTable(
            page_number=page_number,
            row_count=len(rows),
            column_count=max((len(row) for row in rows), default=0),
            rows=rows,
            markdown=_rows_to_markdown(rows) if rows else None,
            html=html,
            bbox=_bbox_from_iterable(item.get("bbox") or item.get("box")),
            confidence=float(item.get("score") or item.get("confidence") or 0.0),
            metadata={"source_backend": self.backend.value},
        )

    @staticmethod
    def _extract_text_from_paddle_item(item: dict[str, Any]) -> str:
        if isinstance(item.get("text"), str):
            return str(item["text"]).strip()
        if isinstance(item.get("res"), dict):
            res = item["res"]
            if isinstance(res.get("text"), str):
                return str(res["text"]).strip()
            if isinstance(res.get("texts"), list):
                return "\n".join(str(v) for v in res["texts"] if v).strip()
        return ""


class GlmOcrNormalizer(DocumentNormalizer):
    backend = NormalizationBackend.GLM_OCR

    def __init__(self, config: DocumentNormalizationConfig, *, builder: DocumentRepresentationBuilder | None = None) -> None:
        self.config = config
        self.builder = builder or DocumentRepresentationBuilder()
        self._availability_cache: tuple[bool, str | None] | None = None

    def is_available(self) -> tuple[bool, str | None]:
        if self._availability_cache is not None:
            return self._availability_cache

        if shutil.which("ollama") is None and not self.config.ollama_host:
            self._availability_cache = (False, "ollama not found")
            return self._availability_cache

        host = self.config.ollama_host.rstrip("/")
        model = self.config.glm_model
        # Probe daemon and model in one shot. A broken model load returns 500
        # per call and burns ~5s each; one upfront probe avoids that storm.
        try:
            with httpx.Client(timeout=5.0) as client:
                tags = client.get(f"{host}/api/tags")
                tags.raise_for_status()
                names = {m.get("name", "") for m in (tags.json().get("models") or [])}
                # Ollama tag names may include ":latest" suffix
                if model not in names and f"{model}:latest" not in names:
                    self._availability_cache = (
                        False,
                        f"ollama model {model!r} not present at {host}",
                    )
                    return self._availability_cache

            # Tiny generate call confirms the model can actually load.
            with httpx.Client(timeout=30.0) as client:
                gen = client.post(
                    f"{host}/api/generate",
                    json={"model": model, "prompt": "ok", "stream": False},
                )
                if gen.status_code != 200:
                    body = gen.text[:200]
                    self._availability_cache = (
                        False,
                        f"ollama generate probe failed status={gen.status_code} body={body!r}",
                    )
                    return self._availability_cache
        except Exception as exc:
            self._availability_cache = (False, f"ollama probe failed: {exc}")
            return self._availability_cache

        self._availability_cache = (True, None)
        return self._availability_cache

    def normalize(
        self,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None,
    ) -> tuple[list[PageRepresentation], str, str | None, NormalizationMetrics, list[NormalizationWarning]]:
        import fitz

        warnings: list[NormalizationWarning] = []
        start = time.perf_counter()
        source_pdf = Path(str(doc.get("local_path") or ""))
        if not source_pdf.exists():
            raise FileNotFoundError(f"Source PDF not found: {source_pdf}")

        page_start = max(int(doc.get("start_page") or 1), 1)
        page_end = int(doc.get("end_page") or page_start)
        page_numbers = list(range(page_start, page_end + 1))[: self.config.glm_max_pages]
        pages: list[PageRepresentation] = []
        text_parts: list[str] = []
        with fitz.open(source_pdf) as pdf:
            for page_number in page_numbers:
                page = pdf[page_number - 1]
                image = _render_fitz_page(page, dpi=180)
                try:
                    text = self._ocr_page(image)
                except Exception as exc:
                    warnings.append(
                        NormalizationWarning(
                            code="glm_ocr_failed",
                            message=str(exc),
                            page_number=page_number,
                            backend=self.backend.value,
                        )
                    )
                    text = ""
                text_parts.append(text)
                pages.append(
                    PageRepresentation(
                        page_number=page_number,
                        text=text,
                        markdown=text or None,
                        source="glm_ocr",
                        backend=NormalizationBackend.GLM_OCR,
                        blocks=self.builder._build_blocks(text, page_number),
                        metadata={"source_backend": self.backend.value, "model": self.config.glm_model},
                    )
                )
        combined_text = "\n\n".join(item for item in text_parts if item).strip() or raw_text
        metrics = NormalizationMetrics(
            backend=NormalizationBackend.GLM_OCR,
            page_count=len(pages),
            text_char_count=len(combined_text),
            low_text_page_count=sum(1 for item in pages if len(item.text.strip()) < self.config.suspicious_page_text_chars),
            elapsed_ms=int((time.perf_counter() - start) * 1000),
            metadata={
                "ollama_host": self.config.ollama_host,
                "model": self.config.glm_model,
            },
        )
        return pages, combined_text, combined_text or None, metrics, warnings

    def _ocr_page(self, image: Any) -> str:
        image_bytes = io.BytesIO()
        image.save(image_bytes, format="PNG")
        encoded = base64.b64encode(image_bytes.getvalue()).decode("ascii")
        payload = {
            "model": self.config.glm_model,
            "prompt": (
                "Perform OCR on this page and return the readable text in natural reading order. "
                "Preserve table rows and headings when practical."
            ),
            "images": [encoded],
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(self.config.glm_retry_count + 1):
            try:
                with httpx.Client(timeout=self.config.glm_timeout_seconds) as client:
                    response = client.post(
                        f"{self.config.ollama_host.rstrip('/')}/api/generate",
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    text = str(data.get("response") or "").strip()
                    if text:
                        return text
            except Exception as exc:  # pragma: no cover - network/runtime dependent
                last_error = exc
                if attempt >= self.config.glm_retry_count:
                    break
        raise RuntimeError(f"GLM OCR request failed: {last_error}")


class DocumentNormalizationRouter:
    def __init__(
        self,
        config: DocumentNormalizationConfig | None = None,
        *,
        builder: DocumentRepresentationBuilder | None = None,
        native_normalizer: DocumentNormalizer | None = None,
        paddle_normalizer: DocumentNormalizer | None = None,
        glm_normalizer: DocumentNormalizer | None = None,
    ) -> None:
        self.config = config or DocumentNormalizationConfig()
        self.builder = builder or DocumentRepresentationBuilder()
        self.native_normalizer = native_normalizer or NativePdfNormalizer(builder=self.builder)
        self.paddle_normalizer = paddle_normalizer or PaddleStructureNormalizer(self.config, builder=self.builder)
        self.glm_normalizer = glm_normalizer or GlmOcrNormalizer(self.config, builder=self.builder)

    def normalize_historical_document(
        self,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None,
    ):
        decision = self.route_historical_document(
            doc,
            raw_text=raw_text,
            page_artifacts=page_artifacts,
        )
        pages, normalized_text, markdown_text, metrics, warnings = self._run_backend(
            decision.backend,
            doc,
            raw_text=raw_text,
            page_artifacts=page_artifacts,
        )

        if self.config.page_level_escalation and decision.backend in {
            NormalizationBackend.NATIVE_PDF,
            NormalizationBackend.PAGE_ARTIFACT,
            NormalizationBackend.PADDLE_STRUCTURE,
        }:
            pages, normalized_text, markdown_text, metrics, warnings = self._maybe_escalate_pages(
                doc,
                pages=pages,
                normalized_text=normalized_text,
                markdown_text=markdown_text,
                metrics=metrics,
                warnings=warnings,
            )

        return self.builder.build_normalized_document(
            doc,
            pages=pages,
            raw_text=normalized_text,
            markdown_text=markdown_text,
            backend=metrics.backend,
            metrics=metrics,
            warnings=warnings,
            document_metadata={
                "requested_normalizer_backend": decision.backend.value,
                "actual_normalizer_backend": metrics.backend.value,
                "normalization_fallback_backend": metrics.fallback_backend.value
                if metrics.fallback_backend
                else None,
                "normalization_route_reason": decision.reason,
                "normalization_route_metadata": decision.metadata,
            },
        )

    def route_historical_document(
        self,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None,
    ) -> NormalizationRouteDecision:
        page_count = len(page_artifacts or [])
        artifact_text_chars = sum(len(str(page.get("text_content") or "")) for page in page_artifacts or [])
        has_good_artifact_text = page_count > 0 and artifact_text_chars >= self.config.native_min_text_chars
        raw_text_chars = len((raw_text or "").strip())
        suspicious_text = raw_text_chars < self.config.native_min_text_chars

        if self.config.enable_native_pdf and (has_good_artifact_text or raw_text_chars >= self.config.native_min_text_chars):
            backend = NormalizationBackend.PAGE_ARTIFACT if has_good_artifact_text else NormalizationBackend.NATIVE_PDF
            return NormalizationRouteDecision(
                backend=backend,
                reason="usable_text_layer_available",
                metadata={
                    "raw_text_chars": raw_text_chars,
                    "artifact_text_chars": artifact_text_chars,
                    "page_count": page_count,
                },
            )

        paddle_available, _ = self.paddle_normalizer.is_available()
        if self.config.enable_paddle_structure and paddle_available:
            return NormalizationRouteDecision(
                backend=NormalizationBackend.PADDLE_STRUCTURE,
                reason="poor_native_text_or_scanned_layout",
                fallback_backend=NormalizationBackend.GLM_OCR if self.config.enable_glm_ocr else None,
                metadata={"raw_text_chars": raw_text_chars, "suspicious_text": suspicious_text},
            )

        glm_available, _ = self.glm_normalizer.is_available()
        if self.config.enable_glm_ocr and glm_available:
            return NormalizationRouteDecision(
                backend=NormalizationBackend.GLM_OCR,
                reason="ocr_fallback_when_native_and_paddle_unavailable",
                metadata={"raw_text_chars": raw_text_chars},
            )

        return NormalizationRouteDecision(
            backend=NormalizationBackend.PAGE_ARTIFACT if page_artifacts else NormalizationBackend.NATIVE_PDF,
            reason="fallback_to_existing_native_path",
            metadata={"raw_text_chars": raw_text_chars},
        )

    def _run_backend(
        self,
        backend: NormalizationBackend,
        doc: dict[str, Any],
        *,
        raw_text: str,
        page_artifacts: list[dict[str, Any]] | None,
    ) -> tuple[list[PageRepresentation], str, str | None, NormalizationMetrics, list[NormalizationWarning]]:
        backend_impl: DocumentNormalizer
        if backend in {NormalizationBackend.PAGE_ARTIFACT, NormalizationBackend.NATIVE_PDF}:
            backend_impl = self.native_normalizer
        elif backend == NormalizationBackend.PADDLE_STRUCTURE:
            backend_impl = self.paddle_normalizer
        elif backend == NormalizationBackend.GLM_OCR:
            backend_impl = self.glm_normalizer
        else:
            backend_impl = self.native_normalizer

        available, reason = backend_impl.is_available()
        if not available:
            logger.warning("Normalization backend %s unavailable: %s", backend_impl.backend.value, reason)
            if backend != NormalizationBackend.GLM_OCR and self.config.enable_glm_ocr:
                pages, normalized_text, markdown_text, metrics, warnings = self._run_backend(
                    NormalizationBackend.GLM_OCR,
                    doc,
                    raw_text=raw_text,
                    page_artifacts=page_artifacts,
                )
                metrics.fallback_backend = backend
                warnings.append(
                    NormalizationWarning(
                        code="backend_unavailable_fallback_to_glm",
                        message=reason or f"{backend_impl.backend.value} unavailable",
                        backend=backend.value,
                    )
                )
                return pages, normalized_text, markdown_text, metrics, warnings

            pages, normalized_text, markdown_text, metrics, warnings = self.native_normalizer.normalize(
                doc,
                raw_text=raw_text,
                page_artifacts=page_artifacts,
            )
            metrics.fallback_backend = backend
            warnings.append(
                NormalizationWarning(
                    code="backend_unavailable_fallback_to_native",
                    message=reason or f"{backend_impl.backend.value} unavailable",
                    backend=backend.value,
                )
            )
            return pages, normalized_text, markdown_text, metrics, warnings

        try:
            return backend_impl.normalize(doc, raw_text=raw_text, page_artifacts=page_artifacts)
        except Exception as exc:
            logger.warning("Normalization backend %s failed for %s: %s", backend.value, doc.get("id"), exc)
            if backend == NormalizationBackend.PADDLE_STRUCTURE and self.config.enable_glm_ocr:
                pages, normalized_text, markdown_text, metrics, warnings = self._run_backend(
                    NormalizationBackend.GLM_OCR,
                    doc,
                    raw_text=raw_text,
                    page_artifacts=page_artifacts,
                )
                metrics.fallback_backend = backend
                warnings.append(
                    NormalizationWarning(
                        code="paddle_fallback_to_glm",
                        message=str(exc),
                        backend=backend.value,
                    )
                )
                return pages, normalized_text, markdown_text, metrics, warnings
            pages, normalized_text, markdown_text, metrics, warnings = self.native_normalizer.normalize(
                doc,
                raw_text=raw_text,
                page_artifacts=page_artifacts,
            )
            metrics.fallback_backend = backend
            warnings.append(
                NormalizationWarning(
                    code="backend_failed_fallback_to_native",
                    message=str(exc),
                    backend=backend.value,
                )
            )
            return pages, normalized_text, markdown_text, metrics, warnings

    def _maybe_escalate_pages(
        self,
        doc: dict[str, Any],
        *,
        pages: list[PageRepresentation],
        normalized_text: str,
        markdown_text: str | None,
        metrics: NormalizationMetrics,
        warnings: list[NormalizationWarning],
    ) -> tuple[list[PageRepresentation], str, str | None, NormalizationMetrics, list[NormalizationWarning]]:
        # Short-circuit on the config flag BEFORE probing availability — the
        # availability probe is a network call (Ollama), and skipping it when
        # GLM is disabled is the whole reason extract-rates-nc disables it.
        if not self.config.enable_glm_ocr:
            return pages, normalized_text, markdown_text, metrics, warnings
        glm_available, _ = self.glm_normalizer.is_available()
        if not glm_available:
            return pages, normalized_text, markdown_text, metrics, warnings

        suspicious_pages = [
            page
            for page in pages
            if _page_is_low_text(page, self.config)
            or _page_has_symbol_noise(page, self.config)
        ]
        if not suspicious_pages:
            return pages, normalized_text, markdown_text, metrics, warnings

        low_text_pages = [page for page in suspicious_pages if _page_is_low_text(page, self.config)]
        symbol_noise_pages = [page for page in suspicious_pages if _page_has_symbol_noise(page, self.config)]
        if (
            len(low_text_pages) / max(len(pages), 1) < self.config.suspicious_text_ratio
            and not symbol_noise_pages
        ):
            return pages, normalized_text, markdown_text, metrics, warnings

        glm_pages, glm_text, glm_markdown, glm_metrics, glm_warnings = self._run_backend(
            NormalizationBackend.GLM_OCR,
            doc,
            raw_text=normalized_text,
            page_artifacts=None,
        )
        page_map = {page.page_number: page for page in pages}
        for page in glm_pages:
            original_page = page_map.get(page.page_number)
            if original_page is None or _should_replace_page_with_glm(
                original_page,
                page,
                self.config,
            ):
                page_map[page.page_number] = page

        merged_pages = [page_map[number] for number in sorted(page_map)]
        combined_text = "\n\n".join(page.text for page in merged_pages if page.text).strip() or normalized_text
        combined_markdown = "\n\n".join(page.markdown or page.text for page in merged_pages if (page.markdown or page.text)).strip() or markdown_text
        metrics.fallback_backend = glm_metrics.backend
        metrics.low_text_page_count = sum(
            1 for page in merged_pages if len(page.text.strip()) < self.config.suspicious_page_text_chars
        )
        warnings.extend(glm_warnings)
        warnings.append(
            NormalizationWarning(
                code="page_level_glm_escalation",
                message=(
                    "Escalated low-text or symbol-noise pages to GLM OCR."
                    if symbol_noise_pages
                    else "Escalated low-text pages to GLM OCR."
                ),
                backend=NormalizationBackend.GLM_OCR.value,
            )
        )
        return merged_pages, combined_text, combined_markdown, metrics, warnings


def _page_is_low_text(page: PageRepresentation, config: DocumentNormalizationConfig) -> bool:
    return len(page.text.strip()) < config.suspicious_page_text_chars


def _page_has_symbol_noise(page: PageRepresentation, config: DocumentNormalizationConfig) -> bool:
    if not config.enable_symbol_noise_escalation:
        return False
    quality = analyze_text_quality(page.text)
    if quality.suspicious_codes:
        page.metadata.setdefault("text_quality_suspicious_codes", quality.suspicious_codes)
    if quality.redline_codes:
        page.metadata.setdefault("text_quality_redline_codes", quality.redline_codes)
    return quality.suspicious_hit_count >= config.suspicious_symbol_min_hits


def _should_replace_page_with_glm(
    original_page: PageRepresentation,
    glm_page: PageRepresentation,
    config: DocumentNormalizationConfig,
) -> bool:
    original_quality = analyze_text_quality(original_page.text)
    glm_quality = analyze_text_quality(glm_page.text)
    original_low_text = _page_is_low_text(original_page, config)
    glm_low_text = _page_is_low_text(glm_page, config)

    if original_low_text and not glm_low_text:
        return True
    if glm_quality.suspicious_hit_count < original_quality.suspicious_hit_count and glm_page.text.strip():
        return True
    if (
        glm_quality.suspicious_hit_count == original_quality.suspicious_hit_count
        and len(glm_page.text.strip()) > len(original_page.text.strip())
        and glm_page.text.strip()
    ):
        return True
    return False


def _resolve_gpu_preference(use_gpu: bool | None, prefer_gpu: bool) -> bool:
    if use_gpu is not None:
        return use_gpu and _is_gpu_available()
    return prefer_gpu and _is_gpu_available()


def _is_gpu_available() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        import paddle

        return bool(paddle.is_compiled_with_cuda())
    except Exception:
        return False


def _chunk_iterable(items: list[Any], size: int) -> Iterable[list[Any]]:
    if size <= 0:
        size = 1
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _render_fitz_page(page: Any, *, dpi: int):
    import fitz
    from PIL import Image

    scale = max(dpi / 72.0, 1.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def _bbox_from_iterable(value: Any) -> BoundingBox | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        return BoundingBox(x0=float(value[0]), y0=float(value[1]), x1=float(value[2]), y1=float(value[3]))
    except Exception:
        return None


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    headers = rows[0]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows[1:]:
        padded = row + [""] * max(0, len(headers) - len(row))
        lines.append("| " + " | ".join(padded[: len(headers)]) + " |")
    return "\n".join(lines)
