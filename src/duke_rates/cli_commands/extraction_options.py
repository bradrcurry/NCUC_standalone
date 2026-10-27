"""Shared CLI options for commands that run the bulk extractor (Phase 2C).

Compatibility note: before Phase 2C these commands replaced ``tariff_charges``
in place. They now store immutable candidate runs and leave ``tariff_charges``
alone. ``--legacy-replace`` (or ``DUKE_RATES_LEGACY_REPLACE=1``) restores the
old effect, but through ``provenance.promote``, so it is logged and can be
undone with ``duke-rates provenance rollback``.
"""

from __future__ import annotations

import typer

LEGACY_REPLACE_HELP = (
    "LEGACY: promote each extraction over tariff_charges immediately, without review "
    "(logged; undo with `provenance rollback`). Default: store candidate runs only."
)


def legacy_replace_option() -> bool:
    return typer.Option(False, "--legacy-replace", help=LEGACY_REPLACE_HELP)


def announce_persistence(legacy_replace: bool | None) -> bool:
    """Print what the run will do to tariff_charges; returns the effective setting."""
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import legacy_replace_requested

    replace = legacy_replace_requested(legacy_replace or None)
    if replace:
        typer.secho(
            "!!! LEGACY REPLACE: extractions will be PROMOTED over tariff_charges without review. "
            "Each promotion is logged; undo with `duke-rates provenance rollback`. !!!",
            fg=typer.colors.RED,
            bold=True,
            err=True,
        )
    else:
        typer.echo(
            "Extractions are stored as candidate runs; tariff_charges is not changed. "
            "Review with `duke-rates provenance candidates` / `compare`, then `promote`.",
            err=True,
        )
    return replace


def result_line(result) -> str:
    """One-line summary of a ``ProcessResult``."""
    parts = [f"extracted={result.extracted}", f"run={result.run_id}"]
    if result.validation_status:
        parts.append(f"validation={result.validation_status}")
    if result.needs_review:
        parts.append("NEEDS_REVIEW")
    if result.persistence == "legacy_replace":
        parts.append(f"inserted={result.inserted}")
    if result.error:
        parts.append(f"error={result.error[:80]}")
    return " ".join(parts)
