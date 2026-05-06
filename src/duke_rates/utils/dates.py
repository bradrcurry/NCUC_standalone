from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_utc_now() -> str:
    return utc_now().isoformat()
