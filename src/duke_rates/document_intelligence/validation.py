from __future__ import annotations

import re

from duke_rates.document_intelligence.models import (
    DocumentRepresentation,
    DocumentSchemaType,
    ExtractionResult,
    ValidationMessage,
    ValidationReport,
)


class ExtractionValidationEngine:
    """Source-aware validation for schema-mapped extraction results."""

    def validate(
        self,
        representation: DocumentRepresentation,
        extraction: ExtractionResult,
    ) -> ValidationReport:
        text = representation.raw_text.lower()
        checks_run: list[str] = []
        errors: list[ValidationMessage] = []
        warnings: list[ValidationMessage] = []

        leaf_no = str(extraction.data.get("leaf_no") or "").strip()
        if leaf_no:
            checks_run.append("leaf_no_present_in_source")
            if leaf_no not in text:
                warnings.append(
                    ValidationMessage(
                        code="leaf_no_not_found",
                        message=f"Leaf number {leaf_no} not found in normalized source text.",
                        severity="warning",
                        source_pages=[page.page_number for page in representation.pages],
                    )
                )

        effective_start = str(extraction.data.get("effective_start") or "").strip()
        if effective_start:
            checks_run.append("effective_date_present")
            year = effective_start[:4]
            if year and year not in text:
                warnings.append(
                    ValidationMessage(
                        code="effective_date_not_evident",
                        message=f"Effective start year {year} not found in source text.",
                        severity="warning",
                        source_pages=[page.page_number for page in representation.pages],
                    )
                )

        if extraction.schema_type in {DocumentSchemaType.TARIFF_SHEET, DocumentSchemaType.RIDER}:
            checks_run.append("family_key_present")
            if not extraction.data.get("family_key"):
                errors.append(
                    ValidationMessage(
                        code="family_key_missing",
                        message="Structured extraction is missing family_key.",
                        severity="error",
                    )
                )

        if extraction.schema_type == DocumentSchemaType.TARIFF_SHEET:
            checks_run.append("schedule_code_present")
            schedule_code = extraction.data.get("schedule_code")
            if not schedule_code:
                warnings.append(
                    ValidationMessage(
                        code="schedule_code_missing",
                        message="Tariff-sheet extraction did not resolve a schedule code.",
                        severity="warning",
                    )
                )
            elif not re.search(rf"schedule\s+{re.escape(str(schedule_code).lower())}\b", text):
                warnings.append(
                    ValidationMessage(
                        code="schedule_code_not_evident",
                        message=f"Schedule code {schedule_code} not found in source text.",
                        severity="warning",
                    )
                )

        if extraction.schema_type == DocumentSchemaType.RIDER:
            checks_run.append("rider_code_present")
            rider_code = extraction.data.get("rider_code")
            if not rider_code:
                warnings.append(
                    ValidationMessage(
                        code="rider_code_missing",
                        message="Rider extraction did not resolve a rider code.",
                        severity="warning",
                    )
                )

        passed = not errors
        return ValidationReport(
            passed=passed,
            errors=errors,
            warnings=warnings,
            checks_run=checks_run,
        )
