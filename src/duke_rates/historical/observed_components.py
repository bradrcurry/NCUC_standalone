from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from duke_rates.models.bill_observation import BillComponentObservation
from duke_rates.models.observed_component_history import ObservedComponentHistoryEntry


@dataclass(frozen=True)
class _NormalizedObservation:
    bill_id: int
    component_key: str
    component_label: str
    rate_code: str | None
    unit: str
    value: float
    start_date: date
    end_date: date
    confidence: float


class ProgressNCObservedComponentHistoryService:
    def __init__(self, observations: list[BillComponentObservation]):
        self.observations = observations

    def build_series(
        self,
        *,
        component_key: str | None = None,
        rate_code: str | None = None,
    ) -> list[ObservedComponentHistoryEntry]:
        normalized = self._prepare(component_key=component_key, rate_code=rate_code)
        grouped: dict[tuple[str, str | None, str, float], list[_NormalizedObservation]] = {}
        for item in normalized:
            key = (item.component_key, item.rate_code, item.unit, item.value)
            grouped.setdefault(key, []).append(item)

        entries: list[ObservedComponentHistoryEntry] = []
        for (component, rate, unit, value), items in grouped.items():
            items.sort(key=lambda row: (row.start_date, row.end_date, row.bill_id))
            streak: list[_NormalizedObservation] = []
            for item in items:
                if not streak or _extends_streak(streak[-1], item):
                    streak.append(item)
                    continue
                entries.append(_streak_to_entry(component, rate, unit, value, streak))
                streak = [item]
            if streak:
                entries.append(_streak_to_entry(component, rate, unit, value, streak))

        entries.sort(
            key=lambda row: (
                row.component_key,
                row.rate_code or "",
                row.start_date,
                row.normalized_value,
            )
        )
        return entries

    def select_entry(
        self,
        *,
        component_key: str,
        rate_code: str | None,
        target_start: date,
        target_end: date,
        exclude_bill_id: int | None = None,
        max_gap_days: int = 45,
    ) -> ObservedComponentHistoryEntry | None:
        observations = self.observations
        if exclude_bill_id is not None:
            observations = [item for item in observations if item.bill_id != exclude_bill_id]
        entries = ProgressNCObservedComponentHistoryService(observations).build_series(
            component_key=component_key,
            rate_code=rate_code,
        )
        if not entries:
            return None
        ranked = sorted(
            (
                (entry, _entry_score(entry, target_start=target_start, target_end=target_end))
                for entry in entries
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        best_entry, score = ranked[0]
        if score[0] == 0 and score[2] > max_gap_days:
            return None
        return best_entry

    def _prepare(
        self,
        *,
        component_key: str | None = None,
        rate_code: str | None = None,
    ) -> list[_NormalizedObservation]:
        filtered = [
            item
            for item in self.observations
            if item.section_name == "Electric"
            and item.inferred_value is not None
            and (component_key is None or item.component_key == component_key)
            and (rate_code is None or (item.rate_code or "").upper() == rate_code.upper())
        ]

        detailed_keys = {
            (item.bill_id, item.component_key)
            for item in filtered
            if _is_split_period(item)
        }

        normalized: list[_NormalizedObservation] = []
        for item in filtered:
            if (item.bill_id, item.component_key) in detailed_keys and not _is_split_period(item):
                continue
            start_date = item.period_start or item.service_start
            end_date = item.period_end or item.service_end
            if start_date is None or end_date is None:
                continue
            normalized_unit, normalized_value = _normalize_unit_and_value(item)
            if normalized_unit is None or normalized_value is None:
                continue
            normalized.append(
                _NormalizedObservation(
                    bill_id=item.bill_id,
                    component_key=item.component_key,
                    component_label=item.component_label,
                    rate_code=item.rate_code,
                    unit=normalized_unit,
                    value=normalized_value,
                    start_date=start_date,
                    end_date=end_date,
                    confidence=item.confidence,
                )
            )
        return normalized


def _normalize_unit_and_value(
    observation: BillComponentObservation,
) -> tuple[str | None, float | None]:
    unit = observation.inferred_unit
    value = observation.inferred_value
    if unit is None or value is None:
        return None, None
    if unit == "dollars_per_kwh" and observation.component_key != "energy_charge":
        return "cents_per_kwh", round(value * 100.0, 3)
    return unit, round(value, 3)


def _is_split_period(observation: BillComponentObservation) -> bool:
    return (
        observation.period_start is not None
        and observation.period_end is not None
        and (
            observation.service_start != observation.period_start
            or observation.service_end != observation.period_end
        )
    )


def _extends_streak(previous: _NormalizedObservation, current: _NormalizedObservation) -> bool:
    if previous.end_date > current.start_date:
        return True
    gap_days = (current.start_date - previous.end_date).days
    return gap_days <= 45


def _streak_to_entry(
    component_key: str,
    rate_code: str | None,
    unit: str,
    value: float,
    streak: list[_NormalizedObservation],
) -> ObservedComponentHistoryEntry:
    labels: list[str] = []
    for item in streak:
        if item.component_label not in labels:
            labels.append(item.component_label)
    notes: list[str] = []
    if len(streak) == 1:
        notes.append("Single observed billing period.")
    if any(item.start_date != item.end_date for item in streak):
        notes.append("Derived from bill-observed component values.")
    return ObservedComponentHistoryEntry(
        component_key=component_key,
        rate_code=rate_code,
        component_label=labels[0],
        normalized_unit=unit,
        normalized_value=value,
        start_date=min(item.start_date for item in streak),
        end_date=max(item.end_date for item in streak),
        sample_count=len(streak),
        bill_ids=[item.bill_id for item in streak],
        source_labels=labels,
        min_confidence=min(item.confidence for item in streak),
        max_confidence=max(item.confidence for item in streak),
        notes=notes,
    )


def _entry_score(
    entry: ObservedComponentHistoryEntry,
    *,
    target_start: date,
    target_end: date,
) -> tuple[int, int, int, int, float, int]:
    overlap_start = max(entry.start_date, target_start)
    overlap_end = min(entry.end_date, target_end)
    overlap_days = (
        (overlap_end - overlap_start).days + 1 if overlap_start <= overlap_end else 0
    )
    covers = int(entry.start_date <= target_start and entry.end_date >= target_end)
    gap_days = 0
    if overlap_days == 0:
        if entry.end_date < target_start:
            gap_days = (target_start - entry.end_date).days
        elif entry.start_date > target_end:
            gap_days = (entry.start_date - target_end).days
    return (
        covers,
        overlap_days,
        -gap_days,
        entry.sample_count,
        entry.max_confidence,
        entry.end_date.toordinal(),
    )
