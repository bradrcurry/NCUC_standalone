from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlparse

from duke_rates.db.repository import Repository
from duke_rates.models.bill_relevant_gap import BillRelevantGapRecord
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.parse.normalization import parse_effective_date

BILL_RELEVANT_PROGRESS_NC_LEAFS = {
    "500",
    "501",
    "502",
    "503",
    "504",
    "571",
    "572",
    "600",
    "601",
    "602",
    "604",
    "605",
    "607",
    "608",
    "609",
    "610",
    "611",
    "613",
    "640",
    "662",
    "670",
    "672",
}

OBSERVED_COMPONENT_BY_BILL_LABEL = {
    "Clean Energy Rider": "clean_energy_rider",
    "Energy Conservation Credit": "energy_conservation_credit",
    "Storm Recovery Charge": "storm_recovery_charge",
    "Summary of Rider Adjustments": "summary_rider_adjustments",
}


class ProgressNCBillRelevantGapService:
    def __init__(
        self,
        repository: Repository,
        *,
        state: str = "NC",
        company: str = "progress",
    ):
        self.repository = repository
        self.state = state
        self.company = company

    def build_records(self) -> list[BillRelevantGapRecord]:
        historical_by_leaf = self._historical_by_leaf()
        historical_by_code = self._historical_by_code()
        observed_keys = {
            item.component_key
            for item in self.repository.list_bill_component_observations()
            if item.rate_code == "RES"
        }
        records: list[BillRelevantGapRecord] = []
        for document in self.repository.list_documents(state=self.state, company=self.company):
            if document.kind != "pdf":
                continue
            leaf_no = _leaf_from_url(document.document_url)
            if leaf_no not in BILL_RELEVANT_PROGRESS_NC_LEAFS:
                continue

            parse_result = self.repository.latest_parse_result(document.id)
            parsed_component_labels: list[str] = []
            current_applicable_schedules: list[str] = []
            has_parsed_schedule = False
            has_parsed_rider = False
            primary_code: str | None = None
            current_effective_start = None
            if parse_result:
                if parse_result.schedule:
                    has_parsed_schedule = True
                    primary_code = parse_result.schedule.schedule_code
                    current_applicable_schedules = [parse_result.schedule.schedule_code]
                    current_effective_start = parse_result.schedule.effective_start
                if parse_result.rider:
                    has_parsed_rider = True
                    primary_code = parse_result.rider.code
                    current_effective_start = parse_effective_date(
                        parse_result.rider.effective_date
                    )
                    parsed_component_labels = list(
                        dict.fromkeys(
                            component.bill_label
                            for component in parse_result.rider.charge_components
                        )
                    )
                    current_applicable_schedules = list(
                        dict.fromkeys(parse_result.rider.applicable_schedules)
                    )

            historical_entries: list[dict[str, str | None]] = []
            historical_match_modes: list[str] = []
            if leaf_no in historical_by_leaf:
                historical_entries.extend(historical_by_leaf[leaf_no])
                historical_match_modes.append("leaf")
            if primary_code and primary_code in historical_by_code:
                historical_entries.extend(historical_by_code[primary_code])
                historical_match_modes.append("code")
            historical_entries = _dedupe_historical_entries(historical_entries)
            historical_entries = [
                item
                for item in historical_entries
                if _is_predecessor_version(item, current_effective_start)
            ]
            historical_effective_ranges = list(
                dict.fromkeys(
                    _format_effective_range(
                        item["effective_start"],
                        item["effective_end"],
                    )
                    for item in historical_entries
                )
            )
            observed_component_keys = sorted(
                {
                    OBSERVED_COMPONENT_BY_BILL_LABEL[label]
                    for label in parsed_component_labels
                    if OBSERVED_COMPONENT_BY_BILL_LABEL.get(label) in observed_keys
                }
            )
            gap_flags: list[str] = []
            notes: list[str] = []
            if parse_result is None:
                gap_flags.append("missing_current_parse")
            elif parse_result.status.value != "parsed":
                gap_flags.append("partial_current_parse")
            if not historical_entries:
                gap_flags.append("missing_historical_leaf")
            if document.category == "rider" and not parsed_component_labels:
                gap_flags.append("missing_bill_component_extraction")
            if document.category == "rider" and not current_applicable_schedules:
                gap_flags.append("missing_applicable_schedules")
            if observed_component_keys and "missing_historical_leaf" in gap_flags:
                notes.append(
                    "Bill observations exist for this component despite missing leaf history."
                )
            if leaf_no == "600":
                notes.append(
                    "Summary leaf is a current aggregation source, not a standalone rider formula."
                )

            records.append(
                BillRelevantGapRecord(
                    current_document_id=document.id,
                    leaf_no=leaf_no,
                    title=document.title,
                    category=document.category,
                    primary_code=primary_code,
                    parse_status=parse_result.status.value if parse_result else None,
                    has_parsed_schedule=has_parsed_schedule,
                    has_parsed_rider=has_parsed_rider,
                    parsed_component_labels=parsed_component_labels,
                    current_applicable_schedules=current_applicable_schedules,
                    historical_version_count=len(historical_entries),
                    historical_match_modes=historical_match_modes,
                    historical_effective_ranges=historical_effective_ranges,
                    observed_component_keys=observed_component_keys,
                    gap_flags=gap_flags,
                    notes=notes,
                )
            )
        records.sort(
            key=lambda item: (
                "missing_historical_leaf" not in item.gap_flags,
                "missing_current_parse" not in item.gap_flags,
                "partial_current_parse" not in item.gap_flags,
                item.leaf_no,
                item.title.lower(),
            )
        )
        return records

    def _historical_by_leaf(self) -> dict[str, list[dict[str, str | None]]]:
        grouped: dict[str, list[dict[str, str | None]]] = defaultdict(list)
        for item in self.repository.list_historical_documents(state=self.state, company=self.company):
            leaf_no = _normalize_historical_leaf(item.leaf_no)
            if not leaf_no:
                parsed = _leaf_from_family_key(item.family_key)
                leaf_no = parsed
            if not leaf_no:
                continue
            grouped[leaf_no].append(
                {
                    "effective_start": item.effective_start,
                    "effective_end": item.effective_end,
                }
            )
        return grouped

    def _historical_by_code(self) -> dict[str, list[dict[str, str | None]]]:
        grouped: dict[str, list[dict[str, str | None]]] = defaultdict(list)
        for item in self.repository.list_historical_documents(state=self.state, company=self.company):
            code = _historical_primary_code(item.parsed_result_json)
            if not code:
                continue
            grouped[code].append(
                {
                    "effective_start": item.effective_start,
                    "effective_end": item.effective_end,
                }
            )
        return grouped


