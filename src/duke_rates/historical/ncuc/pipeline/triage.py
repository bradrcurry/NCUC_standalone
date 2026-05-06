import hashlib
import re
from pathlib import Path
from typing import Optional, Dict

from duke_rates.models.pipeline import DocumentTriage, PipelineRoute

# Vocabulary heuristics for triage
TARIFF_VOCAB = ["AVAILABILITY", "RATE", "RIDER", "SERVICE RENDERED", "per kWh", "per kW", "Customer Charge"]
PROCEDURAL_VOCAB = ["motion", "testimony", "affidavit", "brief", "commission", "certificate of service", "order granting"]
LEAF_PHRASES = [r"Leaf[ \-]No\.? \d+", r"Original Leaf", r"Revised Leaf", r"NCUC No\.? \d+"]


def _estimate_table_like_density(text: str) -> float:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    table_like = sum(
        1 for line in lines
        if re.search(r"\s{3,}", line) or re.search(r"[\$¢]\s*\d|\d+\.\d{2,}\s*(?:/|per)", line, re.I)
    )
    return table_like / len(lines)


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _estimate_reading_order_risk(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0

    long_lines = sum(1 for line in lines if len(line) >= 110)
    heading_phrase_count = sum(
        len(re.findall(r"\b(APPLICABILITY|AVAILABILITY|RATE|MONTHLY|CUSTOMER CHARGE|TYPE OF SERVICE|SERVICE RENDERED)\b", line, re.I))
        for line in lines
    )
    merged_heading_lines = sum(
        1
        for line in lines
        if len(line) >= 55
        and re.search(r"\b(APPLICABILITY|AVAILABILITY|RATE|MONTHLY|CUSTOMER CHARGE)\b", line, re.I)
        and re.search(r"[A-Z]{4,}.*\b(APPLICABILITY|AVAILABILITY|RATE|MONTHLY|CUSTOMER CHARGE)\b", line)
    )
    low_space_lines = sum(1 for line in lines if len(line) >= 40 and line.count(" ") <= 3)
    signal = (
        min(long_lines / len(lines), 1.0) * 0.45
        + min(merged_heading_lines / len(lines), 1.0) * 0.4
        + min(low_space_lines / len(lines), 1.0) * 0.15
        + min(heading_phrase_count / max(len(lines) * 2, 1), 1.0) * 0.25
    )
    return _clamp_score(signal)


def _infer_table_mode(*, is_scanned: bool, table_density: float, tariff_hits: int, page_count: int) -> str | None:
    if table_density >= 0.28:
        return "scanned_table" if is_scanned else "native_table"
    if is_scanned and (page_count >= 8 or tariff_hits >= 2):
        return "scanned_text"
    if tariff_hits >= 2:
        return "native_text"
    return None


def _infer_document_archetype(
    *,
    is_scanned: bool,
    tariff_hits: int,
    procedural_hits: int,
    page_count: int,
    has_leaf_phrases: bool,
    table_density: float,
) -> str:
    if is_scanned and page_count >= 8:
        return "scanned_bundle"
    if tariff_hits > 2 or has_leaf_phrases:
        if page_count > 5:
            return "compliance_bundle"
        return "tariff_sheet"
    if procedural_hits > tariff_hits:
        return "procedural"
    if page_count > 20:
        return "large_unknown"
    return "unknown"


def _compute_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
    except Exception:
        return ""
    return hasher.hexdigest()

def triage_pdf(file_path: str) -> DocumentTriage:
    """
    Perform a fast first-pass triage on a PDF file to determine parsing route.
    Prefers PyMuPDF (fitz) for speed but falls back to pdfplumber/PyPDF2 if needed.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Cannot triage missing file: {file_path}")

    triage = DocumentTriage(
        file_path=str(file_path),
        file_hash=_compute_hash(file_path),
        file_size_bytes=path.stat().st_size
    )

    try:
        import fitz  # PyMuPDF
        triage.native_text_backend = "pymupdf"
        doc = fitz.open(str(path))
        triage.page_count = doc.page_count
        
        # Sample first 3 pages
        first_pages_text = ""
        for i in range(min(3, doc.page_count)):
            first_pages_text += doc[i].get_text("text") + "\n"
            
        triage.text_char_count_first_pages = len(first_pages_text.strip())
        
        # Sample mid and end for large docs
        sampled_text = first_pages_text
        if doc.page_count > 10:
            mid_idx = doc.page_count // 2
            end_idx = doc.page_count - 1
            sampled_text += doc[mid_idx].get_text("text") + "\n"
            sampled_text += doc[end_idx].get_text("text") + "\n"
            
        triage.text_char_count_sampled = len(sampled_text.strip())
        doc.close()
        
    except ImportError:
        # Fallback to pdfplumber if PyMuPDF not available
        import pdfplumber
        triage.native_text_backend = "pdfplumber"
        with pdfplumber.open(str(path)) as pdf:
            triage.page_count = len(pdf.pages)
            first_pages_text = ""
            for i in range(min(3, triage.page_count)):
                page_text = pdf.pages[i].extract_text()
                if page_text:
                    first_pages_text += page_text + "\n"
            
            triage.text_char_count_first_pages = len(first_pages_text.strip())
            
            sampled_text = first_pages_text
            if triage.page_count > 10:
                mid_page = pdf.pages[triage.page_count // 2].extract_text() or ""
                end_page = pdf.pages[-1].extract_text() or ""
                sampled_text += mid_page + "\n" + end_page + "\n"
                
            triage.text_char_count_sampled = len(sampled_text.strip())

    # Assess if scanned (very little text for multiple pages)
    sampled_chars_per_page = (
        triage.text_char_count_sampled / max(1, min(triage.page_count, 5))
        if triage.page_count
        else 0.0
    )
    triage.sampled_chars_per_page = round(sampled_chars_per_page, 2)
    sparse_text_ratio = 1.0 - min(sampled_chars_per_page / 400.0, 1.0)
    triage.ocr_confidence_score = _clamp_score(sparse_text_ratio)
    triage.native_text_quality_score = _clamp_score(1.0 - sparse_text_ratio)
    if sampled_chars_per_page < 80:
        triage.triage_flags.append("very_low_text_density")
    elif sampled_chars_per_page < 160:
        triage.triage_flags.append("low_text_density")

    # Score vocabulary
    sampled_upper = sampled_text.upper()
    tariff_hits = sum(1 for w in TARIFF_VOCAB if w.upper() in sampled_upper)
    procedural_hits = sum(1 for w in PROCEDURAL_VOCAB if w.upper() in sampled_upper)
    table_density = _estimate_table_like_density(sampled_text)
    
    triage.keyword_hits["tariff"] = tariff_hits
    triage.keyword_hits["procedural"] = procedural_hits
    triage.keyword_hits["table_like_lines"] = int(table_density * 100)
    triage.keyword_hits["sampled_chars_per_page"] = int(sampled_chars_per_page)

    for pattern in LEAF_PHRASES:
        if re.search(pattern, sampled_text, re.IGNORECASE):
            triage.has_leaf_phrases = True
            break

    complexity = 0.0
    complexity += min(triage.page_count / 40.0, 0.35)
    complexity += min(table_density * 0.45, 0.45)
    if tariff_hits and procedural_hits:
        complexity += 0.15
        triage.triage_flags.append("mixed_tariff_procedural_content")
    if triage.page_count > 25:
        triage.triage_flags.append("large_document")
    if table_density > 0.2:
        triage.triage_flags.append("table_heavy_layout")
    triage.structure_complexity_score = _clamp_score(complexity)
    triage.reading_order_risk_score = _estimate_reading_order_risk(sampled_text)
    if triage.reading_order_risk_score >= 0.45:
        triage.triage_flags.append("reading_order_risk")

    if triage.page_count > 0 and triage.text_char_count_sampled < (triage.page_count * 20):
        triage.is_likely_scanned = True
        triage.route_recommendation = PipelineRoute.OCR_REQUIRED
        triage.triage_flags.append("ocr_required_high_confidence")
        if triage.ocr_confidence_score < 0.8:
            triage.ocr_confidence_score = 0.8
        if (
            triage.structure_complexity_score >= 0.55
            or table_density > 0.25
            or triage.page_count > 25
        ):
            triage.gpu_ocr_candidate = True
            triage.triage_flags.append("gpu_ocr_candidate")
        triage.table_mode_candidate = _infer_table_mode(
            is_scanned=True,
            table_density=table_density,
            tariff_hits=tariff_hits,
            page_count=triage.page_count,
        )
        triage.document_archetype_candidate = _infer_document_archetype(
            is_scanned=True,
            tariff_hits=tariff_hits,
            procedural_hits=procedural_hits,
            page_count=triage.page_count,
            has_leaf_phrases=triage.has_leaf_phrases,
            table_density=table_density,
        )
        triage.confidence_score = round(
            (triage.ocr_confidence_score * 0.45)
            + (triage.structure_complexity_score * 0.2)
            + (0.35 if triage.is_likely_scanned else 0.0),
            4,
        )
        return triage

    # Classification heuristic
    if tariff_hits > 2 or triage.has_leaf_phrases:
        triage.is_likely_tariff_related = True
        if triage.page_count > 5:
            triage.route_recommendation = PipelineRoute.PAGE_SEGMENT
            triage.likely_document_class = "compliance_book"
        else:
            triage.route_recommendation = PipelineRoute.TEXT_PARSE
            triage.likely_document_class = "tariff_sheet"
    elif procedural_hits > tariff_hits:
        triage.likely_document_class = "procedural"
        if procedural_hits > 5 and tariff_hits == 0:
            triage.route_recommendation = PipelineRoute.SKIP_IRRELEVANT
        else:
            triage.route_recommendation = PipelineRoute.MANUAL_REVIEW
    else:
        triage.route_recommendation = PipelineRoute.MANUAL_REVIEW

    if triage.structure_complexity_score >= 0.65:
        triage.triage_flags.append("complex_structure_review")
    if triage.ocr_confidence_score >= 0.6 and triage.structure_complexity_score >= 0.55:
        triage.gpu_ocr_candidate = True
        triage.triage_flags.append("gpu_ocr_candidate")

    triage.table_mode_candidate = _infer_table_mode(
        is_scanned=triage.is_likely_scanned,
        table_density=table_density,
        tariff_hits=tariff_hits,
        page_count=triage.page_count,
    )
    triage.document_archetype_candidate = _infer_document_archetype(
        is_scanned=triage.is_likely_scanned,
        tariff_hits=tariff_hits,
        procedural_hits=procedural_hits,
        page_count=triage.page_count,
        has_leaf_phrases=triage.has_leaf_phrases,
        table_density=table_density,
    )
    triage.confidence_score = round(
        (triage.native_text_quality_score * 0.35)
        + (triage.structure_complexity_score * 0.25)
        + (0.25 if triage.is_likely_tariff_related else 0.0)
        + (0.15 if triage.route_recommendation != PipelineRoute.MANUAL_REVIEW else 0.0),
        4,
    )
    return triage
