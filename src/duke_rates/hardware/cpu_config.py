"""
CPU configuration for PyTorch inference.

Sets MKL/OMP thread counts and PyTorch CPU thread pools for optimal
single-document and batch inference performance.

IMPORTANT: Call configure_cpu() before importing torch or docling.
Environment variables set after torch is imported have no effect on MKL.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Physical core count for the RTX 4060 Laptop paired CPU (typically 8-core).
# Hyperthreading hurts memory-bandwidth-bound CNN inference — use physical cores only.
_DEFAULT_PHYSICAL_CORES = 8


def configure_cpu(n_physical_cores: int = _DEFAULT_PHYSICAL_CORES) -> None:
    """Set MKL/OMP thread counts and PyTorch CPU thread pools.

    Must be called before any torch or docling import to take effect.
    Safe to call multiple times (subsequent calls are no-ops if already set).
    """
    # MKL thread pool — cap to physical cores, disable auto-scaling
    os.environ.setdefault("MKL_NUM_THREADS", str(n_physical_cores))
    os.environ.setdefault("OMP_NUM_THREADS", str(n_physical_cores))
    os.environ.setdefault("MKL_DYNAMIC", "FALSE")

    # HuggingFace tokenizer parallelism warning suppression
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    # PyTorch memory allocator — expandable segments reduce fragmentation
    # when model sizes vary across documents (must be set before torch import)
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    # Docling model cache — point to a local pre-fetched path to avoid HuggingFace
    # downloads on every first run. Run `docling-tools models download` once to populate.
    # Leave unset to use the default HuggingFace cache (~/.cache/docling/models).
    # os.environ.setdefault("DOCLING_ARTIFACTS_PATH", "data/docling_models")

    logger.debug(
        "cpu_config: MKL_NUM_THREADS=%s OMP_NUM_THREADS=%s MKL_DYNAMIC=%s",
        os.environ["MKL_NUM_THREADS"],
        os.environ["OMP_NUM_THREADS"],
        os.environ["MKL_DYNAMIC"],
    )


def configure_torch_inference() -> None:
    """Apply PyTorch global inference settings after torch is imported.

    Call once at process startup, after configure_cpu() and after torch is available.
    Safe to call multiple times.
    """
    try:
        import torch
    except ImportError:
        logger.debug("torch not available — skipping inference configuration")
        return

    # TF32: Ada Lovelace / Ampere tensor core path — ~1.3-1.8x speedup on matmul/conv
    # with negligible precision loss for document AI workloads.
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

    # Disable gradient tracking globally — this is an inference-only process.
    torch.set_grad_enabled(False)

    # CPU thread pools (effective only if torch is imported after env vars above;
    # these set() calls are always safe as a belt-and-suspenders fallback).
    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", _DEFAULT_PHYSICAL_CORES)))
    torch.set_num_interop_threads(2)

    if torch.cuda.is_available():
        # cuDNN benchmark: profiles convolution algorithms on first batch.
        # Docling feeds variable-size pages so we keep this False to avoid
        # repeated re-profiling stalls. Enable per-run if input sizes are uniform.
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = False

        logger.debug(
            "torch inference config: TF32=True, grad=False, CUDA device=%s (%s)",
            torch.cuda.current_device(),
            torch.cuda.get_device_name(0),
        )
    else:
        logger.debug("torch inference config: TF32=True, grad=False, CUDA=unavailable")


def warmup_gpu() -> bool:
    """Run a small dummy forward pass to trigger cuDNN algorithm selection.

    Returns True if GPU warmup succeeded, False if CUDA is unavailable.
    Call once after configure_torch_inference(), before the first real document.
    """
    try:
        import torch
    except ImportError:
        return False

    if not torch.cuda.is_available():
        return False

    try:
        dummy = torch.zeros(1, 3, 64, 64, device="cuda", dtype=torch.bfloat16)
        # Tiny conv-like operation to trigger cuDNN init
        dummy = dummy + dummy
        del dummy
        torch.cuda.empty_cache()
        logger.debug("GPU warmup complete")
        return True
    except Exception as exc:
        logger.warning("GPU warmup failed: %s", exc)
        return False
