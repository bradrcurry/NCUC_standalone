from __future__ import annotations

import re

from duke_rates.document_intelligence.models import (
    DocumentFingerprintResult,
    DocumentRepresentation,
    DocumentType,
    ParseLane,
)
from duke_rates.document_intelligence.text_quality import analyze_text_quality


class HybridDocumentFingerprinter:
    """Heuristic-first fingerprinting that can later be extended with LLM classifiers."""

    def fingerprint(self, representation: DocumentRepresentation) -> DocumentFingerprintResult:
        text = representation.raw_text.lower()
        title = (representation.title or "").lower()
        family_key = (representation.family_key or "").lower()
        features: list[str] = []
        quality = analyze_text_quality(representation.raw_text)

        if "leaf no." in text:
            features.append("leaf_marker")
        if "schedule " in text:
            features.append("schedule_marker")
        if "rider " in text:
            features.append("rider_marker")
        if "summary of rider adjustments" in text:
            features.append("rider_summary")
        if "order dated" in text or "commission" in text:
            features.append("commission_signal")
        if "testimony" in text:
            features.append("testimony_signal")
        if "certificate of service" in text:
            features.append("service_certificate")
        if re.search(r"\b(on-peak|off-peak|time of use|demand charge)\b", text):
            features.append("structured_rate_terms")
        if re.search(r"\bper\s+kwh\b|\$/kwh|¢/kwh", text):
            features.append("rate_unit_signal")
        for code in quality.redline_codes:
            features.append(code)
        for code in quality.suspicious_codes:
            features.append(code)

        doc_type = DocumentType.UNKNOWN
        subtype: str | None = None
        confidence = 0.35
        lane = ParseLane.HYBRID

        if quality.redline_hit_count > 0 or "redline" in title:
            doc_type = DocumentType.REDLINE
            subtype = "tracked_tariff_revision"
            confidence = 0.84
            lane = ParseLane.LLM_ASSISTED
        elif "summary of rider adjustments" in text:
            doc_type = DocumentType.SUMMARY
            subtype = "rider_adjustment_summary"
            confidence = 0.92
            lane = ParseLane.DETERMINISTIC
        elif (
            (family_key.startswith("nc-") and "rider" in family_key)
            or ("rider " in title)
            or ("rider " in text and "leaf_marker" in features)
        ):
            doc_type = DocumentType.RIDER
            subtype = "tariff_rider"
            confidence = 0.86 if "rate_unit_signal" in features else 0.74
            lane = ParseLane.DETERMINISTIC if "rate_unit_signal" in features else ParseLane.HYBRID
        elif (
            (family_key.startswith("nc-") and "schedule" in family_key)
            or ("schedule " in title)
            or ("schedule " in text and "leaf_marker" in features)
        ):
            doc_type = DocumentType.TARIFF_SHEET
            subtype = "rate_schedule"
            confidence = 0.88 if "structured_rate_terms" in features else 0.76
            lane = ParseLane.DETERMINISTIC if "structured_rate_terms" in features else ParseLane.HYBRID
        elif "order dated" in text or "commission order" in title:
            doc_type = DocumentType.COMMISSION_ORDER
            subtype = "regulatory_order"
            confidence = 0.8
            lane = ParseLane.SKIP
        elif "testimony" in text or "prepared direct testimony" in title:
            doc_type = DocumentType.TESTIMONY
            subtype = "regulatory_testimony"
            confidence = 0.78
            lane = ParseLane.SKIP
        elif "letter" in title or "dear ms." in text:
            doc_type = DocumentType.CORRESPONDENCE
            subtype = "filing_letter"
            confidence = 0.72
            lane = ParseLane.SKIP
        elif "program" in family_key or "program" in title:
            doc_type = DocumentType.PROGRAM
            subtype = "program_tariff_or_program_sheet"
            confidence = 0.65
            lane = ParseLane.HYBRID

        if "ocr" in (representation.document_metadata.get("artifact_source") or "").lower():
            features.append("ocr_source")
            lane = ParseLane.LLM_ASSISTED if confidence < 0.75 else lane

        return DocumentFingerprintResult(
            doc_type=doc_type,
            subtype=subtype,
            confidence=confidence,
            parse_lane=lane,
            features_detected=features,
            metadata={
                "family_key": representation.family_key,
                "title": representation.title,
                "page_count": len(representation.pages),
            },
        )
