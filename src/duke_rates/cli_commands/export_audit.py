"""Audit and export sub-apps: NC + DEP tariff completeness, coverage, anomaly,
redline, missing-doc, and storm-rider audits/exports.

Wired into the main CLI as `duke-rates audit <command>` and `duke-rates export <command>`.

Two sub-apps live in one module because they share the same import surface
(`get_settings`, `Repository`) and the same operational concern (read-only
reports). Splitting into two files would force the same imports to be
duplicated.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository

from duke_rates.cli_commands._cli_utils import _bootstrap


audit_app = typer.Typer(help="Tariff completeness, coverage, and cross-attribution audits.")
export_app = typer.Typer(help="NC and DEP audit/inventory report exports.")


# -------------------------------------------------------------------------
# Audit commands
# -------------------------------------------------------------------------

@audit_app.command("tariff-timeline")
def audit_tariff_timeline(
    family: str = typer.Argument(..., help="Tariff family key, e.g. nc-progress-leaf-601"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    show_charges: bool = typer.Option(False, "--show-charges", help="Include charge_count column"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of table"),
) -> None:
    """Show version timeline, date gaps, and supersession chain for one tariff family."""
    import datetime as _dt
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)
    tm = svc.build_temporal_map(family)

    if json_out:
        typer.echo(tm.model_dump_json(indent=2))
        return

    typer.echo(f"\nFamily:  {tm.family_key}")
    typer.echo(f"Title:   {tm.title or '(none)'}")
    typer.echo(f"Type:    {tm.family_type}")
    typer.echo(f"Status:  {tm.timeline_status.upper()}")

    if tm.supersession_chain:
        typer.echo("\nSupersession chain:")
        for i, label in enumerate(tm.supersession_chain):
            prefix = "  +- " if i == len(tm.supersession_chain) - 1 else "  |- "
            typer.echo(f"{prefix}{label}")
    if tm.orphaned_revisions:
        typer.echo(f"\nOrphaned (no chain link): {', '.join(tm.orphaned_revisions)}")

    typer.echo(f"\nVersions ({len(tm.versions)}):")
    header = f"  {'Start':<12} {'End':<12} {'Charges':>7}  {'Nulls':>5}  Revision"
    if show_charges:
        typer.echo(header)
    else:
        typer.echo(f"  {'Start':<12} {'End':<12} Status     Revision")
    typer.echo("  " + "-" * 72)
    for v in tm.versions:
        start = v.effective_start or "undated"
        end = v.effective_end or "open"
        status = v.charge_status
        rev = v.revision_label or "(no label)"
        if show_charges:
            typer.echo(f"  {start:<12} {end:<12} {v.charge_count:>7}  {v.null_rate_count:>5}  {rev}")
        else:
            status_disp = {"ok": "OK        ", "no_charges": "NO CHARGES", "null_rates": "NULL RATES"}
            typer.echo(f"  {start:<12} {end:<12} {status_disp.get(status, status):<10} {rev}")

    if tm.gaps:
        typer.echo(f"\nGaps ({len(tm.gaps)}):")
        for g in tm.gaps:
            if g.gap_type == "undated_version":
                typer.echo(f"  [undated version -- id {g.successor_version_id}]")
            elif g.gap_type == "between_versions":
                days = f"{g.gap_days}d" if g.gap_days is not None else "?"
                typer.echo(f"  {g.gap_start} -> {g.gap_end}  ({days})")
            else:
                typer.echo(f"  {g.gap_type}: {g.gap_start} -> {g.gap_end}")
    else:
        typer.echo("\nNo gaps detected.")
    typer.echo("")


@audit_app.command("tariff-coverage")
def audit_tariff_coverage(
    schedule: str = typer.Argument(..., help="Rate schedule family key, e.g. nc-progress-leaf-500"),
    date: str = typer.Option(None, "--date", help="Date (YYYY-MM-DD), defaults to today"),
    customer_class: str = typer.Option("residential", "--class", help="Customer class"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of table"),
) -> None:
    """Audit rider coverage for one rate schedule at a given date."""
    import datetime as _dt
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)
    as_of = _dt.date.fromisoformat(date) if date else _dt.date.today()
    cm = svc.build_coverage_map(schedule, as_of, customer_class)

    if json_out:
        typer.echo(cm.model_dump_json(indent=2))
        return

    verdict_icon = {"complete": "[OK]", "partial": "[~]", "missing_riders": "[!]", "no_data": "[X]"}
    icon = verdict_icon.get(cm.audit_verdict, "[?]")
    typer.echo(f"\n{icon} {cm.schedule_family_key}  [{cm.audit_verdict.upper()}]  as of {cm.as_of_date}")
    typer.echo(f"  Revision: {cm.schedule_revision_label or '(none)'}")
    typer.echo(f"  Schedule: {cm.schedule_charge_status}")

    if cm.leaf600_total_cents_per_kwh is not None:
        tol_flag = "OK" if cm.delta_within_tolerance else "MISMATCH"
        typer.echo(
            f"  Leaf-600: {cm.leaf600_total_cents_per_kwh:.4f} c/kWh  "
            f"engine: {cm.engine_summary_total_cents_per_kwh:.4f} c/kWh  "
            f"delta={cm.delta_cents_per_kwh:.4f} [{tol_flag}]"
        )

    typer.echo(f"\n  Riders ({len(cm.riders)})  ok={cm.riders_ok}  issues={cm.riders_missing}")
    typer.echo(f"  {'Family key':<32} {'Summary':>7} {'Status':<12} {'Rate(c/kWh)':>11}")
    typer.echo("  " + "-" * 70)
    for r in cm.riders:
        summ = "Y" if r.in_rider_summary else "N"
        rate = f"{r.rate_cents_per_kwh:.4f}" if r.rate_cents_per_kwh is not None else "--"
        status_flag = "  " if r.coverage_status == "ok" else "! "
        typer.echo(f"  {status_flag}{r.rider_family_key:<30} {summ:>7} {r.coverage_status:<12} {rate:>11}")

    if cm.warnings:
        typer.echo("\n  Warnings:")
        for w in cm.warnings:
            typer.echo(f"    - {w}")
    typer.echo("")


@audit_app.command("tariff-null-scan")
def audit_tariff_null_scan(
    state: str = typer.Option("NC", "--state", help="State code"),
    company: str = typer.Option("progress", "--company", help="Company name"),
    date: str = typer.Option(None, "--date", help="Date (YYYY-MM-DD), defaults to today"),
    customer_class: str = typer.Option("residential", "--class", help="Customer class"),
    verdicts: str = typer.Option(
        None, "--verdicts",
        help="Comma-separated verdicts to show: complete,partial,missing_riders,no_data"
    ),
    family_type: str = typer.Option("rate_schedule", "--family-type", help="Family type to scan"),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of table"),
) -> None:
    """Batch coverage scan for all rate schedules of a state/company."""
    import datetime as _dt
    from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService

    settings = get_settings()
    repo = Repository(str(database or settings.database_path))
    svc = TariffCompletenessAuditService(repo)
    as_of = _dt.date.fromisoformat(date) if date else _dt.date.today()

    results = svc.build_null_audit(state, company, as_of, family_type, customer_class)

    verdict_filter = set(verdicts.split(",")) if verdicts else None
    if verdict_filter:
        results = [r for r in results if r.audit_verdict in verdict_filter]

    if json_out:
        typer.echo(json.dumps([r.model_dump() for r in results], indent=2))
        return

    typer.echo(f"\nTariff null scan | {state.upper()} {company}  as of {as_of}  ({len(results)} schedules)\n")
    typer.echo(f"  {'Schedule':<36} {'Verdict':<16} {'OK':>4} {'Issues':>6}  Revision")
    typer.echo("  " + "-" * 82)
    counts = {"complete": 0, "partial": 0, "missing_riders": 0, "no_data": 0}
    for cm in results:
        rev = (cm.schedule_revision_label or "")[:30]
        typer.echo(
            f"  {cm.schedule_family_key:<36} {cm.audit_verdict:<16} "
            f"{cm.riders_ok:>4} {cm.riders_missing:>6}  {rev}"
        )
        counts[cm.audit_verdict] = counts.get(cm.audit_verdict, 0) + 1

    typer.echo(f"\n  Summary: complete={counts['complete']}  partial={counts['partial']}  "
               f"missing_riders={counts['missing_riders']}  no_data={counts['no_data']}")
    typer.echo("")


@audit_app.command("cross-attributed-charges-nc")
def audit_cross_attributed_charges_nc(
    state: str = typer.Option("NC", "--state", help="State filter for historical_documents."),
    delete: bool = typer.Option(
        False,
        "--delete",
        help=(
            "Actually delete the suspect charges. Default is dry-run "
            "(list only). Always review the dry-run output first."
        ),
    ),
    family_specific_only: bool = typer.Option(
        True,
        "--family-specific-only/--include-generic",
        help=(
            "When true (default), only flag charges where the prior processing "
            "run shows a family-specific initial profile that fell back to "
            "generic_residential. Set --include-generic to also flag "
            "generic_residential->generic_residential runs."
        ),
    ),
    limit: int = typer.Option(200, "--limit", help="Max docs to list."),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Find tariff_charges polluted by the family-specific -> generic_residential fallback.

    Surfaces docs whose most recent processing run records a family-specific
    initial profile (e.g. progress_jaa_rider) that fell back to
    generic_residential, AND still has charges attached. After the 2026-05-20
    fallback guard broadening, future re-extractions of these docs will
    produce 0 charges; the old polluted rows survive only because
    insert_charges skips its DELETE on empty input.

    Recommended workflow:
      1. python -m duke_rates audit-cross-attributed-charges-nc    # dry-run, review
      2. Choose: --delete (direct cleanup) OR queue + reprocess process-queue-nc --enforce-cleanup
    """
    import sqlite3
    from collections import Counter

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    family_specific_filter = (
        " AND json_extract(pr.metadata_json, '$.selection.initial_parser_profile') != 'generic_residential'"
        if family_specific_only else ""
    )

    c.execute(
        f"""
        WITH latest AS (
          SELECT historical_document_id, MAX(id) as mid
          FROM historical_processing_runs
          GROUP BY historical_document_id
        ),
        suspects AS (
          SELECT pr.historical_document_id as hd,
                 hd.family_key,
                 json_extract(pr.metadata_json, '$.selection.initial_parser_profile') as initial_profile,
                 pr.parser_profile as final_profile,
                 pr.charge_count as run_charge_count
          FROM historical_processing_runs pr
          JOIN latest l ON pr.id = l.mid
          JOIN historical_documents hd ON hd.id = pr.historical_document_id
          WHERE hd.state = ?
            AND pr.parser_profile = 'generic_residential'
            AND json_extract(pr.metadata_json, '$.selection.fallback_applied') = 1
            {family_specific_filter}
        )
        SELECT s.hd, s.family_key, s.initial_profile, s.final_profile, s.run_charge_count,
               COUNT(tc.id) as alive_charges
        FROM suspects s
        LEFT JOIN tariff_versions tv ON tv.historical_document_id = s.hd
        LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
        GROUP BY s.hd
        HAVING alive_charges > 0
        ORDER BY alive_charges DESC
        LIMIT ?
        """,
        (state, limit),
    )
    rows = c.fetchall()
    rows = [dict(r) for r in rows]

    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        if delete and rows:
            _audit_cross_attr_delete(conn, rows)
        conn.close()
        return

    total_alive = sum(r["alive_charges"] for r in rows)
    typer.echo(
        f"\nCross-attributed charge audit | {state} | "
        f"{'family-specific only' if family_specific_only else 'all generic_residential fallbacks'}\n"
    )
    typer.echo(f"  {'hd':>5}  {'family':<35}  {'initial -> final':<55}  {'alive':>5}")
    typer.echo("  " + "-" * 110)
    by_initial = Counter()
    for r in rows:
        arrow = f"{r['initial_profile']} -> {r['final_profile']}"
        typer.echo(
            f"  {r['hd']:>5}  {r['family_key']:<35}  {arrow[:55]:<55}  {r['alive_charges']:>5}"
        )
        by_initial[r['initial_profile']] += r['alive_charges']

    typer.echo(f"\n  {len(rows)} docs, {total_alive} alive charges total")
    if by_initial:
        typer.echo("  by initial profile:")
        for profile, charge_count in by_initial.most_common():
            typer.echo(f"    {profile:<45} {charge_count}")
    typer.echo("")

    if delete:
        deleted = _audit_cross_attr_delete(conn, rows)
        typer.echo(f"  DELETED {deleted} charges across {len(rows)} docs.")
    elif rows:
        typer.echo(
            "  Dry-run only. To delete: rerun with --delete, or queue these hd_ids and run\n"
            "  reprocess process-queue-nc --enforce-cleanup to clean via the extractor.\n"
        )
    conn.close()


