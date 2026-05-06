from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from duke_rates.historical.ncuc.pipeline.page_miner import _extract_page_features_from_text
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
)
from duke_rates.models.pipeline import PageEvidence

logger = logging.getLogger(__name__)

OCR_BACKEND_OCRMYPDF = "ocrmypdf_tesseract"
OCR_BACKEND_PYTESSERACT = "pytesseract_cpu"
OCR_BACKEND_DOCLING = "docling_gpu"
OCR_BACKEND_GLM = "glm_ocr_gpu"
OCR_BACKEND_AUTO = "auto"

# Thresholds for progressive escalation
OCR_MIN_AVG_CHARS_PER_PAGE = 80  # below this, escalate to next backend
OCR_MIN_TEXT_CHARS_TOTAL = 120   # below this total, escalate to next backend


def get_ocr_backend_unavailable_reason(backend: str = OCR_BACKEND_AUTO) -> str | None:
    if backend == OCR_BACKEND_AUTO:
        if get_ocr_backend_unavailable_reason(OCR_BACKEND_OCRMYPDF) is None:
            return None
        return get_ocr_backend_unavailable_reason(OCR_BACKEND_PYTESSERACT)

    if backend == OCR_BACKEND_OCRMYPDF:
        missing: list[str] = []
        try:
            import fitz  # noqa: F401
        except ImportError:
            missing.append("pymupdf")
        if shutil.which("ocrmypdf") is None:
            missing.append("ocrmypdf")
        if missing:
            return (
                "OCR backend ocrmypdf_tesseract unavailable: missing "
                + ", ".join(missing)
                + ". Install OCRmyPDF and ensure the executable is available on PATH."
            )
        return None

    if backend != OCR_BACKEND_PYTESSERACT:
        return f"OCR backend unsupported: {backend}"

    missing: list[str] = []
    try:
        import fitz  # noqa: F401
    except ImportError:
        missing.append("pymupdf")
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        missing.append("pytesseract")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("Pillow")

    if missing:
        return (
            "OCR backend pytesseract_cpu unavailable: missing "
            + ", ".join(missing)
            + ". Install the Python OCR dependencies and ensure the Tesseract "
            + "binary is available on PATH."
        )
    return None


def _compute_file_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _ocr_text_sidecar_path(file_path: str) -> Path:
    path = Path(file_path)
    return path.with_suffix(path.suffix + ".ocr.txt")


def _ocr_pages_sidecar_path(file_path: str) -> Path:
    path = Path(file_path)
    return path.with_suffix(path.suffix + ".ocr_pages.json")