def _leaf_from_url(url: str) -> str | None:
    path = urlparse(url).path
    match = re.search(r"leaf-no-(\d+)", path, re.I)
    return match.group(1) if match else None


def _leaf_from_family_key(family_key: str) -> str | None:
    match = re.search(r"leaf-no-(\d+)", family_key, re.I)
    return match.group(1) if match else None


def _normalize_historical_leaf(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d+)", value)
    return match.group(1) if match else None


def _format_effective_range(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} -> {end}"
    if start:
        return f"{start} -> current"
    if end:
        return f"unknown -> {end}"
    return "unknown"


def _historical_primary_code(parsed_result_json: str | None) -> str | None:
    if not parsed_result_json:
        return None
    parsed = DocumentParseResult.model_validate_json(parsed_result_json)
    if parsed.schedule and parsed.schedule.schedule_code:
        return parsed.schedule.schedule_code
    if parsed.rider and parsed.rider.code:
        return parsed.rider.code
    return None


def _dedupe_historical_entries(
    entries: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    deduped: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for entry in entries:
        key = (entry.get("effective_start"), entry.get("effective_end"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _is_predecessor_version(
    entry: dict[str, str | None],
    current_effective_start,
) -> bool:
    if current_effective_start is None:
        return True

    entry_start = parse_effective_date(entry.get("effective_start"))
    if entry_start is not None:
        return entry_start < current_effective_start

    entry_end = parse_effective_date(entry.get("effective_end"))
    if entry_end is not None:
        return entry_end < current_effective_start

    return True
