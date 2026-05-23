"""Search pipeline sub-app: probe, run, ingest, and document-parameter
search commands for the multi-stage NCUC search workflow.

Wired into the main CLI as `duke-rates search <command>`.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from duke_rates.cli_commands._cli_utils import _bootstrap


search_app = typer.Typer(help="Multi-stage NCUC search: probe, query, run, ingest, doc-param.")


@search_app.command("probe-compat")
def search_probe_compat(
    delay: float = typer.Option(1.5, help="Seconds between probe requests (be polite)."),
    save: bool = typer.Option(True, help="Save results to data/manifests/search_compat.json"),
) -> None:
    """
    Stage 1: Test which NCUC full-text search query syntax patterns are accepted
    without SQL/parse errors.  Run this before the first search to discover safe
    query patterns. Results are saved and reused automatically by subsequent commands.
    """
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.search_compat import SearchCompatibilityHarness
    harness = SearchCompatibilityHarness(settings, delay_seconds=delay)
    try:
        report = harness.run_full_probe(save=save, delay=delay)
        harness.print_summary(report)
        typer.echo(f"\nSafe pattern types: {', '.join(r.pattern_type for r in report.safe_patterns[:5])}")
        typer.echo(f"Report saved: {harness.persist_path}")
    finally:
        harness.close()


@search_app.command("show-compat")
def search_show_compat() -> None:
    """Show the most recently saved search compatibility report summary."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.search_compat import SearchCompatibilityHarness
    harness = SearchCompatibilityHarness(settings)
    harness.print_summary()


@search_app.command("probe-query")
def search_probe_query(
    query: str = typer.Argument(..., help="Raw NCUC full-text query to test."),
) -> None:
    """Probe one query after NCUC-safe normalization and show the live result."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.query_syntax import sanitize_ncuc_query, classify_pattern_type
    from duke_rates.historical.ncuc.search_compat import SearchCompatibilityHarness

    harness = SearchCompatibilityHarness(settings)
    try:
        safe_types = harness.load_safe_pattern_types() or {"single_term", "two_term"}
        sanitized = sanitize_ncuc_query(query, safe_pattern_types=safe_types)
        result = harness.probe_single(sanitized, pattern_type=classify_pattern_type(sanitized))
        typer.echo(f"Raw:       {query}")
        typer.echo(f"Sanitized: {sanitized}")
        typer.echo(f"Pattern:   {result.pattern_type}")
        typer.echo(f"Success:   {result.success}")
        typer.echo(f"Errors:    {result.error_detected}")
        typer.echo(f"Zero:      {result.zero_results}")
        typer.echo(f"Count:     {result.result_count}")
        if result.error_snippet:
            typer.echo(f"Snippet:   {result.error_snippet}")
    finally:
        harness.close()


@search_app.command("run")
def search_run(
    utility: str | None = typer.Option(
        None, "--utility", "-u",
        help="Target utility: 'progress', 'carolinas', or full name. Default: both DEP and PEC.",
    ),
    schedule_codes: str | None = typer.Option(
        None, "--schedules", "-s",
        help="Comma-separated schedule codes, e.g. 501,602,607",
    ),
    rider_names: str | None = typer.Option(
        None, "--riders", "-r",
        help="Comma-separated rider names, e.g. JAA,REPS,DSM",
    ),
    doc_types: str | None = typer.Option(
        None, "--doc-types",
        help="Comma-separated doc types to target, e.g. tariff,rider,schedule",
    ),
    max_queries: int = typer.Option(30, "--max-queries", help="Maximum queries to execute."),
    refinement_rounds: int = typer.Option(
        1, "--refine", help="Number of iterative refinement rounds (0 to disable)."
    ),
    use_llm: bool = typer.Option(False, "--llm", help="Run LLM classification on top candidates."),
    use_portal: bool = typer.Option(
        True,
        "--portal/--no-portal",
        help="Use authenticated DocumentsParameterSearch before public search when NCID credentials are available.",
    ),
    portal_only: bool = typer.Option(
        False,
        "--portal-only",
        help="Use only the authenticated DocumentsParameterSearch portal and skip public Zoom search.",
    ),
    portal_max_results: int = typer.Option(
        250,
        "--portal-max",
        help="Maximum structured portal results to collect per company search.",
    ),
    top_n: int = typer.Option(25, "--top", help="Number of top ideal candidates to show."),
    export_csv: Path | None = typer.Option(None, "--csv", help="Export ranked candidates to this CSV file."),
    export_json_path: Path | None = typer.Option(None, "--json", help="Export ranked candidates to this JSON file."),
    no_save: bool = typer.Option(False, "--no-save", help="Do not persist outputs to disk."),
) -> None:
    """Run the full multi-stage NCUC tariff document search pipeline."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.search_pipeline import SearchPipeline

    # Parse utility name
    util_hint: str | None = None
    if utility:
        u = utility.lower()
        if "progress" in u or u == "dep":
            util_hint = "Duke Energy Progress"
        elif "carolinas" in u or u == "dec":
            util_hint = "Duke Energy Carolinas"
        else:
            util_hint = utility

    codes = [c.strip() for c in schedule_codes.split(",")] if schedule_codes else None
    riders = [r.strip() for r in rider_names.split(",")] if rider_names else None
    dtypes = [d.strip() for d in doc_types.split(",")] if doc_types else None

    pipeline = SearchPipeline(settings)
    try:
        typer.echo(
            f"Starting search pipeline (max_queries={max_queries}, refine={refinement_rounds}, "
            f"portal={'on' if use_portal else 'off'})..."
        )
        result = pipeline.run(
            utility=util_hint,
            schedule_codes=codes,
            rider_names=riders,
            doc_types=dtypes,
            max_queries=max_queries,
            refinement_rounds=refinement_rounds,
            top_n_ideal=top_n,
            use_llm=use_llm,
            use_portal=use_portal,
            portal_only=portal_only,
            portal_max_results=portal_max_results,
            save=not no_save,
        )
        result.print_summary(top_n=top_n)

        if export_csv:
            result.export_csv(export_csv, top_n=top_n)

        if export_json_path:
            result.export_json(export_json_path, top_n=top_n)

        if not no_save:
            typer.echo(f"\nOutputs saved to: {settings.data_dir}/manifests/search_pipeline/")

    finally:
        pipeline.close()


