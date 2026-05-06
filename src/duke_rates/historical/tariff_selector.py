from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from duke_rates.db.repository import Repository
from duke_rates.historical.lineage import ProgressNCLineageService
from duke_rates.historical.notice_links import ProgressNCNoticeLinkService
from duke_rates.models.history_chain import HistoryVersion
from duke_rates.models.notice_link import NoticeLinkRecord
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.parse.normalization import parse_effective_date


class HistoricalRiderSelection(BaseModel):
    code: str
    title: str | None = None
    status: Literal["dated", "undated"] = "undated"
    version: HistoryVersion
    parse_result: DocumentParseResult | None = None


class HistoricalTariffSelection(BaseModel):
    requested_service_date: date
    version: HistoryVersion
    parse_result: DocumentParseResult
    schedule: RateScheduleData
    riders: list[HistoricalRiderSelection] = Field(default_factory=list)
    supporting_notices: list[NoticeLinkRecord] = Field(default_factory=list)
    unresolved_rider_codes: list[str] = Field(default_factory=list)
    future_rider_codes: list[str] = Field(default_factory=list)


class ProgressNCHistoricalTariffSelector:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.lineage = ProgressNCLineageService(repository)
        self.notice_links = ProgressNCNoticeLinkService(repository)

    def select_schedule(
        self,
        *,
        schedule_code: str,
        service_date: date,
    ) -> HistoricalTariffSelection:
        normalized_code = schedule_code.strip().upper()
        candidates: list[tuple[HistoryVersion, DocumentParseResult]] = []

        for chain in self.lineage.build_chains(recovered_only=False):
            for version in chain.versions:
                if not _schedule_code_matches(
                    requested_code=normalized_code,
                    candidate_code=(version.schedule_code or "").upper(),
                ):
                    continue
                parse_result = self._load_parse_result(version)
                if not parse_result or not parse_result.schedule:
                    continue
                candidates.append((version, parse_result))

        if not candidates:
            raise ValueError(
                "No Progress NC parsed schedule matched "
                f"schedule_code={normalized_code}"
            )

        version, parse_result = max(
            candidates,
            key=lambda item: _selection_score(item[0], service_date),
        )
        schedule = _merge_schedule_metadata(parse_result.schedule, version)
        riders, unresolved_rider_codes, future_rider_codes = self._select_riders(
            schedule, service_date
        )
        supporting_notices = self._supporting_notices(version, riders)
        return HistoricalTariffSelection(
            requested_service_date=service_date,
            version=version,
            parse_result=parse_result,
            schedule=schedule,
            riders=riders,
            supporting_notices=supporting_notices,
            unresolved_rider_codes=unresolved_rider_codes,
            future_rider_codes=future_rider_codes,
        )

    def _load_parse_result(self, version: HistoryVersion) -> DocumentParseResult | None:
        if version.source_kind == "current":
            return self.repository.latest_parse_result(version.document_id)
        historical = self.repository.get_historical_document(version.document_id)
        if not historical or not historical.parsed_result_json:
            return None
        return DocumentParseResult.model_validate_json(historical.parsed_result_json)

    def _select_riders(
        self,
        schedule: RateScheduleData,
        service_date: date,
    ) -> tuple[list[HistoricalRiderSelection], list[str], list[str]]:
        rider_references = [
            ((ref.code or "").upper(), ref.title) for ref in schedule.riders if ref.code
        ]
        if not rider_references:
            return ([], [], [])

        rider_chains = [
            chain
            for chain in self.lineage.build_chains(recovered_only=False)
            if chain.category == "rider"
        ]
        selections: list[HistoricalRiderSelection] = []
        unresolved: list[str] = []
        future_only: list[str] = []
        seen_codes: set[str] = set()

        for code, title in rider_references:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            candidates: list[tuple[HistoryVersion, DocumentParseResult | None]] = []
            for chain in rider_chains:
                for version in chain.versions:
                    parse_result = self._load_parse_result(version)
                    if code not in _combined_rider_codes(version, parse_result):
                        continue
                    candidates.append((version, parse_result))
            if not candidates:
                unresolved.append(code)
                continue
            dated_candidates = [
                item for item in candidates if _rider_version_is_not_future(item[0], service_date)
            ]
            if dated_candidates:
                version, rider_parse = max(
                    dated_candidates,
                    key=lambda item: _selection_score(item[0], service_date),
                )
                status = "dated"
            else:
                undated_candidates = [
                    item
                    for item in candidates
                    if parse_effective_date(item[0].effective_start) is None
                ]
                if undated_candidates:
                    version, rider_parse = max(
                        undated_candidates,
                        key=lambda item: _selection_score(item[0], service_date),
                    )
                    status = "undated"
                else:
                    future_only.append(code)
                    continue
            selections.append(
                HistoricalRiderSelection(
                    code=code,
                    title=title,
                    status=status,
                    version=version,
                    parse_result=rider_parse,
                )
            )

        selections.sort(key=lambda item: (item.code, item.version.family_key))
        unresolved.sort()
        future_only.sort()
        return (selections, unresolved, future_only)

    def _supporting_notices(
        self,
        schedule_version: HistoryVersion,
        riders: list[HistoricalRiderSelection],
    ) -> list[NoticeLinkRecord]:
        relevant_family_keys = {schedule_version.family_key}
        relevant_family_keys.update(rider.version.family_key for rider in riders)

        filtered: list[NoticeLinkRecord] = []
        for notice in self.notice_links.build_links():
            matches = [
                match for match in notice.matches if match.family_key in relevant_family_keys
            ]
            if not matches:
                continue
            filtered.append(notice.model_copy(update={"matches": matches}))
        filtered.sort(key=lambda item: (item.title.lower(), item.historical_id))
        return filtered


