"""Provenance sub-app: immutable extraction runs, review, promotion, audit.

Wired into the main CLI as `duke-rates provenance <command>`.

Every command takes an explicit ``--db``. Commands that write refuse the
configured production database unless ``--allow-production`` is passed, so
the migration and promotions are rehearsed on scratch copies first.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import typer

from duke_rates.config import get_settings
from duke_rates.db import provenance as pv
from duke_rates.db.provenance_schema import (
    drop_provenance_schema,
    ensure_provenance_schema,
    provenance_schema_installed,
    provenance_usage,
)
from duke_rates.db.sqlite import connect as connect_sqlite

provenance_app = typer.Typer(
    help="Immutable extraction runs: migrate, backfill, compare, promote, roll back, audit."
)

_DB_OPTION = typer.Option(..., "--db", help="Database to operate on (use a scratch copy).")
_ALLOW_PROD = typer.Option(
    False, "--allow-production", help="Permit writes to the configured production database."
)


def _open(db: Path, *, write: bool, allow_production: bool = False) -> sqlite3.Connection:
    if not db.exists():
        raise typer.BadParameter(f"{db} does not exist")
    production = get_settings().database_path.resolve()
    if write and db.resolve() == production and not allow_production:
        raise typer.BadParameter(
            f"{db} is the production database; rehearse on a scratch copy or pass --allow-production"
        )
    return connect_sqlite(db)


def _echo(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str, ensure_ascii=False))


@provenance_app.command("migrate")
def migrate_cmd(db: Path = _DB_OPTION, allow_production: bool = _ALLOW_PROD) -> None:
    """Install the provenance tables and audit triggers (additive, idempotent)."""
    conn = _open(db, write=True, allow_production=allow_production)
    ensure_provenance_schema(conn)
    _echo({"installed": provenance_schema_installed(conn)})


@provenance_app.command("drop-schema")
def drop_schema_cmd(
    db: Path = _DB_OPTION,
    allow_production: bool = _ALLOW_PROD,
    force: bool = typer.Option(False, help="Also discard non-legacy runs and audit history."),
) -> None:
    """Roll back the migration (legacy tables are never touched)."""
    conn = _open(db, write=True, allow_production=allow_production)
    _echo({"before": provenance_usage(conn)})
    drop_provenance_schema(conn, force=force)
    _echo({"installed": provenance_schema_installed(conn)})


@provenance_app.command("backfill")
def backfill_cmd(
    db: Path = _DB_OPTION,
    allow_production: bool = _ALLOW_PROD,
    by: str = typer.Option(..., help="Who is running the backfill."),
) -> None:
    """Record live legacy data as active legacy runs, then verify byte-for-byte."""
    conn = _open(db, write=True, allow_production=allow_production)
    stats = pv.backfill_legacy(conn, created_by=by)
    _echo({"backfill": stats, "verify": pv.verify_materialization(conn)})


@provenance_app.command("verify")
def verify_cmd(db: Path = _DB_OPTION) -> None:
    """Check the read model still matches every active run."""
    conn = _open(db, write=False)
    report = pv.verify_materialization(conn)
    _echo(report)
    raise typer.Exit(code=0 if report["ok"] else 1)


@provenance_app.command("extract-candidate")
def extract_candidate_cmd(
    historical_document_id: int = typer.Option(..., "--historical-document-id"),
    db: Path = _DB_OPTION,
    allow_production: bool = _ALLOW_PROD,
    by: str = typer.Option(..., help="Who requested the run."),
    version_id: int | None = typer.Option(None, "--version-id"),
) -> None:
    """Run the bulk extractor into a sealed candidate run (tariff_charges untouched)."""
    from duke_rates.historical.ncuc.pipeline.candidate_runs import extract_candidate

    _open(db, write=True, allow_production=allow_production).close()
    _echo(extract_candidate(db, historical_document_id, created_by=by, version_id=version_id))


@provenance_app.command("compare")
def compare_cmd(run_id: int = typer.Option(..., "--run-id"), db: Path = _DB_OPTION) -> None:
    """Diff a run against what billing reads today."""
    _echo(pv.compare_run(_open(db, write=False), run_id))


@provenance_app.command("promote")
def promote_cmd(
    run_id: int = typer.Option(..., "--run-id"),
    db: Path = _DB_OPTION,
    allow_production: bool = _ALLOW_PROD,
    by: str = typer.Option(..., help="Reviewer approving the promotion."),
    reason: str = typer.Option(..., help="Why; reference the reviewed diff."),
    expected_active_run_id: int | None = typer.Option(
        None, help="Refuse unless this run is currently active (omit for none)."
    ),
    supersede: list[int] = typer.Option(
        [], help="Protected tariff_charges id this replaces (repeatable)."
    ),
    allow_empty: bool = typer.Option(False),
    dry_run: bool = typer.Option(False, help="Show the diff without changing anything."),
) -> None:
    """Make a reviewed candidate run the active result."""
    conn = _open(db, write=not dry_run, allow_production=allow_production)
    _echo(
        pv.promote(
            conn,
            run_id,
            promoted_by=by,
            reason=reason,
            expected_active_run_id=expected_active_run_id,
            supersede_protected_ids=supersede,
            allow_empty=allow_empty,
            dry_run=dry_run,
        )
    )


@provenance_app.command("rollback")
def rollback_cmd(
    target_kind: str = typer.Option(
        ..., help="tariff_version | historical_document | bill_statement"
    ),
    target_id: int = typer.Option(...),
    db: Path = _DB_OPTION,
    allow_production: bool = _ALLOW_PROD,
    by: str = typer.Option(...),
    reason: str = typer.Option(...),
) -> None:
    """Undo the latest promotion of a target exactly."""
    conn = _open(db, write=True, allow_production=allow_production)
    _echo(pv.rollback(conn, target_kind, target_id, by=by, reason=reason))


@provenance_app.command("reject")
def reject_cmd(
    run_id: int = typer.Option(..., "--run-id"),
    db: Path = _DB_OPTION,
    allow_production: bool = _ALLOW_PROD,
    by: str = typer.Option(...),
    reason: str = typer.Option(...),
) -> None:
    """Record that a candidate run was reviewed and not promoted."""
    conn = _open(db, write=True, allow_production=allow_production)
    _echo({"promotion_log_id": pv.reject_run(conn, run_id, by=by, reason=reason)})


@provenance_app.command("history")
def history_cmd(
    table: str = typer.Option(..., help="historical_documents | tariff_versions | tariff_families"),
    row_id: int = typer.Option(...),
    db: Path = _DB_OPTION,
) -> None:
    """Metadata revisions for one row (and parsed-payload history if it has one)."""
    conn = _open(db, write=False)
    out = {"metadata": pv.metadata_history(conn, table, row_id)}
    if table == "historical_documents":
        out["parsed_result_json"] = pv.payload_history(conn, "historical_document", row_id)
    _echo(out)


@provenance_app.command("completeness")
def completeness_cmd(db: Path = _DB_OPTION) -> None:
    """Share of extracted charges with each provenance field available."""
    _echo(pv.provenance_completeness(_open(db, write=False)))


@provenance_app.command("unsupported-charges")
def unsupported_charges_cmd(
    db: Path = typer.Option(None, "--db", help="Defaults to the configured database (read-only)."),
    out: Path | None = typer.Option(None, help="Also write the full JSON report here."),
) -> None:
    """Classify every stored charge_type the billing engine does not price."""
    from duke_rates.billing.charge_types import ENGINE_PRICED_TYPES, classify_charge
    from duke_rates.parse.canonical import parse_unit

    path = (db or get_settings().database_path).resolve()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT tc.id, tc.version_id, tc.family_key, tc.charge_type, tc.charge_label, tc.rate_value, "
        "tc.rate_unit, tc.tou_period, tc.customer_class, tv.effective_start "
        "FROM tariff_charges tc LEFT JOIN tariff_versions tv ON tv.id = tc.version_id"
    ).fetchall()
    report = []
    unrecognized_units = []
    for r in rows:
        if r["charge_type"] in ENGINE_PRICED_TYPES:
            if not parse_unit(r["rate_unit"]).recognized:
                unrecognized_units.append(dict(r))
            continue
        c = classify_charge(
            r["charge_type"],
            tou_period=r["tou_period"],
            rate_unit=r["rate_unit"],
            charge_label=r["charge_label"],
        )
        report.append(
            {
                **dict(r),
                "category": c.category,
                "suggested_type": c.suggested_type,
                "reason": c.reason,
            }
        )
    by_category = Counter(item["category"] for item in report)
    by_type = Counter((item["category"], item["charge_type"]) for item in report)
    summary = {
        "total_unpriced": len(report),
        "by_category": dict(by_category),
        "by_type": {f"{cat} / {ctype}": n for (cat, ctype), n in sorted(by_type.items())},
        "families": len({item["family_key"] for item in report}),
        "priced_charges_with_unrecognized_unit": len(unrecognized_units),
    }
    _echo(summary)
    if out:
        out.write_text(
            json.dumps(
                {"summary": summary, "charges": report, "priced_unrecognized_units": unrecognized_units},
                indent=2,
                default=str,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
