"""OCR sub-app: queue management, remediation, batch processing, and benchmark reporting.

Wired into the main CLI as `duke-rates ocr <command>`.
"""

from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import typer

from duke_rates.db.sqlite import connect as connect_sqlite
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
)
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf

from duke_rates.cli_commands._cli_utils import _bootstrap
from duke_rates.cli_commands._ocr_reports import (
    _build_ocr_benchmark_nc_report,
    _build_ocr_remediation_candidates_nc_report,
)


ocr_app = typer.Typer(help="OCR queue management, remediation, batch processing, and benchmarks.")


# ---------------------------------------------------------------------------
# Private worker — claim and process one OCR queue item. Lives here because
# only OCR commands use it.
# ---------------------------------------------------------------------------


def _process_single_ocr_queue_item(database_path: Path, force: bool = False) -> dict[str, int | bool]:
    """Claim and process one OCR queue item. Safe unit of parallel local work.

    Holds one DB connection for the lifetime of the item. The connection is
    only used at claim time and again at completion (the OCR work itself is
    pure CPU/IO outside SQLite), so the lock is not held during long OCR runs.
    """
    from duke_rates.db.ocr_queue import (
        claim_next_ocr_queue_item,
        complete_ocr_queue_item,
        upsert_ocr_artifact,
    )
    from duke_rates.historical.ncuc.pipeline.ocr import (
        _compute_file_hash,
        _ocr_pages_sidecar_path,
        _ocr_text_sidecar_path,
        extract_ocr_document_pages,
        get_ocr_backend_unavailable_reason,
        load_ocr_sidecar_payload,
        summarize_ocr_payload,
    )

    conn = connect_sqlite(database_path)
    try:
        item = claim_next_ocr_queue_item(conn)
        if not item:
            return {"processed": False, "completed": 0, "failed": 0}

        queue_id = int(item["id"])
        source_pdf = str(item["source_pdf"])
        backend = str(item.get("backend") or "pytesseract_cpu")

        if not Path(source_pdf).exists():
            complete_ocr_queue_item(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=f"OCR source missing: {source_pdf}",
            )
            conn.commit()
            return {"processed": True, "completed": 0, "failed": 1}

        unavailable_reason = get_ocr_backend_unavailable_reason(backend)
        if unavailable_reason:
            complete_ocr_queue_item(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=unavailable_reason,
            )
            conn.commit()
            return {"processed": True, "completed": 0, "failed": 1}

        try:
            pages = extract_ocr_document_pages(source_pdf, force=force, backend=backend)
        except Exception as exc:
            complete_ocr_queue_item(
                conn,
                queue_id=queue_id,
                status="failed",
                error_message=str(exc),
            )
            conn.commit()
            return {"processed": True, "completed": 0, "failed": 1}

        file_hash = _compute_file_hash(source_pdf)
        payload = load_ocr_sidecar_payload(source_pdf)
        ocr_summary = summarize_ocr_payload(payload)

        artifact_id = upsert_ocr_artifact(
            conn,
            discovery_record_id=item.get("discovery_record_id"),
            source_pdf=source_pdf,
            file_hash=file_hash,
            backend=backend,
            status="completed" if pages else "empty",
            text_sidecar_path=str(_ocr_text_sidecar_path(source_pdf)),
            pages_sidecar_path=str(_ocr_pages_sidecar_path(source_pdf)),
            page_count=len(pages),
            ocr_confidence=item.get("ocr_confidence"),
            metadata={
                "gpu_candidate": bool(item.get("gpu_candidate")),
                "queue_id": queue_id,
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                **ocr_summary,
            },
        )
        complete_ocr_queue_item(
            conn,
            queue_id=queue_id,
            status="completed" if pages else "failed",
            latest_artifact_id=artifact_id,
            error_message=None if pages else "OCR produced no pages.",
            metadata={
                "page_count": len(pages),
                **ocr_summary,
            },
        )
        conn.commit()
        return {"processed": True, "completed": 1 if pages else 0, "failed": 0 if pages else 1}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@ocr_app.command("enqueue-nc")