def _audit_cross_attr_delete(conn, rows: list[dict]) -> int:
    """Delete tariff_charges for the audit-flagged docs. Internal helper."""
    if not rows:
        return 0
    hd_ids = [r["hd"] for r in rows]
    placeholders = ",".join("?" for _ in hd_ids)
    c = conn.cursor()
    c.execute(
        f"""
        DELETE FROM tariff_charges
        WHERE id IN (
            SELECT tc.id FROM tariff_charges tc
            JOIN tariff_versions tv ON tc.version_id = tv.id
            WHERE tv.historical_document_id IN ({placeholders})
        )
        """,
        hd_ids,
    )
    deleted = c.rowcount
    conn.commit()
    return deleted


# -------------------------------------------------------------------------
# Export commands
# -------------------------------------------------------------------------

@export_app.command("nc-coverage-assessment")
def export_nc_coverage_assessment_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_coverage_assessment"),
        "--output-dir",
        help="Directory for generated coverage assessment exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export DB-driven NC schedule coverage matrices for DEP and DEC."""
    from duke_rates.analytics.nc_coverage_assessment import export_nc_coverage_assessment

    paths = export_nc_coverage_assessment(output_dir, database_path=database)
    typer.echo("Wrote NC coverage assessment exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-anomaly-audit")
