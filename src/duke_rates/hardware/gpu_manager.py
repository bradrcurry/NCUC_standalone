"""
GPU VRAM budget manager.

Tracks available VRAM and provides a thread-safe semaphore for serializing
GPU work when multiple threads are in play (e.g. CPU thread pool + GPU worker).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Approximate BF16 VRAM footprints for Docling's three models (GB)
_LAYOUT_MODEL_GB = 0.65   # DocLayNet (CNN backbone)
_TABLE_MODEL_GB = 0.42    # TableFormer
_OCR_MODEL_GB = 0.08      # RapidOCR CRNN
_ALL_MODELS_GB = _LAYOUT_MODEL_GB + _TABLE_MODEL_GB + _OCR_MODEL_GB  # ~1.15 GB

# Reserve for cuDNN workspace, page tensors, and driver overhead
_SAFETY_MARGIN_GB = 0.75

# Minimum free VRAM (after models loaded) before we defer to CPU
VRAM_MIN_HEADROOM_GB = 2.5


class GPUBudgetManager:
    """Query VRAM availability and gate GPU work through a semaphore.

    Usage::

        manager = GPUBudgetManager()
        if manager.should_use_gpu(triage):
            with manager.gpu_lock():
                result = convert_pdf_with_docling(path, accelerator="cuda")
                manager.release()
    """

    def __init__(self, device: int = 0) -> None:
        self.device = device
        self._semaphore = threading.Semaphore(1)  # single GPU — serialize access

    def is_cuda_available(self) -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def free_vram_gb(self) -> float:
        """Return estimated free VRAM in GB (after PyTorch allocator reserve)."""
        if not self.is_cuda_available():
            return 0.0
        try:
            import torch
            props = torch.cuda.get_device_properties(self.device)
            reserved = torch.cuda.memory_reserved(self.device)
            free = props.total_memory - reserved
            return (free / 1024 ** 3) - _SAFETY_MARGIN_GB
        except Exception as exc:
            logger.debug("VRAM query failed: %s", exc)
            return 0.0

    def can_fit_docling_models(self, *, enable_ocr: bool = True) -> bool:
        """Return True if VRAM headroom is sufficient for Docling model set."""
        needed = _LAYOUT_MODEL_GB + _TABLE_MODEL_GB
        if enable_ocr:
            needed += _OCR_MODEL_GB
        needed += 1.0  # page tensor budget
        return self.free_vram_gb() >= needed

    def release(self) -> None:
        """Free PyTorch allocator cache after a GPU job completes."""
        if not self.is_cuda_available():
            return
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

    def gpu_lock(self):
        """Context manager that acquires the GPU semaphore."""
        return _GPUSemaphoreContext(self._semaphore)

    def summary(self) -> dict:
        """Return a dict of current GPU state for logging/diagnostics."""
        if not self.is_cuda_available():
            return {"cuda": False}
        try:
            import torch
            props = torch.cuda.get_device_properties(self.device)
            return {
                "cuda": True,
                "device_name": props.name,
                "total_vram_gb": round(props.total_memory / 1024 ** 3, 2),
                "free_vram_gb": round(self.free_vram_gb(), 2),
                "allocated_gb": round(torch.cuda.memory_allocated(self.device) / 1024 ** 3, 2),
                "reserved_gb": round(torch.cuda.memory_reserved(self.device) / 1024 ** 3, 2),
            }
        except Exception as exc:
            return {"cuda": True, "error": str(exc)}


class _GPUSemaphoreContext:
    def __init__(self, semaphore: threading.Semaphore) -> None:
        self._sem = semaphore

    def __enter__(self):
        self._sem.acquire()
        return self

    def __exit__(self, *_):
        self._sem.release()


# Module-level singleton — one manager per process
_default_manager: Optional[GPUBudgetManager] = None
_manager_lock = threading.Lock()


def get_gpu_manager() -> GPUBudgetManager:
    """Return the process-level GPUBudgetManager singleton."""
    global _default_manager
    if _default_manager is None:
        with _manager_lock:
            if _default_manager is None:
                _default_manager = GPUBudgetManager(device=0)
    return _default_manager
