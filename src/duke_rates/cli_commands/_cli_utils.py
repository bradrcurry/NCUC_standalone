"""Shared CLI helper utilities used by sub-app modules.

These were originally defined inline in cli.py. They live here so sub-app modules
in cli_commands/ can import them without circular dependencies.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import typer

from duke_rates.billing.calculators import UsageInput
from duke_rates.billing.usage_io import read_usage_file
from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.logging_config import configure_logging


def _safe_cli_text(value: object) -> str:
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _format_optional_pct(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "-"


def _bootstrap():
    settings = get_settings()
    configure_logging(settings.log_level)
    return settings, Repository(settings.database_path)


def _read_usage_file(path: Path) -> UsageInput:
    try:
        return read_usage_file(path)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _count_rows(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[object, ...] = (),
) -> int:
    row = conn.execute(query, params).fetchone()
    if not row:
        return 0
    return int(row[0])