def _selection_score(
    version: HistoryVersion,
    service_date: date,
) -> tuple[int, int, int, int, int]:
    start = parse_effective_date(version.effective_start)
    end = parse_effective_date(version.effective_end)

    if _covers_service_date(start, end, service_date):
        return (
            3,
            1 if end else 0,
            (start or date.min).toordinal(),
            1 if version.source_kind == "current" else 0,
            version.document_id,
        )

    if start and start <= service_date:
        return (
            2,
            0,
            start.toordinal(),
            1 if version.source_kind == "current" else 0,
            version.document_id,
        )

    if start:
        return (
            1,
            0,
            -abs((start - service_date).days),
            1 if version.source_kind == "current" else 0,
            version.document_id,
        )

    return (
        0,
        0,
        date.min,
        1 if version.source_kind == "current" else 0,
        version.document_id,
    )


def _covers_service_date(start: date | None, end: date | None, service_date: date) -> bool:
    if start and service_date < start:
        return False
    if end and service_date > end:
        return False
    return bool(start or end)


def _schedule_code_matches(*, requested_code: str, candidate_code: str) -> bool:
    if not candidate_code:
        return False
    requested = _normalize_schedule_code(requested_code)
    candidate = _normalize_schedule_code(candidate_code)
    return (
        requested == candidate
        or candidate.startswith(f"{requested}-")
        or requested.startswith(f"{candidate}-")
    )


def _normalize_schedule_code(value: str) -> str:
    return "".join(value.upper().split())


def _merge_schedule_metadata(
    schedule: RateScheduleData,
    version: HistoryVersion,
) -> RateScheduleData:
    return schedule.model_copy(
        update={
            "state": schedule.state or version.state,
            "company": schedule.company or version.company,
            "schedule_code": schedule.schedule_code or version.schedule_code,
            "effective_start": schedule.effective_start
            or parse_effective_date(version.effective_start),
            "effective_end": schedule.effective_end or parse_effective_date(version.effective_end),
        }
    )


def _version_rider_codes(version: HistoryVersion) -> set[str]:
    codes: set[str] = set()
    if version.rider_id:
        codes.add(version.rider_id.upper())
    family_key = version.family_key.lower()
    if "rider-" in family_key:
        token = family_key.split("rider-", maxsplit=1)[1].split(".", maxsplit=1)[0]
        normalized = token.replace("-ry1", "").replace("-ry", "").upper()
        if normalized:
            codes.add(normalized)
    title_tokens = (version.title or "").upper().split()
    if title_tokens:
        codes.add(title_tokens[-1].strip("(),"))
    return {code for code in codes if code}


def _combined_rider_codes(
    version: HistoryVersion,
    parse_result: DocumentParseResult | None,
) -> set[str]:
    codes = _version_rider_codes(version)
    rider = parse_result.rider if parse_result else None
    if rider and rider.code:
        codes.add(rider.code.upper())
    if rider and rider.version_code:
        codes.add(rider.version_code.upper())
        codes.add(rider.version_code.split("-", maxsplit=1)[0].upper())
    return {code for code in codes if code}


def _rider_version_is_not_future(version: HistoryVersion, service_date: date) -> bool:
    start = parse_effective_date(version.effective_start)
    return bool(start and start <= service_date)