def load_ocr_sidecar_payload(file_path: str) -> dict | None:
    pages_path = _ocr_pages_sidecar_path(file_path)
    if not pages_path.exists():
        return None
    try:
        return json.loads(pages_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed reading OCR sidecar %s: %s", pages_path, exc)
        return None


def _load_cached_ocr_document_pages(
    file_path: str,
    expected_hash: str | None = None,
    expected_backend: str | None = None,
) -> list[PageEvidence]:
    payload = load_ocr_sidecar_payload(file_path)
    if not payload:
        return []
    if expected_hash and payload.get("file_hash") not in {None, expected_hash}:
        return []
    if payload.get("backend_version") not in {None, OCR_BACKEND_VERSION}:
        return []
    if expected_backend and payload.get("backend") not in {None, expected_backend}:
        return []
    return [PageEvidence.model_validate(item) for item in payload.get("pages", [])]


def _write_ocr_artifacts(
    file_path: str,
    *,
    file_hash: str,
    backend: str,
    page_texts: list[str],
    pages: list[PageEvidence],
    metadata: dict | None = None,
) -> tuple[Path, Path]:
    text_path = _ocr_text_sidecar_path(file_path)
    pages_path = _ocr_pages_sidecar_path(file_path)
    text_path.write_text("\n\n".join(page_texts), encoding="utf-8")
    pages_path.write_text(
        json.dumps(
            {
                "file_hash": file_hash,
                "backend": backend,
                "backend_version": OCR_BACKEND_VERSION,
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                "page_count": len(pages),
                "metadata": metadata or {},
                "pages": [page.model_dump(mode="json") for page in pages],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return text_path, pages_path


def summarize_ocr_payload(payload: dict | None) -> dict[str, object]:
    if not payload:
        return {}
    pages = list(payload.get("pages") or [])
    text_lengths = [int(page.get("text_length") or 0) for page in pages if isinstance(page, dict)]
    return {
        "selected_backend": payload.get("backend"),
        "attempted_backends": list(payload.get("metadata", {}).get("attempted_backends") or []),
        "ocr_backend_version": payload.get("backend_version"),
        "ocr_normalization_version": payload.get("ocr_normalization_version"),
        "page_count": int(payload.get("page_count") or len(pages)),
        "avg_text_length": round(sum(text_lengths) / len(text_lengths), 2) if text_lengths else 0.0,
        "max_text_length": max(text_lengths) if text_lengths else 0,
        "min_text_length": min(text_lengths) if text_lengths else 0,
    }


def _extract_pages_with_pytesseract(file_path: str, max_pages: Optional[int] = None) -> tuple[list[str], list[PageEvidence]]:
    import fitz
    import pytesseract
    from PIL import Image

    pages: list[PageEvidence] = []
    page_texts: list[str] = []
    doc = fitz.open(file_path)
    try:
        limit = min(doc.page_count, max_pages) if max_pages else doc.page_count
        for index in range(limit):
            page = doc[index]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            text = pytesseract.image_to_string(image) or ""
            page_texts.append(text)
            pages.append(_extract_page_features_from_text(text, index + 1))
    finally:
        doc.close()
    return page_texts, pages


def _extract_pages_with_ocrmypdf(file_path: str, max_pages: Optional[int] = None) -> tuple[list[str], list[PageEvidence]]:
    import fitz

    source = Path(file_path)
    with tempfile.TemporaryDirectory(prefix="ocrmypdf-") as temp_dir:
        temp_root = Path(temp_dir)
        output_pdf = temp_root / f"{source.stem}.searchable.pdf"
        sidecar_txt = temp_root / f"{source.stem}.sidecar.txt"
        command = [
            "ocrmypdf",
            "--skip-text",
            "--force-ocr",
            "--sidecar",
            str(sidecar_txt),
            str(source),
            str(output_pdf),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown ocrmypdf error"
            raise RuntimeError(stderr)

        pages: list[PageEvidence] = []
        page_texts: list[str] = []
        doc = fitz.open(str(output_pdf))
        try:
            limit = min(doc.page_count, max_pages) if max_pages else doc.page_count
            for index in range(limit):
                text = doc[index].get_text("text") or ""
                page_texts.append(text)
                pages.append(_extract_page_features_from_text(text, index + 1))
        finally:
            doc.close()
    return page_texts, pages


def _backend_candidates(backend: str) -> list[str]:
    if backend == OCR_BACKEND_AUTO:
        return [OCR_BACKEND_OCRMYPDF, OCR_BACKEND_PYTESSERACT]
    return [backend]


def extract_ocr_document_pages(
    file_path: str,
    max_pages: Optional[int] = None,
    *,
    force: bool = False,
    backend: str = OCR_BACKEND_AUTO,
) -> list[PageEvidence]:
    """Run a CPU-first OCR fallback and return mined page evidence.

    This path is intentionally best-effort. It only activates for documents
    already triaged as `OCR_REQUIRED`, and it degrades cleanly when OCR
    dependencies are unavailable.
    """
    file_hash = ""
    try:
        file_hash = _compute_file_hash(file_path)
    except Exception as exc:
        logger.warning("Unable to hash OCR source %s: %s", file_path, exc)

    if not force:
        cached = _load_cached_ocr_document_pages(
            file_path,
            expected_hash=file_hash or None,
            expected_backend=None if backend == OCR_BACKEND_AUTO else backend,
        )
        if cached:
            return cached

    pages: list[PageEvidence] = []
    page_texts: list[str] = []
    attempted_backends: list[str] = []
    selected_backend: str | None = None
    for candidate in _backend_candidates(backend):
        attempted_backends.append(candidate)
        unavailable_reason = get_ocr_backend_unavailable_reason(candidate)
        if unavailable_reason:
            logger.info("%s Source=%s", unavailable_reason, file_path)
            continue
        try:
            if candidate == OCR_BACKEND_OCRMYPDF:
                page_texts, pages = _extract_pages_with_ocrmypdf(file_path, max_pages=max_pages)
            elif candidate == OCR_BACKEND_PYTESSERACT:
                page_texts, pages = _extract_pages_with_pytesseract(file_path, max_pages=max_pages)
            else:
                logger.warning("Unsupported OCR backend candidate %s Source=%s", candidate, file_path)
                continue
        except Exception as exc:
            logger.warning("OCR backend %s failed for %s: %s", candidate, file_path, exc)
            continue
        if pages:
            selected_backend = candidate
            break

    if not pages or not selected_backend:
        logger.warning("No OCR backend succeeded for %s attempted=%s", file_path, attempted_backends)
        return []

    try:
        _write_ocr_artifacts(
            file_path,
            file_hash=file_hash,
            backend=selected_backend,
            page_texts=page_texts,
            pages=pages,
            metadata={
                "selected_backend": selected_backend,
                "attempted_backends": attempted_backends,
                "requested_backend": backend,
                "avg_text_length": round(sum(len(text or "") for text in page_texts) / len(page_texts), 2) if page_texts else 0.0,
            },
        )
    except Exception as exc:
        logger.warning("Failed writing OCR artifacts for %s: %s", file_path, exc)

    return pages


def select_ocr_backend(
    *,
    triage_route: str | None = None,
    gpu_ocr_candidate: bool = False,
    table_density: float = 0.0,
    structure_complexity: float = 0.0,
    document_archetype: str | None = None,
    table_mode: str | None = None,
    page_count: int = 0,
    native_text_chars: int = 0,
) -> list[str]:
    """Return the recommended OCR backend priority order for a document.

    Decision matrix based on document archetypes and known backend strengths:

    | Archetype / Signal         | Priority 1      | Priority 2     | Priority 3     |
    |----------------------------|-----------------|----------------|----------------|
    | scanned_bundle (8+ pages)  | docling_gpu     | glm_ocr_gpu    | pytesseract    |
    | scanned_table              | docling_gpu     | glm_ocr_gpu    | pytesseract    |
    | compliance_bundle (5+ pgs) | docling_gpu     | pytesseract    | glm_ocr_gpu    |
    | tariff_sheet (1-4 pages)   | pytesseract     | docling_gpu    | glm_ocr_gpu    |
    | native_text (good text)    | native_pdf      | —              | —              |
    | table_heavy (density>0.25) | docling_gpu     | glm_ocr_gpu    | pytesseract    |
    | complex_structure (>=0.55) | docling_gpu     | glm_ocr_gpu    | pytesseract    |
    | gpu_ocr_candidate (any)    | docling_gpu     | glm_ocr_gpu    | pytesseract    |
    | default/unknown            | pytesseract     | docling_gpu    | glm_ocr_gpu    |

    Docling GPU: Best for table-heavy structured tariff sheets (TableFormer ACCURATE mode).
    GLM-OCR GPU: Best for degraded scanned text, symbol noise, decorative fonts.
    CPU Tesseract: Fast, reliable for clean scanned single-page tariff sheets.
    Native PDF: Best for modern PDFs with clean embedded text layers.
    """
    # Native text is sufficient — no OCR needed
    if triage_route is not None and triage_route != "ocr_required" and native_text_chars >= 400:
        return ["native_pdf"]

    # GPU candidate signals: complex tables, scanned bundles, or heavy structure
    is_scanned_bundle = document_archetype in ("scanned_bundle",) or page_count >= 8
    is_table_heavy = table_density > 0.25 or table_mode in ("scanned_table", "native_table")
    is_complex = structure_complexity >= 0.55

    if gpu_ocr_candidate or is_scanned_bundle or is_table_heavy or is_complex:
        return [OCR_BACKEND_DOCLING, OCR_BACKEND_GLM, OCR_BACKEND_PYTESSERACT]

    # Tariff sheets (small page count, structured): try CPU first, then GPU
    if page_count <= 4:
        return [OCR_BACKEND_PYTESSERACT, OCR_BACKEND_DOCLING, OCR_BACKEND_GLM]

    # Default: CPU first, escalate to GPU
    return [OCR_BACKEND_PYTESSERACT, OCR_BACKEND_DOCLING, OCR_BACKEND_GLM]


def _ocr_text_quality_ok(pages: list[PageEvidence]) -> bool:
    """Check whether OCR output has sufficient text quality to skip escalation."""
    if not pages:
        return False
    total_chars = sum(len(p.text_content or "") for p in pages)
    avg_chars = total_chars / len(pages)
    return total_chars >= OCR_MIN_TEXT_CHARS_TOTAL and avg_chars >= OCR_MIN_AVG_CHARS_PER_PAGE


def _try_docling_ocr(file_path: str) -> list[PageEvidence] | None:
    """Attempt Docling GPU conversion and return PageEvidence, or None on failure."""
    try:
        from duke_rates.historical.ncuc.pipeline.docling_backend import (
            convert_pdf_with_docling,
            get_docling_unavailable_reason,
        )
        if get_docling_unavailable_reason():
            logger.info("Docling GPU unavailable for progressive OCR: %s", get_docling_unavailable_reason())
            return None
        result = convert_pdf_with_docling(file_path, force=True)
        if not result or not result.get("document"):
            return None
        from duke_rates.historical.ncuc.pipeline.docling_page_miner import mine_pages_from_docling_artifact
        pages, _metadata = mine_pages_from_docling_artifact(result)
        if pages:
            logger.info("Docling GPU OCR succeeded for %s: %d pages, %d avg chars",
                        file_path, len(pages),
                        sum(len(p.text_content or "") for p in pages) // max(len(pages), 1))
        return pages or None
    except Exception as exc:
        logger.warning("Docling GPU OCR failed for %s: %s", file_path, exc)
        return None


def _try_glm_ocr(file_path: str) -> list[PageEvidence] | None:
    """Attempt GLM-OCR (Ollama) conversion and return PageEvidence, or None on failure."""
    try:
        import fitz
        import base64
        import io
        import httpx
        from PIL import Image

        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        # Quick check that Ollama is reachable
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{ollama_host.rstrip('/')}/api/tags")
                resp.raise_for_status()
        except Exception:
            logger.info("GLM-OCR (Ollama) unreachable for progressive OCR")
            return None

        doc = fitz.open(file_path)
        pages: list[PageEvidence] = []
        try:
            for idx in range(min(doc.page_count, 6)):  # limit to first 6 pages for GLM
                page = doc[idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                encoded = base64.b64encode(buf.getvalue()).decode("ascii")

                payload = {
                    "model": "glm-ocr",
                    "prompt": "Perform OCR on this page and return the readable text in natural reading order. Preserve table rows and headings.",
                    "images": [encoded],
                    "stream": False,
                }
                with httpx.Client(timeout=90) as client:
                    response = client.post(
                        f"{ollama_host.rstrip('/')}/api/generate",
                        json=payload,
                    )
                    response.raise_for_status()
                    text = str(response.json().get("response") or "").strip()
                pages.append(_extract_page_features_from_text(text, idx + 1))
        finally:
            doc.close()

        if pages:
            logger.info("GLM-OCR GPU succeeded for %s: %d pages, %d avg chars",
                        file_path, len(pages),
                        sum(len(p.text_content or "") for p in pages) // max(len(pages), 1))
        return pages or None
    except Exception as exc:
        logger.warning("GLM-OCR GPU failed for %s: %s", file_path, exc)
        return None


def extract_pages_with_progressive_ocr(
    file_path: str,
    max_pages: Optional[int] = None,
    *,
    prefer_gpu: bool = False,
    table_density: float = 0.0,
    structure_complexity: float = 0.0,
    document_archetype: str | None = None,
    table_mode: str | None = None,
    page_count: int = 0,
) -> tuple[list[PageEvidence], str, list[str]]:
    """Extract pages with progressive OCR escalation.

    Uses select_ocr_backend() decision matrix for backend priority ordering.
    When prefer_gpu is True, skips CPU quality check and escalates to GPU immediately.

    Returns (pages, selected_backend, attempted_backends).
    """
    backend_priority = select_ocr_backend(
        triage_route="ocr_required",
        gpu_ocr_candidate=prefer_gpu,
        table_density=table_density,
        structure_complexity=structure_complexity,
        document_archetype=document_archetype,
        table_mode=table_mode,
        page_count=page_count,
    )
    attempted: list[str] = []
    best_pages: list[PageEvidence] = []

    # Map backend names to extraction functions
    _BACKEND_EXTRACTORS = {
        OCR_BACKEND_PYTESSERACT: lambda: extract_ocr_document_pages(file_path, max_pages=max_pages, force=False),
        OCR_BACKEND_OCRMYPDF: lambda: extract_ocr_document_pages(file_path, max_pages=max_pages, force=False, backend=OCR_BACKEND_OCRMYPDF),
        OCR_BACKEND_DOCLING: lambda: _try_docling_ocr(file_path) or [],
        OCR_BACKEND_GLM: lambda: _try_glm_ocr(file_path) or [],
    }

    for backend in backend_priority:
        if backend == "native_pdf":
            continue  # native PDF path is handled by caller
        if backend not in _BACKEND_EXTRACTORS:
            continue
        attempted.append(backend)
        try:
            pages = _BACKEND_EXTRACTORS[backend]()
        except Exception as exc:
            logger.warning("Backend %s raised for %s: %s", backend, file_path, exc)
            continue
        if not pages:
            continue
        if not best_pages:
            best_pages = pages
        if _ocr_text_quality_ok(pages):
            return pages, backend, attempted

    if best_pages:
        return best_pages, attempted[-1] if attempted else "", attempted

    logger.warning("All OCR backends failed for %s: attempted=%s", file_path, attempted)
    return [], "", attempted
