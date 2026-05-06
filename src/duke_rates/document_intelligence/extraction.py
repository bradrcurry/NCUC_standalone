from __future__ import annotations

import re
from typing import Any

from duke_rates.document_intelligence.models import (
    CommissionOrderSchema,
    DocumentFingerprintResult,
    DocumentRepresentation,
    DocumentSchemaType,
    DocumentType,
    ExtractionResult,
    RiderSchema,
    TariffSheetSchema,
    TestimonySchema,
)


class SchemaExtractionAdapter:
    """Map the existing deterministic pipeline outputs into structured schema results."""

    def build_extraction_result(
        self,
        representation: DocumentRepresentation,
        fingerprint: DocumentFingerprintResult,
        *,
        parser_profile: str | None,
        charge_count: int,
        status: str,
    ) -> ExtractionResult:
        data: dict[str, Any]
        schema_type = DocumentSchemaType.UNKNOWN
        confidence = min(0.95, fingerprint.confidence)

        if fingerprint.doc_type == DocumentType.TARIFF_SHEET:
            schedule_code = self._derive_schedule_code(representation.family_key, representation.raw_text)
            data = TariffSheetSchema(
                family_key=representation.family_key,
                title=representation.title,
                schedule_code=schedule_code,
                leaf_no=representation.document_metadata.get("leaf_no"),
                effective_start=representation.document_metadata.get("effective_start"),
                revision_label=representation.document_metadata.get("revision_label"),
                supersedes_label=representation.document_metadata.get("supersedes_label"),
                charge_count=charge_count,
                parser_profile=parser_profile,
            ).model_dump(mode="json")
            schema_type = DocumentSchemaType.TARIFF_SHEET
            confidence += 0.03 if charge_count else -0.1
        elif fingerprint.doc_type == DocumentType.RIDER:
            rider_code = self._derive_rider_code(representation.family_key, representation.raw_text)
            data = RiderSchema(
                family_key=representation.family_key,
                title=representation.title,
                rider_code=rider_code,
                leaf_no=representation.document_metadata.get("leaf_no"),
                effective_start=representation.document_metadata.get("effective_start"),
                charge_count=charge_count,
                parser_profile=parser_profile,
            ).model_dump(mode="json")
            schema_type = DocumentSchemaType.RIDER
            confidence += 0.03 if charge_count else -0.1
        elif fingerprint.doc_type == DocumentType.COMMISSION_ORDER:
            data = CommissionOrderSchema(
                title=representation.title,
                docket_number=representation.document_metadata.get("docket_number"),
                order_date=representation.document_metadata.get("order_date"),
            ).model_dump(mode="json")
            schema_type = DocumentSchemaType.COMMISSION_ORDER
        elif fingerprint.doc_type == DocumentType.TESTIMONY:
            data = TestimonySchema(
                title=representation.title,
                docket_number=representation.document_metadata.get("docket_number"),
                witness_name=self._extract_witness_name(representation.raw_text),
            ).model_dump(mode="json")
            schema_type = DocumentSchemaType.TESTIMONY
        else:
            data = {
                "family_key": representation.family_key,
                "title": representation.title,
                "status": status,
                "charge_count": charge_count,
            }

        return ExtractionResult(
            schema_type=schema_type,
            data=data,
            confidence=max(0.0, min(confidence, 0.98)),
            source_pages=[page.page_number for page in representation.pages],
            validation_passed=False,
            parser_used=parser_profile,
            extraction_mode=fingerprint.parse_lane.value,
            metadata={
                "status": status,
                "doc_type": fingerprint.doc_type.value,
            },
        )

    @staticmethod
    def _derive_schedule_code(family_key: str | None, text: str) -> str | None:
        if family_key and "-schedule-" in family_key.lower():
            return family_key.split("-schedule-", 1)[1].upper()
        match = re.search(r"schedule\s+([a-z0-9\-]+)", text, re.I)
        return match.group(1).upper() if match else None

    @staticmethod
    def _derive_rider_code(family_key: str | None, text: str) -> str | None:
        if family_key and "-rider-" in family_key.lower():
            return family_key.split("-rider-", 1)[1].upper()
        match = re.search(r"rider\s+([a-z0-9\-]+)", text, re.I)
        return match.group(1).upper() if match else None

    @staticmethod
    def _extract_witness_name(text: str) -> str | None:
        match = re.search(r"testimony of\s+([A-Z][A-Za-z .'-]+)", text, re.I)
        return match.group(1).strip() if match else None
