"""Progress-NC sub-app: historical recovery, lead mining, search packs,
google dorks, regulator inbox, bill-relevant timeline, predecessor domain,
and OpenEI/archive.today probes for the Progress NC pipeline.

Wired into the main CLI as `duke-rates progress <command>`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from duke_rates.billing.calculators import UsageInput
from duke_rates.billing.engine import BillingEngine
from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.historical.archive_today import (
    ArchiveTodayClient,
    write_archive_today_markdown_report,
)
from duke_rates.historical.bill_relevant_gaps import ProgressNCBillRelevantGapService
from duke_rates.historical.citation_miner import HistoricalCitationMiner
from duke_rates.historical.family_crosswalk import ProgressNCFamilyCrosswalkService
from duke_rates.historical.family_targets import find_target_by_query
from duke_rates.historical.inbox import ProgressNCHistoricalInboxService
from duke_rates.historical.lead_registry import ProgressNCLeadRegistryService
from duke_rates.historical.lineage import ProgressNCLineageService
from duke_rates.historical.manual_import import ProgressNCHistoricalImportService
from duke_rates.historical.notice_links import ProgressNCNoticeLinkService
from duke_rates.historical.openei_progress_nc import (
    ProgressNCOpenEIHistoricalRecoveryService,
)
from duke_rates.historical.progress_nc import ProgressNCHistoricalRecoveryService
from duke_rates.historical.provenance import ProgressNCProvenanceService
from duke_rates.historical.public_notices import ProgressNCPublicNoticeRecoveryService
from duke_rates.historical.regulator_gaps import ProgressNCRegulatorGapService
from duke_rates.historical.regulator_leads import ProgressNCRegulatorLeadService
from duke_rates.historical.root_url_lists import ProgressNCRootUrlListService
from duke_rates.historical.search_packs import ProgressNCSearchPackService
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector
from duke_rates.historical.url_archaeology import ProgressNCUrlArchaeologyService
from duke_rates.models.document import DocumentCategory
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.jurisdiction import JurisdictionQuery

from duke_rates.cli_commands._cli_utils import _bootstrap, _read_usage_file, _safe_cli_text


progress_app = typer.Typer(help="Progress NC historical recovery, search, leads, regulator/openei/archive pipelines.")


# Helpers (only progress uses these)

def _parse_service_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter("Expected --service-date in YYYY-MM-DD format.") from exc


# Commands

@progress_app.command("recover-history")
def recover_history_progress_nc(
    limit_documents: int = typer.Option(
        15,
        help="How many current Progress NC PDFs to use as seeds.",
    ),
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        10,
        help="Max archived revisions per seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_documents=limit_documents,
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
        )
    finally:
        service.close()

    typer.echo(f"Recovered {len(recovered)} historical Progress NC documents.")
    for record in recovered:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.revision_label or "-",
                    "direct" if record.direct_downloadable else "archive-only",
                    record.title,
                ]
            )
        )


@progress_app.command("list-history")
def list_history_progress_nc() -> None:
    _, repository = _bootstrap()
    rows = repository.list_historical_documents(state="NC", company="progress")
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row.id or "-"),
                    row.leaf_no or "-",
                    row.effective_start or "-",
                    row.effective_end or "-",
                    row.revision_label or "-",
                    row.supersedes_label or "-",
                    "direct" if row.direct_downloadable else "archive-only",
                    row.title,
                ]
            )
        )


@progress_app.command("recover-public-notices")
def recover_public_notices_progress_nc(
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_page_snapshots: int = typer.Option(8, help="Max archived public-notice pages to inspect."),
    max_documents_per_snapshot: int = typer.Option(
        20,
        help="Max linked PDFs to process from each archived notice page.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCPublicNoticeRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            from_year=from_year,
            max_page_snapshots=max_page_snapshots,
            max_documents_per_snapshot=max_documents_per_snapshot,
        )
    finally:
        service.close()

    typer.echo(f"Recovered {len(recovered)} public-notice historical Progress NC documents.")
    for record in recovered:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.category,
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.revision_label or "-",
                    record.title,
                ]
            )
        )


@progress_app.command("list-history-chains")
def list_history_chains_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, revision label, or parsed schedule/rider id.",
    ),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCLineageService(repository)
    chains = service.build_chains(query=query, recovered_only=True)
    for chain in chains:
        latest = chain.versions[0]
        typer.echo(
            "\t".join(
                [
                    chain.family_key,
                    chain.leaf_no or "-",
                    chain.category,
                    str(len(chain.versions)),
                    latest.effective_start or "-",
                    latest.revision_label or "-",
                    chain.title,
                ]
            )
        )


@progress_app.command("preview-history-family-crosswalk")
def preview_history_family_crosswalk_progress_nc() -> None:
    _, repository = _bootstrap()
    matches = ProgressNCFamilyCrosswalkService(repository).preview()
    typer.echo(
        json.dumps(
            [match.model_dump(mode="json") for match in matches],
            indent=2,
            default=str,
        )
    )


@progress_app.command("apply-history-family-crosswalk")
def apply_history_family_crosswalk_progress_nc() -> None:
    _, repository = _bootstrap()
    matches = ProgressNCFamilyCrosswalkService(repository).apply()
    typer.echo(
        json.dumps(
            [match.model_dump(mode="json") for match in matches],
            indent=2,
            default=str,
        )
    )


@progress_app.command("show-history-chain")
def show_history_chain_progress_nc(query: str) -> None:
    _, repository = _bootstrap()
    service = ProgressNCLineageService(repository)
    chains = service.build_chains(query=query, recovered_only=True)
    if not chains:
        raise typer.BadParameter(f"No Progress NC historical chain matched query={query!r}")
    typer.echo(
        json.dumps(
            [chain.model_dump(mode="json") for chain in chains],
            indent=2,
            default=str,
        )
    )


@progress_app.command("list-history-notice-links")
def list_history_notice_links_progress_nc() -> None:
    _, repository = _bootstrap()
    links = ProgressNCNoticeLinkService(repository).build_links()
    for link in links:
        typer.echo(
            "\t".join(
                [
                    str(link.historical_id),
                    ",".join(link.docket_numbers) or "-",
                    ",".join(link.related_rider_codes) or "-",
                    ",".join(link.related_schedule_codes) or "-",
                    " | ".join(f"{match.basis}->{match.title}" for match in link.matches),
                    link.title,
                ]
            )
        )


@progress_app.command("show-history-tariff")
def show_history_tariff_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
) -> None:
    _, repository = _bootstrap()
    selection = ProgressNCHistoricalTariffSelector(repository).select_schedule(
        schedule_code=schedule_code,
        service_date=_parse_service_date(service_date),
    )
    typer.echo(json.dumps(selection.model_dump(mode="json"), indent=2, default=str))


@progress_app.command("estimate-history-bill")
def estimate_history_bill_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
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
    selection = ProgressNCHistoricalTariffSelector(repository).select_schedule(
        schedule_code=schedule_code,
        service_date=_parse_service_date(service_date),
    )
    estimate = BillingEngine().estimate(
        selection.schedule,
        usage,
        rider_parse_results=[
            rider.parse_result for rider in selection.riders if rider.parse_result
        ],
    )
    typer.echo(
        json.dumps(
            {
                "service_date": service_date,
                "selected_version": selection.version.model_dump(mode="json"),
                "applicable_riders": [
                    rider.model_dump(mode="json") for rider in selection.riders
                ],
                "supporting_notices": [
                    notice.model_dump(mode="json") for notice in selection.supporting_notices
                ],
                "unresolved_rider_codes": selection.unresolved_rider_codes,
                "future_rider_codes": selection.future_rider_codes,
                "bill_estimate": estimate.model_dump(mode="json"),
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("recover-history-gaps")
def recover_history_gaps_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
    limit_documents: int = typer.Option(
        12,
        help="How many current rider PDFs to use as Wayback seeds.",
    ),
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per rider seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    selector = ProgressNCHistoricalTariffSelector(repository)
    requested_date = _parse_service_date(service_date)
    before = selector.select_schedule(schedule_code=schedule_code, service_date=requested_date)
    gap_codes = sorted(
        {
            *before.future_rider_codes,
            *(rider.code for rider in before.riders if rider.status == "undated"),
        }
    )
    if not gap_codes:
        typer.echo(
            json.dumps(
                {
                    "service_date": service_date,
                    "schedule_code": schedule_code.upper(),
                    "gap_rider_codes": [],
                    "recovered": [],
                    "after": before.model_dump(mode="json"),
                },
                indent=2,
                default=str,
            )
        )
        raise typer.Exit()

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_documents=limit_documents,
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            categories={DocumentCategory.RIDER.value},
            target_rider_codes=set(gap_codes),
        )
    finally:
        service.close()

    after = ProgressNCHistoricalTariffSelector(repository).select_schedule(
        schedule_code=schedule_code,
        service_date=requested_date,
    )
    typer.echo(
        json.dumps(
            {
                "service_date": service_date,
                "schedule_code": schedule_code.upper(),
                "gap_rider_codes": gap_codes,
                "recovered": [record.model_dump(mode="json") for record in recovered],
                "remaining_future_rider_codes": after.future_rider_codes,
                "remaining_undated_rider_codes": [
                    rider.code for rider in after.riders if rider.status == "undated"
                ],
                "after": after.model_dump(mode="json"),
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("inspect-history-gaps")
def inspect_history_gaps_progress_nc(
    schedule_code: str = typer.Option(..., help="Schedule code such as RES or SGS."),
    service_date: str = typer.Option(..., help="Service date in YYYY-MM-DD format."),
    limit_documents: int = typer.Option(
        12,
        help="How many current rider PDFs to inspect as Wayback seeds.",
    ),
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per rider seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    selector = ProgressNCHistoricalTariffSelector(repository)
    requested_date = _parse_service_date(service_date)
    before = selector.select_schedule(schedule_code=schedule_code, service_date=requested_date)
    gap_codes = sorted(
        {
            *before.future_rider_codes,
            *(rider.code for rider in before.riders if rider.status == "undated"),
        }
    )

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview_targets(
            limit_documents=limit_documents,
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            categories={DocumentCategory.RIDER.value},
            target_rider_codes=set(gap_codes),
        )
    finally:
        service.close()

    typer.echo(
        json.dumps(
            {
                "service_date": service_date,
                "schedule_code": schedule_code.upper(),
                "gap_rider_codes": gap_codes,
                "preview": preview,
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("import-history")
def import_history_progress_nc(
    title: str = typer.Option(..., help="Human-readable document title."),
    category: str = typer.Option(
        ...,
        help="Document category such as rate, rider, public_notice, or other.",
    ),
    source_label: str = typer.Option(
        ...,
        help="Source label such as ncuc, ncuc-manual, or regulator-order.",
    ),
    source_authority: str | None = typer.Option(
        None,
        help="Optional authority classification such as regulator, utility, archive, or external.",
    ),
    source_type: str | None = typer.Option(
        None,
        help="Optional source type such as ncuc, fpsc, wayback, or manual-regulator-pdf.",
    ),
    url: str | None = typer.Option(None, help="Official public document URL."),
    file: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    docket_number: str | None = typer.Option(None, help="Optional docket reference."),
) -> None:
    if not url and not file:
        raise typer.BadParameter("Provide --url or --file.")
    settings, repository = _bootstrap()
    service = ProgressNCHistoricalImportService(settings, repository)
    try:
        record = service.import_document(
            title=title,
            category=category,
            source_label=source_label,
            source_authority=source_authority,
            source_type=source_type,
            source_url=url,
            local_file=file,
            docket_number=docket_number,
        )
    finally:
        service.close()
    typer.echo(json.dumps(record.model_dump(mode="json"), indent=2, default=str))


@progress_app.command("list-history-sources")
def list_history_sources_progress_nc() -> None:
    _, repository = _bootstrap()
    rows = ProgressNCProvenanceService(repository).list_historical_sources()
    for row, provenance in rows:
        typer.echo(
            "\t".join(
                [
                    str(row.id or "-"),
                    provenance.authority,
                    provenance.source_type,
                    provenance.docket_number or "-",
                    row.category,
                    row.leaf_no or "-",
                    row.effective_start or "-",
                    row.title,
                ]
            )
        )


@progress_app.command("show-history-coverage")
def show_history_coverage_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    _, repository = _bootstrap()
    coverage = ProgressNCProvenanceService(repository).build_chain_coverage(query=query)
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in coverage],
            indent=2,
            default=str,
        )
    )


@progress_app.command("list-regulator-gaps")
def list_regulator_gaps_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    _, repository = _bootstrap()
    gaps = ProgressNCRegulatorGapService(repository).build_gaps(query=query)
    for gap in gaps:
        typer.echo(
            "\t".join(
                [
                    str(gap.gap_priority),
                    gap.category,
                    gap.leaf_no or "-",
                    ",".join(gap.evidence_authorities) or "-",
                    ",".join(gap.suggested_dockets) or "-",
                    gap.title,
                ]
            )
        )


@progress_app.command("show-regulator-gaps")
def show_regulator_gaps_progress_nc(
    query: str | None = typer.Option(
        None,
        help="Filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    _, repository = _bootstrap()
    gaps = ProgressNCRegulatorGapService(repository).build_gaps(query=query)
    typer.echo(json.dumps([gap.model_dump(mode="json") for gap in gaps], indent=2, default=str))


@progress_app.command("parse-bill-relevant")
def parse_bill_relevant_progress_nc(
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-parse bill-relevant documents even if they already have a parse result.",
    ),
) -> None:
    _, repository = _bootstrap()
    records = ProgressNCBillRelevantGapService(repository).build_records()
    parsed = 0
    skipped = 0
    for record in records:
        if not force and record.parse_status is not None:
            skipped += 1
            continue
        from duke_rates.cli import _parse_document  # lazy
        _parse_document(record.current_document_id, repository)
        parsed += 1
    typer.echo(f"Parsed {parsed} bill-relevant Progress NC documents; skipped {skipped}.")


@progress_app.command("list-bill-relevant-gaps")
def list_bill_relevant_gaps_progress_nc() -> None:
    _, repository = _bootstrap()
    records = ProgressNCBillRelevantGapService(repository).build_records()
    for record in records:
        typer.echo(
            "\t".join(
                [
                    record.leaf_no,
                    record.category,
                    record.parse_status or "-",
                    str(record.historical_version_count),
                    ",".join(record.parsed_component_labels) or "-",
                    ",".join(record.gap_flags) or "-",
                    record.title,
                ]
            )
        )


@progress_app.command("show-bill-relevant-gaps")
def show_bill_relevant_gaps_progress_nc() -> None:
    _, repository = _bootstrap()
    records = ProgressNCBillRelevantGapService(repository).build_records()
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in records],
            indent=2,
            default=str,
        )
    )


@progress_app.command("mine-historical-leads")
def mine_historical_leads_progress_nc(
    source: str = typer.Option(
        "all",
        help="Lead source class: all, openei, notices, imported, regulator.",
    ),
    limit_references: int = typer.Option(
        120,
        help="Maximum OpenEI references to inspect when source includes openei.",
    ),
) -> None:
    settings, repository = _bootstrap()
    miner = HistoricalCitationMiner(settings, repository)
    regulator = ProgressNCRegulatorLeadService(settings, repository)
    created = []
    normalized_source = source.lower()
    if normalized_source in {"all", "openei"}:
        if not settings.openei_api_key:
            raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to mine OpenEI leads.")
        created.extend(
            miner.mine_openei_progress_nc(limit_references=limit_references, missing_only=True)
        )
    if normalized_source in {"all", "notices"}:
        created.extend(miner.mine_notice_archive_progress_nc())
    if normalized_source in {"all", "imported"}:
        created.extend(miner.mine_imported_documents_progress_nc())
    if normalized_source in {"all", "regulator"}:
        regulator.mine_existing_regulator_leads()
    typer.echo(
        json.dumps(
            {
                "source": normalized_source,
                "created_leads": len(created),
                "stored_historical_leads": len(repository.list_historical_leads()),
                "stored_docket_leads": len(repository.list_regulatory_docket_leads()),
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("ingest-manual-lead")
def ingest_manual_lead_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
    source_type: str = typer.Option(..., help="Source type, e.g. search_engine or dsire."),
    source_label: str = typer.Option(..., help="Short source label."),
    title: str = typer.Option(..., help="Lead title."),
    provenance_class: str = typer.Option(
        "external",
        help="Provenance class: regulator, external, reference, utility.",
    ),
    url: str | None = typer.Option(None, help="Optional discovered attachment/viewer URL."),
    text_file: Path | None = typer.Option(
        None,
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Optional text file containing citations or notes.",
    ),
    docket_number: str | None = typer.Option(None, help="Optional docket clue."),
) -> None:
    settings, repository = _bootstrap()
    miner = HistoricalCitationMiner(settings, repository)
    text = (
        text_file.read_text(encoding="utf-8", errors="ignore")
        if text_file
        else docket_number or ""
    )
    leads = miner.ingest_manual_lead(
        family_query=family_query,
        source_class=source_type,
        provenance_class=provenance_class,
        source_label=source_label,
        source_location=str(text_file) if text_file else url,
        source_url=url,
        text=text,
        title=title,
        docket_number=docket_number,
    )
    typer.echo(
        json.dumps([lead.model_dump(mode="json") for lead in leads], indent=2, default=str)
    )


@progress_app.command("score-historical-leads")
def score_historical_leads_progress_nc() -> None:
    _, repository = _bootstrap()
    result = ProgressNCLeadRegistryService(repository).rescore_all()
    typer.echo(json.dumps(result, indent=2, default=str))


@progress_app.command("list-historical-leads")
def list_historical_leads_progress_nc(
    family_query: str | None = typer.Option(None),
) -> None:
    _, repository = _bootstrap()
    family_key = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        family_key = target.family_key if target else family_query
    for lead in repository.list_historical_leads(family_key=family_key):
        typer.echo(
            "\t".join(
                [
                    str(lead.id or "-"),
                    lead.target_leaf_no or "-",
                    lead.target_code or "-",
                    f"{lead.confidence_score:.1f}",
                    lead.provenance_class,
                    lead.source_class,
                    lead.docket_number or "-",
                    lead.extracted_url or lead.source_url or "-",
                ]
            )
        )


@progress_app.command("preview-root-url-lists")
def preview_root_url_lists_progress_nc(
    family_query: str | None = typer.Option(None, help="Optional leaf/code/title filter."),
    include_noisy: bool = typer.Option(
        False,
        help="Include low-signal matches from the noisier root URL lists.",
    ),
    missing_only: bool = typer.Option(
        True,
        help="Restrict matching to families still missing historical coverage.",
    ),
    limit: int = typer.Option(50, help="Maximum number of leads to print."),
    file: list[Path] | None = typer.Option(
        None,
        "--file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Optional root URL list file(s) to scan.",
    ),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCRootUrlListService(repository)
    leads = service.preview_leads(
        file_paths=file,
        family_query=family_query,
        include_noisy=include_noisy,
        missing_only=missing_only,
        limit=limit,
    )
    if not leads:
        typer.echo("No plausible Progress NC historical URL leads found.")
        raise typer.Exit()
    for lead in leads:
        typer.echo(
            "\t".join(
                [
                    lead.target_leaf_no or "-",
                    lead.target_code or "-",
                    f"{lead.confidence_score:.1f}",
                    lead.source_label or "-",
                    lead.extracted_url or "-",
                ]
            )
        )


@progress_app.command("import-root-url-lists")
def import_root_url_lists_progress_nc(
    family_query: str | None = typer.Option(None, help="Optional leaf/code/title filter."),
    include_noisy: bool = typer.Option(
        False,
        help="Include low-signal matches from the noisier root URL lists.",
    ),
    missing_only: bool = typer.Option(
        True,
        help="Restrict matching to families still missing historical coverage.",
    ),
    min_score: float = typer.Option(
        45.0,
        help="Minimum lead score required before persisting.",
    ),
    limit: int | None = typer.Option(
        None,
        help="Optional maximum number of leads to persist.",
    ),
    file: list[Path] | None = typer.Option(
        None,
        "--file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Optional root URL list file(s) to scan.",
    ),
) -> None:
    _, repository = _bootstrap()
    service = ProgressNCRootUrlListService(repository)
    leads = service.import_leads(
        file_paths=file,
        family_query=family_query,
        include_noisy=include_noisy,
        missing_only=missing_only,
        min_score=min_score,
        limit=limit,
    )
    typer.echo(f"Imported {len(leads)} root-url historical leads.")
    for lead in leads[:20]:
        typer.echo(
            "\t".join(
                [
                    str(lead.id or "-"),
                    lead.target_leaf_no or "-",
                    lead.target_code or "-",
                    f"{lead.confidence_score:.1f}",
                    lead.extracted_url or "-",
                ]
            )
        )


@progress_app.command("generate-search-packs")
def generate_search_packs_progress_nc() -> None:
    _, repository = _bootstrap()
    packs = ProgressNCSearchPackService(repository).generate_missing_family_packs()
    typer.echo(f"Generated {len(packs)} search packs.")
    for pack in packs:
        typer.echo(
            "\t".join(
                [
                    pack.target_leaf_no or "-",
                    pack.target_code or "-",
                    pack.family_type,
                    pack.target_title,
                ]
            )
        )


@progress_app.command("list-search-packs")
def list_search_packs_progress_nc() -> None:
    _, repository = _bootstrap()
    for pack in repository.list_search_packs():
        typer.echo(
            "\t".join(
                [
                    pack.target_leaf_no or "-",
                    pack.target_code or "-",
                    pack.family_type,
                    pack.target_title,
                ]
            )
        )


@progress_app.command("show-search-pack")
def show_search_pack_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
) -> None:
    _, repository = _bootstrap()
    target = find_target_by_query(repository, family_query, missing_only=False)
    family_key = target.family_key if target else family_query
    pack = repository.get_search_pack(family_key)
    if not pack:
        raise typer.BadParameter(f"No search pack found for {family_query!r}.")
    typer.echo(json.dumps(json.loads(pack.payload_json), indent=2, default=str))


@progress_app.command("preview-google-dorks")
def preview_google_dorks_progress_nc(
    family_query: str | None = typer.Option(
        None, help="Limit to one family (leaf no., code, title, or family key)."
    ),
    strategy: str | None = typer.Option(
        None, help="Limit to one strategy (e.g. legacy_filename, ncuc_docket, boilerplate)."
    ),
    missing_only: bool = typer.Option(
        True, help="Only generate queries for families with no historical documents."
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON array of query strings."),
) -> None:
    """Preview Google Dork queries without executing them.

    Use this to inspect what would be searched before spending API quota.
    """
    from duke_rates.historical.dork_runner import export_queries_json, preview_queries

    _, repository = _bootstrap()
    family_keys: list[str] | None = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        if not target:
            raise typer.BadParameter(f"Family not found: {family_query!r}")
        family_keys = [target.family_key]

    strategies = [strategy] if strategy else None
    queries = preview_queries(
        repository,
        family_keys=family_keys,
        strategies=strategies,
        missing_only=missing_only,
    )

    if as_json:
        typer.echo(json.dumps([q.query for q in queries], indent=2))
    else:
        typer.echo(f"{len(queries)} queries across {len({q.family_key for q in queries})} families\n")
        for q in queries:
            typer.echo(f"  [{q.strategy:<25}] {q.family_key}  {q.query}")


@progress_app.command("run-google-dorks")
def run_google_dorks_progress_nc(
    family_query: str | None = typer.Option(
        None, help="Limit to one family (leaf no., code, title, or family key)."
    ),
    strategy: str | None = typer.Option(
        None, help="Limit to one strategy (legacy_filename, ncuc_docket, boilerplate, etc.)."
    ),
    missing_only: bool = typer.Option(
        True, help="Only query families with no historical documents."
    ),
    max_queries: int | None = typer.Option(
        None, help="Cap total API calls (default: unlimited)."
    ),
    max_results: int = typer.Option(
        10, help="Max results per query (1-30)."
    ),
    auto_import_threshold: float | None = typer.Option(
        None,
        "--auto-import",
        help="Confidence threshold (0-100) above which PDFs are automatically imported.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Log what would be saved without writing to the database."
    ),
) -> None:
    """Execute Google Dork queries via Custom Search API and store results as historical leads.

    Requires DUKE_RATES_GOOGLE_API_KEY and DUKE_RATES_GOOGLE_CSE_ID in .env.
    Free tier: 100 queries/day.

    Results are written to the historical_leads table with source_class='google_cse'.
    Use list-historical-leads-progress-nc to review them afterward.

    Examples:
        # Preview before spending quota:
        duke-rates preview-google-dorks-progress-nc --missing-only

        # Run all missing-family queries (up to 50), store leads:
        duke-rates run-google-dorks-progress-nc --max-queries 50

        # Run only legacy-filename strategy for one family:
        duke-rates run-google-dorks-progress-nc --family-query 503 --strategy legacy_filename

        # Run with auto-import of high-confidence PDF hits:
        duke-rates run-google-dorks-progress-nc --auto-import 75 --max-queries 20
    """
    from duke_rates.historical.dork_runner import ProgressNCDorkRunnerService

    settings, repository = _bootstrap()

    if settings.google_api_key and settings.google_cse_id:
        typer.echo("Search backend: Google CSE")
    else:
        typer.echo("Search backend: DuckDuckGo (free)")

    family_keys: list[str] | None = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        if not target:
            raise typer.BadParameter(f"Family not found: {family_query!r}")
        family_keys = [target.family_key]

    service = ProgressNCDorkRunnerService(settings, repository)
    result = service.run(
        family_keys=family_keys,
        missing_only=missing_only,
        strategies=[strategy] if strategy else None,
        max_queries=max_queries,
        max_results_per_query=min(max(max_results, 1), 30),
        min_confidence_for_import=auto_import_threshold,
        dry_run=dry_run,
    )

    prefix = "DRY-RUN " if dry_run else ""
    typer.echo(
        f"\n{prefix}Results:"
        f"\n  Queries run:    {result.queries_run}"
        f"\n  Leads {'logged' if dry_run else 'saved'}:     {result.leads_saved}"
        f"\n  Auto-imported:  {result.auto_imported}"
        f"\n  Quota exhausted: {result.quota_exhausted}"
    )
    if result.errors:
        typer.echo(f"\n  Errors ({len(result.errors)}):")
        for err in result.errors[:10]:
            typer.echo(f"    {err}")


@progress_app.command("export-google-dorks")
def export_google_dorks_progress_nc(
    missing_only: bool = typer.Option(
        True, help="Only export queries for families with no historical documents."
    ),
    family_query: str | None = typer.Option(
        None, help="Limit to one family (leaf no., code, title, or family key)."
    ),
    output: str | None = typer.Option(None, help="Write to file instead of stdout."),
) -> None:
    """Export all Google Dork query strings as a JSON array.

    Useful for manual execution in a browser, Google Custom Search UI,
    or for feeding into a third-party search tool.
    """
    from duke_rates.historical.dork_runner import export_queries_json

    _, repository = _bootstrap()
    family_keys: list[str] | None = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        if not target:
            raise typer.BadParameter(f"Family not found: {family_query!r}")
        family_keys = [target.family_key]

    queries = export_queries_json(repository, family_keys=family_keys, missing_only=missing_only)
    out = json.dumps(queries, indent=2)
    if output:
        Path(output).write_text(out, encoding="utf-8")
        typer.echo(f"Wrote {len(queries)} queries to {output}")
    else:
        typer.echo(out)


@progress_app.command("show-docket-leads")
def show_docket_leads_progress_nc(
    family_query: str | None = typer.Option(None, help="Optional family leaf/code/title query."),
) -> None:
    _, repository = _bootstrap()
    family_key = None
    if family_query:
        target = find_target_by_query(repository, family_query, missing_only=False)
        family_key = target.family_key if target else family_query
    rows = repository.list_regulatory_docket_leads(family_key=family_key)
    typer.echo(
        json.dumps([row.model_dump(mode="json") for row in rows], indent=2, default=str)
    )


@progress_app.command("list-unresolved-historical-families")
def list_unresolved_historical_families_progress_nc() -> None:
    _, repository = _bootstrap()
    gap_records = ProgressNCBillRelevantGapService(repository).build_records()
    for gap in gap_records:
        if "missing_historical_leaf" not in gap.gap_flags:
            continue
        target = find_target_by_query(repository, gap.leaf_no, missing_only=False)
        family_key = target.family_key if target else gap.leaf_no
        lead_count = len(repository.list_historical_leads(family_key=family_key))
        variant_count = len(repository.list_candidate_url_variants(family_key=family_key))
        docket_count = len(repository.list_regulatory_docket_leads(family_key=family_key))
        typer.echo(
            "\t".join(
                [
                    gap.leaf_no,
                    gap.primary_code or "-",
                    gap.category,
                    str(lead_count),
                    str(variant_count),
                    str(docket_count),
                    gap.title,
                ]
            )
        )


@progress_app.command("seed-family-documents")
def seed_family_documents_progress_nc(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print rows without inserting."),
) -> None:
    """Insert placeholder documents rows for Progress NC families that have no current document.

    Each family needs a documents entry with a leaf-no-NNN URL so the gap service and
    URL archaeology service can find and track it.  Already-present URLs are skipped.
    """
    from datetime import UTC, datetime

    from duke_rates.models.document import DiscoveryRecord, DocumentCategory, DocumentKind

    settings, repository = _bootstrap()

    # Map: leaf_no -> (title, category, code, predicted_current_url)
    # URLs use the /-/media/pdfs/for-your-home/rates/electric-nc/ pattern seen on current site.
    FAMILY_SEEDS = [
        ("501", "Residential Service Time-of-Use Schedule R-TOUD (Smart Usage Select Option)", "rate", "R-TOUD",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-501-schedule-r-toud.pdf"),
        ("503", "Residential Service Time-of-Use with Critical Peak Pricing Schedule R-TOU-CPP (Flex Savings Option)", "rate", "R-TOU-CPP",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-503-schedule-r-tou-cpp.pdf"),
        ("504", "Residential Service Pilot Time of Use with Discount Charging Period Schedule R-TOU-EV ( EV Overnight Advantage)", "rate", "R-TOU-EV",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-504-schedule-r-tou-ev.pdf"),
        ("571", "Street Lighting Service", "rate", "SLS",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-571-schedule-sls.pdf"),
        ("572", "Street Lighting Service - Residential Subdivisions", "rate", "SLR",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-572-schedule-slr.pdf"),
        ("607", "Storm Securitization Rider STS", "rider", "STS",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-607-rider-sts-ry1.pdf"),
        ("609", "Earnings Sharing Mechanism Rider ESM", "rider", "ESM",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-609-rider-esm-ry1.pdf"),
        ("613", "Storm Securitization Rider", "rider", "STS",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-613-rider-sts.pdf"),
        ("662", "Residential Service Equal Payment Plan (WeatherProtect) Pilot EPPWP", "rider", "EPPWP",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-662-rider-eppwp-ry1.pdf"),
        ("670", "Residential Solar Choice Rider RSC", "rider", "RSC",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-670-rider-rsc-ry1.pdf"),
        ("672", "Clean Energy Impact Rider", "rider", "CEI",
         "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/leaf-no-672-rider-cei.pdf"),
    ]

    inserted = 0
    skipped = 0
    for leaf_no, title, category_str, code, url in FAMILY_SEEDS:
        existing = [d for d in repository.list_documents() if str(d.document_url) == url]
        if existing:
            skipped += 1
            if dry_run:
                typer.echo(f"SKIP (exists) leaf={leaf_no} {title}")
            continue
        record = DiscoveryRecord(
            title=title,
            source_page_url="https://www.duke-energy.com/home/billing/rates?jur=NC",
            document_url=url,
            state="NC",
            company="progress",
            category=DocumentCategory(category_str),
            kind=DocumentKind.PDF,
            retrieval_timestamp=datetime.now(UTC),
            notes=[f"leaf_no={leaf_no}", f"code={code}", "seeded=family_seed"],
        )
        if dry_run:
            typer.echo(f"DRY-RUN leaf={leaf_no:3s} {category_str:5s} {code:8s} {url}")
        else:
            import sqlite3 as _sqlite3
            now = datetime.now(UTC).isoformat()
            with _sqlite3.connect(settings.database_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO documents
                       (title, source_page_url, document_url, state, company,
                        category, kind, local_path, content_hash,
                        discovered_at, retrieved_at, metadata_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        title,
                        "https://www.duke-energy.com/home/billing/rates?jur=NC",
                        url,
                        "NC",
                        "progress",
                        category_str,
                        "pdf",
                        "",   # empty string satisfies NOT NULL; no local file yet
                        "",   # same
                        now,
                        now,
                        json.dumps({"leaf_no": leaf_no, "code": code, "seeded": True}),
                    ),
                )
            typer.echo(f"Inserted leaf={leaf_no:3s} {title}")
            inserted += 1

    if not dry_run:
        typer.echo(f"\nDone: {inserted} inserted, {skipped} already present.")
    else:
        typer.echo(f"\nDry-run: {len(FAMILY_SEEDS) - skipped} would be inserted, {skipped} already present.")


@progress_app.command("preview-predecessor-domain")
def preview_predecessor_domain_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
    max_variants: int = typer.Option(
        40,
        help="Maximum candidate URL variants to probe.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCUrlArchaeologyService(settings, repository)
    try:
        rows = service.generate_variants_for_family(
            family_query,
            max_variants=max_variants,
        )
    finally:
        service.close()
    typer.echo(
        json.dumps([row.model_dump(mode="json") for row in rows[:50]], indent=2, default=str)
    )


@progress_app.command("recover-predecessor-domain")
def recover_predecessor_domain_progress_nc(
    family_query: str = typer.Option(..., help="Leaf number, code, title, or family key."),
    from_year: int = typer.Option(2010, help="Earliest Wayback year for candidate URLs."),
    max_variants: int = typer.Option(
        40,
        help="Maximum candidate URL variants to probe before attempting recovery.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCUrlArchaeologyService(settings, repository)
    try:
        recovered = service.recover_family(
            family_query,
            from_year=from_year,
            max_variants=max_variants,
        )
    finally:
        service.close()
    typer.echo(
        json.dumps(
            [row.model_dump(mode="json") for row in recovered],
            indent=2,
            default=str,
        )
    )


@progress_app.command("preview-bill-relevant-history")
def preview_bill_relevant_history_progress_nc(
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per bill-relevant seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    gap_records = ProgressNCBillRelevantGapService(repository).build_records()
    target_leafs = sorted(
        {
            record.leaf_no
            for record in gap_records
            if "missing_historical_leaf" in record.gap_flags
        }
    )
    if not target_leafs:
        typer.echo(
            json.dumps(
                {
                    "target_leafs": [],
                    "preview": [],
                    "message": "No missing bill-relevant historical leafs detected.",
                },
                indent=2,
                default=str,
            )
        )
        raise typer.Exit()

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview_targets(
            limit_documents=len(target_leafs),
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            target_leaf_numbers=set(target_leafs),
        )
    finally:
        service.close()

    typer.echo(
        json.dumps(
            {
                "target_leafs": target_leafs,
                "preview": preview,
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("recover-bill-relevant-history")
def recover_bill_relevant_history_progress_nc(
    from_year: int = typer.Option(2023, help="Earliest year to query from Wayback."),
    max_versions_per_document: int = typer.Option(
        8,
        help="Max archived revisions per bill-relevant seed PDF.",
    ),
) -> None:
    settings, repository = _bootstrap()
    gap_records = ProgressNCBillRelevantGapService(repository).build_records()
    target_leafs = sorted(
        {
            record.leaf_no
            for record in gap_records
            if "missing_historical_leaf" in record.gap_flags
        }
    )
    if not target_leafs:
        typer.echo(
            json.dumps(
                {
                    "target_leafs": [],
                    "recovered": [],
                    "remaining_missing_leafs": [],
                    "message": "No missing bill-relevant historical leafs detected.",
                },
                indent=2,
                default=str,
            )
        )
        raise typer.Exit()

    service = ProgressNCHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_documents=len(target_leafs),
            from_year=from_year,
            max_versions_per_document=max_versions_per_document,
            target_leaf_numbers=set(target_leafs),
        )
    finally:
        service.close()

    remaining_leafs = sorted(
        {
            record.leaf_no
            for record in ProgressNCBillRelevantGapService(repository).build_records()
            if "missing_historical_leaf" in record.gap_flags
        }
    )
    typer.echo(
        json.dumps(
            {
                "target_leafs": target_leafs,
                "recovered": [record.model_dump(mode="json") for record in recovered],
                "remaining_missing_leafs": remaining_leafs,
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("preview-bill-relevant-openei")
def preview_bill_relevant_openei_progress_nc(
    limit_references: int = typer.Option(
        80,
        help="Maximum number of Progress NC OpenEI references to inspect.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    target_keys = sorted(
        {
            record.primary_code
            for record in ProgressNCBillRelevantGapService(repository).build_records()
            if "missing_historical_leaf" in record.gap_flags and record.primary_code
        }
    )
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview(
            limit_references=limit_references,
            target_keys=set(target_keys),
        )
    finally:
        service.close()
    typer.echo(
        json.dumps(
            {
                "target_keys": target_keys,
                "preview": preview,
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("recover-bill-relevant-openei")
def recover_bill_relevant_openei_progress_nc(
    limit_references: int = typer.Option(
        80,
        help="Maximum number of Progress NC OpenEI references to inspect.",
    ),
    from_year: int = typer.Option(
        2010,
        help="Earliest year to query Wayback for old static Duke PDF paths.",
    ),
    max_wayback_snapshots: int = typer.Option(
        8,
        help="Maximum Wayback captures to try per OpenEI source URL.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    target_keys = sorted(
        {
            record.primary_code
            for record in ProgressNCBillRelevantGapService(repository).build_records()
            if "missing_historical_leaf" in record.gap_flags and record.primary_code
        }
    )
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_references=limit_references,
            from_year=from_year,
            max_wayback_snapshots=max_wayback_snapshots,
            target_keys=set(target_keys),
        )
    finally:
        service.close()
    remaining = ProgressNCBillRelevantGapService(repository).build_records()
    typer.echo(
        json.dumps(
            {
                "target_keys": target_keys,
                "recovered": [record.model_dump(mode="json") for record in recovered],
                "remaining_missing_leafs": sorted(
                    {
                        record.leaf_no
                        for record in remaining
                        if "missing_historical_leaf" in record.gap_flags
                    }
                ),
            },
            indent=2,
            default=str,
        )
    )


@progress_app.command("generate-regulator-inbox")
def generate_regulator_inbox_progress_nc(
    output: Path = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.jsonl"),
        help="Path to write the JSONL inbox manifest.",
    ),
    query: str | None = typer.Option(
        None,
        help="Optional filter by title, leaf number, schedule/rider id, or revision label.",
    ),
) -> None:
    settings, repository = _bootstrap()
    count = ProgressNCHistoricalInboxService(settings, repository).generate_regulator_manifest(
        output_path=output,
        query=query,
    )
    typer.echo(f"Wrote {count} regulator target rows to {output}")


@progress_app.command("import-history-inbox")
def import_history_inbox_progress_nc(
    manifest: Path = typer.Option(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="JSONL manifest of manually collected historical documents.",
    ),
) -> None:
    settings, repository = _bootstrap()
    imported = ProgressNCHistoricalInboxService(settings, repository).import_manifest(manifest)
    typer.echo(f"Imported {len(imported)} historical documents from {manifest}.")
    for record in imported:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.category,
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.title,
                ]
            )
        )


@progress_app.command("export-history-inbox")
def export_history_inbox_progress_nc(
    manifest: Path = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.jsonl"),
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="JSONL inbox manifest to export.",
    ),
    csv_output: Path | None = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.csv"),
        help="Optional CSV output path.",
    ),
    markdown_output: Path | None = typer.Option(
        Path("data/history_inbox/progress_nc/regulator_targets.md"),
        help="Optional Markdown output path.",
    ),
) -> None:
    settings, repository = _bootstrap()
    service = ProgressNCHistoricalInboxService(settings, repository)
    if csv_output:
        count = service.export_manifest_csv(manifest_path=manifest, output_path=csv_output)
        typer.echo(f"Wrote {count} rows to {csv_output}")
    if markdown_output:
        count = service.export_manifest_markdown(
            manifest_path=manifest,
            output_path=markdown_output,
        )
        typer.echo(f"Wrote {count} rows to {markdown_output}")


@progress_app.command("preview-openei-history")
def preview_openei_history_progress_nc(
    limit_references: int = typer.Option(
        25,
        help="Maximum number of Progress NC OpenEI references to preview.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        preview = service.preview(limit_references=limit_references)
    finally:
        service.close()
    typer.echo(json.dumps(preview, indent=2, default=str))


@progress_app.command("recover-openei-history")
def recover_openei_history_progress_nc(
    limit_references: int = typer.Option(
        25,
        help="Maximum number of Progress NC OpenEI references to recover.",
    ),
    from_year: int = typer.Option(
        2010,
        help="Earliest year to query Wayback for old static Duke PDF paths.",
    ),
    max_wayback_snapshots: int = typer.Option(
        8,
        help="Maximum Wayback captures to try per OpenEI source URL.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
    service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    try:
        recovered = service.recover(
            limit_references=limit_references,
            from_year=from_year,
            max_wayback_snapshots=max_wayback_snapshots,
        )
    finally:
        service.close()
    typer.echo(f"Recovered {len(recovered)} Progress NC historical OpenEI references.")
    for record in recovered:
        typer.echo(
            "\t".join(
                [
                    str(record.id or "-"),
                    record.category,
                    record.leaf_no or "-",
                    record.effective_start or "-",
                    record.revision_label or "-",
                    "direct" if record.direct_downloadable else "archive-only",
                    record.title,
                ]
            )
        )


@progress_app.command("probe-archive-today")
def probe_archive_today_progress_nc(
    limit_references: int = typer.Option(
        12,
        help="How many Progress NC OpenEI-discovered Duke URLs to probe.",
    ),
    markdown_output: Path | None = typer.Option(
        Path("data/history_inbox/progress_nc/archive_today_probe.md"),
        help="Optional Markdown report output path.",
    ),
) -> None:
    settings, repository = _bootstrap()
    if not settings.openei_api_key:
        raise typer.BadParameter("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")

    openei_service = ProgressNCOpenEIHistoricalRecoveryService(settings, repository)
    archive_client = ArchiveTodayClient(
        timeout=settings.request_timeout,
        user_agent=settings.user_agent,
    )
    try:
        preview = openei_service.preview(limit_references=limit_references)
        results = [
            archive_client.probe(
                title=str(item["title"]),
                source_url=str(item["source_url"]),
            )
            for item in preview
        ]
    finally:
        archive_client.close()
        openei_service.close()

    if markdown_output:
        write_archive_today_markdown_report(results=results, output_path=markdown_output)
        typer.echo(f"Wrote {len(results)} rows to {markdown_output}")
    typer.echo(json.dumps([result.model_dump(mode="json") for result in results], indent=2))


