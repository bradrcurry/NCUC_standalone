"""
Docling backend for structured document conversion.

Optional backend for hard NCUC documents: scanned PDFs, table-heavy rider
summaries, and mixed-layout compliance books.  This module is intentionally
import-safe: if docling is not installed the availability check returns a
reason string and callers degrade gracefully.

Artifacts are cached by (source_pdf, file_hash, backend_version, accelerator).
When a DB connection is provided, content is stored in the docling_artifacts
table (doc_json_content, plain_text_content, tables_json_content columns) and
no sidecar files are written.  If no DB connection is given, sidecar files are
written alongside the source PDF as a fallback.

Two conversion routes are available:

  standard  — Layout (docling-layout-heron) + TableFormer ACCURATE + OCR
              Best for native-text PDFs, rider tables, compliance books.
              Use accelerator=cpu or accelerator=cuda.

  vlm       — SmolDocling / GraniteDocling VLM pipeline (full-page vision)
              Best for hard pre-1990 scans where standard OCR fails.
              Requires CUDA for practical throughput; slow on CPU.

Do NOT route every PDF through Docling.  Use only when:
  - triage marks the document OCR_REQUIRED and high structure_complexity
  - a document is flagged gpu_ocr_candidate
  - repeated weak/empty parse review suggests the text path cannot recover it
  - an operator explicitly requests Docling analysis for a specific file
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from duke_rates.historical.ncuc.pipeline.stage_versions import DOCLING_BACKEND_VERSION

logger = logging.getLogger(__name__)

# Sentinel so callers can check whether Docling is available without importing it.
DOCLING_PACKAGE = "docling"

# Accelerator labels (mirrors the docling AcceleratorDevice enum names we care about)
ACCELERATOR_CPU = "cpu"
ACCELERATOR_CUDA = "cuda"
ACCELERATOR_MPS = "mps"  # Apple Silicon – reserved for future use

# Conversion pipeline routes
PIPELINE_STANDARD = "standard"  # layout-heron + TableFormer + OCR
PIPELINE_VLM = "vlm"            # SmolDocling / GraniteDocling full-page VLM

_DEFAULT_ACCELERATOR = ACCELERATOR_CPU
_DEFAULT_PIPELINE = PIPELINE_STANDARD

# Optional local model cache path — set DOCLING_ARTIFACTS_PATH env var or pass explicitly.
# When set, Docling skips HuggingFace downloads and uses pre-fetched models.
_ARTIFACTS_PATH: Optional[str] = os.environ.get("DOCLING_ARTIFACTS_PATH")


def get_docling_unavailable_reason() -> str | None:
    """Return a human-readable string if Docling cannot be used, else None."""
    try:
        import docling  # noqa: F401
    except ImportError:
        return (
            "Docling is not installed. "
            "Install the 'docling' extra: pip install -e .[docling]"
        )
    return None


def _compute_file_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sidecar_stem(accelerator: str, pipeline: str) -> str:
    """Build the sidecar filename stem, encoding both accelerator and pipeline route."""
    if pipeline == PIPELINE_VLM:
        return f"docling_{accelerator}_vlm"
    return f"docling_{accelerator}"


def _docling_json_sidecar_path(file_path: str, accelerator: str, pipeline: str = PIPELINE_STANDARD) -> Path:
    path = Path(file_path)
    return path.with_suffix(f"{path.suffix}.{_sidecar_stem(accelerator, pipeline)}.json")


def _docling_text_sidecar_path(file_path: str, accelerator: str, pipeline: str = PIPELINE_STANDARD) -> Path:
    path = Path(file_path)
    return path.with_suffix(f"{path.suffix}.{_sidecar_stem(accelerator, pipeline)}.txt")


def _docling_tables_sidecar_path(file_path: str, accelerator: str, pipeline: str = PIPELINE_STANDARD) -> Path:
    path = Path(file_path)
    return path.with_suffix(f"{path.suffix}.{_sidecar_stem(accelerator, pipeline)}_tables.json")


def _load_cached_docling_artifact(
    file_path: str,
    accelerator: str,
    pipeline: str = PIPELINE_STANDARD,
    expected_hash: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Return cached artifact dict if present and still valid, else None.

    Checks DB first (when conn is provided), falls back to sidecar file.
    """
    # --- DB-first lookup ---
    if conn is not None:
        try:
            from duke_rates.db.artifact_cache import load_docling_artifact
            db_hash = expected_hash  # may be None
            row = load_docling_artifact(
                conn,
                source_pdf=file_path,
                file_hash=db_hash,
                backend_version=DOCLING_BACKEND_VERSION,
                accelerator=accelerator,
            )
            if row and row.get("status") == "success":
                row_pipeline = row.get("pipeline") or PIPELINE_STANDARD
                if row_pipeline == pipeline:
                    # Reconstruct the artifact envelope from DB content
                    payload: dict = {
                        "file_hash": row.get("file_hash"),
                        "backend": DOCLING_PACKAGE,
                        "backend_version": DOCLING_BACKEND_VERSION,
                        "accelerator": accelerator,
                        "pipeline": pipeline,
                        "page_count": row.get("page_count", 0),
                        "conversion_status": row.get("status", "success"),
                        "conversion_confidence": row.get("conversion_confidence"),
                        "document": json.loads(row["doc_json_content"]) if row.get("doc_json_content") else {},
                        "plain_text": row.get("plain_text_content") or "",
                        "tables": json.loads(row["tables_json_content"]) if row.get("tables_json_content") else [],
                        "_source": "db",
                    }
                    return payload
        except Exception as exc:
            logger.debug("Docling DB cache lookup failed, falling back to sidecar: %s", exc)

    # --- Sidecar file fallback ---
    json_path = _docling_json_sidecar_path(file_path, accelerator, pipeline)
    if not json_path.exists():
        return None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed reading Docling sidecar %s: %s", json_path, exc)
        return None
    if expected_hash and payload.get("file_hash") not in {None, expected_hash}:
        logger.debug("Docling cache miss: file hash changed for %s", file_path)
        return None
    if payload.get("backend_version") not in {None, DOCLING_BACKEND_VERSION}:
        logger.debug("Docling cache miss: backend version changed for %s", file_path)
        return None
    if payload.get("accelerator") not in {None, accelerator}:
        return None
    if payload.get("pipeline") not in {None, pipeline}:
        return None
    payload["_source"] = "sidecar"
    return payload


