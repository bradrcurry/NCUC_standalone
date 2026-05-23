"""NCUC sub-app: search, discovery, fetch/import, portal, wayback,
docket inventory, and pending-rates commands for the NCUC (North
Carolina Utilities Commission) data pipeline.

Wired into the main CLI as `duke-rates ncuc <command>`.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect as connect_sqlite

from duke_rates.cli_commands._cli_utils import _bootstrap, _safe_cli_text


ncuc_app = typer.Typer(help="NCUC portal/search/wayback/docket pipeline.")


# Helpers (only ncuc commands use these)

def _pick_best_ncuc_docket_match(matches: list[dict[str, str]]) -> dict[str, str] | None:
    if not matches:
        return None
    match_rank = {
        "exact": 0,
        "normalized_exact": 1,
        "same_base_and_sub": 2,
        "partial": 3,
    }
    return sorted(
        matches,
        key=lambda item: (match_rank.get(str(item.get("match_type") or ""), 9), item.get("docket_number") or ""),
    )[0]


def _print_ncuc_docket_documents(
    docs: list[dict[str, Any]],
    *,
    top_n: int = 50,
) -> None:
    display_count = len(docs) if top_n <= 0 else min(len(docs), top_n)
    for i, doc in enumerate(docs[:display_count], start=1):
        typer.echo(
            f"  [{i:02d}] {doc['doc_type']:12s} {doc['date_filed'] or '?':12s} "
            f"{doc['description'][:70]}"
        )
        if doc["view_file_urls"]:
            typer.echo(f"        Files: {len(doc['view_file_urls'])}")
    if display_count < len(docs):
        typer.echo(f"  ... and {len(docs) - display_count} more documents")


# Commands

@ncuc_app.command("seed-discover")
def ncuc_seed_discover(
    max_per_docket: int = typer.Option(
        50, help="Maximum discovery records to collect per seed docket."
    ),
    docket: str | None = typer.Option(
        None,
        help="Override: discover a single docket (e.g. 'E-2, Sub 1142'). "
        "If omitted, uses built-in Duke Progress seed list.",
    ),
    dry_run: bool = typer.Option(False, help="Print discovered records but do not persist."),
) -> None:
    """Discover NCUC documents from seeded docket list via eDocket portal."""
    from duke_rates.historical.ncuc.discovery import (
        DUKE_PROGRESS_E2_DOCKETS,
        NcucDiscoveryService,
    )
    from duke_rates.models.ncuc import NcucDocketSeed

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        seeds = (
            [NcucDocketSeed(docket_number=docket)]
            if docket
            else DUKE_PROGRESS_E2_DOCKETS
        )
        count = 0
        for result in svc.discover_from_seed_dockets(seeds, max_per_docket=max_per_docket):
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number} "
                f"status={rec.fetch_status.value} "
                f"title={rec.filing_title or '(unknown)'}"
            )
            count += 1
        typer.echo(f"\nDiscovered {count} NCUC records.")
    finally:
        svc.close()


@ncuc_app.command("search")
def ncuc_search(
    query: str = typer.Argument(..., help="Search text (e.g. 'Duke Energy Progress rate schedule 605')."),
    docket_hint: str | None = typer.Option(None, help="Docket number hint (e.g. 'E-2')."),
    family_query: str | None = typer.Option(None, help="Family key to link results to (e.g. '605')."),
    max_results: int = typer.Option(100, help="Maximum results to return."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Search NCUC eDocket portal by keyword and persist discovered leads."""
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucSearchQuery

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        q = NcucSearchQuery(
            query_text=query,
            docket_hint=docket_hint,
            family_key_hint=family_query,
        )
        count = 0
        for result in svc.search_edocket_keyword(q, max_results=max_results):
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number} "
                f"title={rec.filing_title or '(none)'} "
                f"url={rec.discovered_url or ''}"
            )
            count += 1
        typer.echo(f"\nFound {count} NCUC records for query: {query!r}")
    finally:
        svc.close()


