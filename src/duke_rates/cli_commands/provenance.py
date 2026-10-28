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


_MODE_OPTION = typer.Option(
    "hybrid", "--mode", help="legacy | docling | native | hybrid (structured input mode)."
)
_RUN_SUMMARY_KEYS = (
    "run_id",
    "version_id",
    "extractor_status",
    "profile",
    "structured_mode",
    "status",
    "validation_status",
    "needs_review",
    "output_count",
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
    mode: str = _MODE_OPTION,
) -> None:
    """Run the bulk extractor into a sealed candidate run (tariff_charges untouched)."""
    from duke_rates.historical.ncuc.pipeline.candidate_runs import extract_candidate

    _open(db, write=True, allow_production=allow_production).close()
    _echo(
        extract_candidate(
            db, historical_document_id, created_by=by, version_id=version_id, structured_mode=mode
        )
    )


@provenance_app.command("extract-candidates")
def extract_candidates_cmd(
    db: Path = _DB_OPTION,
    allow_production: bool = _ALLOW_PROD,
    by: str = typer.Option(..., help="Who requested the runs."),
    historical_document_id: list[int] = typer.Option(
        [], "--historical-document-id", help="Documents to extract (repeatable); default: all eligible."
    ),
    family_key: str | None = typer.Option(None, "--family-key"),
    limit: int | None = typer.Option(None, help="At most this many documents."),
    mode: str = _MODE_OPTION,
    workers: int = typer.Option(2, min=1, help="Parallel documents."),
    out: Path | None = typer.Option(None, help="Also write the full JSON report here."),
) -> None:
    """Bulk candidate extraction: one sealed run per document; nothing is promoted.

    Classification recorders are disabled, so only the run tables are written.
    """
    from concurrent.futures import ThreadPoolExecutor

    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
    from duke_rates.historical.ncuc.pipeline.candidate_runs import extract_candidate

    conn = _open(db, write=True, allow_production=allow_production)
    if not provenance_schema_installed(conn):
        raise typer.BadParameter(f"{db} has no provenance schema; run `provenance migrate` first")
    conn.close()
    ids = list(historical_document_id) or [
        int(d["id"])
        for d in BulkExtractor(str(db)).get_documents_needing_extraction(family_key=family_key)
    ]
    ids = ids[:limit] if limit is not None else ids

    def one(hd_id: int) -> dict:
        try:
            r = extract_candidate(db, hd_id, created_by=by, structured_mode=mode)
            return {"historical_document_id": hd_id, **{k: r[k] for k in _RUN_SUMMARY_KEYS if k in r}}
        except Exception as exc:  # reported per document; the batch continues
            return {"historical_document_id": hd_id, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, ids))
    report = {
        "documents": len(rows),
        "runs": sum(1 for r in rows if "run_id" in r),
        "errors": sum(1 for r in rows if "error" in r),
        "needs_review": sum(1 for r in rows if r.get("needs_review")),
        "validation": dict(Counter(r.get("validation_status", "error") for r in rows)),
        "structured_mode": mode,
        "promoted": 0,
    }
    _echo({**report, "results": rows if len(rows) <= 50 else rows[:50]})
    if out:
        out.write_text(json.dumps({**report, "results": rows}, indent=2, default=str), encoding="utf-8")