@search_app.command("query-report")
def search_query_report(
    top_n: int = typer.Option(20, help="Number of top queries to show."),
) -> None:
    """Show the query optimizer performance report (which queries are most useful)."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.query_optimizer import QueryOptimizer
    optimizer = QueryOptimizer(settings)
    optimizer.load()
    optimizer.print_report(n=top_n)
    terms = optimizer.suggest_refinement_terms()
    if terms:
        typer.echo(f"\nSuggested refinement terms: {', '.join(terms[:10])}")


@search_app.command("show-results")
def search_show_results(
    top_n: int = typer.Option(20, help="Number of top results to show."),
    ideal_only: bool = typer.Option(False, "--ideal", help="Show only ideal candidates."),
) -> None:
    """Show the most recently saved ranked search results."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc import search_persistence as persist
    rows = persist.load_latest_scored_results(settings)
    if not rows:
        typer.echo("No scored results found. Run 'search-run' first.")
        return

    if ideal_only:
        rows = [r for r in rows if r.get("is_ideal_candidate")]

    rows = rows[:top_n]
    typer.echo(f"\n{'=' * 65}")
    typer.echo(f"Top {len(rows)} {'ideal ' if ideal_only else ''}results from last search run")
    typer.echo(f"{'=' * 65}\n")

    for i, row in enumerate(rows, 1):
        title = (row.get("title") or "(no title)")[:55]
        score = row.get("combined_score", 0.0)
        doc_type = row.get("doc_type_guess", "?")
        finality = row.get("likely_finality", "?")
        is_ideal = row.get("is_ideal_candidate", False)
        ideal_tag = "[IDEAL]" if is_ideal else "       "
        typer.echo(f"{i:3d}. {ideal_tag} [{doc_type:<16}] [{finality:<14}] score={score:.2f}")
        typer.echo(f"     {title}")
        url = row.get("url", "")
        typer.echo(f"     {url[:80]}")
        expl = row.get("explanation", "")
        if expl:
            typer.echo(f"     >> {expl[:100]}")
        typer.echo()