@ncuc_app.command("smart-search")
def ncuc_smart_search(
    family_key: str | None = typer.Option(None, help="Family key to seed queries from (e.g. nc-progress-leaf-602)."),
    leaf_no: str | None = typer.Option(None, help="Specific leaf number to search for (e.g. 602)."),
    rider_code: str | None = typer.Option(None, help="Rider code to search for (e.g. JAA, STS)."),
    tier: str = typer.Option("T1,T2", help="Comma-separated quality tiers to use as HQ signal sources."),
    max_results: int = typer.Option(50, help="Maximum results per query."),
    dry_run: bool = typer.Option(False, help="Print queries and results without persisting."),
) -> None:
    """Build and run high-confidence NCUC searches seeded from known T1/T2 HQ documents.

    Loads known high-quality document titles from ncuc_discovery_records (filtered by
    quality tier), extracts structural tokens (leaf numbers, rider codes, revision labels),
    and builds targeted search queries that are much more likely to find clean compliance
    tariff sheets rather than redline/procedural documents.

    Examples:
        duke-rates ncuc smart-search --family-key nc-progress-leaf-602
        duke-rates ncuc smart-search --rider-code JAA --dry-run
        duke-rates ncuc smart-search --leaf-no 607 --tier T1
    """
    import sqlite3
    from duke_rates.historical.ncuc.query_builder import QueryBuilder
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.config import Settings

    settings, repository = _bootstrap()
    db_path = settings.database_path

    # Load HQ documents from DB
    allowed_tiers = {t.strip() for t in tier.split(",")}
    hq_docs: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        clauses = ["doc_quality_tier IN ({})".format(",".join("?" * len(allowed_tiers)))]
        params: list = list(allowed_tiers)
        if family_key:
            clauses.append("family_keys_json LIKE ?")
            params.append(f"%{family_key}%")
        if leaf_no:
            clauses.append("referenced_leaf_nos_json LIKE ?")
            params.append(f"%{leaf_no}%")
        if rider_code:
            clauses.append(
                "(filing_title LIKE ? OR referenced_rider_codes_json LIKE ?)"
            )
            params += [f"%{rider_code}%", f"%{rider_code}%"]
        where = " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT filing_title, utility, docket_number, "
            f"referenced_leaf_nos_json, referenced_rider_codes_json, family_keys_json "
            f"FROM ncuc_discovery_records WHERE {where} AND filing_title IS NOT NULL",
            params,
        ).fetchall()
        import json as _json
        for row in rows:
            hq_docs.append({
                "filing_title": row["filing_title"],
                "utility": row["utility"],
                "docket_number": row["docket_number"],
                "referenced_leaf_nos": _json.loads(row["referenced_leaf_nos_json"] or "[]"),
                "referenced_rider_codes": _json.loads(row["referenced_rider_codes_json"] or "[]"),
            })

    if not hq_docs:
        typer.echo(
            f"No HQ documents found for tiers={tier}"
            + (f" family={family_key}" if family_key else "")
            + (f" leaf={leaf_no}" if leaf_no else "")
            + (f" rider={rider_code}" if rider_code else ""),
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Loaded {len(hq_docs)} HQ document(s) as signal source.")

    builder = QueryBuilder(settings)
    queries = builder.build_hq_signal_queries(hq_docs)
    queries.sort(key=lambda q: -q.priority)

    typer.echo(f"Generated {len(queries)} targeted queries (top 10):")
    for q in queries[:10]:
        typer.echo(f"  [{q.priority:.1f}] [{q.template_name}] {q.query_text!r}")

    if dry_run:
        typer.echo("\n(dry-run — no searches executed)")
        return

    svc = NcucDiscoveryService(settings)
    try:
        total_new = 0
        for q_spec in queries:
            ncuc_query = q_spec.to_ncuc_query()
            count = 0
            for result in svc.search_edocket_keyword(ncuc_query, max_results=max_results):
                rec = result.record
                # Attach search ideality from scorer if available
                if hasattr(result, "ideality") and result.ideality:
                    rec = rec.model_copy(update={
                        "search_confidence_score": result.ideality.confidence,
                        "search_ideality": (
                            "ideal" if result.ideality.is_ideal_candidate else "probable"
                        ),
                    })
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                count += 1
                total_new += 1
            typer.echo(f"  {q_spec.query_text!r}: {count} results")
        typer.echo(f"\nTotal upserted: {total_new} records.")
    finally:
        svc.close()


@ncuc_app.command("ingest-url")
def ncuc_ingest_url(
    url: str = typer.Argument(..., help="NCUC viewer or document URL to ingest."),
    title: str | None = typer.Option(None, help="Filing title override."),
    docket: str | None = typer.Option(None, help="Docket number (e.g. 'E-2, Sub 1142')."),
    notes: str | None = typer.Option(None, help="Comma-separated provenance notes."),
    method: str = typer.Option(
        "search_engine",
        help="Acquisition method: search_engine | manual_seed | direct_http.",
    ),
) -> None:
    """Ingest a single externally-discovered NCUC document URL as a discovery lead."""
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucAcquisitionMethod

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        acq_method = NcucAcquisitionMethod(method)
        record = svc.ingest_discovered_url(
            url,
            title=title,
            docket_hint=docket,
            notes=notes.split(",") if notes else [],
            acquisition_method=acq_method,
        )
        rec_id = repository.upsert_ncuc_discovery_record(record)
        typer.echo(f"Ingested NCUC URL as record id={rec_id}")
        typer.echo(f"  docket={record.docket_number}  classification={record.filing_classification.value}")
        typer.echo(f"  schedule_codes={record.referenced_schedule_codes}")
        typer.echo(f"  fetch_status={record.fetch_status.value}")
    finally:
        svc.close()


@ncuc_app.command("fetch")
def ncuc_fetch(
    record_id: int | None = typer.Option(
        None, help="Fetch a specific NCUC discovery record by id."
    ),
    pending: bool = typer.Option(False, help="Fetch all pending records."),
    retry_failed: bool = typer.Option(False, help="Retry all previously failed records."),
    limit: int = typer.Option(20, help="Maximum records to fetch in batch mode."),
    playwright: bool = typer.Option(
        False, help="Force Playwright browser for this fetch (overrides HTTP)."
    ),
) -> None:
    """Download NCUC documents: fetch specific record, all pending, or retry failed."""
    from duke_rates.historical.ncuc.downloader import NcucDownloader

    settings, repository = _bootstrap()
    dl = NcucDownloader(settings, repository)
    try:
        if record_id is not None:
            rec = repository.get_ncuc_discovery_record(record_id)
            if not rec:
                raise typer.BadParameter(f"NCUC record {record_id} not found.")
            if playwright:
                # Force playwright by clearing download_url to trigger viewer resolution
                from duke_rates.historical.ncuc.downloader import NcucDownloader as _DL
                url = rec.viewer_url or rec.download_url or rec.discovered_url
                if url:
                    content, ct, fu = dl._playwright_fetch(url)
                    if content:
                        from duke_rates.historical.ncuc.downloader import _build_ncuc_path
                        from duke_rates.download.hashing import sha256_bytes
                        from datetime import UTC, datetime as _dt
                        suffix = ".pdf" if "pdf" in ct.lower() else ".bin"
                        path = _build_ncuc_path(settings.raw_dir, rec, suffix)
                        path.write_bytes(content)
                        ch = sha256_bytes(content)
                        repository.mark_ncuc_fetch_status(
                            record_id,
                            status=repository._row_to_ncuc_discovery_record(
                                repository._connect().execute(
                                    "SELECT * FROM ncuc_discovery_records WHERE id=?",
                                    (record_id,),
                                ).fetchone()
                            ).fetch_status.__class__.SUCCESS,
                            local_path=str(path),
                            content_hash=ch,
                            content_type=ct,
                            file_size_bytes=len(content),
                        )
                        typer.echo(f"Playwright download -&gt; {path}")
                    else:
                        typer.echo("Playwright fetch returned no PDF content.")
                return
            result = dl.fetch(rec)
            typer.echo(f"Fetch complete: status={result.fetch_status.value} path={result.local_path}")
        elif pending:
            results = dl.fetch_pending(limit=limit)
            ok = sum(1 for r in results if r.fetch_status.value == "success")
            typer.echo(f"Fetched {len(results)} pending records: {ok} succeeded.")
        elif retry_failed:
            results = dl.retry_failed(limit=limit)
            ok = sum(1 for r in results if r.fetch_status.value == "success")
            typer.echo(f"Retried {len(results)} failed records: {ok} now succeeded.")
        else:
            typer.echo("Specify --record-id, --pending, or --retry-failed.")
    finally:
        dl.close()


@ncuc_app.command("fetch-portal")
def ncuc_fetch_portal(
    limit: int = typer.Option(50, help="Maximum records to fetch per run."),
    dep_only: bool = typer.Option(False, "--dep-only", help="Only fetch Duke Energy Progress records."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Also retry previously failed portal records."),
) -> None:
    """Fetch pending NCUC portal document-detail records using authenticated Playwright."""
    from duke_rates.historical.ncuc.document_param_search import fetch_document_detail
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        download_view_file,
    )
    from duke_rates.download.hashing import sha256_bytes
    from duke_rates.utils.files import ensure_parent
    from duke_rates.utils.text import slugify
    from duke_rates.models.ncuc import NcucFetchStatus
    import datetime as _dt

    settings, repository = _bootstrap()

    statuses = ["pending"]
    if retry_failed:
        statuses.append("failed")

    all_records = []
    for status in statuses:
        all_records.extend(
            repository.list_ncuc_discovery_records(fetch_status=status)
        )

    # Only records that have a starw1 document-detail URL and a title
    # (title indicates they came from the search pipeline ingest, not legacy blank records)
    portal_records = [
        r for r in all_records
        if r.discovered_url and "PSCDocumentDetailsPageNCUC" in r.discovered_url
        and r.filing_title  # skip legacy blank-title records that lack ViewFile links
    ]
    if dep_only:
        portal_records = [
            r for r in portal_records
            if "Duke Energy Progress" in (r.filing_title or "") or
               "DEP" in (r.filing_title or "")[:20] or
               (r.docket_number or "").startswith("E-2 Sub") or
               (r.docket_number or "").startswith("E-2, Sub")
        ]
    # Sort by id descending so newest pipeline records are fetched first
    portal_records.sort(key=lambda r: r.id or 0, reverse=True)
    portal_records = portal_records[:limit]

    if not portal_records:
        typer.echo("No portal document-detail records pending fetch.")
        return

    typer.echo(f"Fetching {len(portal_records)} portal document-detail records via authenticated session...")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Authentication failed: {exc}")
        raise typer.Exit(1)

    succeeded = 0
    failed = 0
    try:
        for rec in portal_records:
            detail_url = rec.discovered_url
            typer.echo(f"  [{rec.id}] {(rec.filing_title or detail_url)[:60]}")
            try:
                detail = fetch_document_detail(page, detail_url)
                view_urls = detail.get("view_file_urls", [])
                if not view_urls:
                    typer.echo(f"    -> no view-file links found on detail page")
                    repository.mark_ncuc_fetch_status(
                        rec.id,
                        status=NcucFetchStatus.FAILED,
                        error_detail="no_view_file_links",
                    )
                    failed += 1
                    continue

                # Download via authenticated browser session (handles ViewFile.aspx)
                pdf_url = view_urls[0]
                typer.echo(f"    -> downloading {pdf_url[:70]}")

                docket_slug = slugify(rec.docket_number or "unknown-docket")
                title_slug = slugify((rec.filing_title or "document")[:80])
                date_part = (rec.filing_date or "nodate").replace("-", "")[:8]
                filename = f"{docket_slug}-{date_part}-{title_slug}.pdf"
                dest = ensure_parent(
                    settings.raw_dir / "historical" / "ncuc" / docket_slug / filename
                )

                size = download_view_file(page, pdf_url, dest)
                if size == 0:
                    typer.echo(f"    -> empty file downloaded")
                    repository.mark_ncuc_fetch_status(
                        rec.id,
                        status=NcucFetchStatus.FAILED,
                        error_detail="empty_file",
                    )
                    failed += 1
                    continue

                content_hash = sha256_bytes(dest.read_bytes())
                repository.mark_ncuc_fetch_status(
                    rec.id,
                    status=NcucFetchStatus.SUCCESS,
                    local_path=str(dest),
                    content_hash=content_hash,
                    file_size_bytes=size,
                    fetched_at=_dt.datetime.now(_dt.timezone.utc),
                )
                typer.echo(f"    -> saved {dest.name} ({size:,} bytes)")
                succeeded += 1

            except Exception as exc:
                typer.echo(f"    -> error: {exc}")
                repository.mark_ncuc_fetch_status(
                    rec.id,
                    status=NcucFetchStatus.FAILED,
                    error_detail=str(exc)[:200],
                )
                failed += 1
    finally:
        close_authenticated_context(pw, ctx)

    typer.echo(f"\nDone: {succeeded} downloaded, {failed} failed.")
    if succeeded:
        typer.echo("Run 'ncuc-content-mine' then 'ncuc import-pipeline' to process downloads.")


@ncuc_app.command("list")
def ncuc_list(
    docket: str | None = typer.Option(None, help="Filter by docket number."),
    status: str | None = typer.Option(
        None, help="Filter by fetch status: pending|success|failed|requires_browser."
    ),
    family_query: str | None = typer.Option(None, help="Filter by family key substring."),
    limit: int = typer.Option(50, help="Maximum rows to display."),
    show_urls: bool = typer.Option(False, help="Show discovered URLs in output."),
) -> None:
    """List NCUC discovery records."""
    settings, repository = _bootstrap()
    records = repository.list_ncuc_discovery_records(
        docket_number=docket,
        fetch_status=status,
        family_key=family_query,
    )
    typer.echo(f"{'ID':>5}  {'STATUS':18}  {'DOCKET':20}  {'DATE':10}  TITLE")
    typer.echo("-" * 90)
    for rec in records[:limit]:
        row = (
            f"{rec.id or 0:>5}  "
            f"{rec.fetch_status.value:18}  "
            f"{(rec.docket_number or ''):20}  "
            f"{(rec.filing_date or ''):10}  "
            f"{(rec.filing_title or '(no title)')[:50]}"
        )
        typer.echo(row)
        if show_urls and rec.discovered_url:
            typer.echo(f"       url: {rec.discovered_url}")
    if len(records) > limit:
        typer.echo(f"... ({len(records) - limit} more records not shown)")
    typer.echo(f"\nTotal: {len(records)} records")


@ncuc_app.command("show")
def ncuc_show(
    record_id: int = typer.Argument(..., help="NCUC discovery record id."),
) -> None:
    """Show full detail for one NCUC discovery record."""
    settings, repository = _bootstrap()
    rec = repository.get_ncuc_discovery_record(record_id)
    if not rec:
        raise typer.BadParameter(f"NCUC record {record_id} not found.")
    typer.echo(json.dumps(rec.model_dump(mode="json"), indent=2, default=str))


@ncuc_app.command("import-pipeline")
def ncuc_import_pipeline(
    record_id: int | None = typer.Option(
        None, help="Import a specific NCUC record into the historical pipeline."
    ),
    all_downloaded: bool = typer.Option(
        False, help="Import all successfully downloaded NCUC records."
    ),
    all_discovered: bool = typer.Option(
        False, help="Import all NCUC discovery records (including pending/failed as leads)."
    ),
) -> None:
    """Import NCUC downloads/discoveries into the historical lead and docket pipeline."""
    from duke_rates.historical.ncuc.importer import NcucPipelineImporter

    settings, repository = _bootstrap()
    importer = NcucPipelineImporter(settings, repository)

    if record_id is not None:
        rec = repository.get_ncuc_discovery_record(record_id)
        if not rec:
            raise typer.BadParameter(f"NCUC record {record_id} not found.")
        result = importer.import_discovery_record(rec)
        typer.echo(f"Imported NCUC record {record_id}:")
        typer.echo(f"  family_keys={result['family_keys_matched']}")
        typer.echo(f"  lead_ids={result['lead_ids']}")
        typer.echo(f"  docket_lead_ids={result['docket_lead_ids']}")
    elif all_downloaded:
        summaries = importer.import_all_pending_downloads()
        ok = sum(1 for s in summaries if "error" not in s)
        typer.echo(f"Imported {ok}/{len(summaries)} downloaded NCUC records.")
    elif all_discovered:
        summaries = importer.import_all_discovered()
        ok = sum(1 for s in summaries if "error" not in s)
        typer.echo(f"Imported {ok}/{len(summaries)} NCUC discovery records as leads.")
    else:
        typer.echo("Specify --record-id, --all-downloaded, or --all-discovered.")


@ncuc_app.command("mine-pdf-content")
def ncuc_mine_pdf_content(
    docket: str | None = typer.Option(None, help="Filter to one docket number."),
    family_query: str | None = typer.Option(
        None, help="Filter to family/code text such as 605 or 640."
    ),
    limit: int | None = typer.Option(None, help="Maximum records to mine."),
    max_pages: int = typer.Option(12, help="Maximum PDF pages to inspect per record."),
    force: bool = typer.Option(False, help="Re-extract text even if a sidecar exists."),
) -> None:
    """Mine downloaded NCUC PDFs for schedule/rider/leaf/effective-date signals."""
    from duke_rates.historical.ncuc.content_miner import NcucPdfContentMiner

    settings, repository = _bootstrap()
    miner = NcucPdfContentMiner(settings, repository)
    summaries = miner.mine_records(
        docket_number=docket,
        family_query=family_query,
        limit=limit,
        max_pages=max_pages,
        force=force,
    )
    tariff_like = 0
    for summary in summaries:
        if summary["contains_tariff_text"]:
            tariff_like += 1
        typer.echo(
            f"id={summary['record_id']} docket={summary['docket_number']} "
            f"codes={summary['schedule_codes'] or summary['rider_codes']} "
            f"leafs={summary['leaf_nos']} tariff_text={summary['contains_tariff_text']}"
        )
    typer.echo(
        f"\nMined {len(summaries)} NCUC PDFs; {tariff_like} look like tariff/rider text exhibits."
    )


@ncuc_app.command("list-exhibit-candidates")
def ncuc_list_exhibit_candidates(
    family_query: str = typer.Option(..., help="Progress NC family query such as 605 or 640."),
    limit: int = typer.Option(20, help="Maximum candidates to display."),
    min_score: float = typer.Option(20.0, help="Minimum exhibit score."),
) -> None:
    """Rank downloaded NCUC PDFs as likely tariff exhibits for one family."""
    from duke_rates.historical.ncuc.exhibit_selector import NcucExhibitSelector

    settings, repository = _bootstrap()
    selector = NcucExhibitSelector(settings, repository)
    candidates = selector.list_candidates(
        family_query=family_query,
        limit=limit,
        min_score=min_score,
    )
    for candidate in candidates:
        typer.echo(
            f"id={candidate.record_id} score={candidate.score:.1f} "
            f"docket={candidate.docket_number} date={candidate.filing_date or '?'} "
            f"title={candidate.derived_title or candidate.filing_title or '(untitled)'}"
        )
        typer.echo(
            f"  class={candidate.filing_classification} tariff_text={candidate.contains_tariff_text} "
            f"codes={candidate.extracted_schedule_codes or candidate.extracted_rider_codes} "
            f"leafs={candidate.extracted_leaf_nos}"
        )
        typer.echo(f"  local={candidate.local_path}")
        typer.echo(f"  reasons={', '.join(candidate.reasons)}")
    typer.echo(f"\nCandidates shown: {len(candidates)}")


@ncuc_app.command("import-exhibit-candidates")
def ncuc_import_exhibit_candidates(
    family_query: str = typer.Option(..., help="Progress NC family query such as 605 or 640."),
    top: int = typer.Option(3, help="Top candidates to import."),
    min_score: float = typer.Option(35.0, help="Minimum exhibit score."),
) -> None:
    """Import top-ranked NCUC exhibit candidates into historical documents."""
    from duke_rates.historical.ncuc.exhibit_selector import NcucExhibitSelector

    settings, repository = _bootstrap()
    selector = NcucExhibitSelector(settings, repository)
    imported = selector.import_candidates(
        family_query=family_query,
        top=top,
        min_score=min_score,
    )
    for row in imported:
        typer.echo(
            f"record_id={row['record_id']} historical_id={row['historical_id']} "
            f"docket={row['docket_number']} title={row['title']}"
        )
    typer.echo(f"\nImported {len(imported)} NCUC exhibit candidates.")


@ncuc_app.command("family-query")
def ncuc_family_query(
    family_query: str = typer.Argument(
        ...,
        help="Family key or schedule number to query (e.g. '605', '670').",
    ),
    show_urls: bool = typer.Option(True, help="Show URLs."),
) -> None:
    """Show all NCUC discovery results for a target family/schedule."""
    settings, repository = _bootstrap()
    records = repository.list_ncuc_discovery_records(family_key=family_query)
    if not records:
        typer.echo(f"No NCUC records found for family: {family_query!r}")
        typer.echo("Tip: run 'ncuc seed-discover' or 'ncuc search' to populate records.")
        return
    typer.echo(f"NCUC records for family {family_query!r}  ({len(records)} total):\n")
    for rec in records:
        typer.echo(
            f"  id={rec.id} docket={rec.docket_number} "
            f"date={rec.filing_date or '?'} "
            f"status={rec.fetch_status.value} "
            f"class={rec.filing_classification.value}"
        )
        typer.echo(f"    title: {rec.filing_title or '(none)'}")
        if show_urls and (rec.download_url or rec.viewer_url or rec.discovered_url):
            typer.echo(f"    url: {rec.download_url or rec.viewer_url or rec.discovered_url}")
        if rec.local_path:
            typer.echo(f"    local: {rec.local_path}")
        typer.echo()


@ncuc_app.command("playwright-discover")
def ncuc_playwright_discover(
    url: str = typer.Argument(..., help="NCUC page URL to navigate with Playwright."),
    docket: str | None = typer.Option(None, help="Docket number hint for the page."),
    max_results: int = typer.Option(50, help="Max document links to extract."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Use Playwright browser to discover NCUC documents from a portal page.

    The NCUC portal (starw1.ncuc.gov) uses Cloudflare bot protection that
    blocks direct HTTP. Use this command to navigate it with a real browser.

    Example:
        duke-rates ncuc playwright-discover \\
            'https://starw1.ncuc.gov/NCUC/page/Dockets/portal.aspx' \\
            --docket 'E-2'
    """
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucDocketSeed

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        seed = NcucDocketSeed(
            docket_number=docket or "unknown",
            utility="Duke Energy Progress",
        )
        results = svc.discover_with_playwright(url, seed=seed, max_results=max_results)
        count = 0
        for result in results:
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number} title={rec.filing_title or '(none)'}"
            )
            count += 1
        typer.echo(f"\nPlaywright discovery: {count} records from {url}")
    finally:
        svc.close()


@ncuc_app.command("public-search")
def ncuc_public_search(
    query: str = typer.Argument(
        ...,
        help="Search text for NCUC public Zoom search (ncuc.gov).",
    ),
    family_query: str | None = typer.Option(None, help="Family key hint (e.g. '605')."),
    max_results: int = typer.Option(50, help="Max results."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Search NCUC public website (ncuc.gov) - accessible without Cloudflare restriction.

    This uses the NCUC Zoom full-text search engine to find document pages.
    Results are less structured than the portal but more reliably accessible.

    Example:
        duke-rates ncuc public-search 'Progress Energy Carolinas rate schedule 605'
    """
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucSearchQuery

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        q = NcucSearchQuery(query_text=query, family_key_hint=family_query)
        count = 0
        try:
            for result in svc.search_ncuc_public(q, max_results=max_results):
                rec = result.record
                if not dry_run:
                    rec_id = repository.upsert_ncuc_discovery_record(rec)
                    rec = rec.model_copy(update={"id": rec_id})
                typer.echo(
                    f"  [{result.relevance_score:.2f}] id={getattr(rec, 'id', '?')} "
                    f"title={rec.filing_title or '(none)'} "
                    f"url={rec.discovered_url or ''}"
                )
                count += 1
        except Exception as exc:
            from duke_rates.cli import _classify_ncuc_access_failure  # lazy
            classification, detail = _classify_ncuc_access_failure(exc, surface="NCUC public keyword search")
            typer.echo(f"Classification: {classification}")
            typer.echo(detail)
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1)
        typer.echo(f"\nNCUC public search: {count} results for {query!r}")
        if count == 0:
            typer.echo("Classification: public search returned 0 results.")
            typer.echo("If you know the docket, prefer `ncuc portal-search --docket-number ...`.")
    finally:
        svc.close()


@ncuc_app.command("wayback-discover")
def ncuc_wayback_discover(
    limit: int = typer.Option(50, help="Max Wayback snapshots to scan."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Discover NCUC docket pages via Wayback Machine CDX index.

    Queries archive.org's CDX API for indexed NCUC DocketDetails pages
    to find known docket IDs that can then be navigated via Playwright.
    """
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService

    settings, repository = _bootstrap()
    svc = NcucDiscoveryService(settings)
    try:
        count = 0
        for result in svc.discover_via_wayback(limit=limit):
            rec = result.record
            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})
            typer.echo(
                f"  id={getattr(rec, 'id', '?')} "
                f"docket={rec.docket_number or '?'} "
                f"url={rec.discovered_url or ''}"
            )
            count += 1
        typer.echo(f"\nWayback NCUC discovery: {count} docket records found.")
    finally:
        svc.close()


@ncuc_app.command("annual-orders-scan")
def ncuc_annual_orders_scan(
    years: str = typer.Option(
        "2016,2017,2018,2019,2020",
        help="Comma-separated list of years to scan.",
    ),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Mine NCUC annual orders PDFs for E-2 Duke Energy Progress sub-docket entries.

    Downloads the NCUC annual orders PDFs (publicly accessible, no Cloudflare)
    from www.ncuc.gov/documents/ordersYYYY.pdf and extracts all E-2 Sub references,
    including title context from the table of contents. Creates NcucDiscoveryRecord
    entries for each sub-docket found.

    Example::

        duke-rates ncuc annual-orders-scan --years 2016,2017,2018,2019,2020
    """
    import httpx
    import io
    import re

    try:
        import pdfplumber
    except ImportError:
        typer.echo("pdfplumber not installed. Run: pip install pdfplumber")
        raise typer.Exit(1)

    from duke_rates.models.ncuc import (
        NcucAcquisitionMethod,
        NcucDiscoveryRecord,
        NcucFetchStatus,
    )
    from duke_rates.historical.ncuc.metadata import classify_filing, extract_schedule_codes

    settings, repository = _bootstrap()

    year_list = [y.strip() for y in years.split(",") if y.strip().isdigit()]
    total_count = 0

    for year in year_list:
        url = f"https://www.ncuc.gov/documents/orders{year}.pdf"
        typer.echo(f"\nDownloading {url}...")
        try:
            r = httpx.get(url, timeout=120, follow_redirects=True)
        except Exception as exc:
            typer.echo(f"  Failed to download: {exc}")
            continue

        if r.status_code != 200:
            typer.echo(f"  HTTP {r.status_code} — skipping")
            continue

        typer.echo(f"  {len(r.content):,} bytes, parsing...")
        content = io.BytesIO(r.content)
        seen_subs: dict[str, str] = {}  # sub_number -> title context

        with pdfplumber.open(content) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for m in re.finditer(r"E-2,?\s+SUB\s+(\d+)[^\n]{0,200}", text, re.I):
                    sub = m.group(1)
                    if sub not in seen_subs:
                        line = m.group(0).replace("\n", " ").strip()
                        seen_subs[sub] = line[:200]

        typer.echo(f"  Found {len(seen_subs)} E-2 sub-dockets in {year}")
        year_count = 0

        for sub_number, context_line in sorted(seen_subs.items(), key=lambda x: int(x[0])):
            docket_number = f"E-2, Sub {sub_number}"
            schedule_codes = extract_schedule_codes(context_line)
            classification = classify_filing(context_line)

            rec = NcucDiscoveryRecord(
                docket_number=docket_number,
                sub_number=sub_number,
                filing_title=context_line[:200],
                filing_date=f"{year}-01-01",
                filing_classification=classification,
                referenced_schedule_codes=schedule_codes,
                discovered_url=url,
                acquisition_method=NcucAcquisitionMethod.MANUAL_SEED,
                fetch_status=NcucFetchStatus.PENDING,
                provenance_notes=[
                    f"source=ncuc_annual_orders_pdf",
                    f"year={year}",
                    f"pdf_url={url}",
                ],
            )

            if not dry_run:
                rec_id = repository.upsert_ncuc_discovery_record(rec)
                rec = rec.model_copy(update={"id": rec_id})

            typer.echo(
                f"  {'[dry]' if dry_run else f'id={rec.id}'} "
                f"{docket_number} | {context_line[:80]}"
            )
            year_count += 1

        total_count += year_count
        typer.echo(f"  -> {year_count} records {'found' if dry_run else 'persisted'} for {year}")

    typer.echo(f"\nAnnual orders scan: {total_count} total E-2 sub-docket records across {len(year_list)} years.")


@ncuc_app.command("login-test")
def ncuc_login_test() -> None:
    """Test NCUC portal login and verify authenticated document access.

    Reads DUKE_RATES_NCID_USERNAME and DUKE_RATES_NCID_PASSWORD from .env,
    logs in via the portal's NCIDLogin form (using installed Chrome to pass
    Cloudflare), then tests access to a known E-2 docket document list.

    Example::

        duke-rates ncuc login-test
    """
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        test_authenticated_access,
    )

    settings, _ = _bootstrap()

    if not settings.ncid_username:
        typer.echo("ERROR: DUKE_RATES_NCID_USERNAME not set in .env")
        raise typer.Exit(1)

    typer.echo(f"Logging in as: {settings.ncid_username}")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        # Test with E-2 Sub 1354 (known docket from portal scrape)
        docket_id = "9b3614b6-11d6-4703-8d18-5e2e2ef3d705"
        typer.echo(f"\nTesting DocketDetails access for E-2 Sub 1354...")
        result = test_authenticated_access(page, docket_id)

        typer.echo(f"  accessible:   {result['accessible']}")
        typer.echo(f"  cf_blocked:   {result['cf_blocked']}")
        typer.echo(f"  status_code:  {result['status_code']}")
        typer.echo(f"  html_length:  {result['html_length']}")
        typer.echo(f"  title:        {result['title']!r}")
        typer.echo(f"  doc_links:    {len(result['doc_links'])}")
        for link in result["doc_links"][:5]:
            typer.echo(f"    {link[:100]}")

        if result["accessible"]:
            typer.echo("\nLogin SUCCESS — authenticated portal access confirmed.")
            typer.echo("Note: this verifies authenticated DocketDetails access only.")
            typer.echo("For exact docket document listing use ncuc resolve-docket-ids + ncuc docket-fetch.")
        else:
            typer.echo("\nLogin completed but portal pages still CF-blocked.")
    finally:
        close_authenticated_context(pw, ctx)


@ncuc_app.command("portal-smoke-test")
def ncuc_portal_smoke_test(
    docket_number: str = typer.Option(
        "E-2, Sub 1354",
        "--docket-number",
        help="Known docket used to verify resolve + DocketDetails + document inventory.",
    ),
) -> None:
    """Canonical authenticated NCUC portal smoke test.

    Verifies credentials/browser login, resolves a human docket string to a
    portal GUID, checks authenticated DocketDetails access, and confirms that
    the docket document inventory loads.
    """
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        get_docket_documents,
        resolve_docket_ids,
        test_authenticated_access,
    )

    settings, _ = _bootstrap()
    if not settings.ncid_username:
        typer.echo("ERROR: DUKE_RATES_NCID_USERNAME not set in .env")
        raise typer.Exit(1)

    typer.echo("NCUC authenticated portal smoke test")
    typer.echo(f"  username:      {settings.ncid_username}")
    typer.echo(f"  docket probe:  {docket_number}")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        matches = resolve_docket_ids(page, docket_number)
        if not matches:
            typer.echo("Resolve failed: authenticated docket search returned 0 matches.")
            raise typer.Exit(1)

        best_match = _pick_best_ncuc_docket_match(matches)
        assert best_match is not None
        typer.echo(
            "  resolve:       "
            f"{best_match['docket_id']} ({best_match.get('match_type') or 'unknown'})"
        )

        access = test_authenticated_access(page, best_match["docket_id"])
        typer.echo(f"  accessible:    {access['accessible']}")
        typer.echo(f"  cf_blocked:    {access['cf_blocked']}")
        typer.echo(f"  status_code:   {access['status_code']}")
        if not access["accessible"]:
            typer.echo("DocketDetails check failed after login.")
            raise typer.Exit(1)

        docs = get_docket_documents(page, best_match["docket_id"])
        typer.echo(f"  doc_inventory: {len(docs)} documents")
        if not docs:
            typer.echo("Document inventory loaded but no documents were returned.")
            raise typer.Exit(1)

        typer.echo("Smoke test SUCCESS — authenticated portal workflow is healthy.")
        typer.echo("Canonical next step: use `ncuc portal-search` for authenticated portal work.")
    finally:
        close_authenticated_context(pw, ctx)


@ncuc_app.command("resolve-docket-ids")
def ncuc_resolve_docket_ids(
    all_seeded: bool = typer.Option(
        False,
        "--all-seeded",
        help="Resolve DocketId GUIDs for the built-in Duke Progress seed list.",
    ),
    docket_number: list[str] = typer.Option(
        None,
        "--docket-number",
        help="Specific docket number(s), e.g. --docket-number 'E-2, Sub 1107'.",
    ),
) -> None:
    """Resolve NCUC DocketId GUIDs through the authenticated portal search."""
    from duke_rates.historical.ncuc.discovery import DUKE_PROGRESS_E2_DOCKETS
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        resolve_docket_ids,
    )

    settings, _ = _bootstrap()
    targets = docket_number or []
    if all_seeded:
        for seed in DUKE_PROGRESS_E2_DOCKETS:
            if seed.docket_number not in targets:
                targets.append(seed.docket_number)
    if not targets:
        raise typer.BadParameter("Specify --docket-number or --all-seeded.")

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        for target in targets:
            matches = resolve_docket_ids(page, target)
            if not matches:
                typer.echo(f"{target}: no docket-id match")
                continue
            for match in matches:
                typer.echo(
                    f"{target}: {match['docket_id']}  {match['href']}  match_type={match.get('match_type') or 'unknown'}"
                )
    finally:
        close_authenticated_context(pw, ctx)


@ncuc_app.command("portal-search")
def ncuc_portal_search(
    docket_number: str = typer.Option(
        "",
        "--docket-number",
        help="If provided, run the exact-docket authenticated path using a human docket string like 'E-2, Sub 1354'.",
    ),
    company: str = typer.Option(
        "Duke Energy Progress",
        "--company",
        help="Structured-search company filter when --docket-number is omitted.",
    ),
    filing_types: str = typer.Option(
        "TARIFF,RATESCED",
        "--types",
        help="Structured-search filing type keys when --docket-number is omitted.",
    ),
    date_after: str = typer.Option("", "--after", help="Structured search: filed on or after MM/DD/YYYY."),
    date_before: str = typer.Option("", "--before", help="Structured search: filed on or before MM/DD/YYYY."),
    max_results: int = typer.Option(500, "--max", help="Maximum results to collect."),
    tariff_only: bool = typer.Option(False, "--tariff-only", help="Structured search: display only tariff-related rows."),
    top_n: int = typer.Option(50, "--top", help="Maximum results/documents to display."),
    export_csv: str = typer.Option("", "--csv", help="Structured search: export rows to this CSV path."),
    export_json: str = typer.Option("", "--json", help="Structured search: export rows to this JSON path."),
) -> None:
    """Canonical authenticated NCUC portal search surface.

    Branches explicitly:
    - with ``--docket-number``: exact-docket resolve + inventory path
    - without ``--docket-number``: authenticated structured search path
    """
    from duke_rates.historical.ncuc.document_param_search import (
        DocumentParamSearcher,
        print_doc_param_results,
    )
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        get_docket_documents,
        resolve_docket_ids,
    )

    settings, _ = _bootstrap()

    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        if docket_number:
            typer.echo("Search mode: authenticated exact-docket")
            matches = resolve_docket_ids(page, docket_number)
            if not matches:
                typer.echo("Result: 0 docket GUID matches.")
                typer.echo("Classification: authenticated exact-docket search returned no matches.")
                raise typer.Exit(1)

            typer.echo(f"Resolved {len(matches)} docket candidate(s):")
            for match in matches[:5]:
                typer.echo(
                    f"  {match['docket_number']} -> {match['docket_id']} "
                    f"({match.get('match_type') or 'unknown'})"
                )
            if len(matches) > 5:
                typer.echo(f"  ... and {len(matches) - 5} more matches")

            best_match = _pick_best_ncuc_docket_match(matches)
            assert best_match is not None
            docs = get_docket_documents(page, best_match["docket_id"])

            typer.echo(
                "Using best match: "
                f"{best_match['docket_number']} -> {best_match['docket_id']} "
                f"({best_match.get('match_type') or 'unknown'})"
            )
            typer.echo(f"Found {len(docs)} docket documents.")
            _print_ncuc_docket_documents(docs, top_n=top_n)
            typer.echo(
                "For download/persistence, run: "
                f"python -m duke_rates ncuc docket-fetch {best_match['docket_id']} "
                f'--docket-number "{best_match["docket_number"]}" --dry-run'
            )
            return

        typer.echo("Search mode: authenticated structured")
        ft_keys = [t.strip().upper() for t in filing_types.split(",") if t.strip()]
        searcher = DocumentParamSearcher(settings)
        results = searcher.search(
            page,
            company_name=company,
            docket_number="",
            filing_types=ft_keys,
            date_after=date_after,
            date_before=date_before,
            max_results=max_results,
        )

        typer.echo(f"Found {len(results)} documents.")
        typer.echo("Classification: authenticated structured search completed.")
        print_doc_param_results(results, top_n=top_n, only_tariff_related=tariff_only)

        if export_csv:
            import csv as _csv

            out = Path(export_csv)
            out.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = [
                "doc_type", "description", "date_filed", "docket_number", "company_name",
                "document_detail_url", "extracted_schedule_codes", "extracted_rider_codes",
                "filing_classification", "view_file_urls",
            ]
            with out.open("w", newline="", encoding="utf-8") as f:
                writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for r in results:
                    writer.writerow({
                        "doc_type": r.doc_type,
                        "description": r.description,
                        "date_filed": r.date_filed,
                        "docket_number": r.docket_number,
                        "company_name": r.company_name,
                        "document_detail_url": r.document_detail_url or "",
                        "extracted_schedule_codes": ", ".join(r.extracted_schedule_codes),
                        "extracted_rider_codes": ", ".join(r.extracted_rider_codes),
                        "filing_classification": r.filing_classification,
                        "view_file_urls": "; ".join(r.view_file_urls),
                    })
            typer.echo(f"Exported {len(results)} rows -> {out}")

        if export_json:
            import dataclasses
            import json as _json

            out = Path(export_json)
            out.parent.mkdir(parents=True, exist_ok=True)
            data = [dataclasses.asdict(r) for r in results]
            out.write_text(_json.dumps(data, indent=2), encoding="utf-8")
            typer.echo(f"Exported {len(results)} rows -> {out}")
    finally:
        close_authenticated_context(pw, ctx)


