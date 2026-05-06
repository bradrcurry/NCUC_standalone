from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from duke_rates.db.repository import Repository
from duke_rates.historical.family_targets import (
    ProgressNCFamilyTarget,
    build_progress_nc_family_targets,
)
from duke_rates.models.family_crosswalk import HistoricalFamilyCrosswalkRecord
from duke_rates.models.parse_result import DocumentParseResult


class ProgressNCFamilyCrosswalkService:
    def __init__(self, repository: Repository, *, state: str = "NC", company: str = "progress"):
        self.repository = repository
        self.state = state
        self.company = company

    def preview(self) -> list[HistoricalFamilyCrosswalkRecord]:
        targets = build_progress_nc_family_targets(self.repository, missing_only=False)
        target_keys = {target.family_key for target in targets.values()}
        matches: list[HistoricalFamilyCrosswalkRecord] = []
        for historical in self.repository.list_historical_documents(state=self.state, company=self.company):
            if historical.family_key in target_keys:
                continue
            if not _is_legacy_utility_family_key(historical.family_key):
                continue
            match = self._match_record(historical, targets)
            if match:
                matches.append(match)
        matches.sort(key=lambda item: (-item.confidence, item.historical_id))
        return matches

    def apply(self) -> list[HistoricalFamilyCrosswalkRecord]:
        matches = self.preview()
        for match in matches:
            self.repository.update_historical_document_family(
                match.historical_id,
                family_key=match.new_family_key,
                current_document_id=match.current_document_id,
                title=match.target_title,
            )
        return matches

    def _match_record(
        self,
        historical,
        targets: dict[str, ProgressNCFamilyTarget],
    ) -> HistoricalFamilyCrosswalkRecord | None:
        parsed = (
            DocumentParseResult.model_validate_json(historical.parsed_result_json)
            if historical.parsed_result_json
            else None
        )
        matched_code = _normalize_code(_extract_record_code(historical, parsed))
        title_norm = _normalize_title(historical.title)
        filename = (
            Path(urlparse(historical.canonical_url).path).name
            or Path(historical.family_key).name
        )
        filename_code = _normalize_code(_extract_code_from_filename(filename))

        best_target: ProgressNCFamilyTarget | None = None
        best_basis = ""
        best_confidence = 0.0

        for target in targets.values():
            target_code = _normalize_code(target.code)
            basis = None
            confidence = 0.0

            if historical.leaf_no and target.leaf_no and historical.leaf_no == target.leaf_no:
                basis = "leaf_no"
                confidence = 100.0
            elif matched_code and target_code and matched_code == target_code:
                basis = "parsed_code"
                confidence = 95.0
            elif filename_code and target_code and filename_code == target_code:
                basis = "filename_code"
                confidence = 90.0
            elif any(_normalize_title(alias) == title_norm for alias in target.aliases if alias):
                basis = "title_alias"
                confidence = 85.0
            elif any(alias and _normalize_title(alias) in title_norm for alias in target.aliases):
                basis = "title_contains_alias"
                confidence = 70.0

            if confidence > best_confidence:
                best_target = target
                best_basis = basis or ""
                best_confidence = confidence

        if not best_target:
            return None

        return HistoricalFamilyCrosswalkRecord(
            historical_id=historical.id or 0,
            old_family_key=historical.family_key,
            new_family_key=best_target.family_key,
            current_document_id=best_target.current_document_id,
            historical_title=historical.title,
            target_title=best_target.title,
            target_leaf_no=best_target.leaf_no,
            target_code=best_target.code,
            matched_code=matched_code or filename_code,
            basis=best_basis,
            confidence=best_confidence,
        )


def _extract_record_code(historical, parsed: DocumentParseResult | None) -> str | None:
    if parsed:
        if parsed.schedule and parsed.schedule.schedule_code:
            return parsed.schedule.schedule_code
        if parsed.rider and parsed.rider.code:
            return parsed.rider.code
    if historical.leaf_no:
        return historical.leaf_no
    return _extract_code_from_filename(Path(urlparse(historical.canonical_url).path).name)


def _extract_code_from_filename(filename: str) -> str | None:
    upper = filename.upper()
    patterns = [
        r"SCHEDULE[-_]?([A-Z]+(?:-[A-Z]+)?)",
        r"NCSCHEDULE([A-Z]+(?:-[A-Z]+)?)",
        r"RIDER[-_]?([A-Z0-9]+(?:-[A-Z0-9]+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, upper)
        if match:
            return match.group(1)
    return None


def _normalize_code(code: str | None) -> str | None:
    if not code:
        return None
    normalized = code.upper().replace("_", "-").strip("- ")
    normalized = re.sub(r"-(\d+[A-Z]*)$", "", normalized)
    normalized = normalized.replace("R-TOUE", "R-TOU")
    return normalized


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def _is_legacy_utility_family_key(family_key: str) -> bool:
    lowered = family_key.lower()
    return lowered.startswith("/pdfs/") or lowered.startswith("/aboutenergy/")