def enqueue_ocr_nc(
    limit: int = typer.Option(100, "--limit", help="Max downloaded discovery records to inspect."),
    backend: str = typer.Option("pytesseract_cpu", "--backend", help="OCR backend label."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label."),
    force_rescan: bool = typer.Option(False, "--force-rescan", help="Queue even if a completed artifact exists."),
) -> None:
    """Queue OCR_REQUIRED NCUC downloads for OCR artifact generation."""
    from duke_rates.db.ocr_queue import enqueue_ocr_candidates
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = enqueue_ocr_candidates(
            conn,
            limit=limit,
            backend=backend,
            requested_by=requested_by,
            force_rescan=force_rescan,
        )
        conn.commit()
    finally:
        conn.close()

    typer.echo(f"OCR queue: inserted={report['inserted']} skipped={report['skipped']}")


@ocr_app.command("show-queue-nc")
def show_ocr_queue_nc(
    status: str | None = typer.Option("pending", "--status", help="pending | running | completed | failed | all"),
    limit: int = typer.Option(50, "--limit", help="Max queue rows to display."),
) -> None:
    """Show the OCR processing queue."""
    from duke_rates.db.ocr_queue import list_ocr_queue
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        counts = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT status, COUNT(*) FROM ocr_processing_queue GROUP BY status"
            ).fetchall()
        }
        rows = list_ocr_queue(conn, status=None if status == "all" else status, limit=limit)
    finally:
        conn.close()

    total = sum(counts.values())
    typer.echo(
        f"OCR Queue (NC): total={total} "
        f"pending={counts.get('pending', 0)} "
        f"running={counts.get('running', 0)} "
        f"completed={counts.get('completed', 0)} "
        f"failed={counts.get('failed', 0)}"
    )

    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["id"]),
                    row["status"],
                    str(row["priority"]),
                    row.get("backend") or "-",
                    row.get("source_pdf") or "-",
                ]
            )
        )


