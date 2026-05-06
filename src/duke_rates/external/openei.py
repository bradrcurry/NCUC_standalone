from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from duke_rates.utils.retry import retry_call

OPENEI_UTILITY_RATES_URL = "https://api.openei.org/utility_rates"


class OpenEIRateReference(BaseModel):
    label: str
    name: str | None = None
    utility: str | None = None
    uri: str | None = None
    sector: str | None = None
    source_url: str | None = None
    source_parent_uri: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    approved: bool | None = None
    supercedes: str | None = None
    description: str | None = None


class OpenEIClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout: float = 30.0,
        user_agent: str = "duke-rates/0.1",
        max_retries: int = 3,
        rate_limit_seconds: float = 0.5,
    ):
        self.api_key = api_key
        self.max_retries = max_retries
        self.rate_limit_seconds = rate_limit_seconds
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self.client.close()

    def lookup_rates(
        self,
        *,
        utility: str | None = None,
        state: str | None = None,
        search_text: str | None = None,
        label: str | None = None,
        limit: int = 25,
    ) -> list[OpenEIRateReference]:
        if not utility and not label:
            raise ValueError("Provide utility or label.")
        payload = retry_call(
            lambda: self.client.get(
                OPENEI_UTILITY_RATES_URL,
                params=_build_lookup_params(
                    api_key=self.api_key,
                    utility=utility,
                    label=label,
                    limit=limit,
                ),
            ),
            retries=max(self.max_retries - 1, 0),
            delay_seconds=self.rate_limit_seconds,
            retry_on=(httpx.HTTPError,),
        )
        payload.raise_for_status()
        data = payload.json()
        items = data.get("items", [])

        normalized_state = state.upper() if state else None
        normalized_search = search_text.lower() if search_text else None
        results: list[OpenEIRateReference] = []
        for item in items:
            if normalized_state and not _matches_state(item, normalized_state):
                continue
            if normalized_search and not _matches_search(item, normalized_search):
                continue
            source_url = _string_or_none(item.get("source") or item.get("sourcepage"))
            results.append(
                OpenEIRateReference(
                    label=_string_or_none(item.get("label")) or "",
                    name=_string_or_none(item.get("name")),
                    utility=_string_or_none(item.get("utility")),
                    uri=_string_or_none(item.get("uri")),
                    sector=_string_or_none(item.get("sector")),
                    source_url=source_url,
                    source_parent_uri=_string_or_none(item.get("sourceparent")),
                    start_date=_timestamp_to_date(item.get("startdate")),
                    end_date=_timestamp_to_date(item.get("enddate")),
                    approved=_bool_or_none(item.get("approved")),
                    supercedes=_string_or_none(item.get("supercedes")),
                    description=_string_or_none(item.get("description")),
                )
            )
        return results

    def lookup_rate_by_url(self, url: str) -> list[OpenEIRateReference]:
        label = _extract_label_from_url(url)
        if not label:
            raise ValueError("Could not extract an OpenEI rate label from the URL.")
        return self.lookup_rates(label=label, limit=1)


def _matches_search(item: dict, search_text: str) -> bool:
    fields = _search_fields(item)
    haystack = " ".join(fields).lower()
    return search_text in haystack


def _matches_state(item: dict, state: str) -> bool:
    haystack = " ".join(_search_fields(item)).upper()
    return state in haystack


def _search_fields(item: dict) -> list[str]:
    fields = [
        _string_or_none(item.get("name")) or "",
        _string_or_none(item.get("label")) or "",
        _string_or_none(item.get("utility")) or "",
        _string_or_none(item.get("description")) or "",
        _string_or_none(item.get("source")) or "",
        _string_or_none(item.get("sourcepage")) or "",
        _string_or_none(item.get("uri")) or "",
        _string_or_none(item.get("sourceparent")) or "",
    ]
    return fields


def _timestamp_to_date(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _string_or_none(value) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _bool_or_none(value) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def _build_lookup_params(
    *,
    api_key: str,
    utility: str | None,
    label: str | None,
    limit: int,
) -> dict[str, object]:
    params: dict[str, object] = {
        "version": "8",
        "format": "json",
        "api_key": api_key,
        "detail": "full",
        "limit": limit,
    }
    if label:
        params["getpage"] = label
    elif utility:
        params["ratesforutility"] = utility
    return params


def _extract_label_from_url(url: str) -> str | None:
    marker = "/rate/view/"
    if marker not in url:
        return None
    suffix = url.split(marker, maxsplit=1)[1]
    label = suffix.split("?", maxsplit=1)[0].split("/", maxsplit=1)[0].strip()
    return label or None