@provenance_app.command("candidates")
def candidates_cmd(
    db: Path = _DB_OPTION,
    version_id: int | None = typer.Option(None, "--version-id"),
    family_key: str | None = typer.Option(None, "--family-key"),
    target_kind: str | None = typer.Option(None, "--target-kind"),
    review: str | None = typer.Option(
        None, help="candidate | active | previously_active | rejected"
    ),
    needs_review: bool = typer.Option(False, "--needs-review", help="Only runs with review findings."),
    limit: int = typer.Option(50),
) -> None:
    """List extraction runs with their review state and validation summary."""
    conn = _open(db, write=False)
    where, params = ["sealed_at IS NOT NULL"], []
    for column, value in (("target_id", version_id), ("family_key", family_key), ("target_kind", target_kind)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    if version_id is not None and target_kind is None:
        where.append("target_kind = 'tariff_version'")
    rows = conn.execute(
        f"SELECT * FROM extraction_runs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        [*params, limit * 5 if (review or needs_review) else limit],
    ).fetchall()
    out = []
    for run in rows:
        state = pv.run_review_status(conn, run["id"])
        flagged = pv.review_findings(conn, run["id"])
        if (review and state != review) or (needs_review and not flagged):
            continue
        config = json.loads(run["config_json"] or "{}")
        out.append(
            {
                "run_id": run["id"],
                "target": f"{run['target_kind']}:{run['target_id']}",
                "family_key": run["family_key"],
                "source_document_id": run["source_document_id"],
                "parser": run["parser_name"],
                "structured_mode": config.get("structured_mode"),
                "persistence": config.get("persistence"),
                "status": run["status"],
                "validation_status": run["validation_status"],
                "review_state": state,
                "review_findings": len(flagged),
                "output_count": json.loads(run["metrics_json"] or "{}").get("charge_count"),
                "created_by": run["created_by"],
                "started_at": run["started_at"],
            }
        )
        if len(out) >= limit:
            break
    _echo(out)


@provenance_app.command("issues")
def issues_cmd(
    run_id: int = typer.Option(..., "--run-id"),
    db: Path = _DB_OPTION,
    level: str | None = typer.Option(None, help="error | warning | info | review"),
) -> None:
    """Validation findings for a run: run-level, then per charge (with the charge label)."""
    conn = _open(db, write=False)
    run = conn.execute("SELECT validation_json, validation_status FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise typer.BadParameter(f"run {run_id} does not exist")
    detail = json.loads(run["validation_json"] or "{}")
    run_issues = (detail.get("summary") or {}).get("run_issues", []) if isinstance(detail, dict) else []
    labels = {
        str(r["id"]): (r["charge_label"], r["rate_value"], r["rate_unit"])
        for r in conn.execute("SELECT id, charge_label, rate_value, rate_unit FROM extracted_charges WHERE run_id = ?", (run_id,))
    }
    per_charge = [
        {"extracted_charge_id": int(cid), "charge": labels.get(cid), **issue}
        for cid, issues in ((detail.get("charges") or {}) if isinstance(detail, dict) else {}).items()
        for issue in issues
    ]

    def keep(issue: dict) -> bool:
        if level is None:
            return True
        return (level == "review" and issue.get("needs_review")) or issue.get("level") == level

    _echo(
        {
            "run_id": run_id,
            "validation_status": run["validation_status"],
            "needs_review": [i for i in run_issues if i.get("needs_review")],
            "run_issues": [i for i in run_issues if keep(i)],
            "charge_issues": [i for i in per_charge if keep(i)],
        }
    )


@provenance_app.command("evidence")
def evidence_cmd(
    run_id: int = typer.Option(..., "--run-id"),
    db: Path = _DB_OPTION,
    charge_id: int | None = typer.Option(None, "--charge-id", help="One extracted_charges id."),
    label: str | None = typer.Option(None, help="Substring of the charge label."),
) -> None:
    """Source evidence per candidate charge: PDF hash, page, bbox, quote, table cell, printed value."""
    conn = _open(db, write=False)
    sql = "SELECT * FROM extracted_charges WHERE run_id = ?"
    params: list = [run_id]
    if charge_id is not None:
        sql += " AND id = ?"
        params.append(charge_id)
    if label:
        sql += " AND charge_label LIKE ?"
        params.append(f"%{label}%")
    rows = conn.execute(sql + " ORDER BY ordinal", params).fetchall()
    _echo(
        [
            {
                "extracted_charge_id": r["id"],
                "charge": f"{r['charge_label']} = {r['rate_value']} {r['rate_unit']}",
                "canonical": f"{r['canonical_value']} {r['canonical_unit']} ({r['unit_normalization']})",
                "printed": {"value": r["printed_value_text"], "unit": r["printed_unit"]},
                "source_pdf": {"path": r["source_pdf_path"], "sha256": r["source_pdf_sha256"], "status": r["source_pdf_status"]},
                "page": {"number": r["page_number"], "status": r["page_status"]},
                "bbox": json.loads(r["bbox_json"]) if r["bbox_json"] else None,
                "quote": {"text": r["source_quote"], "status": r["quote_status"]},
                "table_ref": json.loads(r["table_ref_json"]) if r["table_ref_json"] else None,
                "effective_start": r["effective_start"],
                "method": r["extraction_method"],
            }
            for r in rows
        ]
    )


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
    acknowledge_review: bool = typer.Option(
        False, "--acknowledge-review", help="Promote although the run has findings that need review."
    ),
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
            acknowledge_review=acknowledge_review,
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


@provenance_app.command("audit-docling-coverage")
def audit_docling_coverage_cmd(
    db: Path = typer.Option(None, "--db", help="Defaults to the configured database (read-only)."),
    json_out: Path | None = typer.Option(None, "--json", help="Also write the full JSON report here."),
) -> None:
    """Docling artifacts whose page numbers cannot be verified, and the documents bound to them."""
    from duke_rates.document_intelligence.page_integrity_audit import (
        audit_docling_page_integrity,
        open_read_only,
    )

    conn = open_read_only((db or get_settings().database_path).resolve())
    try:
        report = audit_docling_page_integrity(conn)
    finally:
        conn.close()
    if json_out:
        json_out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    _echo(report["summary"])


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