@ocr_app.command("report-benchmark-nc")
def report_ocr_benchmark_nc(
    limit: int = typer.Option(50, "--limit", help="Maximum number of OCR-backed historical documents to summarize."),
    backend: str | None = typer.Option(None, "--backend", help="Filter to a selected OCR backend."),
    outcome: str | None = typer.Option(None, "--outcome", help="Filter to a selected parse outcome_quality."),
    needs_review_only: bool = typer.Option(False, "--needs-review-only", help="Only include rows whose latest parse review is still needs_review."),
    stale_only: bool = typer.Option(False, "--stale-only", help="Only include rows whose artifacts or parser run are stale vs current stage versions."),
    sort_by: str = typer.Option("recent", "--sort-by", help="Sort rows by recent | weak-first | review-first | stale-first."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
) -> None:
    """Summarize OCR backend, normalization version, and parse outcomes for NC historical documents."""
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        report = _build_ocr_benchmark_nc_report(
            conn,
            limit=limit,
            backend_filter=backend,
            outcome_filter=outcome,
            needs_review_only=needs_review_only,
            stale_only=stale_only,
            sort_by=sort_by,
        )
    finally:
        conn.close()

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(f"ocr_rows={report['row_count']}")
    typer.echo("Backends:")
    for row in report["backend_summary"]:
        typer.echo(f"  {row['backend']}: {row['count']}")
    typer.echo("Normalization:")
    for row in report["normalization_summary"]:
        typer.echo(f"  {row['ocr_normalization_version']}: {row['count']}")
    typer.echo("Outcomes:")
    for row in report["outcome_summary"]:
        typer.echo(f"  {row['outcome_quality']}: {row['count']}")
    typer.echo("Route Reasons:")
    for row in report["route_reason_summary"]:
        typer.echo(f"  {row['route_reason']}: {row['count']}")
    typer.echo("Recommended Lanes:")
    for row in report["recommended_lane_summary"]:
        typer.echo(f"  {row['recommended_lane']}: {row['count']}")
    typer.echo("Page Artifacts:")
    for row in report["page_artifact_version_summary"]:
        typer.echo(f"  {row['page_artifact_version']}: {row['count']}")
    typer.echo("Span Artifacts:")
    for row in report["span_artifact_version_summary"]:
        typer.echo(f"  {row['span_artifact_version']}: {row['count']}")
    typer.echo("Review Outcomes:")
    for row in report["review_outcome_summary"]:
        typer.echo(f"  {row['review_outcome']}: {row['count']}")
    typer.echo("Backend x Outcome:")
    for row in report["backend_outcome_summary"][:10]:
        typer.echo(f"  {row['backend']} | {row['outcome_quality']}: {row['count']}")
    typer.echo("Sample Rows:")
    for row in report["rows"][:10]:
        typer.echo(
            "  "
            f"hd={row['historical_document_id']} "
            f"family={row['family_key']} "
            f"backend={row['backend']} "
            f"outcome={row['outcome_quality']} "
            f"raw_text_chars={row['raw_text_chars']} "
            f"route={row['route_reason']} "
            f"lane={row['recommended_lane']}"
        )


@ocr_app.command("show-remediation-candidates-nc")
def show_ocr_remediation_candidates_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to display."),
    company: str | None = typer.Option(None, help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC OCR remediation candidates such as unknown-profile / no-text historical documents."""
    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_ocr_remediation_candidates_nc_report(
            conn,
            limit=limit,
            company=company,
            family_key=family_key,
        )
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("OCR Remediation Candidates (NC)")
    typer.echo(f"  candidates={report['candidate_count']}")

    typer.echo("\nRoute Reasons")
    for row in report["route_reason_summary"]:
        typer.echo(f"  {row['route_reason']:<36} count={row['count']}")

    typer.echo("\nRecommended Lanes")
    for row in report["recommended_lane_summary"]:
        typer.echo(f"  {row['recommended_lane']:<36} count={row['count']}")

    typer.echo("\nSample Rows")
    for row in report["rows"]:
        typer.echo(
            "  "
            f"hd={row['historical_document_id']} "
            f"family={row['family_key']} "
            f"profile={row['parser_profile']} "
            f"outcome={row['outcome_quality']} "
            f"raw_text_chars={row['raw_text_chars']} "
            f"ocr_backend={row['ocr_backend'] or '-'} "
            f"lane={row['recommended_lane']}"
        )
        typer.echo(
            "    "
            f"reason={row['route_reason']} "
            f"charges={row['charge_count']} "
            f"pages={row['page_count']}"
        )


@ocr_app.command("enqueue-remediation-nc")
def enqueue_ocr_remediation_nc(
    limit: int = typer.Option(500, "--limit", help="Max remediation candidates to enqueue per invocation."),
    company: str | None = typer.Option(None, help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
    backend: str = typer.Option("pytesseract_cpu", "--backend", help="OCR backend label."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview by default; use --execute to enqueue."),
) -> None:
    """Enqueue OCR remediation candidates from the historical-document audit."""
    from duke_rates.db.ocr_queue import enqueue_ocr_queue_item

    settings, _ = _bootstrap()
    conn = connect_sqlite(Path(settings.database_path))
    try:
        report = _build_ocr_remediation_candidates_nc_report(
            conn,
            limit=max(limit * 10, 500),
            company=company,
            family_key=family_key,
        )
        inserted = 0
        skipped = 0
        considered = 0
        sample_rows: list[dict[str, Any]] = []
        for row in report["rows"]:
            if row["recommended_lane"] != "queue_ocr_or_paddle":
                continue
            if inserted >= limit:
                break
            considered += 1
            source_pdf = str(
                conn.execute(
                    "SELECT local_path FROM historical_documents WHERE id = ?",
                    (int(row["historical_document_id"]),),
                ).fetchone()["local_path"]
            )
            if not source_pdf or not Path(source_pdf).exists():
                skipped += 1
                continue
            triage = triage_pdf(source_pdf)
            priority = 80
            if triage.gpu_ocr_candidate:
                priority = 95
            elif triage.ocr_confidence_score >= 0.85:
                priority = 90
            metadata = {
                "requested_by": requested_by,
                "source": "ocr_remediation_audit",
                "historical_document_id": int(row["historical_document_id"]),
                "family_key": row["family_key"],
                "recommended_lane": row["recommended_lane"],
                "route_reason": row["route_reason"],
                "ocr_backend_version": OCR_BACKEND_VERSION,
                "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
            }
            if dry_run:
                sample_rows.append(
                    {
                        "historical_document_id": int(row["historical_document_id"]),
                        "family_key": row["family_key"],
                        "priority": priority,
                        "backend": backend,
                        "route_reason": row["route_reason"],
                    }
                )
                continue
            queue_id, did_insert = enqueue_ocr_queue_item(
                conn,
                discovery_record_id=None,
                source_pdf=source_pdf,
                file_hash=None,
                backend=backend,
                priority=priority,
                ocr_confidence=triage.ocr_confidence_score,
                structure_complexity=triage.structure_complexity_score,
                gpu_candidate=triage.gpu_ocr_candidate,
                metadata=metadata,
            )
            if did_insert and queue_id is not None:
                inserted += 1
                sample_rows.append(
                    {
                        "historical_document_id": int(row["historical_document_id"]),
                        "family_key": row["family_key"],
                        "queue_id": queue_id,
                        "priority": priority,
                        "backend": backend,
                    }
                )
            else:
                skipped += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    mode = "dry_run" if dry_run else "execute"
    typer.echo(f"OCR remediation enqueue ({mode})")
    typer.echo(f"  considered={considered} inserted={inserted} skipped={skipped}")
    for row in sample_rows[: min(10, len(sample_rows))]:
        line = (
            f"  hd={row['historical_document_id']} family={row['family_key']} "
            f"backend={row['backend']} priority={row['priority']}"
        )
        if "queue_id" in row:
            line += f" queue_id={row['queue_id']}"
        if "route_reason" in row:
            line += f" reason={row['route_reason']}"
        typer.echo(line)


@ocr_app.command("process-queue-nc")
def process_ocr_queue_nc(
    limit: int = typer.Option(500, "--limit", help="Max OCR queue items to process per invocation."),
    workers: int = typer.Option(1, "--workers", min=1, help="Parallel local OCR workers. Safe for local file processing; keep portal/search workflows sequential."),
    force: bool = typer.Option(False, "--force", help="Re-run OCR even if sidecars exist."),
    until_empty: bool = typer.Option(False, "--until-empty", help="Keep processing until the queue is empty (overrides --limit)."),
) -> None:
    """Process pending OCR queue items and persist OCR artifacts."""
    settings, _ = _bootstrap()
    processed = 0
    completed = 0
    failed = 0

    if until_empty:
        limit = 1_000_000

    max_workers = max(1, min(workers, limit))
    if max_workers == 1:
        while processed < limit:
            result = _process_single_ocr_queue_item(Path(settings.database_path), force=force)
            if not result["processed"]:
                break
            processed += 1
            completed += int(result["completed"])
            failed += int(result["failed"])
        typer.echo(f"OCR queue processed={processed} completed={completed} failed={failed}")
        return

    submitted = 0
    futures = set()
    queue_exhausted = False
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while submitted < limit and len(futures) < max_workers:
            futures.add(executor.submit(_process_single_ocr_queue_item, Path(settings.database_path), force))
            submitted += 1

        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                if result["processed"]:
                    processed += 1
                    completed += int(result["completed"])
                    failed += int(result["failed"])
                else:
                    queue_exhausted = True

            while not queue_exhausted and submitted < limit and len(futures) < max_workers:
                futures.add(executor.submit(_process_single_ocr_queue_item, Path(settings.database_path), force))
                submitted += 1

    typer.echo(
        f"OCR queue processed={processed} completed={completed} failed={failed} workers={max_workers}"
    )


@ocr_app.command("process-backlog-nc")
def process_ocr_backlog_nc(
    workers: int = typer.Option(4, "--workers", min=1, help="Parallel Tesseract OCR workers."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter for enqueue step."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter for enqueue and extract steps."),
    skip_enqueue: bool = typer.Option(False, "--skip-enqueue", help="Skip the remediation enqueue step (use when the queue is already populated)."),
    skip_extract: bool = typer.Option(False, "--skip-extract", help="Skip the extract-rates step after the queue drains."),
    force: bool = typer.Option(False, "--force", help="Re-run OCR even if sidecars exist."),
    enqueue_limit: int = typer.Option(500, "--enqueue-limit", help="Max candidates to enqueue in the remediation step."),
) -> None:
    """Canonical OCR backlog workflow: enqueue remediation candidates, drain the OCR queue, then extract charges.

    Replaces the hand-written loop around `ocr enqueue-remediation-nc`, `ocr process-queue-nc`
    (repeated), and `extract-rates-nc`. Uses the Tesseract lane (queue_ocr_or_paddle).
    For the structure-sensitive lane (run_docling_or_paddle_structure), run
    `process-docling-batch --ocr-remediation --source historical` after this.
    """
    typer.echo("=== Step 1/3: Enqueue OCR remediation candidates ===")
    if skip_enqueue:
        typer.echo("  skipped (--skip-enqueue)")
    else:
        enqueue_ocr_remediation_nc(
            limit=enqueue_limit,
            company=company,
            family_key=family_key,
            backend="pytesseract_cpu",
            requested_by="ocr_backlog_workflow",
            dry_run=False,
        )

    typer.echo("")
    typer.echo("=== Step 2/3: Drain OCR queue ===")
    process_ocr_queue_nc(
        limit=500,
        workers=workers,
        force=force,
        until_empty=True,
    )

    typer.echo("")
    typer.echo("=== Step 3/3: Extract rates from newly-OCR'd documents ===")
    if skip_extract:
        typer.echo("  skipped (--skip-extract)")
    else:
        # extract_rates_nc still lives in cli.py — import lazily to avoid circular imports.
        from duke_rates.cli import extract_rates_nc

        extract_rates_nc(
            limit=None,
            family_key=family_key,
            verbose=False,
            progress=False,
            progress_interval=30,
        )

    typer.echo("")
    typer.echo("=== OCR backlog workflow complete ===")