def _write_docling_artifacts(
    file_path: str,
    *,
    file_hash: str,
    accelerator: str,
    pipeline: str = PIPELINE_STANDARD,
    doc_json: dict = None,
    plain_text: str,
    tables: list[dict],
    page_count: int,
    conversion_status: str,
    conversion_confidence: Optional[float],
) -> tuple[Path, Path, Path]:
    """Write the three Docling sidecar files and return their paths."""
    json_path = _docling_json_sidecar_path(file_path, accelerator, pipeline)
    text_path = _docling_text_sidecar_path(file_path, accelerator, pipeline)
    tables_path = _docling_tables_sidecar_path(file_path, accelerator, pipeline)

    envelope = {
        "file_hash": file_hash,
        "backend": DOCLING_PACKAGE,
        "backend_version": DOCLING_BACKEND_VERSION,
        "accelerator": accelerator,
        "pipeline": pipeline,
        "page_count": page_count,
        "conversion_status": conversion_status,
        "conversion_confidence": conversion_confidence,
        "document": doc_json,
    }
    json_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    text_path.write_text(plain_text, encoding="utf-8")
    tables_path.write_text(json.dumps(tables, indent=2), encoding="utf-8")

    return json_path, text_path, tables_path


def _extract_tables_from_doc(doc) -> list[dict]:
    """Extract table metadata from a Docling ConversionResult."""
    tables = []
    try:
        for table in doc.document.tables:
            rows = []
            try:
                grid = table.data.grid
                for row in grid:
                    rows.append([str(cell.text) if cell.text else "" for cell in row])
            except Exception:
                pass
            tables.append(
                {
                    "page": getattr(table, "prov", [{}])[0].page_no
                    if getattr(table, "prov", None)
                    else None,
                    "row_count": len(rows),
                    "rows": rows[:20],
                }
            )
    except Exception as exc:
        logger.debug("Could not extract tables from Docling doc: %s", exc)
    return tables


def _build_standard_converter(accel_device, *, has_scanned_pages: bool, artifacts_path: Optional[str]):
    """Build a DocumentConverter using the standard pipeline.

    Uses:
      - docling-layout-heron (current best layout model, replaces legacy layout-v2)
      - TableFormer ACCURATE with do_cell_matching=True
      - Tesseract OCR when scanned pages present (better than EasyOCR for typewritten docs)
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
        LayoutOptions,
        TesseractCliOcrOptions,
    )
    from docling.datamodel.accelerator_options import AcceleratorOptions

    # Layout: use heron model (current default ⭐), fall back gracefully if not importable
    layout_options = None
    try:
        from docling.datamodel.layout_model_specs import DOCLING_LAYOUT_HERON
        layout_options = LayoutOptions(model_spec=DOCLING_LAYOUT_HERON)
    except (ImportError, AttributeError):
        logger.debug("docling-layout-heron spec not available — using Docling default layout model")

    pipeline_options = PdfPipelineOptions(
        **({"artifacts_path": artifacts_path} if artifacts_path else {}),
    )
    pipeline_options.accelerator_options = AcceleratorOptions(device=accel_device)

    # TableFormer ACCURATE: handles multi-row spans and merged cells in regulatory tables
    pipeline_options.table_structure_options = TableStructureOptions(
        mode=TableFormerMode.ACCURATE,
        do_cell_matching=True,
    )

    if layout_options is not None:
        pipeline_options.layout_options = layout_options

    # OCR: only enable for scanned pages; use TesseractCli (CLI-based, avoids the
    # tesserocr binary compilation requirement; works with system Tesseract v5+)
    pipeline_options.do_ocr = has_scanned_pages
    if has_scanned_pages:
        pipeline_options.ocr_options = TesseractCliOcrOptions(
            lang=["eng"],
            force_full_page_ocr=False,  # auto-detect; caller sets True for fully scanned docs
        )

    # Disable stages irrelevant to rate/table extraction
    pipeline_options.generate_page_images = False
    pipeline_options.generate_picture_images = False
    pipeline_options.do_picture_classification = False

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def _build_vlm_converter(accel_device, *, artifacts_path: Optional[str]):
    """Build a DocumentConverter using the VLM pipeline (SmolDocling/GraniteDocling).

    Best for pre-1990 hard scans where standard OCR fails — treats each page as
    a vision task rather than an OCR+layout pipeline.
    Uses GraniteDocling-258M (IBM, DocTags output) on CUDA; falls back to SmolDocling.
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.pipeline.vlm_pipeline import VlmPipeline
    from docling.datamodel.pipeline_options import VlmPipelineOptions
    from docling.datamodel import vlm_model_specs

    # Prefer GraniteDocling (IBM, slightly more accurate on structured docs)
    # Fall back to SmolDocling if GraniteDocling spec unavailable
    try:
        vlm_options = vlm_model_specs.GRANITEDOCLING_TRANSFORMERS
    except AttributeError:
        try:
            vlm_options = vlm_model_specs.SMOLDOCLING_TRANSFORMERS
            logger.debug("VLM: GraniteDocling unavailable, using SmolDocling")
        except AttributeError:
            logger.warning("VLM: neither GraniteDocling nor SmolDocling specs available")
            return None

    pipeline_options = VlmPipelineOptions(vlm_options=vlm_options)

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline,
                pipeline_options=pipeline_options,
            )
        }
    )


