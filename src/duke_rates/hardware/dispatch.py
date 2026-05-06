"""
CPU vs GPU dispatch for Docling document conversion.

Uses existing triage signals (gpu_ocr_candidate, is_likely_scanned,
page_count, triage_flags, route_recommendation) to choose the best
accelerator for each document without re-examining the PDF.

Decision rules (evaluated in priority order):
  1. No CUDA available → CPU always.
  2. Native-text, small, no table flags → skip Docling entirely.
  3. VRAM headroom insufficient → CPU fallback.
  4. Small non-scanned doc (≤ 4 pages, no table flags) → CPU
     (CUDA kernel launch overhead dominates at this scale).
  5. Table-heavy flag OR large doc (≥ 8 pages scanned) → GPU.
  6. gpu_ocr_candidate or OCR_REQUIRED → GPU always.
  7. Default for ambiguous mid-size docs → GPU if available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from duke_rates.models.pipeline import DocumentTriage, PipelineRoute

logger = logging.getLogger(__name__)


class AcceleratorChoice(str, Enum):
    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"


class PipelineChoice(str, Enum):
    STANDARD = "standard"   # layout-heron + TableFormer ACCURATE + Tesseract
    VLM = "vlm"             # SmolDocling/GraniteDocling — for hard pre-1990 scans


@dataclass
class DispatchDecision:
    accelerator: AcceleratorChoice
    pipeline: PipelineChoice
    use_bf16: bool
    enable_table_model: bool
    enable_ocr_model: bool
    has_scanned_pages: bool
    force_full_page_ocr: bool
    batch_size: int
    skip_docling: bool = False   # True = pdfplumber is sufficient, don't call Docling
    reason: str = ""


# Page-count threshold below which CUDA launch overhead is not worth paying
_SMALL_DOC_PAGE_THRESHOLD = 4

# Table-density keyword hit threshold for activating TableFormer
_TABLE_KEYWORD_THRESHOLD = 15


def dispatch(triage: DocumentTriage) -> DispatchDecision:
    """Return a DispatchDecision for the given triage result.

    Imports torch lazily so this module is safe to use in CPU-only environments.
    """
    from duke_rates.hardware.gpu_manager import get_gpu_manager

    manager = get_gpu_manager()
    cuda_available = manager.is_cuda_available()

    is_table_heavy = (
        "table_heavy_layout" in triage.triage_flags
        or triage.route_recommendation == PipelineRoute.TABLE_HEAVY
        or triage.keyword_hits.get("table_like_lines", 0) >= _TABLE_KEYWORD_THRESHOLD
    )
    is_scanned = triage.is_likely_scanned or triage.gpu_ocr_candidate
    is_ocr_required = triage.route_recommendation == PipelineRoute.OCR_REQUIRED

    # Rule 2: purely native-text small doc — pdfplumber handles it, no Docling needed
    if (
        triage.route_recommendation == PipelineRoute.TEXT_PARSE
        and not is_scanned
        and not is_table_heavy
        and triage.page_count <= 6
    ):
        return DispatchDecision(
            accelerator=AcceleratorChoice.CPU,
            pipeline=PipelineChoice.STANDARD,
            use_bf16=False,
            enable_table_model=False,
            enable_ocr_model=False,
            has_scanned_pages=False,
            force_full_page_ocr=False,
            batch_size=1,
            skip_docling=True,
            reason="native_text_pdfplumber_sufficient",
        )

    # Rule 1: no CUDA
    if not cuda_available:
        return DispatchDecision(
            accelerator=AcceleratorChoice.CPU,
            pipeline=PipelineChoice.STANDARD,
            use_bf16=False,
            enable_table_model=is_table_heavy,
            enable_ocr_model=is_scanned or is_ocr_required,
            has_scanned_pages=is_scanned or is_ocr_required,
            force_full_page_ocr=False,
            batch_size=1,
            reason="no_cuda",
        )

    # Rule 3: VRAM headroom check
    if not manager.can_fit_docling_models(enable_ocr=is_scanned or is_ocr_required):
        logger.warning(
            "VRAM headroom insufficient (%.1f GB free) — falling back to CPU for %s",
            manager.free_vram_gb(),
            triage.file_path,
        )
        return DispatchDecision(
            accelerator=AcceleratorChoice.CPU,
            pipeline=PipelineChoice.STANDARD,
            use_bf16=False,
            enable_table_model=is_table_heavy,
            enable_ocr_model=is_scanned or is_ocr_required,
            has_scanned_pages=is_scanned or is_ocr_required,
            force_full_page_ocr=False,
            batch_size=1,
            reason="vram_headroom_insufficient",
        )

    # Rule 4: small non-scanned, non-table doc — CPU avoids CUDA overhead
    if (
        not is_scanned
        and not is_table_heavy
        and not is_ocr_required
        and triage.page_count <= _SMALL_DOC_PAGE_THRESHOLD
    ):
        return DispatchDecision(
            accelerator=AcceleratorChoice.CPU,
            pipeline=PipelineChoice.STANDARD,
            use_bf16=False,
            enable_table_model=False,
            enable_ocr_model=False,
            has_scanned_pages=False,
            force_full_page_ocr=False,
            batch_size=1,
            reason=f"small_doc_{triage.page_count}p_cpu_preferred",
        )

    # Rule 5: hard scanned doc (low confidence OCR candidate) — try VLM pipeline on GPU
    # VLM treats each page as a vision task; much better for pre-1990 typewritten filings
    # where standard Tesseract OCR produces low-confidence output.
    is_hard_scan = (
        is_scanned
        and cuda_available
        and triage.ocr_confidence_score < 0.4  # low OCR confidence → VLM better
    )

    if triage.page_count > 25:
        batch_size = 8
    elif triage.page_count > 10:
        batch_size = 4
    else:
        batch_size = 2

    reason_parts = []
    if is_table_heavy:
        reason_parts.append("table_heavy")
    if is_scanned:
        reason_parts.append("scanned")
    if is_ocr_required:
        reason_parts.append("ocr_required")
    if is_hard_scan:
        reason_parts.append("vlm_route")
    reason_parts.append(f"pages={triage.page_count}")

    return DispatchDecision(
        accelerator=AcceleratorChoice.CUDA,
        pipeline=PipelineChoice.VLM if is_hard_scan else PipelineChoice.STANDARD,
        use_bf16=True,
        enable_table_model=is_table_heavy,
        enable_ocr_model=is_scanned or is_ocr_required,
        has_scanned_pages=is_scanned or is_ocr_required,
        force_full_page_ocr=(triage.route_recommendation == PipelineRoute.OCR_REQUIRED),
        batch_size=batch_size,
        reason="_".join(reason_parts) if reason_parts else "gpu_default",
    )


def describe(decision: DispatchDecision) -> str:
    """Return a human-readable one-liner describing the dispatch decision."""
    if decision.skip_docling:
        return f"skip_docling ({decision.reason})"
    parts = [decision.accelerator.value, decision.pipeline.value]
    if decision.use_bf16:
        parts.append("bf16")
    if decision.enable_table_model:
        parts.append("table")
    if decision.enable_ocr_model:
        parts.append("ocr")
    if decision.force_full_page_ocr:
        parts.append("full_ocr")
    parts.append(f"batch={decision.batch_size}")
    return f"{' '.join(parts)}  [{decision.reason}]"