def export_nc_anomaly_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_anomaly_audit"),
        "--output-dir",
        help="Directory for generated NC anomaly audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a ranked NC tariff anomaly audit to drive reparse and backfill work."""
    from duke_rates.analytics.nc_anomaly_audit import export_nc_anomaly_audit

    paths = export_nc_anomaly_audit(output_dir, database_path=database)
    typer.echo("Wrote NC anomaly audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-schedule-inventory-audit")
def export_nc_schedule_inventory_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_schedule_inventory_audit"),
        "--output-dir",
        help="Directory for generated NC schedule inventory audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a full NC rate_schedule inventory audit against the focused matrix scope."""
    from duke_rates.analytics.nc_schedule_inventory_audit import export_nc_schedule_inventory_audit

    paths = export_nc_schedule_inventory_audit(output_dir, database_path=database)
    typer.echo("Wrote NC schedule inventory audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-document-intelligence-audit")
def export_nc_document_intelligence_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_document_intelligence_audit"),
        "--output-dir",
        help="Directory for generated NC document-intelligence audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
    limit: int = typer.Option(150, "--limit", help="Maximum number of candidate rows to analyze."),
) -> None:
    """Apply the document-intelligence layer to NC zero-charge and malformed historical rows."""
    from duke_rates.analytics.nc_document_intelligence_audit import (
        export_nc_document_intelligence_audit,
    )

    paths = export_nc_document_intelligence_audit(output_dir, database_path=database, limit=limit)
    typer.echo("Wrote NC document-intelligence audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-document-gap-audit")