@ncuc_app.command("docket-fetch")
def ncuc_docket_fetch(
    docket_id: str = typer.Argument(..., help="NCUC DocketId GUID."),
    docket_number: str = typer.Option("", help="Human-readable docket number (e.g. 'E-2, Sub 1354')."),
    download: bool = typer.Option(False, help="Download all ViewFile documents to local storage."),
    dry_run: bool = typer.Option(False, help="List documents without downloading or persisting."),
) -> None:
    """Fetch all documents for an NCUC docket using authenticated session.

    Logs into the NCUC portal with NCID credentials, navigates to the docket's
    Documents tab, lists all filings, and optionally downloads their files.

    Example::

        duke-rates ncuc docket-fetch 9b3614b6-11d6-4703-8d18-5e2e2ef3d705 \\
            --docket-number "E-2, Sub 1354" --download
    """
    from duke_rates.historical.ncuc.session import (
        NcucSessionError,
        close_authenticated_context,
        create_authenticated_context,
        download_view_file,
        get_docket_documents,
    )
    from duke_rates.models.ncuc import (
        NcucAcquisitionMethod,
        NcucDiscoveryRecord,
        NcucFetchStatus,
        NcucFilingClassification,
    )
    from duke_rates.historical.ncuc.metadata import classify_filing, extract_schedule_codes, extract_rider_codes

    settings, repository = _bootstrap()

    if not settings.ncid_username:
        typer.echo("ERROR: DUKE_RATES_NCID_USERNAME not set in .env")
        raise typer.Exit(1)

    typer.echo(f"Logging in as: {settings.ncid_username}")
    try:
        pw, ctx, page = create_authenticated_context(settings)
    except NcucSessionError as exc:
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(1)

    try:
        typer.echo(f"\nFetching documents for docket: {docket_number or docket_id}")
        try:
            docs = get_docket_documents(page, docket_id)
        except Exception as exc:
            from duke_rates.cli import _classify_ncuc_access_failure  # lazy
            classification, detail = _classify_ncuc_access_failure(exc, surface="Authenticated docket inventory")
            typer.echo(f"Classification: {classification}")
            typer.echo(detail)
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1)
        typer.echo(f"Found {len(docs)} documents")
        typer.echo("Classification: authenticated exact-docket inventory completed.\n")

        for i, doc in enumerate(docs):
            typer.echo(
                f"  [{i+1:02d}] {doc['doc_type']:12s} {doc['date_filed'] or '?':12s} "
                f"{doc['description'][:70]}"
            )
            if doc["view_file_urls"]:
                typer.echo(f"        Files: {len(doc['view_file_urls'])}")

            if dry_run:
                continue

            import re
            sub_m = re.search(r"Sub\s+(\d+)", docket_number or "", re.I)
            sub_number = sub_m.group(1) if sub_m else None
            combined = f"{docket_number} {doc['description']}"
            schedule_codes = extract_schedule_codes(combined)
            rider_codes = extract_rider_codes(combined)
            classification = classify_filing(f"{doc['doc_type']} {doc['description']}")
            detail_url = (
                doc["document_url"]
                or f"https://starw1.ncuc.gov/NCUC/page/docket-docs/PSC/DocketDetails.aspx?DocketId={docket_id}"
            )

            attachment_urls = doc["view_file_urls"] or [None]
            for attachment_index, attachment_url in enumerate(attachment_urls, start=1):
                attachment_suffix = ""
                if len(attachment_urls) > 1:
                    attachment_suffix = f" [attachment {attachment_index}/{len(attachment_urls)}]"

                rec = NcucDiscoveryRecord(
                    docket_number=docket_number or None,
                    sub_number=sub_number,
                    filing_title=f"{doc['description']}{attachment_suffix}",
                    filing_date=doc["date_filed"],
                    proceeding_type=doc["doc_type"],
                    filing_classification=classification,
                    referenced_schedule_codes=schedule_codes,
                    referenced_rider_codes=rider_codes,
                    discovered_url=detail_url,
                    viewer_url=attachment_url or doc["document_url"],
                    attachment_url=attachment_url,
                    acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
                    fetch_status=NcucFetchStatus.PENDING,
                    provenance_notes=[
                        "source=authenticated_docket_docs",
                        f"docket_id={docket_id}",
                        f"view_files={len(doc['view_file_urls'])}",
                        f"attachment_index={attachment_index}",
                    ],
                )

                if download and attachment_url:
                    import re as _re

                    file_id_m = _re.search(r"Id=([a-f0-9-]{36})", attachment_url, _re.I)
                    file_id = file_id_m.group(1) if file_id_m else f"attachment-{attachment_index}"
                    slug = re.sub(r"[^a-z0-9]+", "-", (docket_number or docket_id).lower()).strip("-")
                    dest = settings.historical_dir / "ncuc" / slug / f"{file_id}.pdf"
                    try:
                        size = download_view_file(page, attachment_url, dest)
                        rec = rec.model_copy(update={
                            "fetch_status": NcucFetchStatus.SUCCESS,
                            "local_path": str(dest),
                            "file_size_bytes": size,
                        })
                        typer.echo(f"        -> Downloaded {size:,} bytes to {dest.name}")
                    except Exception as exc:
                        rec = rec.model_copy(update={
                            "fetch_status": NcucFetchStatus.FAILED,
                            "error_detail": str(exc)[:200],
                        })
                        typer.echo(f"        -> Download failed: {exc}")

                rec_id = repository.upsert_ncuc_discovery_record(rec)
                typer.echo(f"        -> Persisted as record id={rec_id}")

        if not dry_run:
            typer.echo(f"\nDone. {len(docs)} records persisted.")
    finally:
        close_authenticated_context(pw, ctx)