def convert_pdf_with_docling(
    file_path: str,
    *,
    accelerator: str = _DEFAULT_ACCELERATOR,
    pipeline: str = _DEFAULT_PIPELINE,
    force: bool = False,
    max_pages: Optional[int] = None,
    has_scanned_pages: bool = False,
    force_full_page_ocr: bool = False,
    artifacts_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    discovery_record_id: Optional[int] = None,
) -> Optional[dict]:
    """Convert a PDF with Docling and return the cached artifact envelope, or None on failure.

    Args:
        file_path:            Path to the source PDF.
        accelerator:          "cpu", "cuda", or "mps".
        pipeline:             "standard" (layout+table+OCR) or "vlm" (SmolDocling/GraniteDocling).
        force:                Re-run even if a cached artifact exists.
        max_pages:            Limit conversion to the first N pages.
        has_scanned_pages:    Enable OCR (Tesseract) for pages with image content.
        force_full_page_ocr:  Apply OCR to every page (use for fully scanned documents).
        artifacts_path:       Local model cache path (overrides DOCLING_ARTIFACTS_PATH env var).
        conn:                 Open DB connection. When provided, content is stored in the
                              docling_artifacts table and sidecar files are not written.
        discovery_record_id:  FK into ncuc_discovery_records (optional, stored with artifact).

    The returned dict has these keys:
        file_hash, backend, backend_version, accelerator, pipeline,
        page_count, conversion_status, conversion_confidence,
        document  (the raw Docling export dict),
        plain_text  (str, extracted text),
        tables      (list[dict], extracted table grids),
        plain_text_path  (str | None, path to the .txt sidecar if written to disk),
        tables_path      (str | None, path to the tables JSON sidecar if written to disk),
        json_path        (str, path to the main JSON sidecar)
    """
    unavailable = get_docling_unavailable_reason()
    if unavailable:
        logger.warning("%s  Source=%s", unavailable, file_path)
        return None

    if not Path(file_path).exists():
        logger.warning("Docling: source file not found: %s", file_path)
        return None

    resolved_artifacts = artifacts_path or _ARTIFACTS_PATH

    file_hash = ""
    try:
        file_hash = _compute_file_hash(file_path)
    except Exception as exc:
        logger.warning("Docling: cannot hash source %s: %s", file_path, exc)

    if not force:
        cached = _load_cached_docling_artifact(
            file_path, accelerator, pipeline, expected_hash=file_hash or None, conn=conn
        )
        if cached is not None:
            logger.debug(
                "Docling: cache hit (%s) for %s (%s/%s)",
                cached.get("_source", "unknown"),
                file_path, accelerator, pipeline,
            )
            # Ensure path keys are present (may be None if DB-stored)
            cached.setdefault("json_path", None)
            cached.setdefault("plain_text_path", None)
            cached.setdefault("tables_path", None)
            return cached

    # --- actual Docling conversion ---
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice
    except ImportError as exc:
        logger.warning("Docling import failed: %s", exc)
        return None

    accel_device = {
        ACCELERATOR_CPU: AcceleratorDevice.CPU,
        ACCELERATOR_CUDA: AcceleratorDevice.CUDA,
        ACCELERATOR_MPS: AcceleratorDevice.MPS,
    }.get(accelerator, AcceleratorDevice.CPU)

    try:
        if pipeline == PIPELINE_VLM:
            converter = _build_vlm_converter(accel_device, artifacts_path=resolved_artifacts)
            if converter is None:
                logger.error("VLM converter could not be built for %s", file_path)
                return None
        else:
            # Apply force_full_page_ocr by enabling has_scanned_pages
            effective_scanned = has_scanned_pages or force_full_page_ocr
            converter = _build_standard_converter(
                accel_device,
                has_scanned_pages=effective_scanned,
                artifacts_path=resolved_artifacts,
            )
            # Override force_full_page_ocr on the pipeline options if needed
            if force_full_page_ocr:
                try:
                    fmt_opt = converter.format_to_options.get(
                        __import__("docling.datamodel.base_models", fromlist=["InputFormat"]).InputFormat.PDF
                    )
                    if fmt_opt and hasattr(fmt_opt, "pipeline_options"):
                        fmt_opt.pipeline_options.ocr_options.force_full_page_ocr = True
                except Exception:
                    pass

        convert_kwargs: dict = {}
        if max_pages is not None:
            convert_kwargs["max_num_pages"] = max_pages

        # BF16 autocast on CUDA (Ada Lovelace has native BF16 tensor cores)
        if accelerator == ACCELERATOR_CUDA:
            try:
                import torch
                if torch.cuda.is_available():
                    with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                        result = converter.convert(file_path, **convert_kwargs)
                else:
                    result = converter.convert(file_path, **convert_kwargs)
            except Exception:
                result = converter.convert(file_path, **convert_kwargs)
        else:
            result = converter.convert(file_path, **convert_kwargs)

    except Exception as exc:
        logger.error("Docling conversion failed for %s: %s", file_path, exc)
        return None
    finally:
        if accelerator == ACCELERATOR_CUDA:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    # --- post-processing ---
    try:
        plain_text = result.document.export_to_markdown()
    except Exception:
        plain_text = ""

    try:
        page_count = len(result.document.pages)
    except Exception:
        page_count = 0

    conversion_status = "success"
    conversion_confidence: Optional[float] = None
    try:
        status_val = getattr(result, "status", None)
        if status_val is not None:
            conversion_status = str(status_val)
    except Exception:
        pass

    try:
        doc_json = result.document.export_to_dict()
    except Exception:
        doc_json = {}

    tables = _extract_tables_from_doc(result)

    json_path: Optional[Path] = None
    text_path: Optional[Path] = None
    tables_path: Optional[Path] = None

    if conn is not None:
        # --- Primary: store content in DB ---
        try:
            from duke_rates.db.artifact_cache import save_docling_artifact
            save_docling_artifact(
                conn,
                discovery_record_id=discovery_record_id,
                source_pdf=file_path,
                file_hash=file_hash or None,
                backend_version=DOCLING_BACKEND_VERSION,
                accelerator=accelerator,
                pipeline=pipeline,
                status=conversion_status,
                json_sidecar_path=None,
                text_sidecar_path=None,
                tables_sidecar_path=None,
                doc_json_content=json.dumps(doc_json),
                plain_text_content=plain_text,
                tables_json_content=json.dumps(tables),
                page_count=page_count,
                conversion_confidence=conversion_confidence,
                table_count=len(tables),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("Docling: failed saving artifact to DB for %s: %s", file_path, exc)
            # Fall through to sidecar fallback
            conn = None

    if conn is None:
        # --- Fallback: write sidecar files ---
        try:
            json_path, text_path, tables_path = _write_docling_artifacts(
                file_path,
                file_hash=file_hash,
                accelerator=accelerator,
                pipeline=pipeline,
                doc_json=doc_json,
                plain_text=plain_text,
                tables=tables,
                page_count=page_count,
                conversion_status=conversion_status,
                conversion_confidence=conversion_confidence,
            )
        except Exception as exc:
            logger.warning("Docling: failed writing sidecar artifacts for %s: %s", file_path, exc)
            return None

    logger.info(
        "Docling: converted %s pages=%d accelerator=%s pipeline=%s status=%s tables=%d storage=%s",
        file_path,
        page_count,
        accelerator,
        pipeline,
        conversion_status,
        len(tables),
        "db" if json_path is None else "sidecar",
    )

    return {
        "file_hash": file_hash,
        "backend": DOCLING_PACKAGE,
        "backend_version": DOCLING_BACKEND_VERSION,
        "accelerator": accelerator,
        "pipeline": pipeline,
        "page_count": page_count,
        "conversion_status": conversion_status,
        "conversion_confidence": conversion_confidence,
        "document": doc_json,
        "plain_text": plain_text,
        "tables": tables,
        "json_path": str(json_path) if json_path else None,
        "plain_text_path": str(text_path) if text_path else None,
        "tables_path": str(tables_path) if tables_path else None,
    }


# =====================================================================
# Safe processing wrapper — automatic retry, degradation, and chunking
#
# Wraps the Docling pipeline with memory-safe batch sizes, progressive
# degradation (no OCR → no tables → CPU), and page-level chunked
# fallback for large or image-heavy PDFs that exhaust GPU memory.
# =====================================================================

import tempfile as _tempfile
import time as _time

# Degradation-mode bitfield flags
_DEG_NONE = 0
_DEG_NO_OCR = 1 << 0       # Disable OCR (largest memory consumer for scans)
_DEG_NO_TABLES = 1 << 1    # Disable table structure extraction

# Max seconds per single conversion attempt — prevents hanging on
# pathological PDFs where Docling logs bad_alloc per page indefinitely.
# Raised from 120 to 200 to accommodate 25-page chunks with TableFormer
# ACCURATE on CUDA (avg ~3s/page, worst-case ~7s/page × 25 = 175s).
_SAFE_DOCUMENT_TIMEOUT = 200.0

# Per-page seconds used when scaling timeout with chunk size. A 25-page
# chunk gets 25 * 8 = 200 s capped at the ceiling; a single page gets
# the floor. Bumped from 5.0 to 8.0 after testing showed CUDA+TableFormer
# processes tariff pages at 3-7 s/page; 5.0 caused every multi-page chunk
# to hit PARTIAL_SUCCESS and subdivide unnecessarily.
_SAFE_TIMEOUT_PER_PAGE = 8.0
_SAFE_TIMEOUT_FLOOR = 30.0


def _scale_timeout_for_chunk_size(base_timeout: float, chunk_size: int) -> float:
    """Return a per-attempt timeout scaled to chunk_size.

    Bounded by _SAFE_TIMEOUT_FLOOR (so we don't kill a slow first-page load)
    and the caller's base_timeout (the existing global ceiling).
    """
    if chunk_size <= 0:
        return base_timeout
    scaled = chunk_size * _SAFE_TIMEOUT_PER_PAGE
    return max(_SAFE_TIMEOUT_FLOOR, min(base_timeout, scaled))


def _build_safe_converter(
    accel_device,
    *,
    artifacts_path: Optional[str] = None,
    has_scanned_pages: bool = False,
    force_full_page_ocr: bool = False,
    deg_flags: int = _DEG_NONE,
    document_timeout: Optional[float] = _SAFE_DOCUMENT_TIMEOUT,
):
    """Build a Docling converter with conservative batching and optional degradation.

    Parameters
    ----------
    accel_device : AcceleratorDevice
        CUDA, CPU, or MPS device enum.
    deg_flags : int
        Bitfield of _DEG_* flags. _DEG_NO_OCR disables OCR; _DEG_NO_TABLES
        disables table structure extraction.
    document_timeout : float or None
        Max seconds before Docling returns PARTIAL_SUCCESS (None = no limit).
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableStructureOptions,
        TableFormerMode,
        LayoutOptions,
        TesseractCliOcrOptions,
    )
    from docling.datamodel.accelerator_options import AcceleratorOptions

    layout_opts = None
    try:
        from docling.datamodel.layout_model_specs import DOCLING_LAYOUT_HERON
        layout_opts = LayoutOptions(model_spec=DOCLING_LAYOUT_HERON)
    except (ImportError, AttributeError):
        pass

    pipeline_opts = PdfPipelineOptions(
        **({"artifacts_path": artifacts_path} if artifacts_path else {}),
    )

    # ---- Batch sizes — GPU can handle concurrent inference, CPU stays minimal ----
    from docling.datamodel.accelerator_options import AcceleratorDevice as _AccelDev
    if accel_device == _AccelDev.CUDA:
        pipeline_opts.layout_batch_size = 1  # layout=1 avoids CUDA OOM on complex tariff pages
        pipeline_opts.table_batch_size = 2
    else:
        pipeline_opts.layout_batch_size = 1
        pipeline_opts.table_batch_size = 1
    pipeline_opts.ocr_batch_size = 1  # OCR is the memory bottleneck; keep at 1
    pipeline_opts.queue_max_size = 10
    pipeline_opts.batch_polling_interval_seconds = 0.1

    # Per-attempt timeout prevents hanging on endless bad_alloc loops
    if document_timeout is not None:
        pipeline_opts.document_timeout = document_timeout

    # Limit CPU threads to reduce memory contention
    pipeline_opts.accelerator_options = AcceleratorOptions(
        device=accel_device,
        num_threads=2,
    )

    # ---- Apply degradation flags ----
    do_ocr = has_scanned_pages and not (deg_flags & _DEG_NO_OCR)
    do_tables = not (deg_flags & _DEG_NO_TABLES)
    pipeline_opts.do_ocr = do_ocr
    pipeline_opts.do_table_structure = do_tables

    if do_ocr:
        pipeline_opts.ocr_options = TesseractCliOcrOptions(
            lang=["eng"],
            force_full_page_ocr=force_full_page_ocr,
        )

    if layout_opts is not None:
        pipeline_opts.layout_options = layout_opts

    # All non-essential features disabled
    pipeline_opts.generate_page_images = False
    pipeline_opts.generate_picture_images = False
    pipeline_opts.do_picture_classification = False
    pipeline_opts.do_chart_extraction = False
    pipeline_opts.do_code_enrichment = False
    pipeline_opts.do_formula_enrichment = False

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_opts)}
    )


def _safe_convert_attempt(
    file_path: str,
    *,
    accelerator: str,
    has_scanned_pages: bool,
    force_full_page_ocr: bool,
    artifacts_path: Optional[str],
    deg_flags: int,
    document_timeout: float,
    page_offset: int,
) -> tuple[Optional[dict], Optional[str]]:
    """Single conversion attempt with the given degradation level.

    Returns (post-processed_result_dict, error_or_None).
    The result dict has: plain_text, page_count, conversion_status,
    conversion_confidence, document (dict), tables (list), page_offset.

    Does NOT store to DB/sidecar — the caller handles that.
    """
    from docling.datamodel.accelerator_options import AcceleratorDevice

    accel_device = {
        ACCELERATOR_CPU: AcceleratorDevice.CPU,
        ACCELERATOR_CUDA: AcceleratorDevice.CUDA,
    }.get(accelerator, AcceleratorDevice.CPU)

    converter = _build_safe_converter(
        accel_device,
        artifacts_path=artifacts_path,
        has_scanned_pages=has_scanned_pages,
        force_full_page_ocr=force_full_page_ocr,
        deg_flags=deg_flags,
        document_timeout=document_timeout,
    )

    convert_kwargs = {}
    result_obj = None

    try:
        if accelerator == ACCELERATOR_CUDA:
            import torch
            if torch.cuda.is_available():
                with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                    result_obj = converter.convert(file_path, **convert_kwargs)
            else:
                result_obj = converter.convert(file_path, **convert_kwargs)
        else:
            result_obj = converter.convert(file_path, **convert_kwargs)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if accelerator == ACCELERATOR_CUDA:
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    if result_obj is None:
        return None, "converter returned None"

    # ---- Post-process with page offset for chunk stitching ----
    try:
        plain_text = result_obj.document.export_to_markdown()
    except Exception:
        plain_text = ""

    try:
        page_count = len(result_obj.document.pages)
    except Exception:
        page_count = 0

    # Treat empty results (0 pages, no text) as failure — avoids caching
    # worthless artifacts from timeout-during-bad_alloc storms
    if page_count == 0 and not plain_text.strip():
        return None, "empty result (0 pages, no text)"

    status = "success"
    try:
        s = getattr(result_obj, "status", None)
        if s is not None:
            status = str(s)
    except Exception:
        pass

    try:
        doc_json = result_obj.document.export_to_dict()
    except Exception:
        doc_json = {}

    tables = _extract_tables_from_doc(result_obj)
    # Adjust table page numbers by the chunk offset
    if page_offset:
        for t in tables:
            if t.get("page") is not None:
                try:
                    t["page"] = int(t["page"]) + page_offset
                except (TypeError, ValueError):
                    pass

    return {
        "plain_text": plain_text,
        "page_count": page_count,
        "conversion_status": status,
        "conversion_confidence": None,
        "document": doc_json,
        "tables": tables,
        "page_offset": page_offset,
    }, None


def _degradation_label(deg_flags: int) -> str:
    """Human-readable label for a degradation bitfield."""
    parts = []
    if deg_flags & _DEG_NO_OCR:
        parts.append("no-ocr")
    if deg_flags & _DEG_NO_TABLES:
        parts.append("no-tables")
    return "+".join(parts) if parts else "full"


# Per-page degradation ladder (tried when subdivision reaches single pages)
_PAGE_DEGRADATION: list[tuple[int, str]] = [
    (_DEG_NONE, "full"),
    (_DEG_NO_OCR, "no-ocr"),
    (_DEG_NO_OCR | _DEG_NO_TABLES, "no-ocr+no-tables"),
]


def _try_glm_single_page(file_path: str, page_offset: int) -> Optional[dict]:
    """Last-resort single-page OCR via Ollama GLM-OCR.

    Used when the Docling per-page degradation ladder has exhausted itself
    (full → no-ocr → no-ocr+no-tables) and the page is still failing. GLM-OCR
    runs on a different code path (vision model via HTTP), so failure modes
    rarely overlap with Docling's.

    Returns a result dict in the same shape as ``_safe_convert_attempt``,
    or None if GLM is unreachable, the model is missing, or OCR returns no text.
    """
    try:
        import base64
        import io as _io
        import os as _os

        import fitz
        import httpx
        from PIL import Image as _Image
    except Exception as exc:
        logger.debug("GLM single-page deps unavailable: %s", exc)
        return None

    ollama_host = _os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        with httpx.Client(timeout=5.0) as client:
            tags = client.get(f"{ollama_host}/api/tags")
            tags.raise_for_status()
            names = {m.get("name", "") for m in (tags.json().get("models") or [])}
            if "glm-ocr" not in names and "glm-ocr:latest" not in names:
                logger.debug("GLM single-page: model not present at %s", ollama_host)
                return None
    except Exception as exc:
        logger.debug("GLM single-page: ollama unreachable: %s", exc)
        return None

    try:
        doc = fitz.open(file_path)
    except Exception as exc:
        logger.warning("GLM single-page: cannot open %s: %s", file_path, exc)
        return None

    try:
        if doc.page_count == 0:
            return None
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        image = _Image.open(_io.BytesIO(pix.tobytes("png")))
        # Cap image dimension — large rendered pages can crash the vision
        # model on Ollama with payload-size errors.
        max_dim = 2000
        if max(image.width, image.height) > max_dim:
            scale = max_dim / max(image.width, image.height)
            image = image.resize(
                (int(image.width * scale), int(image.height * scale)),
                _Image.LANCZOS,
            )
        buf = _io.BytesIO()
        image.save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        doc.close()

    payload = {
        "model": "glm-ocr",
        "prompt": (
            "Perform OCR on this page and return the readable text in natural "
            "reading order. Preserve table rows and headings."
        ),
        "images": [encoded],
        "stream": False,
    }
    try:
        with httpx.Client(timeout=90.0) as client:
            response = client.post(f"{ollama_host}/api/generate", json=payload)
            if response.status_code != 200:
                logger.warning(
                    "GLM single-page: status=%d body=%r",
                    response.status_code, response.text[:200],
                )
                return None
            text = str(response.json().get("response") or "").strip()
    except Exception as exc:
        logger.warning("GLM single-page: request failed: %s", exc)
        return None

    if not text:
        return None

    return {
        "plain_text": text,
        "page_count": 1,
        "conversion_status": "success",
        "conversion_confidence": None,
        "document": {},
        "tables": [],
        "page_offset": page_offset,
    }


def _merge_chunk_results(chunks: list[dict]) -> dict:
    """Merge post-processed results from multiple chunks into one.

    Concatenates plain_text in page order, merges tables, and reports
    PARTIAL_SUCCESS if any chunk had non-success status.
    """
    if not chunks:
        return {}
    if len(chunks) == 1:
        return chunks[0]

    all_text: list[str] = []
    all_tables: list[dict] = []
    total_pages = 0
    overall_status = "success"

    for chunk in chunks:
        all_text.append(chunk.get("plain_text", ""))
        all_tables.extend(chunk.get("tables", []))
        total_pages += chunk.get("page_count", 0)
        if chunk.get("conversion_status", "") not in ("success", "ConversionStatus.SUCCESS"):
            overall_status = "partial_success"

    return {
        "plain_text": "\n\n".join(all_text),
        "page_count": total_pages,
        "conversion_status": overall_status,
        "conversion_confidence": None,
        "document": chunks[-1].get("document", {}),
        "tables": all_tables,
    }


def _split_pdf_chunks(file_path: str, chunk_size: int):
    """Split a PDF into page-range temp files using PyMuPDF.

    Returns
    -------
    chunks : list of (start_1, end_inclusive, temp_path)
    total_pages : int
    """
    import fitz
    src = fitz.open(file_path)
    total = src.page_count
    chunks: list[tuple[int, int, str]] = []

    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        tmp = _tempfile.NamedTemporaryFile(
            suffix=f"_p{start + 1}-{end}.pdf",
            delete=False,
        )
        tmp.close()
        writer = fitz.open()
        writer.insert_pdf(src, from_page=start, to_page=end - 1)
        writer.save(tmp.name)
        writer.close()
        chunks.append((start + 1, end, tmp.name))

    src.close()
    return chunks, total


def _cleanup_temp_files(paths: list[str]):
    """Delete temporary files, ignoring per-file errors."""
    for p in paths:
        try:
            os.unlink(p)
        except Exception:
            pass


def _resolve_chunk(
    file_path: str,
    *,
    accelerator: str,
    has_scanned_pages: bool,
    force_full_page_ocr: bool,
    artifacts_path: Optional[str],
    deg_flags: int,
    chunk_size: int,
    document_timeout: float,
    page_offset: int,
    log_label: str,
) -> tuple[Optional[dict], list[int]]:
    """Process a single chunk with recursive subdivision and per-page degradation.

    Strategy (preserves OCR+tables until subdivision reaches single pages):
      1. Try the chunk as-is with current ``deg_flags``.
      2. If that fails, subdivide (``chunk_size // 2``) and retry each
         sub-chunk independently (same ``deg_flags``).
      3. When subdivision reaches single pages, try the per-page degradation
         ladder (full → no-ocr → no-ocr+no-tables).  Still-failing pages
         are skipped.

    Returns
    -------
    (merged_result_or_None, sorted_list_of_1_indexed_skipped_pages)
    """
    if chunk_size <= 1:
        # Single page — try degradation ladder before giving up. Scale the
        # timeout to a single page so a stuck page surfaces in seconds rather
        # than locking the worker for the full 120 s ceiling.
        single_page_timeout = _scale_timeout_for_chunk_size(document_timeout, 1)
        for try_deg, label in _PAGE_DEGRADATION:
            result, error = _safe_convert_attempt(
                file_path,
                accelerator=accelerator,
                has_scanned_pages=has_scanned_pages,
                force_full_page_ocr=force_full_page_ocr,
                artifacts_path=artifacts_path,
                deg_flags=try_deg,
                document_timeout=single_page_timeout,
                page_offset=page_offset,
            )
            if result:
                if try_deg != _DEG_NONE:
                    result["_page_degraded"] = True
                logger.info("%s p%d OK deg=%s", log_label, page_offset + 1, label)
                return result, []

        # Docling ladder exhausted — try GLM-OCR on this page. Different
        # backend, different failure modes; recovers some pages where Docling
        # crashes on layout but the vision model succeeds.
        glm_result = _try_glm_single_page(file_path, page_offset=page_offset)
        if glm_result:
            glm_result["_page_degraded"] = True
            logger.info("%s p%d OK deg=glm-ocr", log_label, page_offset + 1)
            return glm_result, []

        logger.warning(
            "%s p%d FAILED all degradation levels",
            log_label, page_offset + 1,
        )
        return None, [page_offset + 1]

    # Multi-page — try single-shot first, with a timeout scaled to chunk_size.
    chunk_timeout = _scale_timeout_for_chunk_size(document_timeout, chunk_size)
    result, error = _safe_convert_attempt(
        file_path,
        accelerator=accelerator,
        has_scanned_pages=has_scanned_pages,
        force_full_page_ocr=force_full_page_ocr,
        artifacts_path=artifacts_path,
        deg_flags=deg_flags,
        document_timeout=chunk_timeout,
        page_offset=page_offset,
    )
    if result:
        is_partial = result.get("conversion_status", "") in (
            "ConversionStatus.PARTIAL_SUCCESS", "partial_success",
        )
        if not is_partial:
            return result, []
        # PARTIAL_SUCCESS on a multi-page chunk means the timeout cut short
        # some pages' enrichment.  Subdivide to give each sub-chunk a smaller
        # working set so it completes with full OCR+tables under the deadline.
        logger.info(
            "%s PARTIAL_SUCCESS deg=%s — subdividing for full quality",
            log_label, _degradation_label(deg_flags),
        )

    else:
        # Failed outright — subdivide and retry each sub-chunk
        logger.info(
            "%s FAILED deg=%s — subdividing to chunk_size=%d",
            log_label, _degradation_label(deg_flags), max(1, chunk_size // 2),
        )

    next_size = max(1, chunk_size // 2)

    sub_chunks, _ = _split_pdf_chunks(file_path, next_size)
    results: list[dict] = []
    all_skipped: list[int] = []

    try:
        for sub_start, sub_end, sub_path in sub_chunks:
            sub_result, sub_skipped = _resolve_chunk(
                sub_path,
                accelerator=accelerator,
                has_scanned_pages=has_scanned_pages,
                force_full_page_ocr=force_full_page_ocr,
                artifacts_path=artifacts_path,
                deg_flags=deg_flags,
                chunk_size=next_size,
                document_timeout=document_timeout,
                page_offset=page_offset + sub_start - 1,
                log_label=log_label,
            )
            if sub_result:
                results.append(sub_result)
            all_skipped.extend(sub_skipped)
    finally:
        _cleanup_temp_files([p for _, _, p in sub_chunks])

    merged = _merge_chunk_results(results) if results else None
    return merged, sorted(all_skipped)


def _process_all_chunks(
    file_path: str,
    *,
    accelerator: str,
    has_scanned_pages: bool,
    force_full_page_ocr: bool,
    artifacts_path: Optional[str],
    chunk_size: int,
    document_timeout: float,
    log_label: str,
) -> tuple[list[dict], list[int], bool]:
    """Split a PDF into chunks and process each with ``_resolve_chunk``.

    Returns
    -------
    (chunk_results, sorted_skipped_pages, any_page_degraded)
    """
    chunks_info, _ = _split_pdf_chunks(file_path, chunk_size)
    temp_files = [p for _, _, p in chunks_info]

    results: list[dict] = []
    all_skipped: list[int] = []
    any_degraded = False

    try:
        for start, end, chunk_path in chunks_info:
            cr, skipped = _resolve_chunk(
                chunk_path,
                accelerator=accelerator,
                has_scanned_pages=has_scanned_pages,
                force_full_page_ocr=force_full_page_ocr,
                artifacts_path=artifacts_path,
                deg_flags=_DEG_NONE,
                chunk_size=chunk_size,
                document_timeout=document_timeout,
                page_offset=start - 1,
                log_label=log_label,
            )
            if cr:
                results.append(cr)
                if cr.get("_page_degraded"):
                    any_degraded = True
            all_skipped.extend(skipped)

            # Free GPU memory between chunks to reduce fragmentation under
            # repeated allocations. Per-chunk reset is more effective than
            # the single empty_cache() in convert_pdf_with_docling that runs
            # once at the very end.
            if accelerator == ACCELERATOR_CUDA:
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
    finally:
        _cleanup_temp_files(temp_files)

    return results, sorted(all_skipped), any_degraded


def convert_pdf_safe(
    file_path: str,
    *,
    accelerator: str = _DEFAULT_ACCELERATOR,
    pipeline: str = _DEFAULT_PIPELINE,
    force: bool = False,
    max_pages: Optional[int] = None,
    has_scanned_pages: bool = False,
    force_full_page_ocr: bool = False,
    artifacts_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    discovery_record_id: Optional[int] = None,
    chunk_size: int = 25,
    document_timeout: float = _SAFE_DOCUMENT_TIMEOUT,
    proactive_chunk_threshold: int = 50,
) -> Optional[dict]:
    """Convert a PDF with memory-safe settings, chunked fallback,
    and per-page degradation.

    The fault-tolerant wrapper around Docling:

    1. Checks cache first (same key as ``convert_pdf_with_docling``).
    2. PDFs with page count >= ``proactive_chunk_threshold`` skip the
       full-document attempt and go straight to page-range chunking,
       avoiding ``PARTIAL_SUCCESS`` from the 120 s ``document_timeout``.
    3. Attempts full conversion with conservative batch sizes (batch=1,
       threads=2, timeout=120s) — only for docs below the threshold.
    4. On failure or PARTIAL_SUCCESS, splits the PDF into page-range
       chunks (default 25 pages) and processes each chunk *with full
       OCR+tables settings*.
    5. Within each chunk, subdivides recursively down to single pages
       when needed — subdivision preserves OCR+tables.
    6. When subdivision reaches single pages, tries a degradation ladder
       (no OCR → no tables) before giving up on that page.
    7. If GPU chunking fails entirely, retries all chunks on CPU.
    8. Stitches chunk results into a single artifact dict and stores it
       (DB or sidecar, same format as ``convert_pdf_with_docling``).

    Returns the same dict format as ``convert_pdf_with_docling`` plus
    diagnostic keys:

    * ``_degraded_modes`` — list of mode labels applied (e.g.
      ``["no_ocr"]``, ``["no_ocr", "cpu_fallback"]``)
    * ``_skipped_pages`` — 1-indexed page numbers that could not be
      processed
    * ``_chunked`` — True if page-level chunking was used
    """
    # ---- Prerequisites ----
    unavailable = get_docling_unavailable_reason()
    if unavailable:
        logger.warning("%s  Source=%s", unavailable, file_path)
        return None

    if not Path(file_path).exists():
        logger.warning("Docling safe: source file not found: %s", file_path)
        return None

    resolved_artifacts = artifacts_path or _ARTIFACTS_PATH
    effective_accelerator = accelerator
    if pipeline == PIPELINE_VLM:
        logger.warning("Docling safe: VLM pipeline not supported — falling back to standard")
        pipeline = PIPELINE_STANDARD

    # ---- Hash and cache check ----
    file_hash = ""
    try:
        file_hash = _compute_file_hash(file_path)
    except Exception as exc:
        logger.warning("Docling safe: cannot hash source %s: %s", file_path, exc)

    if not force:
        cached = _load_cached_docling_artifact(
            file_path, accelerator, pipeline,
            expected_hash=file_hash or None, conn=conn,
        )
        if cached is not None:
            cached.setdefault("json_path", None)
            cached.setdefault("plain_text_path", None)
            cached.setdefault("tables_path", None)
            return cached

    # ---- Attempt 1: Full document conversion (conservative batch sizes, timeout) ----
    effective_accelerator = accelerator
    used_chunking = False
    chosen_deg = _DEG_NONE
    page_degradation_needed = False
    result: Optional[dict] = None

    # Large PDFs skip the full-document attempt and go straight to chunking,
    # so every page-range chunk gets full OCR+tables under the timeout.
    _skip_full_doc = False
    try:
        import fitz as _fitz
        _tmp_doc = _fitz.open(file_path)
        _actual_pages = _tmp_doc.page_count
        _tmp_doc.close()
        if proactive_chunk_threshold > 0 and _actual_pages >= proactive_chunk_threshold:
            _skip_full_doc = True
            logger.info(
                "Docling safe: %s has %d pages — proactive chunking (threshold=%d)",
                file_path, _actual_pages, proactive_chunk_threshold,
            )
    except Exception:
        pass

    if not _skip_full_doc:
        t0 = _time.perf_counter()
        result, error = _safe_convert_attempt(
            file_path,
            accelerator=effective_accelerator,
            has_scanned_pages=has_scanned_pages,
            force_full_page_ocr=force_full_page_ocr,
            artifacts_path=resolved_artifacts,
            deg_flags=_DEG_NONE,
            document_timeout=document_timeout,
            page_offset=0,
        )
        elapsed = _time.perf_counter() - t0

        if result:
            logger.info(
                "Docling safe: %s full OK pages=%d t=%.1fs",
                file_path, result.get("page_count", 0), elapsed,
            )

    if not result:
        if not _skip_full_doc:
            logger.warning(
                "Docling safe: %s full FAILED t=%.1fs: %s",
                file_path, elapsed, error,
            )

        # ---- Attempt 2: Chunk with full settings (preserves OCR+tables) ----
        used_chunking = True
        logger.info(
            "Docling safe: %s — chunking with full settings (chunk_size=%d)",
            file_path, chunk_size,
        )

        chunk_results, all_skipped, any_degraded = _process_all_chunks(
            file_path,
            accelerator=effective_accelerator,
            has_scanned_pages=has_scanned_pages,
            force_full_page_ocr=force_full_page_ocr,
            artifacts_path=resolved_artifacts,
            chunk_size=chunk_size,
            document_timeout=document_timeout,
            log_label=os.path.basename(file_path),
        )
        page_degradation_needed = any_degraded

        # ---- Attempt 3: GPU chunking failed — retry on CPU ----
        if not chunk_results and effective_accelerator == ACCELERATOR_CUDA:
            logger.info(
                "Docling safe: %s — GPU chunking failed, retrying on CPU",
                file_path,
            )
            chunk_results, all_skipped, any_degraded = _process_all_chunks(
                file_path,
                accelerator=ACCELERATOR_CPU,
                has_scanned_pages=has_scanned_pages,
                force_full_page_ocr=force_full_page_ocr,
                artifacts_path=resolved_artifacts,
                chunk_size=chunk_size,
                document_timeout=document_timeout,
                log_label=os.path.basename(file_path),
            )
            page_degradation_needed = page_degradation_needed or any_degraded
            if chunk_results:
                effective_accelerator = ACCELERATOR_CPU

        if chunk_results:
            result = _merge_chunk_results(chunk_results)
            logger.info(
                "Docling safe: %s chunked OK pages=%d (skipped %d pages)",
                file_path, result.get("page_count", 0), len(all_skipped),
            )
        else:
            logger.error(
                "Docling safe: %s completely failed — no chunks processed",
                file_path,
            )
            return None

    # ---- Build diagnostic annotations ----
    degraded_modes: list[str] = []
    if page_degradation_needed:
        degraded_modes.append("page_degraded")
    chosen_deg = _DEG_NO_OCR if page_degradation_needed else _DEG_NONE
    if effective_accelerator == ACCELERATOR_CPU and accelerator == ACCELERATOR_CUDA:
        degraded_modes.append("cpu_fallback")
    if used_chunking:
        degraded_modes.append("chunked")

    skipped_pages = all_skipped if used_chunking else []

    # ---- Store result (DB or sidecar) ----
    plain_text = result.get("plain_text", "")
    page_count = result.get("page_count", 0)
    conversion_status = result.get("conversion_status", "success")
    conversion_confidence = result.get("conversion_confidence")
    doc_json = result.get("document", {})
    tables = result.get("tables", [])

    if skipped_pages and conversion_status == "success":
        conversion_status = "partial_success"

    json_path: Optional[Path] = None
    text_path: Optional[Path] = None
    tables_path: Optional[Path] = None

    if conn is not None:
        try:
            from duke_rates.db.artifact_cache import save_docling_artifact
            save_docling_artifact(
                conn,
                discovery_record_id=discovery_record_id,
                source_pdf=file_path,
                file_hash=file_hash or None,
                backend_version=DOCLING_BACKEND_VERSION,
                accelerator=effective_accelerator,
                pipeline=pipeline,
                status=conversion_status,
                json_sidecar_path=None,
                text_sidecar_path=None,
                tables_sidecar_path=None,
                doc_json_content=json.dumps(doc_json),
                plain_text_content=plain_text,
                tables_json_content=json.dumps(tables),
                page_count=page_count,
                conversion_confidence=conversion_confidence,
                table_count=len(tables),
                metadata={
                    "degraded_modes": degraded_modes,
                    "skipped_pages": skipped_pages,
                    "used_chunking": used_chunking,
                },
            )
            conn.commit()
        except Exception as exc:
            logger.warning("Docling safe: DB save failed for %s: %s", file_path, exc)
            conn = None  # fall through to sidecar

    if conn is None:
        try:
            json_path, text_path, tables_path = _write_docling_artifacts(
                file_path,
                file_hash=file_hash,
                accelerator=effective_accelerator,
                pipeline=pipeline,
                doc_json=doc_json,
                plain_text=plain_text,
                tables=tables,
                page_count=page_count,
                conversion_status=conversion_status,
                conversion_confidence=conversion_confidence,
            )
        except Exception as exc:
            logger.warning("Docling safe: sidecar write failed for %s: %s", file_path, exc)
            return None

    logger.info(
        "Docling safe: completed %s pages=%d accel=%s status=%s "
        "tables=%d degraded=%s skipped=%d",
        file_path, page_count, effective_accelerator, conversion_status,
        len(tables), ",".join(degraded_modes) or "none", len(skipped_pages),
    )

    return {
        "file_hash": file_hash,
        "backend": DOCLING_PACKAGE,
        "backend_version": DOCLING_BACKEND_VERSION,
        "accelerator": effective_accelerator,
        "pipeline": pipeline,
        "page_count": page_count,
        "conversion_status": conversion_status,
        "conversion_confidence": conversion_confidence,
        "document": doc_json,
        "plain_text": plain_text,
        "tables": tables,
        "json_path": str(json_path) if json_path else None,
        "plain_text_path": str(text_path) if text_path else None,
        "tables_path": str(tables_path) if tables_path else None,
        "_degraded_modes": degraded_modes,
        "_skipped_pages": skipped_pages,
        "_chunked": used_chunking,
    }
