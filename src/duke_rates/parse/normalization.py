from __future__ import annotations

import re
from datetime import datetime

from duke_rates.models.rate_schedule import RateScheduleData
from duke_rates.utils.duke_company import normalize_duke_company
from duke_rates.utils.text import slugify

COMPANY_RE = re.compile(
    (
        r"\b(duke energy carolinas|duke energy progress|duke energy florida|"
        r"duke energy ohio|duke energy indiana|duke energy kentucky)\b"
    ),
    re.I,
)


def normalize_company(
    title: str,
    text: str,
    *,
    fallback: str | None = None,
    state: str | None = None,
) -> str | None:
    probe = f"{title} {text[:2000]}"
    duke_candidate = normalize_duke_company(probe, fallback=fallback, state=state)
    if duke_candidate:
        return duke_candidate

    match = COMPANY_RE.search(probe)
    candidate = match.group(1).replace("duke energy ", "").lower() if match else fallback
    return candidate


def build_tariff_id(
    state: str | None, company: str | None, schedule_code: str | None, title: str
) -> str:
    parts = [
        part for part in (state, company, schedule_code, slugify(title, max_length=40)) if part
    ]
    return "_".join(parts).lower()


def parse_effective_date(value: str | None):
    if not value:
        return None
    for fmt in ("%B %d, %Y", "%B %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def enrich_schedule(schedule: RateScheduleData) -> RateScheduleData:
    if not schedule.tariff_id:
        schedule.tariff_id = build_tariff_id(
            schedule.state,
            schedule.company,
            schedule.schedule_code,
            schedule.schedule_title,
        )
    return schedule
