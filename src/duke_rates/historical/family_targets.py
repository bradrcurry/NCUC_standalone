from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from duke_rates.db.repository import Repository
from duke_rates.historical.bill_relevant_gaps import ProgressNCBillRelevantGapService


@dataclass(frozen=True)
class ProgressNCFamilyTarget:
    family_key: str
    current_document_id: int
    title: str
    category: str
    family_type: str
    leaf_no: str | None
    code: str | None
    current_url: str
    current_path: str
    current_filename: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    effective_start: str | None = None
    applicable_schedules: tuple[str, ...] = field(default_factory=tuple)


def build_progress_nc_family_targets(
    repository: Repository,
    *,
    missing_only: bool = False,
    state: str = "NC",
    company: str = "progress",
) -> dict[str, ProgressNCFamilyTarget]:
    documents = {doc.id: doc for doc in repository.list_documents(state=state, company=company)}
    gaps = ProgressNCBillRelevantGapService(repository, state=state, company=company).build_records()
    targets: dict[str, ProgressNCFamilyTarget] = {}
    for gap in gaps:
        if missing_only and "missing_historical_leaf" not in gap.gap_flags:
            continue
        document = documents.get(gap.current_document_id)
        if not document:
            continue
        parsed = repository.latest_parse_result(document.id)
        effective_start = None
        aliases: list[str] = [gap.title]
        inferred_code = gap.primary_code
        if parsed and parsed.schedule:
            effective_start = (
                parsed.schedule.effective_start.isoformat()
                if parsed.schedule.effective_start
                else None
            )
            if parsed.schedule.schedule_title:
                aliases.append(parsed.schedule.schedule_title)
            inferred_code = inferred_code or parsed.schedule.schedule_code
        if parsed and parsed.rider:
            effective_start = parsed.rider.effective_date or effective_start
            aliases.append(parsed.rider.title)
            inferred_code = inferred_code or parsed.rider.code
        filename = Path(urlparse(document.document_url).path).name
        inferred_code = inferred_code or _code_from_filename(filename)
        if gap.primary_code:
            aliases.append(gap.primary_code)
        targets[gap.leaf_no] = ProgressNCFamilyTarget(
            family_key=_family_key(document.document_url),
            current_document_id=document.id,
            title=gap.title,
            category=gap.category,
            family_type=_family_type(gap.category, gap.primary_code),
            leaf_no=gap.leaf_no,
            code=inferred_code,
            current_url=document.document_url,
            current_path=urlparse(document.document_url).path,
            current_filename=Path(urlparse(document.document_url).path).name,
            aliases=tuple(dict.fromkeys(alias for alias in aliases if alias)),
            effective_start=effective_start,
            applicable_schedules=tuple(gap.current_applicable_schedules),
        )
    return targets


def find_target_by_query(
    repository: Repository,
    query: str,
    *,
    missing_only: bool = False,
    state: str = "NC",
    company: str = "progress",
) -> ProgressNCFamilyTarget | None:
    normalized = query.strip().lower()
    for target in build_progress_nc_family_targets(
        repository, missing_only=missing_only, state=state, company=company
    ).values():
        haystacks = {
            target.family_key.lower(),
            (target.leaf_no or "").lower(),
            (target.code or "").lower(),
            target.title.lower(),
        }
        haystacks.update(alias.lower() for alias in target.aliases)
        if normalized in haystacks:
            return target
    return None


def _family_key(document_url: str) -> str:
    return urlparse(document_url).path.lower()


def _family_type(category: str, primary_code: str | None) -> str:
    if category == "rate":
        return "base_schedule" if primary_code in {"RES", "SLS", "SLR"} else "optional_service"
    return "rider_adjustment"


def _code_from_filename(filename: str) -> str | None:
    upper = filename.upper()
    for pattern in (r"SCHEDULE-([A-Z0-9-]+)\.PDF", r"RIDER-([A-Z0-9-]+)\.PDF"):
        match = re.search(pattern, upper)
        if match:
            return match.group(1)
    return None
