"""Billing and external-data sub-apps.

- `billing` covers bill calculation, parsing, estimation, comparison,
  observations, and reconciliation.
- `data` covers external rate-data sources: EIA API v2, OpenEI / URDB.

Wired into the main CLI as `duke-rates billing <command>` and
`duke-rates data <command>`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import typer

from duke_rates.billing.calculators import UsageInput
from duke_rates.billing.engine import BillingEngine
from duke_rates.billing.observations import derive_bill_component_observations
from duke_rates.billing.reconciliation import ProgressNCBillReconciliationService
from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.external.openei import OpenEIClient
from duke_rates.external.openei_export import build_openei_export_candidate
from duke_rates.historical.observed_components import (
    ProgressNCObservedComponentHistoryService,
)
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector
from duke_rates.models.bill import BillStatementData
from duke_rates.models.document import DocumentCategory
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.tariff import TariffVersionRecord
from duke_rates.parse.bill_parser import parse_bill_text
from duke_rates.parse.html_extract import extract_html_text
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.selection import (
    canonical_tariff_key,
    estimation_score,
    is_estimatable_schedule,
    supports_usage_input,
)

from duke_rates.cli_commands._cli_utils import _bootstrap, _read_usage_file, _safe_cli_text


logger = logging.getLogger(__name__)
ESTIMATABLE_CATEGORIES = {
    DocumentCategory.RATE.value,
    DocumentCategory.TARIFF.value,
}


def _schedule_has_bill_components(result: DocumentParseResult) -> bool:
    return is_estimatable_schedule(result)


billing_app = typer.Typer(help="Bill calculation, parsing, estimation, comparison, and reconciliation.")
data_app = typer.Typer(help="External rate-data sources: EIA API v2, OpenEI, URDB.")


# Helpers (only billing uses these)

def _parse_bill_file(path: Path, repository: Repository) -> tuple[int, object]:
    text = extract_pdf_text(path)
    settings = get_settings()
    raw_text_dir = settings.processed_dir / "bills"
    raw_text_dir.mkdir(parents=True, exist_ok=True)
    raw_text_path = raw_text_dir / f"{path.stem}.txt"
    raw_text_path.write_text(text, encoding="utf-8")
    statement = parse_bill_text(text, source_path=path)
    content_hash = sha256_bytes(path.read_bytes())
    bill_id = repository.upsert_bill_statement(
        statement,
        content_hash=content_hash,
        raw_text_path=str(raw_text_path),
    )
    return bill_id, statement


def _best_estimatable_results(
    repository: Repository,
    *,
    state: str | None = None,
    company: str | None = None,
    usage: UsageInput | None = None,
) -> dict[str, tuple[object, DocumentParseResult]]:
    best: dict[str, tuple[object, DocumentParseResult]] = {}
    for doc in repository.list_documents(state=state, company=company):
        if doc.category not in ESTIMATABLE_CATEGORIES:
            continue
        result = repository.latest_parse_result(doc.id)
        if not result or not result.schedule or not _schedule_has_bill_components(result):
            continue
        if usage and not supports_usage_input(result, usage):
            continue
        key = canonical_tariff_key(result) or result.schedule.tariff_id
        current = best.get(key)
        new_score = estimation_score(doc, result)
        current_score = estimation_score(current[0], current[1]) if current else None
        if current is None or new_score > current_score:
            best[key] = (doc, result)
    return best


# Billing commands

@billing_app.command("calculate")
def calculate_bill(
    family_key: str = typer.Option(..., help="Tariff family key, e.g. nc-progress-leaf-500."),
    kwh: float = typer.Option(..., help="Monthly kWh usage."),
    service_date: str | None = typer.Option(None, help="Service date YYYY-MM-DD (determines season)."),
    peak_kw: float | None = typer.Option(None, help="Peak demand in kW (for demand-metered schedules)."),
    base_kw: float | None = typer.Option(None, help="Base demand in kW (for TOU demand schedules)."),
    on_peak_kw: float | None = typer.Option(None, help="On-peak demand in kW (for TOU demand schedules)."),
    mid_peak_kw: float | None = typer.Option(None, help="Mid-peak demand in kW (for TOU demand schedules)."),
    off_peak_kw: float | None = typer.Option(None, help="Off-peak demand in kW (for TOU demand schedules)."),
    on_peak_kwh: float | None = typer.Option(None, help="On-peak kWh (for TOU schedules)."),
    off_peak_kwh: float | None = typer.Option(None, help="Off-peak kWh (for TOU schedules)."),
    discount_kwh: float | None = typer.Option(None, help="Discount-period kWh (for TOU-EV schedules)."),
    customer_class: str = typer.Option("residential", help="Customer class for rider matching."),
    no_riders: bool = typer.Option(False, help="Exclude rider adjustments."),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Calculate a bill from tariff_charges tables (Phase 4a engine).

    Uses the structured charge data populated by parse-tariff-versions.
    Run parse-tariff-versions first to populate the tariff data.

    Example:
        duke-rates billing calculate --family-key nc-progress-leaf-500 --kwh 1200 --service-date 2025-08-01
    """
    from duke_rates.billing.tariff_engine import BillInput, TariffBillingEngine

    import datetime as _dt

    _, repository = _bootstrap()

    parsed_date: _dt.date | None = None
    if service_date:
        try:
            parsed_date = _dt.date.fromisoformat(service_date)
        except ValueError:
            typer.echo(f"Invalid service_date: {service_date}. Use YYYY-MM-DD.", err=True)
            raise typer.Exit(1)

    usage = BillInput(
        monthly_kwh=kwh,
        peak_kw=peak_kw,
        base_kw=base_kw,
        on_peak_kw=on_peak_kw,
        mid_peak_kw=mid_peak_kw,
        off_peak_kw=off_peak_kw,
        service_date=parsed_date,
        on_peak_kwh=on_peak_kwh,
        off_peak_kwh=off_peak_kwh,
        discount_kwh=discount_kwh,
    )

    engine = TariffBillingEngine(repository)
    result = engine.calculate(
        family_key,
        usage,
        customer_class=customer_class,
        include_riders=not no_riders,
    )

    if output_json:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        return

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  {result.schedule_title or family_key}")
    typer.echo(f"  Effective: {result.effective_start or 'unknown'}  |  {result.revision_label or ''}")
    if parsed_date:
        season = "summer (May-Sep)" if parsed_date.month in {5, 6, 7, 8, 9} else "winter (Oct-Apr)"
        typer.echo(f"  Date: {parsed_date}  Season: {season}")
    demand_parts = []
    if peak_kw is not None:
        demand_parts.append(f"Peak: {peak_kw} kW")
    if base_kw is not None:
        demand_parts.append(f"Base: {base_kw} kW")
    if on_peak_kw is not None:
        demand_parts.append(f"On-Peak: {on_peak_kw} kW")
    if mid_peak_kw is not None:
        demand_parts.append(f"Mid-Peak: {mid_peak_kw} kW")
    if off_peak_kw is not None:
        demand_parts.append(f"Off-Peak: {off_peak_kw} kW")
    typer.echo(f"  Usage: {kwh:,.0f} kWh" + (f"  Demand: {', '.join(demand_parts)}" if demand_parts else ""))
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  {'CHARGE':<45} {'QTY':>8}  {'RATE':<14}  {'AMOUNT':>8}")
    typer.echo(f"  {'-' * 56}")
    for item in result.line_items:
        qty_str = f"{item.quantity:,.1f}" if item.quantity is not None else ""
        rate_str = f"{item.rate_value} {item.rate_unit}"
        typer.echo(f"  {item.label:<45} {qty_str:>8}  {rate_str:<14}  ${item.amount:>7.2f}")
    typer.echo(f"  {'-' * 56}")
    typer.echo(f"  {'Base subtotal':<45} {'':>8}  {'':14}  ${result.base_subtotal:>7.2f}")
    if result.rider_subtotal:
        typer.echo(f"  {'Rider adjustments':<45} {'':>8}  {'':14}  ${result.rider_subtotal:>7.2f}")
    typer.echo(f"  {'TOTAL':<45} {'':>8}  {'':14}  ${result.total:>7.2f}")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  Confidence: {result.source_confidence:.0%}")

    if result.warnings:
        typer.echo("")
        for w in result.warnings:
            typer.echo(f"  ! {w}")


