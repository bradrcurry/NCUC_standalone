"""
Document pipeline benchmark.

Measures CPU vs GPU conversion time across document categories representative
of the NCUC filing corpus.

Document categories:
  A — Post-2005 native-text tariff sheet (1-4 pages)
  B — Rider summary table doc (5-15 pages, table-heavy)
  C — Scanned historical filing (10-40 pages, image-only)
  D — Large compliance book (50+ pages, mixed)
  E — Complex nested rate table (any size, table_heavy flag)

Usage::

    python -m duke_rates doc-intel benchmark-docling \\
        --pdf path/to/a.pdf --category A \\
        --pdf path/to/b.pdf --category B
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VALID_CATEGORIES = ("A", "B", "C", "D", "E")

CATEGORY_DESCRIPTIONS = {
    "A": "Native-text tariff sheet (1-4 pages)",
    "B": "Rider summary table (5-15 pages, table-heavy)",
    "C": "Scanned historical filing (10-40 pages, image-only)",
    "D": "Large compliance book (50+ pages, mixed)",
    "E": "Complex nested rate table",
}


@dataclass
class BenchmarkResult:
    pdf_path: str
    category: str
    page_count: int

    # Timing breakdown (seconds)
    triage_time_s: float = 0.0
    dispatch_time_s: float = 0.0
    conversion_time_s: float = 0.0
    total_wall_time_s: float = 0.0

    # Dispatch outcome
    accelerator_used: str = ""
    skip_docling: bool = False
    dispatch_reason: str = ""

    # Result quality
    conversion_status: str = ""
    tables_detected: int = 0
    text_chars: int = 0
    pages_per_second: float = 0.0

    # GPU stats (populated if CUDA was used)
    peak_vram_mb: float = 0.0
    free_vram_before_gb: float = 0.0
    free_vram_after_gb: float = 0.0

    error: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def _reset_peak_vram() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def _peak_vram_mb() -> float:
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.max_memory_allocated() / 1024 ** 2
    except Exception:
        pass
    return 0.0


def run_single(pdf_path: str, category: str, accelerator: Optional[str] = None) -> BenchmarkResult:
    """Benchmark a single PDF.

    If accelerator is None, the dispatch module chooses automatically.
    Pass "cpu" or "cuda" to force a specific path.
    """
    from duke_rates.hardware.dispatch import dispatch, describe, AcceleratorChoice
    from duke_rates.hardware.gpu_manager import get_gpu_manager
    from duke_rates.historical.ncuc.pipeline.triage import triage_pdf
    from duke_rates.historical.ncuc.pipeline.docling_backend import convert_pdf_with_docling

    result = BenchmarkResult(pdf_path=pdf_path, category=category, page_count=0)
    manager = get_gpu_manager()
    t_start = time.perf_counter()

    # Triage
    try:
        t0 = time.perf_counter()
        triage = triage_pdf(pdf_path)
        result.triage_time_s = time.perf_counter() - t0
        result.page_count = triage.page_count
    except Exception as exc:
        result.error = f"triage failed: {exc}"
        result.total_wall_time_s = time.perf_counter() - t_start
        return result

    # Dispatch
    try:
        t0 = time.perf_counter()
        decision = dispatch(triage)
        result.dispatch_time_s = time.perf_counter() - t0
        result.dispatch_reason = decision.reason
        result.skip_docling = decision.skip_docling
    except Exception as exc:
        result.error = f"dispatch failed: {exc}"
        result.total_wall_time_s = time.perf_counter() - t_start
        return result

    # Honour forced accelerator override
    chosen_accel = accelerator if accelerator else decision.accelerator.value
    result.accelerator_used = chosen_accel

    if decision.skip_docling and accelerator is None:
        result.conversion_status = "skipped_native_text"
        result.total_wall_time_s = time.perf_counter() - t_start
        result.pages_per_second = (
            result.page_count / result.total_wall_time_s if result.total_wall_time_s > 0 else 0.0
        )
        return result

    # VRAM snapshot before
    result.free_vram_before_gb = manager.free_vram_gb()
    _reset_peak_vram()

    # Conversion
    try:
        t0 = time.perf_counter()
        artifact = convert_pdf_with_docling(
            pdf_path,
            accelerator=chosen_accel,
            force=True,  # always re-run for benchmarking (bypass cache)
        )
        result.conversion_time_s = time.perf_counter() - t0
    except Exception as exc:
        result.error = f"conversion failed: {exc}"
        result.total_wall_time_s = time.perf_counter() - t_start
        return result

    result.free_vram_after_gb = manager.free_vram_gb()
    result.peak_vram_mb = _peak_vram_mb()

    if artifact:
        result.conversion_status = artifact.get("conversion_status", "")
        result.tables_detected = len(
            json.loads(Path(artifact["tables_path"]).read_text(encoding="utf-8"))
            if artifact.get("tables_path") and Path(artifact["tables_path"]).exists()
            else []
        )
        txt_path = artifact.get("plain_text_path", "")
        if txt_path and Path(txt_path).exists():
            result.text_chars = len(Path(txt_path).read_text(encoding="utf-8"))
    else:
        result.conversion_status = "failed"

    result.total_wall_time_s = time.perf_counter() - t_start
    result.pages_per_second = (
        result.page_count / result.conversion_time_s if result.conversion_time_s > 0 else 0.0
    )
    return result


def print_result(r: BenchmarkResult) -> None:
    status_icon = "OK" if r.error is None else "FAIL"
    print(f"\n[{status_icon}] {Path(r.pdf_path).name}  (cat={r.category})")
    if r.error:
        print(f"  ERROR: {r.error}")
        return
    print(f"  Pages       : {r.page_count}")
    print(f"  Accelerator : {r.accelerator_used}  [{r.dispatch_reason}]")
    print(f"  Skip Docling: {r.skip_docling}")
    print(f"  Status      : {r.conversion_status}")
    print(f"  Tables      : {r.tables_detected}")
    print(f"  Text chars  : {r.text_chars:,}")
    print(f"  Triage      : {r.triage_time_s:.3f}s")
    print(f"  Dispatch    : {r.dispatch_time_s:.3f}s")
    print(f"  Conversion  : {r.conversion_time_s:.3f}s")
    print(f"  Total wall  : {r.total_wall_time_s:.3f}s")
    print(f"  Pages/sec   : {r.pages_per_second:.2f}")
    if r.peak_vram_mb > 0:
        print(f"  Peak VRAM   : {r.peak_vram_mb:.0f} MB")
        print(f"  VRAM before : {r.free_vram_before_gb:.2f} GB free")
        print(f"  VRAM after  : {r.free_vram_after_gb:.2f} GB free")
