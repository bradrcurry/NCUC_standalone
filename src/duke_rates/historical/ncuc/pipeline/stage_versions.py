from __future__ import annotations


OCR_BACKEND_VERSION = "pytesseract_cpu_v1"
OCR_NORMALIZATION_VERSION = "ocr_normalization_v2"
PAGE_ARTIFACT_VERSION = "page_miner_v6"
SPAN_ARTIFACT_VERSION = "segmentation_v8"
HISTORICAL_BULK_PARSER_VERSION = "historical_bulk_v2"
DOCLING_BACKEND_VERSION = "docling_v2"  # heron layout + TableFormer ACCURATE + VLM route
DOCLING_PAGE_MINER_VERSION = "docling_page_miner_v1"  # Docling JSON → PageEvidence reconstruction


def current_stage_versions() -> dict[str, str]:
    return {
        "ocr_backend_version": OCR_BACKEND_VERSION,
        "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
        "page_artifact_version": PAGE_ARTIFACT_VERSION,
        "span_artifact_version": SPAN_ARTIFACT_VERSION,
        "parser_version": HISTORICAL_BULK_PARSER_VERSION,
        "docling_backend_version": DOCLING_BACKEND_VERSION,
        "docling_page_miner_version": DOCLING_PAGE_MINER_VERSION,
    }