def export_nc_document_gap_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_document_gap_audit"),
        "--output-dir",
        help="Directory for generated NC document gap audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Identify temporal gaps, ordinal gaps, and thin-source versions where a higher-quality
    NCUC document (compliance bundle or targeted leaf) would improve coverage."""
    from duke_rates.analytics.nc_document_gap_audit import export_nc_document_gap_audit

    paths = export_nc_document_gap_audit(output_dir, database_path=database)
    typer.echo("Wrote NC document gap audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-confidence-audit")
def export_nc_confidence_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_confidence_audit"),
        "--output-dir",
        help="Directory for generated NC confidence audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a family-level NC confidence audit combining lineage, redline, and parse signals."""
    from duke_rates.analytics.nc_confidence_audit import export_nc_confidence_audit

    paths = export_nc_confidence_audit(output_dir, database_path=database)
    typer.echo("Wrote NC confidence audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-redline-lead-audit")
def export_nc_redline_lead_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_redline_lead_audit"),
        "--output-dir",
        help="Directory for generated NC redline lead audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a ranked queue of NC families where redline documents can help locate or validate clean tariffs."""
    from duke_rates.analytics.nc_redline_lead_audit import export_nc_redline_lead_audit

    paths = export_nc_redline_lead_audit(output_dir, database_path=database)
    typer.echo("Wrote NC redline lead audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-redline-parse-audit")