@billing_app.command("compare-tariff-rates")
def compare_tariff_rates(
    state: str = typer.Option("NC", help="State abbreviation."),
    company: str = typer.Option("progress", help="Company short name."),
    kwh: float = typer.Option(..., help="Monthly kWh usage."),
    service_date: str | None = typer.Option(None, help="Service date YYYY-MM-DD."),
    peak_kw: float | None = typer.Option(None, help="Peak demand kW."),
    base_kw: float | None = typer.Option(None, help="Base demand kW for TOU demand schedules."),
    on_peak_kw: float | None = typer.Option(None, help="On-peak demand kW for TOU demand schedules."),
    mid_peak_kw: float | None = typer.Option(None, help="Mid-peak demand kW for TOU demand schedules."),
    off_peak_kw: float | None = typer.Option(None, help="Off-peak demand kW for TOU demand schedules."),
    on_peak_kwh: float | None = typer.Option(None, help="On-peak kWh (for TOU)."),
    off_peak_kwh: float | None = typer.Option(None, help="Off-peak kWh (for TOU)."),
    customer_class: str = typer.Option("residential", help="Customer class filter."),
    family_type: str = typer.Option("rate_schedule", help="Family type filter."),
    group: str | None = typer.Option(
        None,
        "--group",
        help=(
            "Restrict to schedule group(s): residential, sgs, mgs, lgs, gs, specialty, all. "
            "Comma-separate multiple (e.g. residential,sgs). Default: residential."
        ),
    ),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Compare bill amounts across rate schedules for given usage.

    Calculates what the bill would be under each available rate schedule
    for the state/company, then ranks by total cost.  By default only
    residential schedules are shown; use --group to broaden the comparison.

    Examples:
        duke-rates billing compare-tariff-rates --kwh 1200 --service-date 2025-08-01
        duke-rates billing compare-tariff-rates --kwh 800 --service-date 2025-11-01 --on-peak-kwh 300 --off-peak-kwh 500
        duke-rates billing compare-tariff-rates --kwh 3000 --service-date 2025-08-01 --group residential,sgs
        duke-rates billing compare-tariff-rates --kwh 1000 --group all
    """
    from duke_rates.billing.tariff_engine import BillInput, TariffBillingEngine, schedule_group_for

    import datetime as _dt

    _, repository = _bootstrap()

    parsed_date: _dt.date | None = None
    if service_date:
        try:
            parsed_date = _dt.date.fromisoformat(service_date)
        except ValueError:
            typer.echo(f"Invalid service_date: {service_date}. Use YYYY-MM-DD.", err=True)
            raise typer.Exit(1)

    # Parse group filter
    _ALL_GROUPS = {"residential", "sgs", "mgs", "lgs", "gs", "specialty", "unknown"}
    if group is None:
        allowed_groups: set[str] | None = {"residential"}
    elif group.lower() == "all":
        allowed_groups = None  # no filter
    else:
        allowed_groups = {g.strip().lower() for g in group.split(",")}
        invalid = allowed_groups - _ALL_GROUPS
        if invalid:
            typer.echo(f"Unknown group(s): {', '.join(sorted(invalid))}. "
                       f"Valid: {', '.join(sorted(_ALL_GROUPS))}, all", err=True)
            raise typer.Exit(1)

    usage = BillInput(
        monthly_kwh=kwh,
        peak_kw=peak_kw,
        base_kw=base_kw,
        on_peak_kw=on_peak_kw,
        mid_peak_kw=mid_peak_kw,
        off_peak_kw=off_peak_kw,
        service_date=parsed_date,
        on_peak_kwh=on_peak_kwh,
        off_peak_kwh=off_peak_kwh,
    )

    all_families = repository.list_tariff_families(
        state=state, company=company, family_type=family_type
    )
    if not all_families:
        typer.echo(f"No {family_type} families found for {state}/{company}.")
        raise typer.Exit(1)

    # Apply schedule group filter
    if allowed_groups is not None:
        families = [f for f in all_families
                    if schedule_group_for(f.schedule_code) in allowed_groups]
    else:
        families = all_families

    if not families:
        typer.echo(f"No schedules found matching group(s): {group}. "
                   "Try --group all to see all available schedules.")
        raise typer.Exit(1)

    engine = TariffBillingEngine(repository)
    results = []
    partial_coverage: list = []
    for family in families:
        result = engine.calculate(
            family.family_key, usage,
            customer_class=customer_class,
            include_riders=True,
        )
        # Detect partial TOU coverage (understated totals — exclude from ranked list)
        has_partial = any("Partial TOU coverage" in w for w in result.warnings)
        if has_partial:
            partial_coverage.append(result)
            continue
        # Only include schedules with actual base charges (not just rider-only)
        if result.base_subtotal > 0:
            results.append(result)

    results.sort(key=lambda r: r.total)

    if output_json:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return

    group_label = group or "residential"
    typer.echo(f"\nRate comparison — {state}/{company} | {kwh:,.0f} kWh | group={group_label}"
               + (f" | {parsed_date}" if parsed_date else ""))
    typer.echo(f"{'SCHEDULE':<45} {'TOTAL':>8}  {'BASE':>8}  {'RIDERS':>7}  {'CONF':>5}")
    typer.echo("-" * 76)
    for r in results:
        title = (r.schedule_title or r.family_key)[:44]
        typer.echo(
            f"  {title:<43} ${r.total:>7.2f}  ${r.base_subtotal:>7.2f}  ${r.rider_subtotal:>6.2f}  {r.source_confidence:.0%}"
        )
    if results:
        cheapest = results[0]
        priciest = results[-1]
        diff = round(priciest.total - cheapest.total, 2)
        typer.echo(f"\n  Cheapest: {cheapest.schedule_title or cheapest.family_key} (${cheapest.total:.2f})")
        if len(results) > 1:
            typer.echo(f"  Spread:   ${diff:.2f}/month (${diff*12:.0f}/year)")
    if partial_coverage:
        typer.echo(f"\n  Excluded (incomplete charge data — totals would be understated):")
        for r in partial_coverage:
            typer.echo(f"    {(r.schedule_title or r.family_key)[:60]}")


@billing_app.command("estimate")
def estimate_bill(
    tariff_id: str = typer.Option(..., help="Tariff ID from a parsed schedule."),
    usage_file: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    monthly_kwh: float | None = typer.Option(None),
    peak_kw: float | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    matched_result = None
    usage = (
        _read_usage_file(usage_file)
        if usage_file
        else UsageInput(
            monthly_kwh=monthly_kwh or 0.0,
            peak_kw=peak_kw,
        )
    )
    for _, parse_result in _best_estimatable_results(repository, usage=usage).values():
        if parse_result.schedule and parse_result.schedule.tariff_id == tariff_id:
            matched_result = parse_result
            break

    if not matched_result or not matched_result.schedule:
        raise typer.BadParameter(
            f"No estimatable parsed schedule found for tariff_id={tariff_id}"
        )

    estimate = BillingEngine().estimate(matched_result.schedule, usage)
    typer.echo(json.dumps(estimate.model_dump(mode="json"), indent=2))


@billing_app.command("compare-rates")
def compare_rates(
    state: str | None = typer.Option(None),
    company: str | None = typer.Option(None),
    usage_file: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    monthly_kwh: float | None = typer.Option(None),
    peak_kw: float | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    if not usage_file and monthly_kwh is None:
        raise typer.BadParameter("Provide --usage-file or --monthly-kwh.")
    usage = (
        _read_usage_file(usage_file)
        if usage_file
        else UsageInput(
            monthly_kwh=monthly_kwh or 0.0,
            peak_kw=peak_kw,
        )
    )
    candidates = []
    for _, result in _best_estimatable_results(
        repository,
        state=state,
        company=company,
        usage=usage,
    ).values():
        estimate = BillingEngine().estimate(result.schedule, usage)
        candidates.append(
            (estimate.total, result.schedule.tariff_id, result.schedule.schedule_title)
        )

    if not candidates:
        typer.echo("No estimatable parsed tariffs found for the requested scope.")
        raise typer.Exit()

    for total, tariff_id, title in sorted(candidates):
        typer.echo(f"{total:.2f}\t{tariff_id}\t{title}")


@billing_app.command("parse")
def parse_bill(
    bill_file: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False),
) -> None:
    _, repository = _bootstrap()
    bill_id, statement = _parse_bill_file(bill_file, repository)
    typer.echo(
        json.dumps(
            {
                "bill_id": bill_id,
                "statement": statement.model_dump(mode="json"),
            },
            indent=2,
            default=str,
        )
    )


@billing_app.command("parse-batch")
def parse_bills(
    directory: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    _, repository = _bootstrap()
    parsed = 0
    for path in sorted(directory.glob("*.pdf")):
        try:
            _parse_bill_file(path, repository)
            parsed += 1
        except Exception as exc:
            logger.warning("Failed to parse bill %s: %s", path, exc)
    typer.echo(f"Parsed {parsed} bill PDFs.")


@billing_app.command("list")
def list_bills() -> None:
    _, repository = _bootstrap()
    for statement in repository.list_bill_statements():
        typer.echo(
            "\t".join(
                [
                    str(statement.id),
                    statement.bill_date.isoformat() if statement.bill_date else "-",
                    statement.service_start.isoformat() if statement.service_start else "-",
                    statement.service_end.isoformat() if statement.service_end else "-",
                    (
                        f"{statement.total_amount_due:.2f}"
                        if statement.total_amount_due is not None
                        else "-"
                    ),
                    statement.source_path,
                ]
            )
        )


@billing_app.command("show")
def show_bill(bill_id: int) -> None:
    _, repository = _bootstrap()
    stored = repository.get_bill_statement(bill_id)
    if not stored:
        raise typer.BadParameter(f"Bill statement {bill_id} not found.")
    typer.echo(json.dumps(stored.model_dump(mode="json"), indent=2, default=str))


@billing_app.command("derive-observations")
def derive_bill_observations(bill_id: int | None = typer.Option(None)) -> None:
    _, repository = _bootstrap()
    stored_bills = (
        [repository.get_bill_statement(bill_id)]
        if bill_id is not None
        else repository.list_bill_statements()
    )
    processed = 0
    total = 0
    for stored in stored_bills:
        if stored is None:
            continue
        statement = BillStatementData.model_validate_json(stored.statement_json)
        observations = derive_bill_component_observations(
            bill_id=stored.id,
            statement=statement,
        )
        repository.replace_bill_component_observations(
            bill_id=stored.id,
            observations=observations,
        )
        processed += 1
        total += len(observations)
    typer.echo(f"Derived {total} bill component observations across {processed} bills.")


@billing_app.command("list-observations")
def list_bill_observations(
    bill_id: int | None = typer.Option(None),
    component_key: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    rows = repository.list_bill_component_observations(
        bill_id=bill_id,
        component_key=component_key,
    )
    for observation in rows:
        typer.echo(
            "\t".join(
                [
                    str(observation.bill_id),
                    observation.service_end.isoformat() if observation.service_end else "-",
                    observation.section_name,
                    observation.rate_code or "-",
                    observation.component_key,
                    observation.component_label,
                    f"{observation.amount:.2f}",
                    observation.inferred_unit or "-",
                    (
                        f"{observation.inferred_value:.3f}"
                        if observation.inferred_value is not None
                        else "-"
                    ),
                    (
                        f"{observation.quantity_basis_kwh:.3f}"
                        if observation.quantity_basis_kwh is not None
                        else "-"
                    ),
                ]
            )
        )


@billing_app.command("list-observed-component-history-progress-nc")
def list_observed_component_history_progress_nc(
    component_key: str | None = typer.Option(None),
    rate_code: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCObservedComponentHistoryService(
        repository.list_bill_component_observations(component_key=component_key)
    )
    rows = service.build_series(component_key=component_key, rate_code=rate_code)
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    row.component_key,
                    row.rate_code or "-",
                    row.start_date.isoformat(),
                    row.end_date.isoformat(),
                    row.normalized_unit,
                    f"{row.normalized_value:.3f}",
                    str(row.sample_count),
                    ",".join(str(bill_id) for bill_id in row.bill_ids),
                ]
            )
        )


@billing_app.command("show-observed-component-history-progress-nc")
def show_observed_component_history_progress_nc(
    component_key: str = typer.Option(...),
    rate_code: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCObservedComponentHistoryService(
        repository.list_bill_component_observations(component_key=component_key)
    )
    rows = service.build_series(component_key=component_key, rate_code=rate_code)
    typer.echo(json.dumps([row.model_dump(mode="json") for row in rows], indent=2, default=str))


@billing_app.command("reconcile-progress-nc")
def reconcile_bill_progress_nc(bill_id: int) -> None:
    _, repository = _bootstrap()
    stored = repository.get_bill_statement(bill_id)
    if not stored:
        raise typer.BadParameter(f"Bill statement {bill_id} not found.")
    statement = json.loads(stored.statement_json)
    service = ProgressNCBillReconciliationService(
        ProgressNCHistoricalTariffSelector(repository)
    )
    result = service.reconcile(
        bill_id=bill_id,
        statement=BillStatementData.model_validate(statement),
    )
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


@billing_app.command("compare-version-rates")
def compare_version_rates(
    family_key: str | None = typer.Option(None, help="Family key (e.g. nc-progress-leaf-602). Compares latest two versions."),
    version_a: int | None = typer.Option(None, "--version-a", help="Older tariff_version id."),
    version_b: int | None = typer.Option(None, "--version-b", help="Newer tariff_version id."),
    show_unchanged: bool = typer.Option(False, help="Also show unchanged charges."),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Compare rate charges between two tariff versions of the same family.

    If --family-key is given without version ids, compares the two most recent
    versions that have extracted charges.  Highlights redline-to-clean transitions
    and flags when document quality tier changes between versions.

    Examples:
        duke-rates billing compare-version-rates --family-key nc-progress-leaf-602
        duke-rates billing compare-version-rates --version-a 45 --version-b 87
        duke-rates billing compare-version-rates --family-key nc-carolinas-rider-sts --show-unchanged
    """
    import sqlite3, json as _json
    from duke_rates.historical.ncuc.pipeline.version_compare import (
        compare_versions,
        compare_family_latest_two,
    )
    from duke_rates.config import Settings

    settings, _ = _bootstrap()
    db_path = settings.database_path

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        if version_a is not None and version_b is not None:
            result = compare_versions(conn, version_a, version_b)
        elif family_key:
            result = compare_family_latest_two(conn, family_key)
            if result is None:
                typer.echo(
                    f"Fewer than two versions with charges found for {family_key!r}.", err=True
                )
                raise typer.Exit(1)
        else:
            typer.echo(
                "Provide --family-key or both --version-a and --version-b.", err=True
            )
            raise typer.Exit(1)

    if output_json:
        import dataclasses
        typer.echo(_json.dumps(dataclasses.asdict(result), indent=2, default=str))
        return

    # Human-readable output
    typer.echo(f"\n{'='*70}")
    typer.echo(f"  {result.summary}")
    typer.echo(f"{'='*70}")
    typer.echo(
        f"  Version A: id={result.version_a_id}  effective={result.effective_date_a}  "
        f"tier={result.doc_tier_a or 'unknown'}  "
        f"revision={result.revision_label_a or '—'}  "
        f"redline={result.redline_flag_a}"
    )
    typer.echo(
        f"  Version B: id={result.version_b_id}  effective={result.effective_date_b}  "
        f"tier={result.doc_tier_b or 'unknown'}  "
        f"revision={result.revision_label_b or '—'}  "
        f"redline={result.redline_flag_b}"
    )
    typer.echo("")

    # Show changed charges first, then added, then removed
    changed_deltas = [d for d in result.rate_deltas if d.change_type == "changed"]
    added_deltas   = [d for d in result.rate_deltas if d.change_type == "added"]
    removed_deltas = [d for d in result.rate_deltas if d.change_type == "removed"]
    unchanged_deltas = [d for d in result.rate_deltas if d.change_type == "unchanged"]

    if changed_deltas:
        typer.echo(f"  CHANGED ({len(changed_deltas)}):")
        for d in changed_deltas:
            pct_str = f" ({d.delta_pct:+.2f}%)" if d.delta_pct is not None else ""
            typer.echo(
                f"    {d.charge_label:<45} "
                f"{d.old_value} {d.old_unit or ''} -> "
                f"{d.new_value} {d.new_unit or ''}{pct_str}"
            )

    if added_deltas:
        typer.echo(f"\n  ADDED ({len(added_deltas)}):")
        for d in added_deltas:
            typer.echo(f"    + {d.charge_label:<44} {d.new_value} {d.new_unit or ''}")

    if removed_deltas:
        typer.echo(f"\n  REMOVED ({len(removed_deltas)}):")
        for d in removed_deltas:
            typer.echo(f"    - {d.charge_label:<44} {d.old_value} {d.old_unit or ''}")

    if show_unchanged and unchanged_deltas:
        typer.echo(f"\n  UNCHANGED ({len(unchanged_deltas)}):")
        for d in unchanged_deltas:
            typer.echo(f"    = {d.charge_label:<44} {d.old_value} {d.old_unit or ''}")

    typer.echo("")


@billing_app.command("calculator")
def bill_calculator(
    schedule: str = typer.Argument(..., help="Rate schedule code (e.g. RES, SGS, MGS-TOU)."),
    kwh: float = typer.Argument(..., help="kWh consumed in billing period."),
    date: str = typer.Option(
        "",
        "--date", "-d",
        help="Service date YYYY-MM-DD or YYYY-MM (defaults to most recent rates).",
    ),
    kw: float = typer.Option(0.0, "--kw", help="Peak demand kW (demand-metered schedules)."),
    no_riders: bool = typer.Option(False, "--no-riders", help="Exclude Leaf 600 rider adders."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Reconstruct a Duke Energy NC bill for any rate schedule and usage period.

    Looks up base rates from ncuc_ingest_segments and rider adders from
    rider_summary_blocks, then prints a line-by-line bill breakdown.

    Examples:

      python -m duke_rates billing calculator RES 1000 --date 2024-11-01

      python -m duke_rates billing calculator MGS 5000 --kw 25 --date 2023-10-01
    """
    from duke_rates.db.ncuc_loader import calculate_bill
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    effective = date or "2099-12-31"  # if no date, get latest
    result = calculate_bill(
        conn,
        schedule_code=schedule,
        effective_date=effective,
        kwh=kwh,
        kw=kw or None,
        include_riders=not no_riders,
        breakdown=True,
    )
    conn.close()

    if json_out:
        typer.echo(json.dumps(result, indent=2))
        return

    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(1)

    typer.echo(f"\nDuke Energy NC — Bill Estimate")
    typer.echo(f"Schedule:       {result['schedule_code']}")
    typer.echo(f"Rate effective: {result['rate_effective_date']}")
    typer.echo(f"Billing date:   {result['billing_date']}")
    typer.echo(f"Usage:          {result['kwh']:,.1f} kWh" + (f"  /  {result['kw']:.1f} kW" if result.get("kw") else ""))
    typer.echo("")
    typer.echo(f"{'Line Item':<52} {'Rate':>10} {'Amount':>10}")
    typer.echo("-" * 74)

    rider_section_started = False
    for item in result.get("line_items", []):
        cat = item["category"]
        label = item["label"]
        rate_str = f"{item['rate']:>8.4f} {item['unit']}"
        amt_str = f"${item['amount']:>8.4f}"

        if cat == "riders_total" and not rider_section_started:
            typer.echo("")
            typer.echo("  --- Rider Adjustments (Leaf 600) ---")
            rider_section_started = True
            continue
        if cat == "rider":
            indent = "    " if not item.get("is_subtotal") else "  "
            code = f"[{item['rider_code']}]" if item.get("rider_code") else ""
            typer.echo(f"{indent}{label[:44]:<44} {code:<8} {item['rate']:>7.4f}¢  ${item['amount']:>8.4f}")
            continue

        typer.echo(f"  {label[:50]:<50} {item['rate']:>8.4f}  ${item['amount']:>8.4f}")

    typer.echo("-" * 74)
    typer.echo(f"  {'Base energy rate':<50} {result['base_energy_cents_per_kwh']:>7.4f}¢/kWh")
    typer.echo(f"  {'Rider adder':<50} {result['rider_cents_per_kwh']:>7.4f}¢/kWh")
    typer.echo(f"  {'All-in effective rate':<50} {result['total_cents_per_kwh']:>7.4f}¢/kWh")
    typer.echo(f"\n  ESTIMATED TOTAL (pre-tax):  ${result['total_amount']:>10.4f}")
    typer.echo("")


@billing_app.command("compare-schedules")
def compare_schedules_cmd(
    kwh: float = typer.Argument(..., help="kWh consumed in billing period."),
    date: str = typer.Option(
        "",
        "--date", "-d",
        help="Service date YYYY-MM-DD (defaults to most recent rates).",
    ),
    kw: float = typer.Option(0.0, "--kw", help="Peak demand kW."),
    schedules: str = typer.Option(
        "",
        "--schedules",
        help="Comma-separated schedule codes to compare (default: all available).",
    ),
    top: int = typer.Option(0, "--top", help="Show only top N cheapest (0 = all)."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Compare estimated bills across multiple Duke NC rate schedules.

    Shows all available schedules ranked cheapest-to-most-expensive for a
    given kWh usage and billing date.  Includes base rates + Leaf 600 riders.

    Examples:

      python -m duke_rates billing compare-schedules 1000 --date 2024-11-01

      python -m duke_rates billing compare-schedules 1000 --date 2024-11-01 --schedules RES,R-TOUD,R-TOU

      python -m duke_rates billing compare-schedules 5000 --kw 25 --date 2024-11-01 --top 5
    """
    from duke_rates.db.ncuc_loader import compare_schedules
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    effective = date or "2099-12-31"
    sched_list = [s.strip() for s in schedules.split(",") if s.strip()] or None

    results = compare_schedules(
        conn,
        effective_date=effective,
        kwh=kwh,
        kw=kw or None,
        schedules=sched_list,
    )
    conn.close()

    if not results:
        typer.echo("No rate data found for the given parameters.", err=True)
        raise typer.Exit(1)

    if top:
        results = results[:top]

    if json_out:
        typer.echo(json.dumps(results, indent=2))
        return

    typer.echo(f"\nDuke Energy NC — Rate Comparison  ({kwh:,.0f} kWh" + (f", {kw:.1f} kW" if kw else "") + f")  Date: {effective}")
    typer.echo(f"{'Rank':<5} {'Schedule':<22} {'Rate Date':<12} {'Base ¢/kWh':>11} {'Rider ¢/kWh':>12} {'All-in ¢/kWh':>13} {'Total $':>10}")
    typer.echo("-" * 88)
    for r in results:
        typer.echo(
            f"  {r['rank']:<3} {r['schedule_code']:<22} {r['rate_effective_date']:<12}"
            f" {r['base_energy_cents_per_kwh']:>10.4f}¢"
            f" {r['rider_cents_per_kwh']:>11.4f}¢"
            f" {r['total_cents_per_kwh']:>12.4f}¢"
            f" ${r['total_amount']:>9.2f}"
        )
    typer.echo("")


# Data commands

@data_app.command("lookup-openei")
def lookup_openei_rates(
    utility: str | None = typer.Option(
        None,
        help="OpenEI utility label/name, e.g. Duke Energy Progress.",
    ),
    state: str | None = typer.Option(None, help="Optional state filter, e.g. NC."),
    search_text: str | None = typer.Option(
        None,
        help="Optional text filter, e.g. RES or Residential Service.",
    ),
    label: str | None = typer.Option(
        None,
        help="Exact OpenEI USURDB label, e.g. 678abac33d12e18b730b0663.",
    ),
    url: str | None = typer.Option(
        None,
        help="OpenEI USURDB rate URL, from which the label will be extracted.",
    ),
    limit: int = typer.Option(25, help="Maximum number of OpenEI rate references to return."),
) -> None:
    settings, _ = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    if not utility and not label and not url:
        raise typer.BadParameter("Provide --utility, --label, or --url.")
    client = OpenEIClient(
        api_key=settings.openei_api_key,
        timeout=settings.request_timeout,
        user_agent=settings.user_agent,
        max_retries=settings.max_retries,
        rate_limit_seconds=settings.rate_limit_seconds,
    )
    try:
        if url:
            rows = client.lookup_rate_by_url(url)
        else:
            rows = client.lookup_rates(
                utility=utility,
                state=state,
                search_text=search_text,
                label=label,
                limit=limit,
            )
    finally:
        client.close()
    typer.echo(
        json.dumps([row.model_dump(mode="json") for row in rows], indent=2, default=str)
    )


@data_app.command("build-openei-export")
def build_openei_export(
    doc_id: int | None = typer.Option(None, help="Current document id with a parsed schedule."),
    historical_id: int | None = typer.Option(
        None,
        help="Historical document id with a parsed schedule.",
    ),
    openei_label: str | None = typer.Option(
        None,
        help="Optional OpenEI label to enrich the candidate.",
    ),
    openei_url: str | None = typer.Option(
        None,
        help="Optional OpenEI USURDB URL to enrich the candidate.",
    ),
) -> None:
    if not doc_id and not historical_id:
        raise typer.BadParameter("Provide --doc-id or --historical-id.")
    if doc_id and historical_id:
        raise typer.BadParameter("Provide only one of --doc-id or --historical-id.")

    settings, repository = _bootstrap()
    parse_result = None
    source_document = None
    historical_document = None

    if doc_id:
        source_document = repository.get_document(doc_id)
        if not source_document:
            raise typer.BadParameter(f"Document {doc_id} not found.")
        parse_result = repository.latest_parse_result(doc_id)
    else:
        historical_document = repository.get_historical_document(historical_id or 0)
        if not historical_document:
            raise typer.BadParameter(f"Historical document {historical_id} not found.")
        if historical_document.parsed_result_json:
            parse_result = DocumentParseResult.model_validate_json(
                historical_document.parsed_result_json
            )

    if not parse_result or not parse_result.schedule:
        raise typer.BadParameter("Selected record does not have a parsed schedule.")

    openei_reference = None
    if openei_label or openei_url:
        if not settings.openei_api_key:
            raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to enrich from OpenEI.")
        client = OpenEIClient(
            api_key=settings.openei_api_key,
            timeout=settings.request_timeout,
            user_agent=settings.user_agent,
            max_retries=settings.max_retries,
            rate_limit_seconds=settings.rate_limit_seconds,
        )
        try:
            if openei_url:
                refs = client.lookup_rate_by_url(openei_url)
            else:
                refs = client.lookup_rates(label=openei_label, limit=1)
        finally:
            client.close()
        openei_reference = refs[0] if refs else None

    candidate = build_openei_export_candidate(
        parse_result=parse_result,
        source_document=source_document,
        historical_document=historical_document,
        openei_reference=openei_reference,
    )
    typer.echo(json.dumps(candidate.model_dump(mode="json"), indent=2, default=str))


@data_app.command("export-urdb")
def export_urdb(
    family_key: str | None = typer.Option(
        None, "--family-key", "-f",
        help="Export a single tariff family key (e.g. 'nc-progress-leaf-502').",
    ),
    state: str | None = typer.Option(None, "--state", help="Filter by state code (e.g. NC)."),
    company: str | None = typer.Option(
        None, "--company", help="Filter by company slug (e.g. progress).",
    ),
    family_type: str = typer.Option(
        "rate_schedule", "--type",
        help="Family type: rate_schedule or rider.",
    ),
    min_confidence: float = typer.Option(
        0.7, "--min-confidence",
        help="Minimum charge confidence to include in bulk export (0–1).",
    ),
    output: str | None = typer.Option(
        None, "--output", "-o",
        help="Write JSON to this file path (default: print to stdout).",
    ),
    source_url: str | None = typer.Option(
        None, "--source-url",
        help="Override source URL in the export (optional).",
    ),
) -> None:
    """Export tariff data to URDB (OpenEI Utility Rate Database) JSON format.

    Outputs a curation-aid JSON suitable for manual review before submission
    to https://openei.org/apps/USURDB/.

    Examples:
        # Single schedule
        duke-rates data export-urdb --family-key nc-progress-leaf-502

        # All NC Progress rate schedules
        duke-rates data export-urdb --state NC --company progress

        # Write to file
        duke-rates data export-urdb --state NC --company progress -o nc_progress_urdb.json
    """
    import sqlite3

    from duke_rates.external.urdb_export import (
        export_bulk_to_urdb,
        export_family_to_urdb,
        records_to_json,
    )

    settings, _ = _bootstrap()
    conn = sqlite3.connect(str(settings.database_path))

    try:
        if family_key:
            record = export_family_to_urdb(conn, family_key, source_url=source_url)
            if record is None:
                typer.echo(
                    f"Family '{family_key}' not found or has no charges.", err=True
                )
                raise typer.Exit(1)
            result_json = records_to_json([record])
        else:
            if not state and not company:
                typer.echo(
                    "Provide --family-key for a single record, or --state/--company "
                    "for a bulk export.",
                    err=True,
                )
                raise typer.Exit(1)
            records = export_bulk_to_urdb(
                conn,
                state=state,
                company=company,
                family_type=family_type,
                min_confidence=min_confidence,
                source_url_prefix=source_url,
            )
            if not records:
                typer.echo(
                    f"No exportable records found for state={state} company={company}.",
                    err=True,
                )
                raise typer.Exit(1)
            typer.echo(
                f"Exported {len(records)} record(s).", err=True
            )
            result_json = records_to_json(records)
    finally:
        conn.close()

    if output:
        import pathlib
        pathlib.Path(output).write_text(result_json, encoding="utf-8")
        typer.echo(f"Written to {output}", err=True)
    else:
        typer.echo(result_json)


@data_app.command("load-eia-rates")
def load_eia_rates(
    csv_file: Path = typer.Option(
        None,
        "--csv",
        help="EIA avgprice_annual CSV file to load (optional; uses seed data if omitted).",
    ),
    replace: bool = typer.Option(False, "--replace", help="Overwrite existing rows."),
) -> None:
    """Load EIA Form 861 state average retail electricity rates into the database.

    Without --csv, loads bundled seed data covering NC + Southeast neighbors
    and US national average for 2010–2024.

    With --csv, loads a full EIA avgprice_annual.csv download:
      https://www.eia.gov/electricity/data/state/
    """
    from duke_rates.db.eia_loader import load_eia_csv, load_eia_seed
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    if csv_file:
        if not csv_file.exists():
            typer.echo(f"File not found: {csv_file}", err=True)
            raise typer.Exit(1)
        ins, skip = load_eia_csv(conn, csv_file, replace=replace)
        typer.echo(f"EIA CSV loaded: {ins} inserted, {skip} skipped  <- {csv_file}")
    else:
        ins, skip = load_eia_seed(conn)
        typer.echo(f"EIA seed data: {ins} inserted, {skip} skipped (NC + Southeast + US 2010-2024)")

    conn.close()


@data_app.command("nc-rate-context")
def nc_rate_context(
    year: int = typer.Argument(..., help="Year (e.g. 2024)."),
    sector: str = typer.Option("residential", "--sector", help="residential / commercial / industrial / all_sectors"),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC average retail electricity rate vs. Southeast neighbors and US average.

    Data sourced from EIA API v2 (populate with: duke-rates data eia-backfill --states NC SC VA TN GA US).

    Example:

      python -m duke_rates data nc-rate-context 2024
      python -m duke_rates data nc-rate-context 2023 --sector commercial
    """
    from duke_rates.db.eia_loader import get_nc_rate_context
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    ctx = get_nc_rate_context(conn, year, sector)
    conn.close()

    if json_out:
        typer.echo(json.dumps(ctx, indent=2))
        return

    def _fmt(v) -> str:
        return f"{v:.2f}¢/kWh" if v else "  N/A   "

    typer.echo(f"\nEIA Average Retail Electricity Prices — {year}  ({sector})")
    typer.echo("-" * 44)
    typer.echo(f"  North Carolina (NC):   {_fmt(ctx['nc'])}")
    typer.echo(f"  South Carolina (SC):   {_fmt(ctx['sc'])}")
    typer.echo(f"  Virginia       (VA):   {_fmt(ctx['va'])}")
    typer.echo(f"  Tennessee      (TN):   {_fmt(ctx['tn'])}")
    typer.echo(f"  Georgia        (GA):   {_fmt(ctx['ga'])}")
    typer.echo(f"  US National Avg:       {_fmt(ctx['us_avg'])}")
    typer.echo("-" * 44)
    if ctx["nc_vs_us_pct"] is not None:
        direction = "above" if ctx["nc_vs_us_pct"] >= 0 else "below"
        typer.echo(f"  NC is {abs(ctx['nc_vs_us_pct']):.1f}% {direction} the US average")
    if ctx["nc_rank_in_southeast"]:
        typer.echo(f"  NC ranks {ctx['nc_rank_in_southeast']} in the Southeast")
    typer.echo("")


# ---------------------------------------------------------------------------
# EIA API v2 integration commands
# ---------------------------------------------------------------------------


@data_app.command("eia-backfill")
def eia_backfill(
    states: list[str] = typer.Option(None, "--states", "-s", help="State codes to fetch (default: all 50+DC+US)"),
    start: str = typer.Option("2001", "--start", help="Start year (YYYY)"),
    end: str = typer.Option(None, "--end", help="End year (YYYY); default: latest available"),
    skip_generation: bool = typer.Option(False, "--skip-generation", help="Skip generation-by-fuel fetch"),
    skip_profiles: bool = typer.Option(False, "--skip-profiles", help="Skip state-profile-summary"),
    skip_capability: bool = typer.Option(False, "--skip-capability", help="Skip capability fetch"),
    skip_disposition: bool = typer.Option(False, "--skip-disposition", help="Skip source-disposition"),
    cache: bool = typer.Option(True, "--cache/--no-cache", help="Cache API responses locally"),
):
    """Fetch full EIA historical data for all states (one-time backfill).

    Populates eia_retail_sales, eia_generation_by_fuel, eia_state_profile_summary,
    eia_source_disposition, and eia_state_capability tables from the EIA API v2.
    Safe to re-run — all upserts are idempotent.

    Requires EIA_API_KEY in .env or environment.

    Examples::

        duke-rates data eia-backfill
        duke-rates data eia-backfill --states NC SC VA GA TN
        duke-rates data eia-backfill --skip-generation --start 2010
    """
    import json as _json
    from duke_rates.db.schema import migrate
    from duke_rates.eia.client import EIAClient
    from duke_rates.eia.endpoints import ALL_STATES, GENERATION_FUELS, RETAIL_SECTOR_ALL
    from duke_rates.eia.endpoints import (
        fetch_retail_sales, fetch_generation_by_fuel, fetch_state_profile_summary,
        fetch_state_source_disposition, fetch_state_capability,
    )
    from duke_rates.eia.loaders import (
        upsert_retail_sales, upsert_generation_by_fuel, upsert_state_profile_summary,
        upsert_source_disposition, upsert_state_capability,
    )
    from duke_rates.eia.references import seed_state_region_lookup, seed_market_structure_lookup
    from duke_rates.eia.transformers import (
        make_batch_id, transform_retail_sales, transform_generation_by_fuel,
        transform_state_profile_summary, transform_source_disposition, transform_state_capability,
    )

    settings = get_settings()
    if not settings.eia_api_key:
        typer.echo("ERROR: EIA_API_KEY not set in .env or environment", err=True)
        raise typer.Exit(1)

    conn = _open_db(settings)
    migrate(conn)

    cache_dir = settings.eia_cache_dir if cache else None
    client = EIAClient(
        api_key=settings.eia_api_key,
        cache_dir=cache_dir,
        request_delay=settings.eia_request_delay,
    )
    batch_id = make_batch_id()
    target_states = list(states) if states else ALL_STATES

    typer.echo(f"EIA backfill starting  batch={batch_id}  states={len(target_states)}  start={start}")

    ri, rs = seed_state_region_lookup(conn)
    mi, ms = seed_market_structure_lookup(conn)
    typer.echo(f"  reference tables: region {ri}+{rs}, market-structure {mi}+{ms}")

    raw = fetch_retail_sales(client, states=target_states, sectors=RETAIL_SECTOR_ALL, frequency="annual", start=start, end=end)
    ins, _ = upsert_retail_sales(conn, transform_retail_sales(raw, frequency="annual", batch_id=batch_id))
    typer.echo(f"  retail-sales annual:  {ins} rows upserted")

    raw_m = fetch_retail_sales(client, states=target_states, sectors=RETAIL_SECTOR_ALL, frequency="monthly", start=start, end=end)
    ins_m, _ = upsert_retail_sales(conn, transform_retail_sales(raw_m, frequency="monthly", batch_id=batch_id))
    typer.echo(f"  retail-sales monthly: {ins_m} rows upserted")

    if not skip_generation:
        raw_g = fetch_generation_by_fuel(client, states=[s for s in target_states if s != "US"], fuels=GENERATION_FUELS, sectors=["99"], frequency="annual", start=start, end=end)
        ins_g, _ = upsert_generation_by_fuel(conn, transform_generation_by_fuel(raw_g, frequency="annual", batch_id=batch_id))
        typer.echo(f"  generation-by-fuel:   {ins_g} rows upserted")

    if not skip_profiles:
        raw_p = fetch_state_profile_summary(client, states=[s for s in target_states if s != "US"], start=max(start, "2008"), end=end)
        ins_p, _ = upsert_state_profile_summary(conn, transform_state_profile_summary(raw_p, batch_id=batch_id))
        typer.echo(f"  state-profile-summary:{ins_p} rows upserted")

    if not skip_disposition:
        raw_d = fetch_state_source_disposition(client, states=[s for s in target_states if s != "US"], start="1990", end=end)
        ins_d, _ = upsert_source_disposition(conn, transform_source_disposition(raw_d, batch_id=batch_id))
        typer.echo(f"  source-disposition:   {ins_d} rows upserted")

    if not skip_capability:
        raw_c = fetch_state_capability(client, states=[s for s in target_states if s != "US"], energy_sources=["ALL","NG","NUC","HYC","WND","SOL","COL","PET"], start="1990", end=end)
        ins_c, _ = upsert_state_capability(conn, transform_state_capability(raw_c, batch_id=batch_id))
        typer.echo(f"  state-capability:     {ins_c} rows upserted")

    conn.close()
    typer.echo(f"EIA backfill complete  batch={batch_id}")


@data_app.command("eia-update")
def eia_update(
    states: list[str] = typer.Option(None, "--states", "-s", help="State codes (default: all)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be fetched; no writes"),
):
    """Incrementally update EIA tables with the latest data.

    Determines the last period already in each table and fetches only newer
    data.  Safe to run on a schedule (e.g., monthly).

    Examples::

        duke-rates data eia-update
        duke-rates data eia-update --states NC SC VA
        duke-rates data eia-update --dry-run
    """
    from duke_rates.db.schema import migrate
    from duke_rates.eia.client import EIAClient
    from duke_rates.eia.endpoints import ALL_STATES, GENERATION_FUELS, RETAIL_SECTOR_ALL
    from duke_rates.eia.endpoints import (
        fetch_retail_sales, fetch_generation_by_fuel, fetch_state_profile_summary,
        fetch_state_source_disposition, fetch_state_capability,
    )
    from duke_rates.eia.loaders import (
        upsert_retail_sales, upsert_generation_by_fuel, upsert_state_profile_summary,
        upsert_source_disposition, upsert_state_capability,
    )
    from duke_rates.eia.transformers import (
        make_batch_id, transform_retail_sales, transform_generation_by_fuel,
        transform_state_profile_summary, transform_source_disposition, transform_state_capability,
    )

    settings = get_settings()
    if not settings.eia_api_key:
        typer.echo("ERROR: EIA_API_KEY not set", err=True)
        raise typer.Exit(1)

    conn = _open_db(settings)
    migrate(conn)
    client = EIAClient(api_key=settings.eia_api_key, request_delay=settings.eia_request_delay)
    batch_id = make_batch_id()
    target_states = list(states) if states else ALL_STATES

    def _latest(table: str, freq: str) -> str | None:
        row = conn.execute(f"SELECT MAX(period) FROM {table} WHERE frequency=?", (freq,)).fetchone()
        return row[0] if row and row[0] else None

    def _start_from(latest: str | None, default: str, step_back: int = 1) -> str:
        if not latest:
            return default
        try:
            return str(int(str(latest)[:4]) - step_back)
        except ValueError:
            return default

    if dry_run:
        typer.echo("DRY RUN — no data will be written")

    for freq, default in [("annual", "2001"), ("monthly", "2001")]:
        latest = _latest("eia_retail_sales", freq)
        start = _start_from(latest, default)
        typer.echo(f"retail-sales {freq}: latest={latest or 'none'}, will fetch from {start}")
        if not dry_run:
            raw = fetch_retail_sales(client, states=target_states, sectors=RETAIL_SECTOR_ALL, frequency=freq, start=start)
            ins, _ = upsert_retail_sales(conn, transform_retail_sales(raw, frequency=freq, batch_id=batch_id))
            typer.echo(f"  -> {ins} rows upserted")

    conn.close()
    typer.echo("EIA update complete" if not dry_run else "Dry run complete")


@data_app.command("eia-state-price")
def eia_state_price(
    state: str = typer.Argument(..., help="2-letter state code (e.g. NC, TX, CA)"),
    sector: str = typer.Option("RES", "--sector", "-s", help="Sector: RES COM IND ALL"),
    years: int = typer.Option(10, "--years", "-y", help="Number of recent years to show"),
):
    """Show EIA retail price history for a state.

    Examples::

        duke-rates data eia-state-price NC
        duke-rates data eia-state-price TX --sector COM --years 5
    """
    settings = get_settings()
    conn = _open_db(settings)
    conn.row_factory = __import__("sqlite3").Row

    rows = conn.execute(
        """
        SELECT year, price_cents_per_kwh, sales_million_kwh, customers
        FROM eia_retail_sales
        WHERE state=? AND sector=? AND frequency='annual' AND year IS NOT NULL
        ORDER BY year DESC
        LIMIT ?
        """,
        (state.upper(), sector.upper(), years),
    ).fetchall()

    if not rows:
        typer.echo(f"No data found for {state.upper()} / {sector.upper()}. Run: duke-rates data eia-backfill")
        raise typer.Exit(1)

    typer.echo(f"\nEIA Retail Price — {state.upper()} / {sector.upper()}")
    typer.echo(f"{'Year':<6}  {'¢/kWh':>7}  {'Sales (M kWh)':>14}  {'Customers':>12}")
    typer.echo("-" * 46)
    for r in sorted(rows, key=lambda x: x["year"]):
        price = f"{r['price_cents_per_kwh']:.2f}" if r["price_cents_per_kwh"] else "  N/A"
        sales = f"{r['sales_million_kwh']:,.1f}" if r["sales_million_kwh"] else "N/A"
        cust = f"{r['customers']:,}" if r["customers"] else "N/A"
        typer.echo(f"{r['year']:<6}  {price:>7}  {sales:>14}  {cust:>12}")
    typer.echo("")
    conn.close()


@data_app.command("eia-national-comparison")
def eia_national_comparison(
    year: int = typer.Argument(..., help="Year to compare (e.g. 2024)"),
    sector: str = typer.Option("RES", "--sector", "-s", help="Sector: RES COM IND ALL"),
    top: int = typer.Option(10, "--top", help="Show top N cheapest and most expensive states"),
):
    """Show national price comparison: cheapest, most expensive, and NC context.

    Examples::

        duke-rates data eia-national-comparison 2024
        duke-rates data eia-national-comparison 2023 --sector COM --top 5
    """
    settings = get_settings()
    conn = _open_db(settings)
    conn.row_factory = __import__("sqlite3").Row

    rows = conn.execute(
        """
        SELECT r.state, r.state_name, r.price_cents_per_kwh,
               m.market_structure, m.rto
        FROM eia_retail_sales r
        LEFT JOIN eia_market_structure_lookup m ON m.state = r.state
        WHERE r.year=? AND r.sector=? AND r.frequency='annual'
          AND r.price_cents_per_kwh IS NOT NULL
          AND r.state NOT IN ('US','DC')
          AND length(r.state) = 2
        ORDER BY r.price_cents_per_kwh ASC
        """,
        (year, sector.upper()),
    ).fetchall()

    if not rows:
        typer.echo(f"No data for {year}/{sector}. Run: duke-rates data eia-backfill")
        raise typer.Exit(1)

    us_row = conn.execute(
        "SELECT price_cents_per_kwh FROM eia_retail_sales WHERE state='US' AND year=? AND sector=? AND frequency='annual'",
        (year, sector.upper()),
    ).fetchone()
    us_avg = us_row["price_cents_per_kwh"] if us_row else None

    typer.echo(f"\nEIA Retail Price Comparison — {year} / {sector.upper()}")
    if us_avg:
        typer.echo(f"  US National Average: {us_avg:.2f} ¢/kWh")
    typer.echo(f"\n  {'State':<6}  {'¢/kWh':>7}  {'vs US':>7}  {'Structure':<14}  {'RTO'}")
    typer.echo("  " + "-" * 58)

    def _line(r, label=""):
        price = r["price_cents_per_kwh"]
        delta = f"{price - us_avg:+.2f}" if us_avg else "  N/A"
        struct = r["market_structure"] or "?"
        rto = r["rto"] or "none"
        marker = " <--" if r["state"] == "NC" else ""
        typer.echo(f"  {r['state']:<6}  {price:>7.2f}  {delta:>7}  {struct:<14}  {rto}{marker}")

    typer.echo(f"\n  {top} CHEAPEST:")
    for r in rows[:top]:
        _line(r)

    typer.echo(f"\n  {top} MOST EXPENSIVE:")
    for r in rows[-top:]:
        _line(r)

    nc_rows = [r for r in rows if r["state"] == "NC"]
    if nc_rows:
        nc = nc_rows[0]
        nc_rank = next((i + 1 for i, r in enumerate(rows) if r["state"] == "NC"), None)
        typer.echo(f"\n  NC: rank {nc_rank} of {len(rows)} states  ({nc['price_cents_per_kwh']:.2f} ¢/kWh)")
    typer.echo("")
    conn.close()


# ---------------------------------------------------------------------------
# Tariff completeness audit commands
# ---------------------------------------------------------------------------