@search_app.command("export")
def search_export(
    output: Path = typer.Argument(..., help="Output file path (.json or .csv)"),
    top_n: int = typer.Option(50, help="Number of top candidates to export."),
    ideal_only: bool = typer.Option(False, "--ideal", help="Export only ideal candidates."),
) -> None:
    """Export the most recent search results to JSON or CSV."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc import search_persistence as persist

    rows = persist.load_latest_scored_results(settings)
    if not rows:
        typer.echo("No scored results found. Run 'search-run' first.")
        raise typer.Exit(1)

    if ideal_only:
        rows = [r for r in rows if r.get("is_ideal_candidate")]

    rows = rows[:top_n]
    ext = output.suffix.lower()

    if ext == ".csv":
        import csv as _csv
        output.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "rank", "combined_score", "is_ideal_candidate", "doc_type_guess",
            "likely_finality", "confidence", "title", "url",
            "docket_number", "filing_date", "schedule_codes", "rider_codes",
            "ideal_reason", "nonideal_reason", "explanation",
        ]
        with output.open("w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for i, row in enumerate(rows, 1):
                row["rank"] = i
                row["schedule_codes"] = ", ".join(row.get("extracted_schedule_codes", []))
                row["rider_codes"] = ", ".join(row.get("extracted_rider_codes", []))
                writer.writerow(row)
        typer.echo(f"Exported {len(rows)} results -&gt; {output}")
    else:
        import json as _json
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json.dumps(rows, indent=2), encoding="utf-8")
        typer.echo(f"Exported {len(rows)} results -&gt; {output}")


@search_app.command("ingest")
def search_ingest(
    top_n: int = typer.Option(100, help="Number of top scored results to ingest."),
    ideal_only: bool = typer.Option(False, "--ideal", help="Ingest only ideal candidates."),
    dep_only: bool = typer.Option(False, "--dep-only", help="Ingest only Duke Energy Progress results."),
    min_score: float = typer.Option(0.0, help="Minimum combined_score threshold."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be ingested without writing."),
) -> None:
    """Bulk-ingest top scored search pipeline results into NCUC discovery records."""
    from duke_rates.historical.ncuc import search_persistence as persist
    from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
    from duke_rates.models.ncuc import NcucAcquisitionMethod

    settings, repository = _bootstrap()
    rows = persist.load_latest_scored_results(settings)
    if not rows:
        typer.echo("No scored results found. Run 'search-run' first.")
        raise typer.Exit(1)

    if ideal_only:
        rows = [r for r in rows if r.get("is_ideal_candidate")]
    if dep_only:
        rows = [r for r in rows if "Duke Energy Progress" in (r.get("utility_hint") or r.get("snippet") or "")]
    if min_score > 0:
        rows = [r for r in rows if (r.get("combined_score") or 0) >= min_score]
    rows = rows[:top_n]

    if not rows:
        typer.echo("No results matched the given filters.")
        raise typer.Exit(0)

    typer.echo(f"Ingesting {len(rows)} scored results into NCUC discovery records...")
    svc = NcucDiscoveryService(settings)
    ingested = 0
    skipped = 0
    try:
        for row in rows:
            url = row.get("url", "")
            if not url:
                skipped += 1
                continue
            title = row.get("title") or None
            docket = row.get("docket_number") or None
            score = row.get("combined_score", 0)
            doc_type = row.get("doc_type_guess", "")
            notes = [
                f"source=search_pipeline",
                f"score={score:.2f}",
            ]
            if doc_type:
                notes.append(f"doc_type={doc_type}")
            if dry_run:
                typer.echo(f"  [DRY] {url[:80]}  title={str(title)[:50]}  docket={docket}")
                ingested += 1
                continue
            record = svc.ingest_discovered_url(
                url,
                title=title,
                docket_hint=docket,
                notes=notes,
                acquisition_method=NcucAcquisitionMethod.SEARCH_ENGINE,
            )
            repository.upsert_ncuc_discovery_record(record)
            ingested += 1
    finally:
        svc.close()

    if dry_run:
        typer.echo(f"Dry run complete: would ingest {ingested} records (skipped {skipped})")
    else:
        typer.echo(f"Done: ingested {ingested} records (skipped {skipped} with no URL)")
        typer.echo("Run 'ncuc-fetch --pending' to download the documents.")


@search_app.command("doc-param")
def search_doc_param(
    company: str = typer.Option(
        "Duke Energy Progress",
        "--company",
        help="Company name filter (e.g. 'Duke Energy Progress', 'Duke Energy Carolinas').",
    ),
    docket: str = typer.Option("", "--docket", help="Optional docket number filter (e.g. 'E-2 Sub 1190')."),
    filing_types: str = typer.Option(
        "TARIFF,RATESCED",
        "--types",
        help="Comma-separated filing type keys: TARIFF, RATESCED, ORDER, INFOFILE.",
    ),
    date_after: str = typer.Option("", "--after", help="Filed on or after MM/DD/YYYY."),
    date_before: str = typer.Option("", "--before", help="Filed on or before MM/DD/YYYY."),
    max_results: int = typer.Option(500, "--max", help="Maximum results to collect."),
    tariff_only: bool = typer.Option(False, "--tariff-only", help="Filter display to tariff-related rows."),
    top_n: int = typer.Option(50, "--top", help="Number of results to display."),
    export_csv: str = typer.Option("", "--csv", help="Export results to this CSV path."),
    export_json: str = typer.Option("", "--json", help="Export results to this JSON path."),
) -> None:
    """Search NCUC DocumentsParameterSearch for tariff/rate filings.

    This is the authenticated structured-search surface. It is useful for
    company/date/type filtering, but a zero-result docket query does not prove
    that a docket has no documents. For exact docket listings, prefer
    ``ncuc-resolve-docket-ids`` followed by ``ncuc-docket-fetch``.
    """
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.document_param_search import (
        DocumentParamSearcher,
        print_doc_param_results,
    )
    from duke_rates.historical.ncuc.session import (
        create_authenticated_context,
        close_authenticated_context,
    )

    ft_keys = [t.strip().upper() for t in filing_types.split(",") if t.strip()]

    typer.echo(f"Starting authenticated NCUC session...")
    pw, ctx, page = create_authenticated_context(settings)
    try:
        try:
            searcher = DocumentParamSearcher(settings)
            results = searcher.search(
                page,
                company_name=company,
                docket_number=docket,
                filing_types=ft_keys,
                date_after=date_after,
                date_before=date_before,
                max_results=max_results,
            )
        except Exception as exc:
            from duke_rates.cli import _classify_ncuc_access_failure
            classification, detail = _classify_ncuc_access_failure(exc, surface="Authenticated structured search")
            typer.echo(f"Classification: {classification}")
            typer.echo(detail)
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1)
    finally:
        close_authenticated_context(pw, ctx)

    typer.echo(f"Found {len(results)} documents.")
    typer.echo("Classification: authenticated structured search completed.")
    if not results and docket:
        docket_hint = docket if "," in docket else docket.replace(" Sub ", ", Sub ")
        typer.echo("Note: zero structured-search results for a docket does not mean the docket is empty.")
        typer.echo(
            "Use: python -m duke_rates ncuc-resolve-docket-ids --docket-number "
            f"\"{docket_hint}\""
        )
        typer.echo(
            "Then: python -m duke_rates ncuc-docket-fetch <docket-id> "
            f"--docket-number \"{docket_hint}\" --dry-run"
        )
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
        import json as _json
        import dataclasses
        out = Path(export_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = [dataclasses.asdict(r) for r in results]
        out.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        typer.echo(f"Exported {len(results)} rows -> {out}")


@search_app.command("enrich-doc-param")
def search_enrich_doc_param(
    input_json: Path = typer.Argument(..., help="JSON file previously exported by search-doc-param."),
    output_json: Path = typer.Argument(..., help="Output JSON path for enriched rows."),
    delay_seconds: float = typer.Option(0.5, "--delay", help="Delay between detail-page fetches."),
) -> None:
    """Resolve document detail pages into ViewFile URLs and filenames (requires authenticated session)."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.document_param_search import (
        DocParamSearchResult,
        DocumentParamSearcher,
    )
    from duke_rates.historical.ncuc.session import (
        close_authenticated_context,
        create_authenticated_context,
    )

    if not input_json.exists():
        typer.echo(f"Input file not found: {input_json}", err=True)
        raise typer.Exit(1)

    rows = json.loads(input_json.read_text(encoding="utf-8"))
    results = [DocParamSearchResult(**row) for row in rows]

    typer.echo(f"Starting authenticated NCUC session to enrich {len(results)} rows...")
    pw, ctx, page = create_authenticated_context(settings)
    try:
        searcher = DocumentParamSearcher(settings)
        enriched = searcher.enrich_with_document_details(
            page,
            results,
            delay_seconds=delay_seconds,
        )
    finally:
        close_authenticated_context(pw, ctx)

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps([r.__dict__ for r in enriched], indent=2),
        encoding="utf-8",
    )
    with_files = sum(1 for row in enriched if row.view_file_urls)
    typer.echo(f"Enriched {len(enriched)} rows ({with_files} with file links) -> {output_json}")