def export_nc_redline_parse_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_redline_parse_audit"),
        "--output-dir",
        help="Directory for generated NC redline parse audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Audit NC parsed tariff versions whose source PDFs may be redlines, using the corrected detector."""
    from duke_rates.analytics.nc_redline_parse_audit import export_nc_redline_parse_audit

    paths = export_nc_redline_parse_audit(output_dir, database_path=database)
    typer.echo("Wrote NC redline parse audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("nc-missing-clean-doc-audit")
def export_nc_missing_clean_doc_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/nc_missing_clean_doc_audit"),
        "--output-dir",
        help="Directory for generated NC missing-clean-document audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export ranked NC leads for missing clean historical tariff/rider documents."""
    from duke_rates.analytics.nc_missing_clean_doc_audit import export_nc_missing_clean_doc_audit

    paths = export_nc_missing_clean_doc_audit(output_dir, database_path=database)
    typer.echo("Wrote NC missing clean document audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("dep-leaf-503-audit")
def export_dep_leaf_503_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_leaf_503_audit"),
        "--output-dir",
        help="Directory for generated DEP leaf-503 audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a focused audit for DEP leaf-503 (R-TOU-CPP) versions and rider linkage."""
    from duke_rates.analytics.dep_leaf503_audit import export_dep_leaf503_audit

    paths = export_dep_leaf503_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP leaf-503 audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("dep-residential-rider-gap-audit")
def export_dep_residential_rider_gap_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_residential_rider_gap_audit"),
        "--output-dir",
        help="Directory for generated DEP residential rider gap audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export rider-family charge coverage gaps for DEP residential schedules 500-504."""
    from duke_rates.analytics.dep_residential_rider_gap_audit import (
        export_dep_residential_rider_gap_audit,
    )

    paths = export_dep_residential_rider_gap_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP residential rider gap audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("dep-residential-rider-action-queue")
def export_dep_residential_rider_action_queue_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_residential_rider_action_queue"),
        "--output-dir",
        help="Directory for generated DEP residential rider action queue exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a ranked DEP residential rider repair queue derived from the gap audit."""
    from duke_rates.analytics.dep_residential_rider_action_queue import (
        export_dep_residential_rider_action_queue,
    )

    paths = export_dep_residential_rider_action_queue(output_dir, database_path=database)
    typer.echo("Wrote DEP residential rider action queue exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("dep-residential-rider-repair-plan")
def export_dep_residential_rider_repair_plan_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_residential_rider_repair_plan"),
        "--output-dir",
        help="Directory for generated DEP residential rider repair plan exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export an operational DEP residential rider repair plan with parser/discovery guidance."""
    from duke_rates.analytics.dep_residential_rider_repair_plan import (
        export_dep_residential_rider_repair_plan,
    )

    paths = export_dep_residential_rider_repair_plan(output_dir, database_path=database)
    typer.echo("Wrote DEP residential rider repair plan exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("dep-compliance-bundle-audit")
def export_dep_compliance_bundle_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_compliance_bundle_audit"),
        "--output-dir",
        help="Directory for generated DEP compliance bundle audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a DEP rider-family compliance bundle audit for discovery/import/span triage."""
    from duke_rates.analytics.dep_compliance_bundle_audit import (
        export_dep_compliance_bundle_audit,
    )

    paths = export_dep_compliance_bundle_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP compliance bundle audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("dep-storm-rider-audit")
def export_dep_storm_rider_audit_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_storm_rider_audit"),
        "--output-dir",
        help="Directory for generated DEP storm rider audit exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a DEP storm-rider audit for canonical-family, residue, and applicability triage."""
    from duke_rates.analytics.dep_storm_rider_audit import (
        export_dep_storm_rider_audit,
    )

    paths = export_dep_storm_rider_audit(output_dir, database_path=database)
    typer.echo("Wrote DEP storm rider audit exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


@export_app.command("dep-storm-history-inventory")
def export_dep_storm_history_inventory_cmd(
    output_dir: Path = typer.Option(
        Path("docs/reports/dep_storm_history_inventory"),
        "--output-dir",
        help="Directory for generated DEP storm history inventory exports.",
    ),
    database: Path = typer.Option(None, "--database", "-d", help="Override DB path"),
) -> None:
    """Export a DEP storm-history inventory separating canonical families from older docket candidates."""
    from duke_rates.analytics.dep_storm_history_inventory import (
        export_dep_storm_history_inventory,
    )

    paths = export_dep_storm_history_inventory(output_dir, database_path=database)
    typer.echo("Wrote DEP storm history inventory exports:")
    for label, path in paths.items():
        typer.echo(f"  {label}: {path}")