@ncuc_app.command("portal-scrape")
def ncuc_portal_scrape(
    max_pages: int = typer.Option(5, help="Max portal pages to scrape."),
    e2_only: bool = typer.Option(True, help="Limit to E-2 dockets only."),
    all_e_dockets: bool = typer.Option(False, help="Include all E-* dockets (overrides --e2-only)."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Scrape NCUC portal.aspx recent orders via Playwright.

    Loads the NCUC portal.aspx page (accessible without Cloudflare challenge)
    and extracts E-2 Duke Energy Progress docket entries. Paginates using the
    MS Ajax pager. Persists each entry as an NcucDiscoveryRecord.

    Example::

        duke-rates ncuc portal-scrape --max-pages 5 --e2-only
    """
    from duke_rates.historical.ncuc.portal_scraper import NcucPortalScraper

    settings, repository = _bootstrap()
    scraper = NcucPortalScraper(settings)
    records = scraper.scrape_recent_orders(
        max_pages=max_pages,
        e2_only=not all_e_dockets and e2_only,
        all_e_dockets=all_e_dockets,
    )

    if not records:
        typer.echo("No records found.")
        return

    count = 0
    for rec in records:
        if not dry_run:
            rec_id = repository.upsert_ncuc_discovery_record(rec)
            rec = rec.model_copy(update={"id": rec_id})
        typer.echo(
            f"  id={getattr(rec, 'id', '?')} "
            f"docket={rec.docket_number or '?'} "
            f"date={rec.filing_date or '?'} "
            f"title={str(rec.filing_title or '')[:60]}"
        )
        count += 1

    typer.echo(f"\nPortal scrape: {count} records {'found (dry run)' if dry_run else 'persisted'}.")


@ncuc_app.command("wayback-harvest")
def ncuc_wayback_harvest(
    limit: int = typer.Option(200, help="Max Wayback CDX results to retrieve."),
    fetch_snapshots: bool = typer.Option(False, help="Fetch each snapshot via Playwright to extract document links."),
    dry_run: bool = typer.Option(False, help="Print results without persisting."),
) -> None:
    """Harvest NCUC docket GUIDs from Wayback Machine CDX index.

    Queries the Wayback CDX API for all indexed NCUC DocketDetails pages,
    extracting docket GUIDs. Optionally fetches each snapshot via Playwright
    to attempt document link extraction (most snapshots are blank JS-rendered).

    Example::

        duke-rates ncuc wayback-harvest --limit 500
        duke-rates ncuc wayback-harvest --limit 50 --fetch-snapshots
    """
    from duke_rates.historical.ncuc.portal_scraper import NcucWaybackHarvester

    settings, repository = _bootstrap()
    harvester = NcucWaybackHarvester(settings)
    try:
        docket_hits = harvester.harvest_docket_guids(limit=limit)
        typer.echo(f"Wayback CDX: {len(docket_hits)} unique NCUC docket URLs found")

        persisted_count = 0
        snapshot_count = 0

        for hit in docket_hits:
            typer.echo(
                f"  docket_id={hit.get('docket_id') or '?'} "
                f"ts={hit.get('timestamp', '')[:8]} "
                f"url={hit.get('original_url', '')[:80]}"
            )

            if fetch_snapshots and hit.get("wayback_url"):
                snap_records = harvester.fetch_wayback_snapshot_with_playwright(
                    hit["wayback_url"],
                    docket_hint=None,
                )
                snapshot_count += len(snap_records)
                for rec in snap_records:
                    if not dry_run:
                        rec_id = repository.upsert_ncuc_discovery_record(rec)
                        rec = rec.model_copy(update={"id": rec_id})
                        persisted_count += 1
                    typer.echo(
                        f"    -> id={getattr(rec, 'id', '?')} "
                        f"title={str(rec.filing_title or '')[:50]}"
                    )

        msg = f"\nWayback harvest: {len(docket_hits)} docket GUIDs"
        if fetch_snapshots:
            msg += f", {snapshot_count} snapshot documents"
            if not dry_run:
                msg += f" ({persisted_count} persisted)"
        typer.echo(msg)
    finally:
        harvester.close()


@ncuc_app.command("pending-rates")
def ncuc_pending_rates(
    utility: str = typer.Option(
        "progress",
        "--utility",
        help="Duke utility to scan: 'progress' (DEP/E-2) or 'carolinas' (DEC/E-7).",
    ),
    days: int = typer.Option(
        730,
        "--days",
        help="Look back this many days when filtering recent filings (0 = all).",
    ),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Scan NCUC discovery records for pending or recent rate cases and rider filings.

    Reads the local ncuc_discovery_records table (populated by ncuc seed-discover,
    ncuc search, or ncuc annual-orders-scan) and surfaces filings that may signal
    upcoming rate changes:

    \\b
    - APPLICATION / SETTLEMENT filings -- proposed new rates not yet in effect
    - ORDER filings dated within --days -- approved changes that may need re-parsing
    - TARIFF_SHEETS filings -- new tariff page submissions
    - Any filing with high relevance score that mentions schedule or rider codes

    Run ncuc seed-discover or ncuc search first to populate the database, then use
    this command to see what pending changes are in the pipeline.

    Examples:
        duke-rates ncuc pending-rates
        duke-rates ncuc pending-rates --days 365 --json
        duke-rates ncuc pending-rates --utility carolinas
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    _, repository = _bootstrap()

    records = repository.list_ncuc_discovery_records()

    if not records:
        typer.echo(
            "No NCUC discovery records found. Run ncuc seed-discover or ncuc search first."
        )
        raise typer.Exit(0)

    # Filter by utility keyword
    _util_kw = "progress" if utility.lower() == "progress" else "carolinas"
    _docket_prefix = "E-2" if _util_kw == "progress" else "E-7"
    records = [
        r for r in records
        if (r.docket_number or "").startswith(_docket_prefix)
        or _util_kw in (r.utility or "").lower()
    ]

    # Date cutoff
    cutoff = None
    if days > 0:
        cutoff = (_dt.now(_tz.utc) - _td(days=days)).date()

    # Classify into categories of interest
    PENDING_CLASSES = {"application", "settlement"}
    RECENT_ORDER_CLASSES = {"order"}
    TARIFF_CLASSES = {"tariff_sheets"}

    pending: list = []
    recent_orders: list = []
    tariff_filings: list = []
    rider_filings: list = []

    for rec in records:
        cls = rec.filing_classification.value if rec.filing_classification else "other"
        date_str = rec.filing_date or ""
        # Parse date for cutoff check
        rec_date = None
        try:
            if date_str:
                # Handle various date formats stored in the DB
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        rec_date = _dt.strptime(date_str[:10], fmt[:len(date_str[:10])]).date()
                        break
                    except ValueError:
                        continue
        except Exception:
            pass

        after_cutoff = (cutoff is None) or (rec_date is None) or (rec_date >= cutoff)

        has_schedule = bool(rec.referenced_schedule_codes)
        has_rider = bool(rec.referenced_rider_codes or rec.referenced_leaf_nos)

        if cls in PENDING_CLASSES:
            pending.append(rec)
        elif cls in RECENT_ORDER_CLASSES and after_cutoff:
            recent_orders.append(rec)
        elif cls in TARIFF_CLASSES and after_cutoff:
            tariff_filings.append(rec)
        elif has_rider and after_cutoff:
            rider_filings.append(rec)

    if output_json:
        def _rec_to_dict(r):
            return {
                "id": r.id,
                "docket_number": r.docket_number,
                "filing_title": r.filing_title,
                "filing_date": r.filing_date,
                "filing_classification": r.filing_classification.value if r.filing_classification else None,
                "proceeding_type": r.proceeding_type,
                "referenced_schedule_codes": r.referenced_schedule_codes,
                "referenced_rider_codes": r.referenced_rider_codes,
                "fetch_status": r.fetch_status.value if r.fetch_status else None,
                "download_url": r.download_url or r.viewer_url,
            }
        typer.echo(_json.dumps({
            "utility": utility,
            "days": days,
            "pending_rate_cases": [_rec_to_dict(r) for r in pending],
            "recent_orders": [_rec_to_dict(r) for r in recent_orders],
            "tariff_sheet_filings": [_rec_to_dict(r) for r in tariff_filings],
            "rider_filings": [_rec_to_dict(r) for r in rider_filings],
        }, indent=2))
        return

    def _print_section(title: str, items: list, max_rows: int = 20) -> None:
        typer.echo(f"\n{'=' * 70}")
        typer.echo(f"  {title} ({len(items)} records)")
        typer.echo(f"{'=' * 70}")
        if not items:
            typer.echo("  (none)")
            return
        typer.echo(f"  {'DATE':<12}  {'DOCKET':<20}  {'CLASS':<16}  {'TITLE'}")
        typer.echo(f"  {'-' * 66}")
        for rec in items[:max_rows]:
            date_s = (rec.filing_date or "")[:10]
            docket_s = (rec.docket_number or "")[:19]
            cls_s = (rec.filing_classification.value if rec.filing_classification else "other")[:15]
            title_s = (rec.filing_title or "(untitled)")[:40]
            codes = ""
            if rec.referenced_schedule_codes:
                codes = f"  [sched: {','.join(rec.referenced_schedule_codes[:4])}]"
            if rec.referenced_rider_codes:
                codes += f"  [rider: {','.join(rec.referenced_rider_codes[:4])}]"
            typer.echo(f"  {date_s:<12}  {docket_s:<20}  {cls_s:<16}  {title_s}{codes}")
        if len(items) > max_rows:
            typer.echo(f"  ... and {len(items) - max_rows} more (use --json for full output)")

    cutoff_note = f" (last {days} days)" if days > 0 else ""
    typer.echo(f"\nNCUC rate pipeline — {utility.upper()} ({_docket_prefix}){cutoff_note}")
    typer.echo(f"Total NCUC records scanned: {len(records)}")

    _print_section("PENDING RATE CASES (APPLICATION / SETTLEMENT)", pending)
    _print_section(f"RECENT ORDERS{cutoff_note}", recent_orders)
    _print_section(f"TARIFF SHEET FILINGS{cutoff_note}", tariff_filings)
    _print_section(f"RIDER-RELATED FILINGS{cutoff_note}", rider_filings)

    typer.echo(
        f"\nTip: Run 'duke-rates ncuc seed-discover' or 'duke-rates ncuc search' "
        f"to refresh discovery records, then re-run this command."
    )


# ---------------------------------------------------------------------------
# Phase 6.5 — Database Intelligence and Corpus Analytics
# ---------------------------------------------------------------------------