@search_app.command("download-doc-param")
def search_download_doc_param(
    input_json: Path = typer.Argument(..., help="Enriched JSON file from search-enrich-doc-param."),
    output_dir: Path = typer.Argument(..., help="Directory to write downloaded PDFs."),
    limit: int = typer.Option(0, "--limit", help="Maximum files to download (0 = all)."),
    skip_existing: bool = typer.Option(True, "--skip-existing/--overwrite", help="Skip files already present."),
) -> None:
    """Download ViewFile PDFs referenced by enriched portal search results."""
    settings, _ = _bootstrap()
    from duke_rates.historical.ncuc.session import (
        close_authenticated_context,
        create_authenticated_context,
        download_view_file,
    )

    if not input_json.exists():
        typer.echo(f"Input file not found: {input_json}", err=True)
        raise typer.Exit(1)

    rows = json.loads(input_json.read_text(encoding="utf-8"))
    jobs: list[tuple[str, str]] = []
    for row in rows:
        urls = row.get("view_file_urls") or []
        labels = row.get("view_file_labels") or []
        for idx, url in enumerate(urls):
            label = labels[idx] if idx < len(labels) else ""
            jobs.append((url, label))

    if limit > 0:
        jobs = jobs[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Starting authenticated NCUC session to download {len(jobs)} files...")
    pw, ctx, page = create_authenticated_context(settings)
    try:
        downloaded = 0
        skipped = 0
        for url, label in jobs:
            file_id = url.split("Id=")[-1].split("&")[0]
            safe_label = "".join(ch if ch.isalnum() or ch in (" ", "-", "_", ".") else "_" for ch in label).strip()
            filename = f"{file_id}.pdf" if not safe_label else f"{file_id}__{safe_label[:100]}.pdf"
            dest = output_dir / filename
            if skip_existing and dest.exists():
                skipped += 1
                continue
            download_view_file(page, url, dest)
            downloaded += 1
    finally:
        close_authenticated_context(pw, ctx)

    typer.echo(f"Downloaded {downloaded} files, skipped {skipped} existing -> {output_dir}")


